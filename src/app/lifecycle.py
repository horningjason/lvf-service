"""
Server lifecycle: XML schema loading, NTP setup, GIS initialization, and the
FastAPI lifespan startup/shutdown sequence.

Extracted from src/lost/find_service.py. find_service.py's module-level
__getattr__ forwards legacy find_service.<name> attribute access for
initialize/lifespan_startup/lifespan_shutdown/prewarm/_routing_only here,
since src/server.py, prewarm.py, and tests read/call them that way.

find_service.py's _validate_schema() (request-path code, re-exported to
server.py) reads this module's _schema directly (lifecycle._schema) — that
state is produced here by _setup_ntp_and_schema(), so it lives here rather
than being written back across a module boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from lxml import etree

from src import runtime_state
from src.observability import metrics as _metrics
from i3_fe_core.state.store import ElementState, ServiceState
from src.app import role as role_mod
from src.app.role import NodeRole
from src.discrepancy.discrepancy_report import GISProblem, ProblemSeverity, file_gis_dr
from src.gis import provisioning as gis_provisioning
from src.federation import recursion as fed_recursion
from src.federation import coverage as fed_coverage
from src.federation import sync as fed_sync

log = logging.getLogger(__name__)

_routing_only: bool = False

_schema: Optional[etree.XMLSchema] = None


def _load_schema() -> Optional[etree.XMLSchema]:
    schema_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "schemas")
    lost1_xsd = os.path.join(schema_dir, "lost1.xsd")
    try:
        schema_doc = etree.parse(lost1_xsd)
        compiled = etree.XMLSchema(schema_doc)
        log.info("XML schema loaded from %s", lost1_xsd)
        return compiled
    except Exception as exc:
        log.warning(
            "Could not load XML schema from %s: %s — schema validation disabled",
            lost1_xsd, exc,
        )
        return None


def _setup_ntp_and_schema() -> None:
    """Load the XML schema. Shared by initialize() and lifespan_startup() —
    was duplicated verbatim in both. NTP client setup now lives in
    src.server._lifespan() (the core NtpClient requires an async start())."""
    global _schema
    _schema = _load_schema()
    if _schema is None:
        log.warning("Operating without XML schema validation")


def _enter_forest_guide_mode() -> None:
    """Shared Forest Guide-branch prefix for initialize() and
    lifespan_startup() — the info log and _routing_only assignment are
    identical in both. The LVF_GPKG_PATH/LVF_PARENT_URI ignored-warnings are
    NOT shared here: their emitted text differs (plain string in initialize()
    vs. %r-interpolated with the actual env value in lifespan_startup()), and
    initialize()'s LVF_GPKG_PATH check also has a different trigger condition
    (it additionally considers the gpkg_path parameter). Each caller keeps
    its own warning blocks after calling this."""
    global _routing_only
    log.info(
        "Forest Guide mode active (LVF_FOREST_GUIDE_MODE=true): "
        "this node routes requests via redirect or notFound — no GIS validation"
    )
    _routing_only = True


def initialize(gpkg_path: str | None = None) -> None:
    """Load GIS data for use by handle_find_service(). Call once before the first request."""
    global _routing_only
    _setup_ntp_and_schema()
    role_mod.validate_role_config()
    role = role_mod.current_role()

    if role is NodeRole.FOREST_GUIDE:
        _enter_forest_guide_mode()
        if gpkg_path or os.environ.get("LVF_GPKG_PATH"):
            log.warning(
                "LVF_GPKG_PATH is set but ignored in Forest Guide mode — no GIS data will be loaded"
            )
        if os.environ.get("LVF_PARENT_URI"):
            log.warning(
                "LVF_PARENT_URI is set but ignored in Forest Guide mode — "
                "Forest Guides have no parent (RFC 5582 §8)"
            )
        return

    path = gpkg_path or os.environ.get("LVF_GPKG_PATH")
    if path:
        if not runtime_state._default_mapping_source_id:
            raise RuntimeError(
                "LVF_DEFAULT_MAPPING_SOURCE_ID is required but not set. "
                "Recommended value: {00000000-0000-0000-0000-000000000000}"
            )
        gis_provisioning.load(path, runtime_state.now())
        _routing_only = False
    else:
        _routing_only = True
        log.info("Routing-only mode active: no GeoPackage path provided")

    if role is NodeRole.ROOT_AMS:
        fed_coverage._load_ams_provisioning()
    elif os.path.exists(os.path.join(fed_coverage._ams_provisioning_dir(), "ams_civic_coverage.json")):
        log.debug("AMS provisioning files found but LVF_ROOT_AMS is not set — no behavior change")


# ---------------------------------------------------------------------------
# FastAPI lifespan startup (extracted from server._lifespan)
# ---------------------------------------------------------------------------

def prewarm() -> None:
    """Build the GIS JSON cache once before workers fork (section 10).

    No-op in Forest Guide mode. Initializes the NTP client if needed, resolves
    LVF_GPKG_PATH, and loads the GIS data once so the (now-atomic) cache and
    attribute index exist before multiple workers start — they then hit the warm
    cache instead of all cold-building the GPKG concurrently.
    """
    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    if gpkg_path and os.path.exists(gpkg_path):
        gis_provisioning.load(gpkg_path, runtime_state.now())
        log.info("Pre-warm complete")
    else:
        log.info("Pre-warm skipped (no GeoPackage file present — routing-only mode)")


def _on_gpkg_reload_success() -> None:
    """on_success callback passed to gis_provisioning.watch() — restores
    Normal state, records the reload metric, and runs the AMS/repush
    follow-up. GIS code only signals "a reload succeeded"; this decides what
    that means for element/service state, metrics, AMS provisioning, and
    LoST-Sync repush scheduling."""
    _metrics.reload_events_total.labels(trigger="gpkg_watcher", outcome="success").inc()
    log.info("GIS data reload complete — resuming normal service")
    runtime_state.element_notifier.set_state(ElementState.NORMAL, "GIS reload succeeded")
    runtime_state.service_notifier.set_state(ServiceState.NORMAL, "GIS reload succeeded")
    if runtime_state._root_ams:
        fed_coverage._load_ams_provisioning()
    fed_sync._maybe_schedule_repush()


def _on_gpkg_reload_failure(exc: Exception) -> None:
    """on_failure callback passed to gis_provisioning.watch() — sets
    ServiceDisruption state, records the reload metric, and files a GIS
    discrepancy report on the running event loop."""
    _metrics.reload_events_total.labels(trigger="gpkg_watcher", outcome="failure").inc()
    log.error("GIS data reload failed — service remains unavailable", exc_info=exc)
    runtime_state.element_notifier.set_state(ElementState.SERVICE_DISRUPTION, "GIS reload failed")
    event_loop = runtime_state._event_loop
    if event_loop is not None and event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            file_gis_dr(
                problem=GISProblem.GeneralProvisioning,
                severity=ProblemSeverity.Severe,
                detail=str(exc),
            ),
            event_loop,
        )


async def lifespan_startup() -> None:
    """Run startup tasks — call from the FastAPI lifespan context manager."""
    global _routing_only
    _setup_ntp_and_schema()
    role_mod.validate_role_config()
    role = role_mod.current_role()

    runtime_state._event_loop = asyncio.get_running_loop()

    if fed_coverage._acquire_leadership():
        log.info("Worker is the LVF leader — will run SIP notifier and startup sync")
    else:
        log.info("Worker is a non-leader — another worker runs SIP notifier and startup sync")

    if role is NodeRole.FOREST_GUIDE:
        _enter_forest_guide_mode()
        gpkg_env = os.environ.get("LVF_GPKG_PATH")
        if gpkg_env:
            log.warning(
                "LVF_GPKG_PATH=%r is set but ignored in Forest Guide mode — no GIS data will be loaded",
                gpkg_env,
            )
        if os.environ.get("LVF_PARENT_URI"):
            log.warning(
                "LVF_PARENT_URI=%r is set but ignored in Forest Guide mode — "
                "Forest Guides have no parent (RFC 5582 §8)",
                os.environ["LVF_PARENT_URI"],
            )
        fed_coverage._load_child_coverage()
        fed_coverage._start_coverage_watcher()
        if fed_coverage._is_leader:
            asyncio.create_task(fed_sync._startup_sync())
        else:
            log.info("LoST-Sync: startup sync skipped on non-leader worker")
        return

    if runtime_state._parent_uri and "://" not in runtime_state._parent_uri:
        fed_recursion._resolve_lost_url(runtime_state._parent_uri)
    if runtime_state._forest_guide_uri and role is NodeRole.ROOT_AMS and "://" not in runtime_state._forest_guide_uri:
        fed_recursion._resolve_lost_url(runtime_state._forest_guide_uri)

    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    gpkg_exists = gpkg_path and os.path.exists(gpkg_path)

    if gpkg_exists:
        if not runtime_state._default_mapping_source_id:
            raise RuntimeError(
                "LVF_DEFAULT_MAPPING_SOURCE_ID is required but not set. "
                "Recommended value: {00000000-0000-0000-0000-000000000000}"
            )
        try:
            gis_provisioning.load(gpkg_path, runtime_state.now())
        except Exception as exc:
            _metrics.reload_events_total.labels(trigger="startup", outcome="failure").inc()
            log.error("GIS data load failed from %s: %s", gpkg_path, exc, exc_info=True)
            raise
        _metrics.reload_events_total.labels(trigger="startup", outcome="success").inc()
        _routing_only = False
        runtime_state.element_notifier.set_state(ElementState.NORMAL, "GIS data loaded")
        runtime_state.service_notifier.set_state(ServiceState.NORMAL, "GIS data loaded")
        poll_interval = int(os.environ.get("LVF_GPKG_POLL_INTERVAL_SECONDS", "60"))
        if poll_interval > 0:
            threading.Thread(
                target=gis_provisioning.watch,
                args=(
                    runtime_state.now,
                    _on_gpkg_reload_success,
                    _on_gpkg_reload_failure,
                ),
                daemon=True,
            ).start()
            log.info("GPKG watcher started (poll interval: %ds)", poll_interval)
    else:
        _routing_only = True
        runtime_state.element_notifier.set_state(
            ElementState.SERVICE_DISRUPTION,
            "GIS data unavailable, operating in routing-only mode",
        )
        if gpkg_path:
            log.info(
                "Routing-only mode: LVF_GPKG_PATH is set but file not found at %s",
                gpkg_path,
            )
        else:
            log.info("Routing-only mode: LVF_GPKG_PATH is not configured")
        if not runtime_state._parent_uri and not runtime_state._sync_children:
            log.warning(
                "Routing-only mode: neither LVF_PARENT_URI nor LVF_SYNC_CHILDREN is configured "
                "— this node cannot answer or route requests"
            )
        elif role is NodeRole.STANDALONE:
            log.info(
                "Routing-only mode: this node has no local GIS data and will forward all "
                "requests upstream; verify LVF_GPKG_PATH if local validation was intended"
            )

    fed_coverage._load_child_coverage()

    if role is NodeRole.ROOT_AMS:
        fed_coverage._load_ams_provisioning()
    elif os.path.exists(os.path.join(fed_coverage._ams_provisioning_dir(), "ams_civic_coverage.json")):
        log.debug("AMS provisioning files found but LVF_ROOT_AMS is not set — no behavior change")

    fed_coverage._start_coverage_watcher()

    if fed_coverage._is_leader:
        asyncio.create_task(fed_sync._startup_sync())
    else:
        log.info("LoST-Sync: startup sync skipped on non-leader worker")


async def lifespan_shutdown() -> None:
    """Run shutdown cleanup — call from the FastAPI lifespan context manager
    after yield. Cancels the long-running LoST-Sync retry tasks spawned by
    fed_sync._startup_sync() (child pulls, parent push, Forest Guide push) so they
    don't keep the event loop alive past gunicorn's graceful_timeout while
    retrying an unreachable parent/child/Forest Guide."""
    tasks = [t for t in fed_sync._background_sync_tasks if not t.done()]
    fed_sync._background_sync_tasks.clear()
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("LoST-Sync: background sync task raised during shutdown: %s", exc)
