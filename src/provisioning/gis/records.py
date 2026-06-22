"""
GIS row conversion, PIDF-LO <-> GIS field mapping, and JSON cache
(de)serialization helpers.

Extracted from src/lost/find_service.py as part of the GIS-code extraction
into src/provisioning/gis/. Public API (no leading underscore) — used by
src/provisioning/gis/provisioning.py and src/lost/find_service.py.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
from shapely.ops import transform

from src.lost.wire import lost_xml
from src.validation.models import (
    ELEMENT_HIERARCHY,
    CivicCoverageEntry,
    MappingElement,
    RCLRecord,
    SSAPRecord,
    ServiceBoundary,
)

PIDF_PREFIX_NS: dict[str, str] = {
    "ca": lost_xml._NS_CA, "cae": lost_xml._NS_CAE,
    "cdx1": lost_xml._NS_CDX1, "cdx2": lost_xml._NS_CDX2,
}


def pidf_lo_to_clark(pidf_lo: str) -> str:
    prefix, local = pidf_lo.split(":", 1)
    return f"{{{PIDF_PREFIX_NS[prefix]}}}{local}"


CLARK_TO_FIELD: dict[str, str] = {}
for _e in ELEMENT_HIERARCHY:
    _pfx, _local = _e.pidf_lo.split(":", 1)
    _ns = {
        "ca": lost_xml._NS_CA, "cae": lost_xml._NS_CAE,
        "cdx1": lost_xml._NS_CDX1, "cdx2": lost_xml._NS_CDX2,
    }[_pfx]
    CLARK_TO_FIELD[f"{{{_ns}}}{_local}"] = _e.civic_address_field


SSAP_ATTR: dict[str, str] = {
    "country":      "country",
    "a1":           "a1",
    "a2":           "a2",
    "a3":           "a3",
    "a4":           "a4",
    "a5":           "a5",
    "rd":           "st_name",
    "prm":          "st_premod",
    "prd":          "st_predir",
    "stp":          "st_pretyp",
    "stps":         "st_presep",
    "sts":          "st_postyp",
    "pod":          "st_posdir",
    "pom":          "st_posmod",
    "hnp":          "addnum_pre",
    "hns":          "addnum_suf",
    "mp":           "distmarker",
    "site":         "site",
    "subsite":      "subsite",
    "bld":          "structure",
    "wing":         "wing",
    "flr":          "floor",
    "unit_pretype": "unitpretyp",
    "unit_value":   "unitvalue",
    "room":         "room",
    "section":      "section",
    "row":          "row",
    "seat":         "seat",
    "pn":           "locmarker",
    "pcn":          "post_comm",
    "pc":           "post_code",
    "pce":          "postcodeex",
}


# ---------------------------------------------------------------------------
# GeoPackage row helpers
# ---------------------------------------------------------------------------

def get(row: pd.Series, col: str) -> Optional[str]:
    val = row.get(col)
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s or None


def get_int(row: pd.Series, col: str) -> Optional[int]:
    val = row.get(col)
    if val is None or pd.isna(val):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def geom_or_none(row: pd.Series):
    g = row.get("geometry")
    if g is None or g.is_empty:
        return None
    if g.has_z:
        g = transform(lambda x, y, z=None: (x, y), g)
    return g


def row_to_ssap(row: pd.Series) -> SSAPRecord:
    return SSAPRecord(
        country=get(row, "Country"),
        a1=get(row, "A1"),
        a2=get(row, "A2"),
        a3=get(row, "A3"),
        a4=get(row, "A4"),
        a5=get(row, "A5"),
        st_name=get(row, "St_Name"),
        st_premod=get(row, "St_PreMod"),
        st_predir=get(row, "St_PreDir"),
        st_pretyp=get(row, "St_PreTyp"),
        st_presep=get(row, "St_PreSep"),
        st_postyp=get(row, "St_PosTyp"),
        st_posdir=get(row, "St_PosDir"),
        st_posmod=get(row, "St_PosMod"),
        add_number=get_int(row, "Add_Number"),
        addnum_pre=get(row, "AddNum_Pre"),
        addnum_suf=get(row, "AddNum_Suf"),
        distmarker=get(row, "DistMarker"),
        site=get(row, "Site"),
        subsite=get(row, "SubSite"),
        structure=get(row, "Structure"),
        wing=get(row, "Wing"),
        floor=get(row, "Floor"),
        unitpretyp=get(row, "UnitPreTyp"),
        unitvalue=get(row, "UnitValue"),
        room=get(row, "Room"),
        section=get(row, "Section"),
        row=get(row, "Row_"),
        seat=get(row, "Seat"),
        locmarker=get(row, "LocMarker"),
        post_comm=get(row, "Post_Comm"),
        post_code=get(row, "Post_Code"),
        postcodeex=get(row, "PostCodeEx"),
        effective=get(row, "Effective"),
        expire=get(row, "Expire"),
        geometry=geom_or_none(row),
    )


def row_to_rcl(row: pd.Series, fid: Optional[int] = None) -> RCLRecord:
    parity_l = get(row, "Parity_L")
    parity_r = get(row, "Parity_R")
    valid_l  = get(row, "Valid_L")
    valid_r  = get(row, "Valid_R")
    geom = geom_or_none(row)
    if geom is not None and geom.geom_type == "MultiLineString":
        geom = geom.geoms[0]
    return RCLRecord(
        country_l=get(row, "Country_L"),
        country_r=get(row, "Country_R"),
        a1_l=get(row, "A1_L"),
        a1_r=get(row, "A1_R"),
        a2_l=get(row, "A2_L"),
        a2_r=get(row, "A2_R"),
        a3_l=get(row, "A3_L"),
        a3_r=get(row, "A3_R"),
        a4_l=get(row, "A4_L"),
        a4_r=get(row, "A4_R"),
        a5_l=get(row, "A5_L"),
        a5_r=get(row, "A5_R"),
        st_name=get(row, "St_Name"),
        st_premod=get(row, "St_PreMod"),
        st_predir=get(row, "St_PreDir"),
        st_pretyp=get(row, "St_PreTyp"),
        st_presep=get(row, "St_PreSep"),
        st_postyp=get(row, "St_PosTyp"),
        st_posdir=get(row, "St_PosDir"),
        st_posmod=get(row, "St_PosMod"),
        fromaddr_l=get_int(row, "FromAddr_L"),
        toaddr_l=get_int(row, "ToAddr_L"),
        fromaddr_r=get_int(row, "FromAddr_R"),
        toaddr_r=get_int(row, "ToAddr_R"),
        parity_l=parity_l if parity_l in ("E", "O", "B", "Z") else None,
        parity_r=parity_r if parity_r in ("E", "O", "B", "Z") else None,
        valid_l=valid_l if valid_l in ("Y", "N") else None,
        valid_r=valid_r if valid_r in ("Y", "N") else None,
        adnumpre_l=get(row, "AdNumPre_L"),
        adnumpre_r=get(row, "AdNumPre_R"),
        postcomm_l=get(row, "PostComm_L"),
        postcomm_r=get(row, "PostComm_R"),
        postcode_l=get(row, "PostCode_L"),
        postcode_r=get(row, "PostCode_R"),
        effective=get(row, "Effective"),
        expire=get(row, "Expire"),
        geometry=geom,
        fid=fid,
        nguid=get(row, "NGUID"),
    )


def row_to_boundary(row: pd.Series) -> ServiceBoundary:
    nguid = get(row, "NGUID")
    if not nguid:
        raise ValueError(f"Boundary record at row {row.name!r} has a missing or empty NGUID field")
    return ServiceBoundary(
        service_urn=get(row, "ServiceURN") or "",
        effective=get(row, "Effective"),
        expires=get(row, "Expire"),
        last_updated=get(row, "DateUpdate"),
        source=get(row, "Source"),
        source_id=get(row, "SourceId"),
        nguid=nguid,
        agency_id=get(row, "Agency_ID"),
        service_uri=get(row, "ServiceURI"),
        service_num=get(row, "ServiceNum"),
        display_name=get(row, "DsplayName"),
        geometry=geom_or_none(row),
    )


def build_default_mapping(
    service_urn: str,
    *,
    server_start_time: str,
    server_uri: str,
    default_mapping_source_id: str,
    display_name_lang: str,
) -> MappingElement:
    return MappingElement(
        service_urn=service_urn,
        expires="NO-EXPIRATION",
        last_updated=server_start_time,
        source=server_uri,
        source_id=default_mapping_source_id,
        service_uri=None,
        service_num=None,
        display_name="VALIDATION RESULT ONLY",
        display_name_lang=display_name_lang,
    )


# ---------------------------------------------------------------------------
# GIS cache — JSON serialization helpers
# ---------------------------------------------------------------------------

def ssap_to_dict(r: SSAPRecord) -> dict:
    d = r.model_dump(exclude={"geometry"})
    d["geometry"] = r.geometry.wkt if r.geometry is not None else None
    return d


def dict_to_ssap(d: dict) -> SSAPRecord:
    from shapely.wkt import loads as _wkt_loads
    geom_wkt = d.get("geometry")
    return SSAPRecord(
        **{k: v for k, v in d.items() if k != "geometry"},
        geometry=_wkt_loads(geom_wkt) if geom_wkt else None,
    )


def rcl_to_dict(r: RCLRecord) -> dict:
    d = r.model_dump(exclude={"geometry"})
    d["geometry"] = r.geometry.wkt if r.geometry is not None else None
    return d


def dict_to_rcl(d: dict) -> RCLRecord:
    from shapely.wkt import loads as _wkt_loads
    geom_wkt = d.get("geometry")
    return RCLRecord(
        **{k: v for k, v in d.items() if k != "geometry"},
        geometry=_wkt_loads(geom_wkt) if geom_wkt else None,
    )


def boundary_to_dict(b: ServiceBoundary) -> dict:
    d = b.model_dump(exclude={"geometry"})
    d["geometry"] = b.geometry.wkt if b.geometry is not None else None
    return d


def dict_to_boundary(d: dict) -> ServiceBoundary:
    from shapely.wkt import loads as _wkt_loads
    geom_wkt = d.get("geometry")
    return ServiceBoundary(
        **{k: v for k, v in d.items() if k != "geometry"},
        geometry=_wkt_loads(geom_wkt) if geom_wkt else None,
    )


def civic_entry_to_dict(e: CivicCoverageEntry) -> dict:
    return {
        "country":  e.country,
        "a1":       e.a1,
        "a2":       e.a2,
        "a3":       e.a3,
        "a4":       e.a4,
        "a5":       e.a5,
        "boundary": boundary_to_dict(e.boundary) if e.boundary is not None else None,
    }


def dict_to_civic_entry(d: dict) -> CivicCoverageEntry:
    boundary_d = d.get("boundary")
    return CivicCoverageEntry(
        country=d["country"],
        a1=d["a1"],
        a2=d["a2"],
        a3=d.get("a3"),
        a4=d.get("a4"),
        a5=d.get("a5"),
        boundary=dict_to_boundary(boundary_d) if boundary_d is not None else None,
    )
