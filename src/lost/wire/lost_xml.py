"""
Shared LoST/PIDF-LO XML namespace constants and the safe XML parser config.

Used across src/lost/, src/gis/, and src/federation/. No logic belongs
here, only constants.
"""

from __future__ import annotations

from lxml import etree

_XML_PARSER = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)

_NS_LOST    = "urn:ietf:params:xml:ns:lost1"
_NS_EXT_IDS = "urn:emergency:xml:ns:lostExt:Ids"
_NS_CA      = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"
_NS_CAE     = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr:ext"
_NS_CDX1    = "urn:nena:xml:ns:pidf:nenaCivicAddr"
_NS_CDX2    = "urn:nena:xml:ns:pidf:nenaCivicAddr2"
_NS_RLI     = "urn:ietf:params:xml:ns:lost-rli1"
_NS_PLANNED = "urn:ietf:params:xml:ns:lostPlannedChange1"
_NS_SYNC    = "urn:ietf:params:xml:ns:lostsync1"
_NS_GML     = "http://www.opengis.net/gml"
