"""
FastAPI server — thin router for the LVF service.

All business logic lives in src/lost/find_service.py. This module
defines the FastAPI app, wires lifespan and route handlers, and
re-exports the symbols that test harnesses import directly.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from lxml import etree

import src.lost.find_service as _fs
from src.lost.find_service import handle_find_service, initialize, _parent_uri, _server_uri  # noqa: F401 — re-exported for tests
from src.lost import list_services, list_services_by_location, get_service_boundary
from src.validation.models import CivicCoverageEntry

_NS_LOST = _fs._NS_LOST


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _fs.lifespan_startup()
    yield


app = FastAPI(title="LVF Service", lifespan=_lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ssap_records": len(_fs._ssap),
        "rcl_records": len(_fs._rcl),
        "boundaries": len(_fs._boundaries),
        "civic_coverage_entries": len(_fs._civic_coverage),
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


@app.post("/validate")
async def validate(request: Request) -> Response:
    """
    Accept a LoST findService request (RFC 5222) as XML and return a
    findServiceResponse or <errors> element.
    """
    body = await request.body()
    result = await _fs.handle_find_service_async(body)
    return Response(content=result, status_code=200, media_type="application/xml")


@app.post("/lost")
async def lost_endpoint(request: Request) -> Response:
    """LoST protocol endpoint for listServices, listServicesByLocation, getServiceBoundary."""
    body = await request.body()
    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError as exc:
        err = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
        err.set("source", _fs._server_uri)
        br = etree.SubElement(err, f"{{{_NS_LOST}}}badRequest")
        br.set("message", f"Malformed XML: {exc}")
        br.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
        return Response(
            content=etree.tostring(err, xml_declaration=True, encoding="UTF-8", pretty_print=True),
            status_code=200,
            media_type="application/xml",
        )

    if root.tag == f"{{{_NS_LOST}}}listServices":
        service_urns = list({b.service_urn for b in _fs._boundaries})
        result = list_services.build_response(service_urns, _fs._server_uri)
    elif root.tag == f"{{{_NS_LOST}}}listServicesByLocation":
        result = list_services_by_location.build_response(_fs._server_uri)
    elif root.tag == f"{{{_NS_LOST}}}getServiceBoundary":
        result = get_service_boundary.build_response(_fs._server_uri)
    else:
        err = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
        err.set("source", _fs._server_uri)
        br = etree.SubElement(err, f"{{{_NS_LOST}}}badRequest")
        br.set("message", f"Unexpected root element {root.tag!r}")
        br.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
        result = etree.tostring(err, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    return Response(content=result, status_code=200, media_type="application/xml")
