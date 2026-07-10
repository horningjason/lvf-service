"""
LoST findServiceResponse XML serialization (RFC 5222 §8.4, draft-ietf-ecrit-
similar-location completeLocation).

Pure serialization — builds the response XML tree from an already-assembled
FindServiceResponse model. No request parsing, no gate logic, no HTTP.
"""

from __future__ import annotations

import datetime
from typing import Optional

from lxml import etree

from src.lost.wire import lost_xml
from src import runtime_state
from src.gis import records as gis_records
from src.validation.models import ELEMENT_HIERARCHY

_RESPONSE_NSMAP: dict = {
    None:   lost_xml._NS_LOST,
    "ca":   lost_xml._NS_CA,
    "cae":  lost_xml._NS_CAE,
    "cdx1": lost_xml._NS_CDX1,
    "cdx2": lost_xml._NS_CDX2,
}


def _mapping_element(parent: etree._Element, mapping, force_no_cache: bool = False) -> None:
    m = etree.SubElement(parent, f"{{{lost_xml._NS_LOST}}}mapping")
    m.set("expires",     "NO-CACHE" if force_no_cache else (mapping.expires or "NO-EXPIRATION"))
    m.set("lastUpdated", mapping.last_updated or
          runtime_state.now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    m.set("source",   mapping.source or runtime_state._server_uri)
    m.set("sourceId", mapping.source_id or "unknown")

    if mapping.display_name:
        dn = etree.SubElement(m, f"{{{lost_xml._NS_LOST}}}displayName")
        dn.set("{http://www.w3.org/XML/1998/namespace}lang",
               mapping.display_name_lang or runtime_state._display_name_lang)
        dn.text = mapping.display_name

    svc = etree.SubElement(m, f"{{{lost_xml._NS_LOST}}}service")
    svc.text = mapping.service_urn

    if mapping.service_uri:
        uri_el = etree.SubElement(m, f"{{{lost_xml._NS_LOST}}}uri")
        uri_el.text = mapping.service_uri

    if mapping.service_num:
        sn = etree.SubElement(m, f"{{{lost_xml._NS_LOST}}}serviceNumber")
        sn.text = mapping.service_num


def _serialize_find_service_response(
    resp,
    as_of_used: Optional[datetime.datetime] = None,
    upstream_vias: Optional[list[str]] = None,
    location_id: Optional[str] = None,
) -> etree._Element:
    root = etree.Element(
        f"{{{lost_xml._NS_LOST}}}findServiceResponse",
        nsmap=_RESPONSE_NSMAP,
    )
    for mapping in resp.mapping:
        _mapping_element(root, mapping, force_no_cache=(as_of_used is not None))

    if as_of_used is not None:
        as_of_el = etree.SubElement(
            root,
            f"{{{lost_xml._NS_PLANNED}}}asOf",
            nsmap={"planned": lost_xml._NS_PLANNED},
        )
        as_of_el.text = as_of_used.strftime("%Y-%m-%dT%H:%M:%SZ")

    lv = resp.location_validation
    lv_el = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}locationValidation")

    if lv.valid:
        el = etree.SubElement(lv_el, f"{{{lost_xml._NS_LOST}}}valid")
        el.text = " ".join(lv.valid)
    if lv.invalid:
        el = etree.SubElement(lv_el, f"{{{lost_xml._NS_LOST}}}invalid")
        el.text = lv.invalid
    if lv.unchecked:
        el = etree.SubElement(lv_el, f"{{{lost_xml._NS_LOST}}}unchecked")
        el.text = " ".join(lv.unchecked)

    if as_of_used is None:
        planned_el = etree.SubElement(
            lv_el,
            f"{{{lost_xml._NS_PLANNED}}}revalidateAfter",
            nsmap={"planned": lost_xml._NS_PLANNED},
        )
        planned_el.text = resp.revalidate_after or "NO-EXPIRATION"

    if resp.complete_location_record is not None:
        _serialize_complete_location(lv_el, resp.complete_location_record)

    if resp.default_mapping_returned:
        warnings_elem = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}warnings")
        dmr = etree.SubElement(warnings_elem, f"{{{lost_xml._NS_LOST}}}defaultMappingReturned")
        dmr.set("message",
                "Mapping is present for RFC 5222 protocol compliance only. "
                "No geographic authority for submitted address. "
                "Do not use for provisioning decisions.")
        dmr.set("{http://www.w3.org/XML/1998/namespace}lang", "en")

    # RFC 5222 §6: copy the request's upstream vias into the response,
    # then append this server's own via — see the worked example in
    # §8.2.1, where the response path contains both.
    path_el = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}path")
    for via_source in (upstream_vias or []):
        v = etree.SubElement(path_el, f"{{{lost_xml._NS_LOST}}}via")
        v.set("source", via_source)
    via_el = etree.SubElement(path_el, f"{{{lost_xml._NS_LOST}}}via")
    via_el.set("source", runtime_state._server_uri)

    # RFC 5222 §7: identify which submitted <location> was used to
    # answer — mirrors list_services_by_location.py's existing
    # _list_response, which emits this the same way.
    lu = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}locationUsed")
    lu.set("id", location_id or "loc")

    return root


