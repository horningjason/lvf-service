"""
FastAPI server — thin router for the LVF service.

All business logic lives in src/lost/find_service.py. This module
defines the FastAPI app, wires lifespan and route handlers, and
re-exports the symbols that test harnesses import directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from lxml import etree
from starlette.middleware.base import BaseHTTPMiddleware
from i3_fe_core.time.ntp import NtpClient

import src.lost.find_service as _fs
from src import runtime_state
from src.lost.find_service import handle_find_service, initialize, _parent_uri, _server_uri, _validate_schema  # noqa: F401 — re-exported for tests
from src.lost import list_services, list_services_by_location, get_service_boundary, load_shed
from src.gis import provisioning as gis_provisioning
from src.observability import metrics
from src.validation.models import CivicCoverageEntry
from src.core_components import build_core_components, build_tls_settings, validate_tls_files
from src.logging.log_events import generate_query_id
from src.logging.logger import emit_log_event, make_query_event, make_response_event

_NS_LOST = _fs._NS_LOST
log = logging.getLogger(__name__)


def _record_lost_outcome(result: bytes) -> None:
    """Increment lvf_lost_errors_total when `result` is a LoST <errors>
    response, labeled by the actual error child element name (e.g.
    "notFound", "locationInvalid"). No-op for successful responses. Used for
    outcomes produced deep inside find_service.py/list_services.py/etc. where
    there's no cheaper, already-available discriminator without restructuring
    their return type — mirrors the lightweight result-inspection pattern
    find_service.py itself already uses (e.g. _has_loop, _prepend_via_to_response)."""
    try:
        root = etree.fromstring(result, _fs._XML_PARSER)
    except etree.XMLSyntaxError:
        return
    if root.tag != f"{{{_NS_LOST}}}errors" or len(root) == 0:
        return
    child_tag = root[0].tag
    if "}" in child_tag:
        child_tag = child_tag.split("}", 1)[1]
    metrics.lost_errors_total.labels(error_type=child_tag).inc()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    tls_settings = build_tls_settings()
    log.info("TLS mode: %s", os.environ.get("LVF_TLS_MODE", "disabled").lower())
    tls_error = validate_tls_files(tls_settings)
    if tls_error is not None:
        log.error("%s — aborting startup", tls_error)
        raise RuntimeError(f"TLS configuration error: {tls_error}")

    ntp_client = NtpClient(servers=app.state.core.settings.ntp_servers)
    await ntp_client.start()
    app.state.core.ntp_client = ntp_client
    runtime_state._ntp_client = ntp_client
    log.info("NTP client started: is_healthy=%s offset=%s", ntp_client.is_healthy, ntp_client.offset)

    await _fs.lifespan_startup()
    _maybe_start_sip()
    load_shed.start_recovery_watcher_if_needed()
    yield

    # Shutdown — cancel every long-running background asyncio task this
    # lifespan started, so the process exits promptly under gunicorn instead
    # of idling until graceful_timeout. Daemon threads (the GIS dataset
    # watcher, _watch_child_coverage) exit on their own and need no action here.
    sip_notifier = getattr(app.state, "sip_notifier", None)
    if sip_notifier is not None:
        try:
            await sip_notifier.stop()
        except Exception as exc:
            log.warning("Shutdown: SIP notifier stop raised: %s", exc)

    try:
        await load_shed.stop_recovery_watcher()
    except Exception as exc:
        log.warning("Shutdown: load-shed recovery watcher stop raised: %s", exc)

    try:
        await _fs.lifespan_shutdown()
    except Exception as exc:
        log.warning("Shutdown: LoST-Sync background task cleanup raised: %s", exc)

    if ntp_client is not None:
        try:
            await ntp_client.stop()
        except Exception as exc:
            log.warning("Shutdown: NTP client stop raised: %s", exc)

    log.info("LVF shutdown complete")


def _maybe_start_sip() -> None:
    if os.environ.get("LVF_ENABLE_SIP", "true").strip().lower() != "true":
        log.info("SIP notifier disabled (LVF_ENABLE_SIP is not 'true')")
        return
    if not _fs._is_leader:
        log.info("SIP notifier not started — another worker is the leader")
        return
    sip_port_raw = os.environ.get("LVF_SIP_PORT", "5060").strip()
    try:
        sip_port = int(sip_port_raw)
    except ValueError:
        sip_port = 5060
    if sip_port == 0:
        return
    sip_host = os.environ.get("LVF_SIP_HOST", "0.0.0.0").strip()
    from src.notify.sip_notifier import SipWireAdapter
    notifier = SipWireAdapter(
        runtime_state.element_notifier,
        runtime_state.service_notifier,
        host=sip_host,
        port=sip_port,
    )
    # Keep a reference so the notifier is not garbage-collected
    app.state.sip_notifier = notifier
    asyncio.ensure_future(notifier.start())


class LimitBodySize(BaseHTTPMiddleware):
    _LIMITS = {
        "/sync": 10_485_760,
    }
    _DEFAULT = 1_048_576

    async def dispatch(self, request, call_next):
        limit = self._LIMITS.get(request.scope["path"], self._DEFAULT)
        if int(request.headers.get("content-length", 0)) > limit:
            return Response(status_code=413)
        return await call_next(request)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records lvf_http_requests_total / lvf_http_request_duration_seconds for
    every request. Added after LimitBodySize so it wraps it (outermost) — a
    413 rejection from LimitBodySize still passes through call_next here and
    is counted with its actual status code."""

    async def dispatch(self, request, call_next):
        path = request.scope["path"]
        start = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - start
        metrics.http_requests_total.labels(endpoint=path, status=str(response.status_code)).inc()
        metrics.http_request_duration_seconds.labels(endpoint=path).observe(elapsed)
        return response


