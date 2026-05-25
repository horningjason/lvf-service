"""LoST listServicesByLocation implementation (RFC 5222 §11)."""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from lxml import etree

_NS_LOST = "urn:ietf:params:xml:ns:lost1"
_NS_CA   = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"
_NS_GML  = "http://www.opengis.net/gml"

log = logging.getLogger(__name__)


async def handle(xml_bytes: bytes, client_addr: Optional[str] = None) -> bytes:
    """
    Process a <listServicesByLocation> request and return XML response bytes.

    RFC 5222 §11 behaviour:
    - Parse the first recognized location profile (geodetic-2d or civic).
    - Return service URNs whose provisioned boundary polygons contain that location.
    - If out of coverage and LVF_PARENT_URI is set: redirect (recursive=false)
      or recurse (recursive=true) to the parent's /lost endpoint.
    - Routing-only mode (no GIS data) follows the same redirect/recurse logic.
    """
    import src.lost.find_service as _fs
    from src.utils import _is_temporally_active
    from src.logging_events.logger import emit_log_event, make_query_event, make_response_event
    from src.logging_events.log_events import generate_query_id

    server_uri = _fs._server_uri
    parent_uri = _fs._parent_uri

    query_id = generate_query_id()
    timestamp = _fs._ntp_client.get_current_time()
    call_id: Optional[str] = None
    incident_tracking_id: Optional[str] = None

    def _respond(result: bytes, *, response_status: Optional[str] = None) -> bytes:
        emit_log_event(make_response_event(
            timestamp=_fs._ntp_client.get_current_time(),
            response_id=query_id,
            direction="outgoing",
            response_adapter=result.decode("utf-8", errors="replace"),
            response_status=response_status,
            call_id=call_id,
            incident_id=incident_tracking_id,
            ip_address_port=client_addr,
        ))
        return result

    async def _forward_outgoing(body: bytes, target: str) -> bytes:
        out_qid = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=_fs._ntp_client.get_current_time(),
            query_id=out_qid,
            direction="outgoing",
            query_adapter=body.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        result = await _forward(body, target, server_uri)
        emit_log_event(make_response_event(
            timestamp=_fs._ntp_client.get_current_time(),
            response_id=out_qid,
            direction="incoming",
            response_adapter=result.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        return result

    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        emit_log_event(make_query_event(
            timestamp=timestamp,
            query_id=query_id,
            direction="incoming",
            malformed_query=xml_bytes.decode("utf-8", errors="replace")[:2048],
            ip_address_port=client_addr,
        ))
        return _respond(
            _errors("badRequest", f"Malformed XML: {exc}", server_uri),
            response_status="400",
        )

    call_id, incident_tracking_id = _fs._extract_call_incident_ids(root)
    ctx = _fs.RequestContext(call_id=call_id, incident_tracking_id=incident_tracking_id)
    if call_id or incident_tracking_id:
        log.debug("LoST request: callId=%s incidentTrackingId=%s", call_id, incident_tracking_id)

    emit_log_event(make_query_event(
        timestamp=timestamp,
        query_id=query_id,
        direction="incoming",
        query_adapter=xml_bytes.decode("utf-8", errors="replace"),
        call_id=call_id,
        incident_id=incident_tracking_id,
        ip_address_port=client_addr,
    ))

    recursive = root.get("recursive", "false").lower() == "true"

    # Optional <service> child-filter
    service_el = root.find(f"{{{_NS_LOST}}}service")
    service_filter: Optional[str] = None
    if service_el is not None and service_el.text:
        service_filter = service_el.text.strip()

    # Parse the first location element whose profile we recognise
    location_id    = "loc"
    geodetic_point = None
    civic_fields: Optional[dict] = None
    profile_used   = None

    for loc_el in root.findall(f"{{{_NS_LOST}}}location"):
        profile = loc_el.get("profile", "")
        loc_id  = loc_el.get("id", "loc")
        if profile == "geodetic-2d":
            pt = _parse_gml_point(loc_el)
            if pt is not None:
                geodetic_point = pt
                location_id    = loc_id
                profile_used   = "geodetic-2d"
                break
        elif profile == "civic":
            cf = _parse_civic_fields(loc_el)
            if cf is not None:
                civic_fields = cf
                location_id  = loc_id
                profile_used = "civic"
                break

    if profile_used is None:
        return _respond(_errors(
            "badRequest",
            "No recognized location profile found (expected geodetic-2d or civic)",
            server_uri,
        ))

    # ── Routing-only mode (no GIS data loaded) ──────────────────────────────
    if _fs._routing_only:
        # Civic: try child coverage store first
        if civic_fields is not None and _fs._child_coverage:
            child = _fs._lookup_child_coverage(
                civic_fields.get("country"), civic_fields.get("a1"), civic_fields.get("a2"),
                civic_fields.get("a3"), civic_fields.get("a4"), civic_fields.get("a5"),
            )
            if child:
                child_lost = _child_lost_uri(child.get("child_uri", ""))
                if child_lost:
                    if recursive:
                        return _respond(await _forward_outgoing(xml_bytes, child_lost))
                    return _respond(_redirect(child_lost, server_uri))

        if parent_uri:
            parent_lost = parent_uri.rstrip("/") + "/lost"
            if recursive:
                return _respond(await _forward_outgoing(xml_bytes, parent_lost))
            return _respond(_redirect(parent_lost, server_uri))

        return _respond(_list_response([], location_id, server_uri))

    # ── GIS mode ─────────────────────────────────────────────────────────────
    now = _fs._ntp_client.get_current_time()

    if profile_used == "geodetic-2d":
        found_urns: set[str] = {
            b.service_urn
            for b in _fs._boundaries
            if _is_temporally_active(b.effective, b.expires, now)
            and b.geometry is not None
            and b.geometry.contains(geodetic_point)
        }
        # Alias URNs are served wherever urn:service:sos is covered
        if any(u.lower() == "urn:service:sos" for u in found_urns):
            found_urns.update(_fs._sos_alias_urns)

    else:  # civic
        def norm(v: Optional[str]) -> Optional[str]:
            return v.upper() if v else None

        c  = norm(civic_fields.get("country"))
        a1 = norm(civic_fields.get("a1"))
        a2 = norm(civic_fields.get("a2"))
        a3 = norm(civic_fields.get("a3"))
        a4 = norm(civic_fields.get("a4"))
        a5 = norm(civic_fields.get("a5"))

        found_urns = set()
        for entry in _fs._civic_coverage:
            if norm(entry.country) != c or norm(entry.a1) != a1 or norm(entry.a2) != a2:
                continue
            if entry.a3 is not None and norm(entry.a3) != a3:
                continue
            if entry.a4 is not None and norm(entry.a4) != a4:
                continue
            if entry.a5 is not None and norm(entry.a5) != a5:
                continue
            if entry.boundary is not None:
                found_urns.add(entry.boundary.service_urn)

        if any(u.lower() == "urn:service:sos" for u in found_urns):
            found_urns.update(_fs._sos_alias_urns)

    # Out-of-coverage: redirect/recurse to parent when available
    if not found_urns:
        if parent_uri:
            parent_lost = parent_uri.rstrip("/") + "/lost"
            if recursive:
                return _respond(await _forward_outgoing(xml_bytes, parent_lost))
            return _respond(_redirect(parent_lost, server_uri))
        # No parent: return empty service list (no services at this location)
        return _respond(_list_response([], location_id, server_uri))

    # Apply optional <service> child-filter
    result = _apply_service_filter(sorted(found_urns), service_filter)
    return _respond(_list_response(result, location_id, server_uri))


# ---------------------------------------------------------------------------
# Location parsers
# ---------------------------------------------------------------------------

def _parse_gml_point(location_el: etree._Element):
    """Return shapely.geometry.Point(lon, lat) from a geodetic-2d location, or None."""
    from shapely.geometry import Point
    pos_el = location_el.find(f".//{{{_NS_GML}}}pos")
    if pos_el is not None and pos_el.text:
        parts = pos_el.text.split()
        if len(parts) >= 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
                return Point(lon, lat)
            except ValueError:
                pass
    return None


def _parse_civic_fields(location_el: etree._Element) -> Optional[dict]:
    """Return a dict with lowercase keys country/a1…a5 from a civic location, or None."""
    ca_el = location_el.find(f".//{{{_NS_CA}}}civicAddress")
    if ca_el is None:
        return None
    fields: dict = {}
    for tag, key in [
        ("country", "country"), ("A1", "a1"), ("A2", "a2"),
        ("A3", "a3"),           ("A4", "a4"), ("A5", "a5"),
    ]:
        el = ca_el.find(f"{{{_NS_CA}}}{tag}")
        if el is not None and el.text and el.text.strip():
            fields[key] = el.text.strip()
    return fields if fields.get("country") else None


# ---------------------------------------------------------------------------
# Response / error builders
# ---------------------------------------------------------------------------

def _list_response(service_urns: list[str], location_id: str, server_uri: str) -> bytes:
    resp = etree.Element(
        f"{{{_NS_LOST}}}listServicesByLocationResponse", nsmap={None: _NS_LOST}
    )
    sl = etree.SubElement(resp, f"{{{_NS_LOST}}}serviceList")
    sl.text = " ".join(service_urns)
    path_el = etree.SubElement(resp, f"{{{_NS_LOST}}}path")
    via_el  = etree.SubElement(path_el, f"{{{_NS_LOST}}}via")
    via_el.set("source", server_uri)
    lu = etree.SubElement(resp, f"{{{_NS_LOST}}}locationUsed")
    lu.set("id", location_id)
    return etree.tostring(resp, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _redirect(target: str, source: str) -> bytes:
    root = etree.Element(f"{{{_NS_LOST}}}redirect", nsmap={None: _NS_LOST})
    root.set("target", target)
    root.set("source", source)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _errors(error_type: str, message: str, server_uri: str) -> bytes:
    root = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
    root.set("source", server_uri)
    err = etree.SubElement(root, f"{{{_NS_LOST}}}{error_type}")
    err.set("message", message)
    err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_service_filter(urns: list[str], parent: Optional[str]) -> list[str]:
    """
    RFC 5222 §11: return URNs subsumed within parent — i.e. the parent URN
    itself and any descendant URNs (urn:service:sos includes urn:service:sos.police).
    """
    if parent is None:
        return urns
    parent_lower = parent.lower()
    prefix = parent_lower + "."
    return sorted(
        u for u in urns
        if u.lower() == parent_lower or u.lower().startswith(prefix)
    )


def _child_lost_uri(child_validate_uri: str) -> str:
    """Convert a child /validate URI to its /lost URI."""
    base = child_validate_uri.rstrip("/")
    if base.endswith("/validate"):
        base = base[: -len("/validate")]
    return base + "/lost"


async def _forward(xml_bytes: bytes, target_uri: str, server_uri: str) -> bytes:
    """Forward listServicesByLocation request to target and return raw XML bytes."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                target_uri,
                content=xml_bytes,
                headers={"Content-Type": "application/lost+xml"},
            )
        if resp.status_code == 200:
            try:
                etree.fromstring(resp.content)
                return resp.content
            except etree.XMLSyntaxError:
                return _errors("serverError", "Remote server returned unparseable XML", server_uri)
        return _errors("serverError", f"Remote server returned HTTP {resp.status_code}", server_uri)
    except httpx.TimeoutException:
        return _errors("serverTimeout", "Request to remote server timed out", server_uri)
    except Exception as exc:
        return _errors("serverError", f"Could not reach remote server: {exc}", server_uri)
