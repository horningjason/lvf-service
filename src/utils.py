"""Shared utilities for the LVF service."""

from __future__ import annotations

import datetime
import functools
import os
import ssl
from pathlib import Path
from typing import Optional

from i3_fe_core.config.settings import TLSMode, TLSSettings
from i3_fe_core.security.tls import make_client_ssl_context


def _is_temporally_active(
    effective: Optional[str],
    expire: Optional[str],
    now: datetime.datetime,
) -> bool:
    """Return True if the record is temporally active at `now`."""
    if effective:
        try:
            eff_dt = datetime.datetime.fromisoformat(effective)
            if eff_dt.tzinfo is None:
                eff_dt = eff_dt.replace(tzinfo=datetime.timezone.utc)
            if eff_dt > now:
                return False
        except ValueError:
            pass  # unparseable effective date — treat as no constraint
    if expire:
        try:
            exp_dt = datetime.datetime.fromisoformat(expire)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
            if exp_dt <= now:
                return False
        except ValueError:
            pass  # unparseable expiration date — treat as no constraint
    return True


def build_client_tls_settings() -> TLSSettings:
    """Build the *client-identity* TLSSettings for outbound calls to peer LVF nodes.

    This is the outbound twin of core_components.build_tls_settings(), which
    builds this node's *listener* settings. They are deliberately separate
    objects: the GCS reuses one element certificate as both server and client
    identity (i3-fe-core's own app/factory.py pattern), but LVF presents a
    DISTINCT client certificate (LVF_TLS_CLIENT_CERT_FILE / _KEY_FILE) to peers
    for its federation role — child->parent sync push, Forest Guide push,
    parent->child pull, and recursion.

    ┌───────────────────────────────────────────────────────────────────────┐
    │ `mode` HERE IS AN INSTRUCTION, NOT A MIRROR OF LVF_TLS_MODE.          │
    │                                                                       │
    │ make_client_ssl_context() loads a client identity only when           │
    │ `mode == TLSMode.MTLS`. We therefore set MTLS whenever both client    │
    │ cert and key files exist, INDEPENDENT of LVF_TLS_MODE, to preserve    │
    │ this repo's long-standing outbound behavior: the previous             │
    │ outbound_client_cert() helper keyed purely on file existence and      │
    │ never consulted the mode.                                             │
    │                                                                       │
    │ Do NOT "align" this with build_tls_settings(). Gating on              │
    │ LVF_TLS_MODE == mtls would silently stop presenting the client        │
    │ certificate under LVF_TLS_MODE=tls — a live configuration — and the   │
    │ resulting failure would surface at the PEER as a rejected handshake,  │
    │ i.e. as a federation outage attributed to the wrong node. Pinned by   │
    │ tests/security/test_outbound_tls.py::test_client_identity_presented_  │
    │ in_tls_mode.                                                          │
    └───────────────────────────────────────────────────────────────────────┘

    Note this is the deliberate inverse of the GCS's decision 107, which
    removed its own client-cert variables as "a second, unspecified identity".
    LVF keeps its second identity on purpose.
    """
    cert_file = os.environ.get("LVF_TLS_CLIENT_CERT_FILE", "")
    key_file  = os.environ.get("LVF_TLS_CLIENT_KEY_FILE", "")
    ca_file   = os.environ.get("LVF_TLS_CA_FILE", "")

    have_identity = bool(
        cert_file and key_file
        and os.path.isfile(cert_file) and os.path.isfile(key_file)
    )

    return TLSSettings(
        # See the box above: MTLS here means "also load a client identity".
        mode=TLSMode.MTLS if have_identity else TLSMode.TLS,
        cert_path=Path(cert_file) if have_identity else None,
        key_path=Path(key_file) if have_identity else None,
        # Trust our self-signed federation CA when configured; None falls back
        # to the platform trust store inside make_client_ssl_context().
        ca_path=Path(ca_file) if (ca_file and os.path.isfile(ca_file)) else None,
    )


@functools.lru_cache(maxsize=1)
def outbound_ssl_context() -> ssl.SSLContext:
    """Return the SSLContext to pass as httpx's `verify=` for calls to peer LVF nodes.

    Carries BOTH trust (our federation CA) and identity (our client
    certificate) in one context, so call sites pass `verify=` only — httpx
    0.28 deprecates both `verify=<str>` and `cert=...`, and its own deprecation
    message prescribes exactly this shape ("Use `verify=<ssl_context>`
    instead, with `.load_cert_chain()` to configure the certificate chain").

    Built via i3-fe-core so federation egress carries the same §2.8.1
    constraints as the listener: a TLS 1.2 floor and PFS-only TLS 1.2 ciphers.
    Neither is applied by httpx's default context, which is what the previous
    hand-rolled `verify=<ca path>` produced.

    Cached: certificates are read once per process, matching the listener,
    which also builds its context at startup. Rotating certificates therefore
    requires a restart — the same operational constraint the listener already
    has. Tests that manipulate LVF_TLS_* must call
    `outbound_ssl_context.cache_clear()`.

    In LVF_TLS_MODE=disabled deployments the peer URIs are plain http://, where
    httpx ignores `verify=` entirely, so building this context is a harmless
    no-op rather than a behavior change.
    """
    return make_client_ssl_context(build_client_tls_settings())
