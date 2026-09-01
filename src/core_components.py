"""LVF-owned container for i3-fe-core components.

Holds the ElementIdentity + CoreSettings built from LVF's environment,
plus the core component instances built here (StateStore, notifiers,
DiscrepancyReporting, LoggingClient). ntp_client and sip_notifier are
populated separately in server.py (the latter leader-gated in multi-worker
mode). Hung on app.state so handlers reach it via request.app.state.core.

TRANSPORT SECURITY FOR LOGGING SERVICE / DR EGRESS

build_core_components() wires LoggingClient's and DiscrepancyReporting's
outbound HTTP POSTs (LVF_LOGGING_SERVICE_URI, LVF_DR_ENDPOINT) through
src/utils.py::outbound_ssl_context() — the same SSLContext already used for
all peer-LVF federation traffic (child<->parent sync, recursion,
listServicesByLocation forwarding). Without this, both clients fall back to
their own internal default (`http_client or httpx.AsyncClient()` — see
i3-fe-core's LoggingClient.__init__), which carries none of §2.8.1's TLS 1.2
floor / PFS cipher enforcement and does not verify against LVF_TLS_CA_FILE.

This deliberately DIVERGES from gcs-service/src/core_components.py's own
wiring of the equivalent gap, and the divergence is worth stating explicitly
rather than let it read as an unexplained inconsistency between the two
repos. The GCS calls `make_client_ssl_context(settings.tls)` directly — i.e.
its own *listener* TLSSettings — because the GCS has no federation role and
therefore no separate outbound-client-identity concept: its element
certificate already doubles as both server and client identity (decision
107). LVF is not in that position: it already maintains a DISTINCT outbound
identity (LVF_TLS_CLIENT_CERT_FILE/KEY_FILE) specifically because its
federation role (child->parent sync, recursion) requires one. Building a
second context here from the listener's TLSSettings would mean LVF presents
two different identities on two different classes of outbound call for no
principled reason, and would reintroduce exactly the two-path drift the
federation-egress migration (src/utils.py::outbound_ssl_context) exists to
eliminate. So LVF reuses its own outbound path here instead of mirroring the
GCS's call shape: one context, one identity, every outbound call.

CAVEAT — LVF_TLS_CA_FILE now governs ALL outbound trust, not just federation.
LVF_TLS_CA_FILE was documented (and typically provisioned) as this node's
PRIVATE federation CA — the CA that signs peer LVF nodes' certificates. Once
set, it ALSO becomes the sole trust anchor for Logging Service and DR
endpoint calls, which are commonly operated by a DIFFERENT party (README:
"responding agency's /Reports service") holding a publicly-trusted
certificate, not one signed by LVF's federation CA. A deployment that sets
LVF_TLS_CA_FILE for federation and also configures LVF_LOGGING_SERVICE_URI
or LVF_DR_ENDPOINT pointing at an externally-operated HTTPS service will find
those calls now FAIL certificate verification unless that CA bundle also
contains (or chains to) whatever issued the external service's certificate.
Leaving LVF_TLS_CA_FILE unset avoids this by falling back to the platform
trust store (see outbound_ssl_context()'s docstring) — at the cost of no CA
pinning for federation either, since there is currently no way to configure
separate trust stores for the two purposes. This is a genuine, currently
irreducible tradeoff, not an oversight in this wiring; flagging it here
rather than leaving it to be discovered at a production TLS failure.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from i3_fe_core.config.identity import ElementIdentity
from i3_fe_core.config.settings import CoreSettings, TLSMode, TLSSettings
from i3_fe_core.discrepancy import DiscrepancyReporting
from i3_fe_core.logging.logging_client import LoggingClient
from i3_fe_core.time.ntp import NtpClient
from i3_fe_core.state.store import InProcessStateStore, StateStore
from i3_fe_core.state.element_state import ElementStateNotifier
from i3_fe_core.state.service_state import ServiceStateNotifier

from src.federation import coverage as fed_coverage
from src.utils import outbound_ssl_context

_TLS_MODES = {"disabled": TLSMode.OFF, "tls": TLSMode.TLS, "mtls": TLSMode.MTLS}


def build_tls_settings() -> TLSSettings:
    """Build TLSSettings from LVF_TLS_* (mirrors main.py/gunicorn.conf.py/
    server.py's own listener TLS config). Does not re-validate file
    existence — see validate_tls_files() for that."""
    mode_raw = os.environ.get("LVF_TLS_MODE", "disabled").strip().lower()
    mode = _TLS_MODES.get(mode_raw, TLSMode.OFF)

    cert_file = os.environ.get("LVF_TLS_CERT_FILE", "").strip()
    key_file = os.environ.get("LVF_TLS_KEY_FILE", "").strip()
    ca_file = os.environ.get("LVF_TLS_CA_FILE", "").strip()

    return TLSSettings(
        mode=mode,
        cert_path=Path(cert_file) if cert_file else None,
        key_path=Path(key_file) if key_file else None,
        ca_path=Path(ca_file) if ca_file else None,
    )


def validate_tls_files(tls_settings: TLSSettings) -> str | None:
    """Fail-fast file-existence check for *tls_settings*. Returns an error
    message naming the missing/misconfigured variable, or None when
    *tls_settings* is valid for its mode. Callers decide how to fail
    (sys.exit, RuntimeError, ...)."""
    if tls_settings.mode not in (TLSMode.TLS, TLSMode.MTLS):
        return None

    if tls_settings.cert_path is None or not tls_settings.cert_path.exists():
        return (
            "LVF_TLS_CERT_FILE must be set and the file must exist "
            f"(got: {tls_settings.cert_path!r})"
        )
    if tls_settings.key_path is None or not tls_settings.key_path.exists():
        return (
            "LVF_TLS_KEY_FILE must be set and the file must exist "
            f"(got: {tls_settings.key_path!r})"
        )
    if tls_settings.mode == TLSMode.MTLS:
        if tls_settings.ca_path is None or not tls_settings.ca_path.exists():
            return (
                "LVF_TLS_CA_FILE must be set and the file must exist for "
                f"mtls mode (got: {tls_settings.ca_path!r})"
            )
    return None


@dataclass
class CoreComponents:
    identity: ElementIdentity
    settings: CoreSettings
    ntp_client: NtpClient | None = None
    state_store: StateStore | None = None
    element_notifier: ElementStateNotifier | None = None
    service_notifier: ServiceStateNotifier | None = None
    sip_notifier: object | None = None   # set in server.py (leader-gated)
    discrepancy: object | None = None
    logging_client: LoggingClient | None = None


def _build_dr_contact_jcard() -> list:
    """RFC 7095 jCard for the DR contact fields (§3.7.1), built from
    LVF_DR_CONTACT_NAME / LVF_DR_CONTACT_EMAIL."""
    contact_name  = os.environ.get("LVF_DR_CONTACT_NAME", "")
    contact_email = os.environ.get("LVF_DR_CONTACT_EMAIL", "")

    if not contact_name:
        logging.getLogger(__name__).warning(
            "LVF_DR_CONTACT_NAME is not set — using 'LVF Administrator' in DR jCard"
        )
    if not contact_email:
        logging.getLogger(__name__).warning(
            "LVF_DR_CONTACT_EMAIL is not set — DR jCard contact email will be empty"
        )

    return [
        "vcard",
        [
            ["fn",    {}, "text", contact_name  or "LVF Administrator"],
            ["email", {}, "text", contact_email or ""],
        ],
    ]


def build_core_components() -> CoreComponents:
    server_uri = os.environ.get("LVF_SERVER_URI", "lostserver.example.com")

    identity = ElementIdentity(
        element_id=server_uri,
        agency_id=os.environ.get("LVF_AGENCY_ID", server_uri),  # dev default
        service_name="LVF",  # IANA serviceNames token, §10.11
    )
    settings = CoreSettings(
        log_level=os.environ.get("LVF_LOG_LEVEL", "INFO"),
        ntp_servers=[os.environ.get("LVF_NTP_SERVER", "pool.ntp.org")],
        tls=build_tls_settings(),
    )

    state_store = InProcessStateStore()

    # Egress TLS (see module docstring): Logging Service POSTs and DR
    # submissions/resolution-callback traffic carry the same §2.8.1 TLS 1.2
    # floor and PFS cipher enforcement as federation traffic, instead of
    # httpx's plaintext-capable defaults. Built only when settings.tls.mode
    # != OFF, matching i3_fe_core.app.factory.create_app()'s own gating (and
    # gcs-service/src/core_components.py's), read here from the *listener*
    # TLSSettings (settings.tls) because that is LVF's global TLS on/off
    # switch (LVF_TLS_MODE) — build_client_tls_settings() never reports OFF
    # (see its own docstring), so gating on it here would always be true.
    # Two separate AsyncClient instances, not one shared instance, mirroring
    # the GCS's own _outbound_http / _dr_http split — each wraps the SAME
    # cached SSLContext (outbound_ssl_context() is lru_cached), so this adds
    # no additional TLS handshake cost, only two independent connection
    # pools.
    _logging_http: httpx.AsyncClient | None = None
    _dr_http: httpx.AsyncClient | None = None
    if settings.tls.mode != TLSMode.OFF:
        _logging_http = httpx.AsyncClient(verify=outbound_ssl_context())
        _dr_http = httpx.AsyncClient(verify=outbound_ssl_context())

    logging_client = LoggingClient(
        identity=identity,
        logging_service_uri=os.environ.get("LVF_LOGGING_SERVICE_URI", "") or None,
        http_client=_logging_http,
    )

    # Leader gate for the §4.12.3 state-change LogEvents (core >= 0.4.0).
    # In a multi-worker deployment every worker holds its own StateStore and
    # transitions it independently, so without this the SAME transition is
    # logged (and POSTed to LVF_LOGGING_SERVICE_URI) once per worker. The gate
    # covers the LogEvent only — core still fans the NOTIFY body out to every
    # local subscriber in every worker, which is what the SIP adapter needs.
    #
    # Read fed_coverage._is_leader through a lambda, never captured as a value:
    # leadership is decided later, in lifespan_startup() -> _acquire_leadership(),
    # long after build_core_components() runs at import time, and the module
    # attribute is rebound (not mutated) when it is. On Windows / single-worker,
    # fcntl is absent and _acquire_leadership() always reports True, so this is
    # a no-op there — but it is False from import until that call, which is why
    # nothing may emit a state LogEvent before lifespan_startup().
    _is_leader = lambda: fed_coverage._is_leader  # noqa: E731

    element_notifier = ElementStateNotifier(
        identity, state_store, min_notify_interval=1.0, logging_client=logging_client,
        is_leader=_is_leader,
    )
    service_domain = os.environ.get("LVF_SERVICE_DOMAIN", identity.element_id)
    service_notifier = ServiceStateNotifier(
        service=service_domain,
        name="LVF",
        domain=service_domain,
        service_id=service_domain,
        store=state_store,
        min_notify_interval=1.0,
        supports_security_posture=False,
        logging_client=logging_client,
        is_leader=_is_leader,
    )

    discrepancy = DiscrepancyReporting(
        identity=identity,
        contact_jcard=_build_dr_contact_jcard(),
        agent_id=os.environ.get("LVF_AGENCY_ID") or None,
        logging_client=logging_client,
        http_client=_dr_http,
    )

    return CoreComponents(
        identity=identity,
        settings=settings,
        state_store=state_store,
        element_notifier=element_notifier,
        service_notifier=service_notifier,
        discrepancy=discrepancy,
        logging_client=logging_client,
    )