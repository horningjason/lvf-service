"""Stub: LoST listServicesByLocation — returns notFound (RFC 5222 §11)."""

from lxml import etree

_NS_LOST = "urn:ietf:params:xml:ns:lost1"


def build_response(server_uri: str = "lostserver.example.com") -> bytes:
    """Return a <errors><notFound> response — listServicesByLocation not yet implemented."""
    root = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
    root.set("source", server_uri)
    nf = etree.SubElement(root, f"{{{_NS_LOST}}}notFound")
    nf.set("message", "listServicesByLocation is not implemented on this LVF")
    nf.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
