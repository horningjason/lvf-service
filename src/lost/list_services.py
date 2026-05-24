"""Stub: LoST listServices response builder (RFC 5222 §10)."""

from lxml import etree

_NS_LOST = "urn:ietf:params:xml:ns:lost1"


def build_response(service_urns: list[str], server_uri: str = "lostserver.example.com") -> bytes:
    """Return a <listServicesResponse> containing the provided service URNs."""
    root = etree.Element(f"{{{_NS_LOST}}}listServicesResponse", nsmap={None: _NS_LOST})
    sl = etree.SubElement(root, f"{{{_NS_LOST}}}serviceList")
    sl.text = " ".join(sorted(set(service_urns))) if service_urns else ""
    path_el = etree.SubElement(root, f"{{{_NS_LOST}}}path")
    via_el = etree.SubElement(path_el, f"{{{_NS_LOST}}}via")
    via_el.set("source", server_uri)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
