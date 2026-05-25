"""LoST listServices implementation (RFC 5222 §10)."""

from __future__ import annotations

import logging
from typing import Optional
from lxml import etree

_NS_LOST = "urn:ietf:params:xml:ns:lost1"

log = logging.getLogger(__name__)


def handle(xml_bytes: bytes, client_addr: Optional[str] = None) -> bytes:
    """
    Process a <listServices> request and return XML response bytes.

    RFC 5222 §10: No location, no recursion/redirection — answered locally only.
    If <service> is present, return immediate sub-URNs of that URN that this
    server supports.  If absent, return all service URNs this server knows.
    """
    import src.lost.find_service as _fs
    from src.logging_events.logger import emit_log_event, make_query_event, make_response_event
    from src.logging_events.log_events import generate_query_id

    query_id = generate_query_id()
    timestamp = _fs._ntp_client.get_current_time()

    def _respond(result: bytes, *, response_status: Optional[str] = None) -> bytes:
        emit_log_event(make_response_event(
            timestamp=_fs._ntp_client.get_current_time(),
            response_id=query_id,
            direction="outgoing",
            response_adapter=result.decode("utf-8", errors="replace"),
            response_status=response_status,
            ip_address_port=client_addr,
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
            _bad_request(f"Malformed XML: {exc}", _fs._server_uri),
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

    # Optional parent URN filter
    service_el = root.find(f"{{{_NS_LOST}}}service")
    requested_parent: Optional[str] = None
    if service_el is not None and service_el.text:
        requested_parent = service_el.text.strip()

    # All service URNs provisioned in boundary records
    known_urns: set[str] = {b.service_urn for b in _fs._boundaries if b.service_urn}

    # Append SOS alias URNs when urn:service:sos is covered
    if any(u.lower() == "urn:service:sos" for u in known_urns):
        known_urns.update(_fs._sos_alias_urns)

    # Filter to immediate children of the requested parent, or return all
    if requested_parent is not None:
        result_urns = _immediate_children(known_urns, requested_parent)
    else:
        result_urns = sorted(known_urns)

    resp = etree.Element(f"{{{_NS_LOST}}}listServicesResponse", nsmap={None: _NS_LOST})
    sl = etree.SubElement(resp, f"{{{_NS_LOST}}}serviceList")
    sl.text = " ".join(result_urns)
    path_el = etree.SubElement(resp, f"{{{_NS_LOST}}}path")
    via_el = etree.SubElement(path_el, f"{{{_NS_LOST}}}via")
    via_el.set("source", _fs._server_uri)
    return _respond(etree.tostring(resp, xml_declaration=True, encoding="UTF-8", pretty_print=True))


def _immediate_children(all_urns: set[str], parent: str) -> list[str]:
    """Return URNs that are immediate dot-separated children of parent."""
    prefix = parent.rstrip(".") + "."
    prefix_lower = prefix.lower()
    result = []
    for urn in all_urns:
        if not urn.lower().startswith(prefix_lower):
            continue
        suffix = urn[len(prefix):]
        if suffix and "." not in suffix:
            result.append(urn)
    return sorted(result)


def _bad_request(message: str, server_uri: str) -> bytes:
    root = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
    root.set("source", server_uri)
    br = etree.SubElement(root, f"{{{_NS_LOST}}}badRequest")
    br.set("message", message)
    br.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
