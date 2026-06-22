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

import src.lost.find_service as _fs
from src.lost.find_service import handle_find_service, initialize, _parent_uri, _server_uri, _validate_schema  # noqa: F401 — re-exported for tests
from src.lost import list_services, list_services_by_location, get_service_boundary, load_shed
from src.observability import metrics
from src.notifications import element_state as _element_state
from src.notifications import service_state as _service_state
from src.validation.models import CivicCoverageEntry

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
    tls_mode = os.environ.get("LVF_TLS_MODE", "disabled").lower()
    log.info("TLS mode: %s", tls_mode)
    if tls_mode in ("tls", "mtls"):
        cert_file = os.environ.get("LVF_TLS_CERT_FILE", "")
        key_file  = os.environ.get("LVF_TLS_KEY_FILE",  "")
        if not cert_file or not os.path.exists(cert_file):
            log.error(
                "LVF_TLS_CERT_FILE must be set and file must exist (got: %r) — aborting startup",
                cert_file,
            )
            raise RuntimeError("TLS configuration error: missing or invalid LVF_TLS_CERT_FILE")
        if not key_file or not os.path.exists(key_file):
            log.error(
                "LVF_TLS_KEY_FILE must be set and file must exist (got: %r) — aborting startup",
                key_file,
            )
            raise RuntimeError("TLS configuration error: missing or invalid LVF_TLS_KEY_FILE")
        if tls_mode == "mtls":
            ca_file = os.environ.get("LVF_TLS_CA_FILE", "")
            if not ca_file or not os.path.exists(ca_file):
                log.error(
                    "LVF_TLS_CA_FILE must be set and file must exist for mtls mode (got: %r) — aborting startup",
                    ca_file,
                )
                raise RuntimeError("TLS configuration error: missing or invalid LVF_TLS_CA_FILE")
    await _fs.lifespan_startup()
    _maybe_start_sip()
    load_shed.start_recovery_watcher_if_needed()
    yield

    # Shutdown — cancel every long-running background asyncio task this
    # lifespan started, so the process exits promptly under gunicorn instead
    # of idling until graceful_timeout. Daemon threads (_watch_gpkg,
    # _watch_child_coverage) exit on their own and need no action here.
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
    from src.notifications.sip_notifier import SIPNotifier
    notifier = SIPNotifier(host=sip_host, port=sip_port)
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


app = FastAPI(title="LVF Service", lifespan=_lifespan)
app.add_middleware(LimitBodySize)
app.add_middleware(MetricsMiddleware)
app.mount("/metrics", metrics.metrics_app())


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
        "element_state": _element_state.get_state().value,
        "service_state": _service_state.get_state().value,
    }


@app.get("/ready")
async def ready(response: Response):
    """Readiness probe (distinct from /health liveness). Load balancers should
    check this: it reports 503 while GIS data is unavailable so traffic is not
    routed to a worker that cannot validate yet."""
    reloading = _fs._reloading
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


def _get_peer_cert(request: Request):
    """Extract peer certificate from the ASGI connection scope.
    Works under both uvicorn standalone and gunicorn+UvicornWorker."""
    # Try uvicorn's ssl_object first (standalone uvicorn)
    ssl_object = request.scope.get("ssl_object")
    if ssl_object is not None:
        return ssl_object.getpeercert()
    # Fall back to transport (gunicorn + UvicornWorker)
    transport = getattr(request.scope.get("_transport", None), "_ssl_protocol", None)
    if transport is None:
        # Try extensions path used by some uvicorn versions
        extensions = request.scope.get("extensions", {})
        tls = extensions.get("tls", {})
        if tls:
            return tls.get("peer_cert")
    return None


@app.post("/sync")
async def sync_endpoint(request: Request) -> Response:
    """
    LoST-Sync endpoint (RFC 6739).
    Accepts pushMappings and getMappingsRequest in application/lostsync+xml.
    Returns HTTP 200 for both success and protocol-level errors.
    """
    if os.environ.get("LVF_TLS_MODE", "disabled").lower() == "mtls":
        peer_cert = _get_peer_cert(request)
        if not peer_cert:
            return Response(
                content='{"error": "Client certificate required for /sync endpoint"}',
                status_code=401,
                media_type="application/json",
            )
        subject = dict(x[0] for x in peer_cert.get("subject", []))
        log.info("Sync request authenticated: CN=%s", subject.get("commonName", "<unknown>"))

    body = await request.body()
    return await _fs.handle_sync(body, request.client)


@app.post("/lost")
async def lost_endpoint(request: Request) -> Response:
    """
    LoST protocol endpoint (RFC 5222) — findService, listServices,
    listServicesByLocation, getServiceBoundary.
    Content-Type: application/lost+xml.
    """
    if os.environ.get("LVF_TLS_MODE", "disabled").lower() == "mtls":
        peer_cert = _get_peer_cert(request)
        if peer_cert:
            subject = dict(x[0] for x in peer_cert.get("subject", []))
            log.debug("LoST request with client cert: CN=%s", subject.get("commonName", "<unknown>"))
        else:
            log.debug("LoST request without client cert (allowed)")

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
            result = get_service_boundary.build_response(_fs._server_uri)
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