class NoCacheMiddleware(BaseHTTPMiddleware):
    """RFC 5222 §13 MUST: LoST responses use Cache-Control: no-cache to
    disable HTTP-level caching, including by caches configured to return
    stale responses. Applied to /lost only (the LoST protocol endpoint)."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.scope["path"] == "/lost":
            response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="LVF Service", lifespan=_lifespan)
app.state.core = build_core_components()
runtime_state.state_store      = app.state.core.state_store
runtime_state.element_notifier = app.state.core.element_notifier
runtime_state.service_notifier = app.state.core.service_notifier
runtime_state.discrepancy      = app.state.core.discrepancy
runtime_state.logging_client   = app.state.core.logging_client
app.add_middleware(LimitBodySize)
app.add_middleware(MetricsMiddleware)
app.add_middleware(NoCacheMiddleware)

from i3_fe_core.web_service.versions import build_version_entry, make_versions_route

_lvf_version_major = int(os.environ.get("LVF_VERSION_MAJOR", "1"))
_lvf_version_minor = int(os.environ.get("LVF_VERSION_MINOR", "0"))
_lvf_build_fingerprint = os.environ.get("LVF_BUILD_FINGERPRINT", "lvf-service-dev")

app.mount("/metrics", metrics.metrics_app())

if os.environ.get("LVF_ENABLE_DR_SERVICE", "true").strip().lower() == "true":
    # §3.7 MUST: "Each database, service, and agency MUST provide a
    # Discrepancy Reporting web service." Mounted as a sub-app (same pattern
    # as /metrics) since make_discrepancy_routes() returns raw Starlette
    # Route objects, not FastAPI path operations.
    from starlette.applications import Starlette
    from i3_fe_core.discrepancy import make_discrepancy_routes

    _dr_routes = make_discrepancy_routes(app.state.core.discrepancy)
    # §4 MUST: "Each Web Service MUST implement an entry point called
    # 'Versions'." Mounted alongside the DR routes on the same sub-app,
    # so it resolves at .../dr/Versions. requiredAlgorithms is present but
    # empty until JWS signing keys are provisioned (§5.10) — presence is
    # stable across that transition, only the array's contents change.
    _dr_routes.append(
        make_versions_route(
            fingerprint_provider=_lvf_build_fingerprint,
            versions_provider=[
                build_version_entry(
                    _lvf_version_major,
                    _lvf_version_minor,
                    service_info={"requiredAlgorithms": []},
                )
            ],
        )
    )
    app.mount("/dr", Starlette(routes=_dr_routes))

# §4.12 MUST: "Each Web Service MUST implement an entry point called
# 'Versions'." The LoST (/lost) and LoST-Sync (/sync) web services each
# get their own Versions entry point at /lost/Versions and /sync/Versions.
# Neither LoST (RFC 5222) nor LoST-Sync (RFC 6739) defines a serviceInfo
# payload, so serviceInfo is omitted (conditional per §4.12); only the DR
# service carries serviceInfo (requiredAlgorithms). All three services
# share one build fingerprint (they are one code set) and report v1.0.
from starlette.applications import Starlette as _Starlette

_lost_version_entry = build_version_entry(_lvf_version_major, _lvf_version_minor)
app.mount("/lost/Versions", _Starlette(routes=[
    make_versions_route(
        fingerprint_provider=_lvf_build_fingerprint,
        versions_provider=[_lost_version_entry],
        path="/",
    )
]))
app.mount("/sync/Versions", _Starlette(routes=[
    make_versions_route(
        fingerprint_provider=_lvf_build_fingerprint,
        versions_provider=[_lost_version_entry],
        path="/",
    )
]))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ssap_records": len(_fs._ssap),
        "rcl_records": len(_fs._rcl),
        "boundaries": len(_fs._boundaries),
        "civic_coverage_entries": len(_fs._civic_coverage),
        "ssap_index_buckets": len(_fs._ssap_index),
        "rcl_index_buckets":  len(_fs._rcl_index),
        "element_state": runtime_state.state_store.get_element_state().state.value,
        "service_state": runtime_state.state_store.get_service_state().state.value,
    }


@app.get("/ready")
async def ready(response: Response):
    """Readiness probe (distinct from /health liveness). Load balancers should
    check this: it reports 503 while GIS data is unavailable so traffic is not
    routed to a worker that cannot validate yet."""
    reloading = gis_provisioning.is_reloading()
    ssap_n = len(_fs._ssap)
    rcl_n = len(_fs._rcl)

    if reloading:
        ready_flag = False
    elif _fs._routing_only or _fs._forest_guide_mode:
        # Routing-only / Forest Guide nodes legitimately have no GIS records.
        ready_flag = True
    else:
        ready_flag = bool(ssap_n or rcl_n)

    response.status_code = 200 if ready_flag else 503
    return {
        "ready": ready_flag,
        "reloading": reloading,
        "ssap": ssap_n,
        "rcl": rcl_n,
    }


@app.get("/coverage/geodetic")
async def geodetic_coverage():
    import json as _json
    from shapely.geometry import mapping
    return {
        urn: _json.loads(_json.dumps(mapping(geom)))
        for urn, geom in _fs._geodetic_coverage.items()
    }


@app.get("/coverage/civic")
async def civic_coverage():
    def entry_sort_key(e: CivicCoverageEntry):
        return (
            e.country,
            e.a1,
            e.a2,
            (1, e.a3) if e.a3 is not None else (2, ""),
        )

    return [
        {
            "country": e.country,
            "a1":      e.a1,
            "a2":      e.a2,
            "a3":      e.a3 if e.a3 is not None else "*",
            "a4":      e.a4 if e.a4 is not None else "*",
            "a5":      e.a5 if e.a5 is not None else "*",
            "boundary_display_name": e.boundary.display_name if e.boundary is not None else None,
            "boundary_urn":          e.boundary.service_urn  if e.boundary is not None else None,
        }
        for e in sorted(_fs._civic_coverage, key=entry_sort_key)
    ]


@app.get("/coverage/civic/explain")
async def civic_coverage_explain(
    country: str,
    a1: str,
    a2: str,
    boundary: str,
    a3: Optional[str] = None,
    a4: Optional[str] = None,
    a5: Optional[str] = None,
):
    from src.validation import response_assembly

    def norm(v): return v.upper() if v else None
    def is_wildcard(v): return v is None or v == "*"

    c_norm  = norm(country)
    a1_norm = norm(a1)
    a2_norm = norm(a2)
    a3_wc   = is_wildcard(a3)
    a4_wc   = is_wildcard(a4)
    a5_wc   = is_wildcard(a5)
    a3_norm = None if a3_wc else norm(a3)
    a4_norm = None if a4_wc else norm(a4)
    a5_norm = None if a5_wc else norm(a5)

    bnd_lower = boundary.lower()
    target_boundaries = [
        b for b in _fs._boundaries
        if b.display_name is not None and b.display_name.lower() == bnd_lower
    ]

    nguids: list = []
    seen: set = set()

    for i, record in enumerate(_fs._rcl):
        seg_key = record.nguid if record.nguid is not None else (record.fid if record.fid is not None else i)
        if record.geometry is None:
            continue
        for side in ("L", "R"):
            point = response_assembly._rcl_representative_point(record.geometry, side)
            if point is None:
                continue
            if not any(
                b.geometry is not None and b.geometry.contains(point)
                for b in target_boundaries
            ):
                continue
            suffix = "_l" if side == "L" else "_r"
            if norm(getattr(record, f"country{suffix}")) != c_norm:
                continue
            if norm(getattr(record, f"a1{suffix}"))      != a1_norm:
                continue
            if norm(getattr(record, f"a2{suffix}"))      != a2_norm:
                continue
            if not a3_wc and norm(getattr(record, f"a3{suffix}")) != a3_norm:
                continue
            if not a4_wc and norm(getattr(record, f"a4{suffix}")) != a4_norm:
                continue
            if not a5_wc and norm(getattr(record, f"a5{suffix}")) != a5_norm:
                continue
            if seg_key not in seen:
                seen.add(seg_key)
                nguids.append(seg_key)

    return {
        "query": {
            "country": country,
            "a1": a1,
            "a2": a2,
            "a3": a3,
            "a4": a4,
            "a5": a5,
            "boundary": boundary,
        },
        "count": len(nguids),
        "nguids": nguids,
    }


@app.post("/sync")
async def sync_endpoint(request: Request) -> Response:
    """
    LoST-Sync endpoint (RFC 6739).
    Accepts pushMappings and getMappingsRequest in application/lostsync+xml.
    Returns HTTP 200 for both success and protocol-level errors.
    """
    body = await request.body()
    return await _fs.handle_sync(body, request.client)


@app.post("/lost")
async def lost_endpoint(request: Request) -> Response:
    """
    LoST protocol endpoint (RFC 5222) — findService, listServices,
    listServicesByLocation, getServiceBoundary.
    Content-Type: application/lost+xml.
    """
    # Load shedding (spec §3.11.5) — checked before any XML parsing or schema
    # validation. Disabled (no-op) unless LVF_RATE_LIMIT_PER_SOURCE and/or
    # LVF_MAX_CONCURRENT_REQUESTS are configured.
    source_ip = request.client.host if request.client else None
    shed_reason = load_shed.check(source_ip)
    if shed_reason is not None:
        return Response(
            content=f'{{"error": "rate_limited", "reason": "{shed_reason}"}}',
            status_code=429,
            media_type="application/json",
        )

    concurrency_acquired = False
    if _fs._max_concurrent_requests > 0:
        if not load_shed.try_acquire_concurrency():
            load_shed.log_shed(load_shed.CONCURRENCY_CAP, source_ip)
            return Response(
                content='{"error": "rate_limited", "reason": "concurrency_cap"}',
                status_code=429,
                media_type="application/json",
            )
        concurrency_acquired = True

    try:
        body = await request.body()
        try:
            root = etree.fromstring(body, _fs._XML_PARSER)
        except etree.XMLSyntaxError as exc:
            log.error("Lost endpoint: XML parse failed: %s", exc)
            metrics.lost_errors_total.labels(error_type="badRequest").inc()
            err = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
            err.set("source", _fs._server_uri)
            br = etree.SubElement(err, f"{{{_NS_LOST}}}badRequest")
            br.set("message", "Malformed XML.")
            br.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
            return Response(
                content=etree.tostring(err, xml_declaration=True, encoding="UTF-8", pretty_print=True),
                status_code=200,
                media_type="application/lost+xml",
            )

        schema_error = _validate_schema(body)
        if schema_error:
            metrics.lost_errors_total.labels(error_type="badRequest").inc()
            err = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
            err.set("source", _fs._server_uri)
            br = etree.SubElement(err, f"{{{_NS_LOST}}}badRequest")
            br.set("message", schema_error)
            br.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
            return Response(
                content=etree.tostring(err, xml_declaration=True, encoding="UTF-8", pretty_print=True),
                status_code=200,
                media_type="application/lost+xml",
            )

        client_addr = f"{request.client.host}:{request.client.port}" if request.client else None
        if root.tag == f"{{{_NS_LOST}}}findService":
            result = await _fs.handle_find_service_async(body, client_addr=client_addr)
        elif root.tag == f"{{{_NS_LOST}}}listServices":
            result = list_services.handle(body, client_addr=client_addr)
        elif root.tag == f"{{{_NS_LOST}}}listServicesByLocation":
            result = await list_services_by_location.handle(body, client_addr=client_addr)
        elif root.tag == f"{{{_NS_LOST}}}getServiceBoundary":
            _gsb_query_id = generate_query_id()
            _gsb_call_id, _gsb_incident_id = _fs._extract_call_incident_ids(root)
            emit_log_event(make_query_event(
                timestamp=runtime_state.now(),
                query_id=_gsb_query_id,
                direction="incoming",
                query_adapter=body.decode("utf-8", errors="replace"),
                call_id=_gsb_call_id,
                incident_id=_gsb_incident_id,
            ))
            result = get_service_boundary.build_response(_fs._server_uri)
            emit_log_event(make_response_event(
                timestamp=runtime_state.now(),
                response_id=_gsb_query_id,
                direction="outgoing",
                response_adapter=result.decode("utf-8", errors="replace"),
                call_id=_gsb_call_id,
                incident_id=_gsb_incident_id,
            ))
        else:
            metrics.lost_errors_total.labels(error_type="badRequest").inc()
            err = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
            err.set("source", _fs._server_uri)
            br = etree.SubElement(err, f"{{{_NS_LOST}}}badRequest")
            br.set("message", f"Unexpected root element {root.tag!r}")
            br.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
            result = etree.tostring(err, xml_declaration=True, encoding="UTF-8", pretty_print=True)
            return Response(content=result, status_code=200, media_type="application/lost+xml")

        _record_lost_outcome(result)
        return Response(content=result, status_code=200, media_type="application/lost+xml")
    finally:
        if concurrency_acquired:
            load_shed.release_concurrency()