def _serialize_redirect(resp) -> etree._Element:
    root = etree.Element(f"{{{lost_xml._NS_LOST}}}redirect", nsmap={None: lost_xml._NS_LOST})
    root.set("target", resp.target)
    root.set("source", resp.source)
    if resp.message:
        root.set("message", resp.message)
        root.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    return root


def _serialize_errors(resp) -> etree._Element:
    root = etree.Element(f"{{{lost_xml._NS_LOST}}}errors", nsmap={None: lost_xml._NS_LOST})
    root.set("source", runtime_state._server_uri)
    err = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}{resp.type}")
    message = {
        "notFound":              getattr(resp, "message", None) or "No matching address record found",
        "badRequest":            getattr(resp, "message", None) or "Request does not conform to the LoST findService schema",
        "forbidden":             "This server is provisioned as a Location Validation Function (LVF). Only requests with validateLocation='true' are accepted.",
        "locationInvalid":       getattr(resp, "message", None) or "Required element missing or empty",
        "serviceNotImplemented": "Requested service URN has no provisioned boundary",
    }.get(resp.type, "")
    if message:
        err.set("message", message)
        err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    return root


def _serialize_complete_location(parent: etree._Element, data) -> None:
    if data.layer == "SSAP":
        _complete_location_ssap(parent, data)
    else:
        _complete_location_rcl(parent, data)


def _complete_location_ssap(parent: etree._Element, data) -> None:
    record = data.record
    address = data.address
    elements: list[tuple[str, str, str]] = []

    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked:
            continue
        clark = gis_records.pidf_lo_to_clark(elem.pidf_lo)
        field = elem.civic_address_field

        if field == "hno":
            val = record.add_number
            if val is not None:
                elements.append((clark, field, str(val)))
            continue

        ssap_attr = gis_records.SSAP_ATTR.get(field)
        if ssap_attr is None:
            continue
        val = getattr(record, ssap_attr, None)
        if val is not None:
            elements.append((clark, field, str(val)))

    if not elements:
        return

    if address is not None and all(
        getattr(address, field, None) == gis_val for _, field, gis_val in elements
    ):
        return

    _emit_complete_location(parent, elements)


_RCL_SHARED_STREET: dict[str, str] = {
    "rd":   "st_name",
    "prm":  "st_premod",
    "prd":  "st_predir",
    "stp":  "st_pretyp",
    "stps": "st_presep",
    "sts":  "st_postyp",
    "pod":  "st_posdir",
    "pom":  "st_posmod",
}

_RCL_SIDE_SPECIFIC_BASE: dict[str, str] = {
    "hnp": "adnumpre",
    "pcn": "postcomm",
    "pc":  "postcode",
}

_ADMIN_FIELDS: frozenset[str] = frozenset(("country", "a1", "a2", "a3", "a4", "a5"))


def _complete_location_rcl(parent: etree._Element, data) -> None:
    record = data.record
    side = data.side or "L"
    address = data.address
    suffix = "_l" if side == "L" else "_r"
    elements: list[tuple[str, str, str]] = []

    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked:
            continue
        clark = gis_records.pidf_lo_to_clark(elem.pidf_lo)
        field = elem.civic_address_field

        if field in _ADMIN_FIELDS:
            val = getattr(record, f"{field}{suffix}", None)
        elif field == "hno":
            val = address.hno if address is not None else None
        elif field in _RCL_SHARED_STREET:
            val = getattr(record, _RCL_SHARED_STREET[field], None)
        elif field in _RCL_SIDE_SPECIFIC_BASE:
            val = getattr(record, f"{_RCL_SIDE_SPECIFIC_BASE[field]}{suffix}", None)
        else:
            continue  # rcl_unchecked fields have no RCL record mapping

        if val is not None:
            elements.append((clark, field, str(val)))

    if not elements:
        return

    if address is not None and all(
        getattr(address, field, None) == gis_val for _, field, gis_val in elements
    ):
        return

    _emit_complete_location(parent, elements)


def _emit_complete_location(parent: etree._Element, elements: list[tuple[str, str, str]]) -> None:
    cl = etree.SubElement(parent, f"{{{lost_xml._NS_RLI}}}completeLocation", nsmap={"rli": lost_xml._NS_RLI})
    loc = etree.SubElement(cl, f"{{{lost_xml._NS_LOST}}}location")
    loc.set("id", "complete")
    loc.set("profile", "civic")
    ca_el = etree.SubElement(loc, f"{{{lost_xml._NS_CA}}}civicAddress")
    for clark, _, val in elements:
        e = etree.SubElement(ca_el, clark)
        e.text = val
