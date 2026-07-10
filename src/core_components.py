"""LVF-owned container for i3-fe-core components.

Holds the ElementIdentity + CoreSettings built from LVF's environment,
plus the core component instances built here (StateStore, notifiers,
DiscrepancyReporting, LoggingClient). ntp_client and sip_notifier are
populated separately in server.py (the latter leader-gated in multi-worker
mode). Hung on app.state so handlers reach it via request.app.state.core.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from i3_fe_core.config.identity import ElementIdentity
from i3_fe_core.config.settings import CoreSettings
from i3_fe_core.discrepancy import DiscrepancyReporting
from i3_fe_core.logging.logging_client import LoggingClient
from i3_fe_core.time.ntp import NtpClient
from i3_fe_core.state.store import InProcessStateStore, StateStore
from i3_fe_core.state.element_state import ElementStateNotifier
from i3_fe_core.state.service_state import ServiceStateNotifier


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
    )

    state_store = InProcessStateStore()

    logging_client = LoggingClient(
        identity=identity,
        logging_service_uri=os.environ.get("LVF_LOGGING_SERVICE_URI", "") or None,
    )

    element_notifier = ElementStateNotifier(
        identity, state_store, min_notify_interval=1.0, logging_client=logging_client,
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
    )

    discrepancy = DiscrepancyReporting(
        identity=identity,
        contact_jcard=_build_dr_contact_jcard(),
        agent_id=os.environ.get("LVF_AGENCY_ID") or None,
        logging_client=logging_client,
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