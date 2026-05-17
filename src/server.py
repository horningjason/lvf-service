"""
FastAPI server — LVF service entry point.

Accepts LoST findService requests as XML (RFC 5222), runs the three-gate
algorithm, and returns RFC 5222 compliant XML.

Environment variables:
    LVF_GPKG_PATH       Path to the GeoPackage file (required for real use)
    LVF_SSAP_LAYER      GeoPackage layer name for SSAP (default: SiteStructureAddressPoint)
    LVF_RCL_LAYER       GeoPackage layer name for RCL  (default: RoadCenterLine)
    LVF_BOUNDARY_LAYERS Comma-separated GeoPackage layer names for service boundaries
                        (default: PsapPolygon). All listed layers are loaded into the
                        single _boundaries list; Gate 0 and response assembly filter
                        by ServiceURN, so mixing boundary types is safe.
"""

from __future__ import annotations

import logging
import os
import pickle
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

import datetime
import geopandas as gpd
import pandas as pd
from shapely.ops import transform, unary_union
from fastapi import FastAPI, Request, Response
from lxml import etree

from src import gate0, gate1, gate2, response_assembly
from src.utils import _is_temporally_active
from src.models import (
    ELEMENT_HIERARCHY,
    BadRequestResponse,
    CivicAddress,
    CivicCoverageEntry,
    ForbiddenResponse,
    LocationValidationUnavailableResponse,
    MappingElement,
    NotFoundResponse,
    RCLRecord,
    RedirectResponse,
    SSAPRecord,
    ServiceBoundary,
    ValidationRequest,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# XML namespace constants
# ---------------------------------------------------------------------------

_NS_LOST    = "urn:ietf:params:xml:ns:lost1"
_NS_CA      = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"
_NS_CAE     = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr:ext"
_NS_CDX1    = "urn:nena:xml:ns:pidf:nenaCivicAddr"   # legacy NENA namespace — STPS only
_NS_CDX2    = "urn:nena:xml:ns:pidf:nenaCivicAddr2"
_NS_RLI     = "urn:ietf:params:xml:ns:lost-rli1"
_NS_PLANNED = "urn:ietf:params:xml:ns:lostPlannedChange1"

# Namespace map declared on response root elements so QNames in
# valid/invalid/unchecked text are resolvable by clients
_RESPONSE_NSMAP: dict = {
    None:   _NS_LOST,
    "ca":   _NS_CA,
    "cae":  _NS_CAE,
    "cdx1": _NS_CDX1,
    "cdx2": _NS_CDX2,
}

# Clark-notation tag → CivicAddress field name (for XML parsing)
_CLARK_TO_FIELD: dict[str, str] = {}
for _e in ELEMENT_HIERARCHY:
    _pfx, _local = _e.pidf_lo.split(":", 1)
    _ns = {"ca": _NS_CA, "cae": _NS_CAE, "cdx1": _NS_CDX1, "cdx2": _NS_CDX2}[_pfx]
    _CLARK_TO_FIELD[f"{{{_ns}}}{_local}"] = _e.civic_address_field

# PIDF-LO prefix → namespace URI (used by completeLocation serializer)
_PIDF_PREFIX_NS: dict[str, str] = {"ca": _NS_CA, "cae": _NS_CAE, "cdx1": _NS_CDX1, "cdx2": _NS_CDX2}


def _pidf_lo_to_clark(pidf_lo: str) -> str:
    """Convert 'prefix:local' notation to Clark notation '{namespace}local'."""
    prefix, local = pidf_lo.split(":", 1)
    return f"{{{_PIDF_PREFIX_NS[prefix]}}}{local}"


# CivicAddress field name → SSAPRecord attribute name, for completeLocation serialization.
# Mirrors gate2._SSAP_FIELD; kept here so gate2 stays self-contained.
# 'hno' is absent — handled separately (integer → string, SSAPRecord.add_number).
_SSAP_ATTR: dict[str, str] = {
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
# GIS data store (populated at startup)
# ---------------------------------------------------------------------------

_ssap:       list[SSAPRecord]      = []
_rcl:        list[RCLRecord]       = []
_boundaries: list[ServiceBoundary] = []
_geodetic_coverage: dict[str, Any] = {}  # ServiceURN → unary_union geometry
_civic_coverage: list[CivicCoverageEntry] = []

_reloading: bool = False
_reloading_lock = threading.Lock()

_server_uri:        str = os.environ.get("LVF_SERVER_URI",         "lostserver.example.com")
_display_name_lang: str = os.environ.get("LVF_DISPLAY_NAME_LANG",  "en")
_parent_uri:        str = os.environ.get("LVF_PARENT_URI",          "")

# PIDF-LO names of the six admin-level elements subject to the OOC redirect rule
_ADMIN_PIDF_LO: frozenset[str] = frozenset({
    "ca:country", "ca:A1", "ca:A2", "ca:A3", "ca:A4", "ca:A5",
})

_default_mapping_source_id: str = os.environ.get("LVF_DEFAULT_MAPPING_SOURCE_ID", "")
if not _default_mapping_source_id:
    raise RuntimeError(
        "LVF_DEFAULT_MAPPING_SOURCE_ID is required but not set. "
        "Recommended value: {00000000-0000-0000-0000-000000000000}"
    )

_SERVER_START_TIME: str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# XML schema (loaded once at startup; None disables validation rather than refusing all requests)
_schema: Optional[etree.XMLSchema] = None


def _load_schema() -> Optional[etree.XMLSchema]:
    schema_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")
    lost1_xsd = os.path.join(schema_dir, "lost1.xsd")
    try:
        schema_doc = etree.parse(lost1_xsd)
        compiled = etree.XMLSchema(schema_doc)
        log.info("XML schema loaded from %s", lost1_xsd)
        return compiled
    except Exception as exc:
        log.warning(
            "Could not load XML schema from %s: %s — schema validation disabled",
            lost1_xsd, exc,
        )
        return None


def _validate_schema(body: bytes) -> Optional[str]:
    """Return None if body passes schema validation, or a human-readable error string."""
    if _schema is None:
        return None
    try:
        doc = etree.fromstring(body)
    except etree.XMLSyntaxError as exc:
        return f"Malformed XML: {exc}"
    if _schema.validate(doc):
        return None
    error_log = _schema.error_log
    if error_log:
        first = error_log[0]
        return f"{first.message} (line {first.line})"
    return "Request does not conform to the LoST findService schema"

# URN aliases for urn:service:sos. Requests for these URNs are processed against the
# provisioned urn:service:sos boundaries; the response mapping echoes the requested URN.
_sos_alias_urns: frozenset[str] = frozenset(
    urn.strip().lower()
    for urn in os.environ.get("LVF_SOS_ALIAS_URNS", "").split(",")
    if urn.strip()
)


def _resolve_service_urn(requested_urn: str) -> tuple[str, bool]:
    """
    Resolve a requested service URN to the effective provisioned URN.

    Returns (effective_urn, is_alias). When is_alias is True, Gate 0 and boundary
    selection use effective_urn (urn:service:sos), but all mapping elements in the
    response carry the original requested_urn.
    """
    if requested_urn.lower() in _sos_alias_urns:
        return "urn:service:sos", True
    return requested_urn, False


# ---------------------------------------------------------------------------
# GeoPackage helpers
# ---------------------------------------------------------------------------

def _get(row: pd.Series, col: str) -> Optional[str]:
    """Return a GeoDataFrame cell as a stripped string, or None if absent/NaN."""
    val = row.get(col)
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s or None


def _get_int(row: pd.Series, col: str) -> Optional[int]:
    val = row.get(col)
    if val is None or pd.isna(val):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _geom_or_none(row: pd.Series):
    g = row.get("geometry")
    if g is None or g.is_empty:
        return None
    if g.has_z:
        g = transform(lambda x, y, z=None: (x, y), g)
    return g


def _row_to_ssap(row: pd.Series) -> SSAPRecord:
    return SSAPRecord(
        country=_get(row, "Country"),
        a1=_get(row, "A1"),
        a2=_get(row, "A2"),
        a3=_get(row, "A3"),
        a4=_get(row, "A4"),
        a5=_get(row, "A5"),
        st_name=_get(row, "St_Name"),
        st_premod=_get(row, "St_PreMod"),
        st_predir=_get(row, "St_PreDir"),
        st_pretyp=_get(row, "St_PreTyp"),
        st_presep=_get(row, "St_PreSep"),
        st_postyp=_get(row, "St_PosTyp"),
        st_posdir=_get(row, "St_PosDir"),
        st_posmod=_get(row, "St_PosMod"),
        add_number=_get_int(row, "Add_Number"),
        addnum_pre=_get(row, "AddNum_Pre"),
        addnum_suf=_get(row, "AddNum_Suf"),
        distmarker=_get(row, "DistMarker"),
        site=_get(row, "Site"),
        subsite=_get(row, "SubSite"),
        structure=_get(row, "Structure"),
        wing=_get(row, "Wing"),
        floor=_get(row, "Floor"),
        unitpretyp=_get(row, "UnitPreTyp"),
        unitvalue=_get(row, "UnitValue"),
        room=_get(row, "Room"),
        section=_get(row, "Section"),
        row=_get(row, "Row"),
        seat=_get(row, "Seat"),
        locmarker=_get(row, "LocMarker"),
        post_comm=_get(row, "Post_Comm"),
        post_code=_get(row, "Post_Code"),
        postcodeex=_get(row, "PostCodeEx"),
        effective=_get(row, "Effective"),
        expire=_get(row, "Expire"),
        geometry=_geom_or_none(row),
    )


def _row_to_rcl(row: pd.Series, fid: Optional[int] = None) -> RCLRecord:
    parity_l = _get(row, "Parity_L")
    parity_r = _get(row, "Parity_R")
    valid_l  = _get(row, "Valid_L")
    valid_r  = _get(row, "Valid_R")
    geom = _geom_or_none(row)
    if geom is not None and geom.geom_type == "MultiLineString":
        geom = geom.geoms[0]
    return RCLRecord(
        country_l=_get(row, "Country_L"),
        country_r=_get(row, "Country_R"),
        a1_l=_get(row, "A1_L"),
        a1_r=_get(row, "A1_R"),
        a2_l=_get(row, "A2_L"),
        a2_r=_get(row, "A2_R"),
        a3_l=_get(row, "A3_L"),
        a3_r=_get(row, "A3_R"),
        a4_l=_get(row, "A4_L"),
        a4_r=_get(row, "A4_R"),
        a5_l=_get(row, "A5_L"),
        a5_r=_get(row, "A5_R"),
        st_name=_get(row, "St_Name"),
        st_premod=_get(row, "St_PreMod"),
        st_predir=_get(row, "St_PreDir"),
        st_pretyp=_get(row, "St_PreTyp"),
        st_presep=_get(row, "St_PreSep"),
        st_postyp=_get(row, "St_PosTyp"),
        st_posdir=_get(row, "St_PosDir"),
        st_posmod=_get(row, "St_PosMod"),
        fromaddr_l=_get_int(row, "FromAddr_L"),
        toaddr_l=_get_int(row, "ToAddr_L"),
        fromaddr_r=_get_int(row, "FromAddr_R"),
        toaddr_r=_get_int(row, "ToAddr_R"),
        parity_l=parity_l if parity_l in ("E", "O", "B") else None,
        parity_r=parity_r if parity_r in ("E", "O", "B") else None,
        valid_l=valid_l if valid_l in ("Y", "N") else None,
        valid_r=valid_r if valid_r in ("Y", "N") else None,
        adnumpre_l=_get(row, "AdNumPre_L"),
        adnumpre_r=_get(row, "AdNumPre_R"),
        postcomm_l=_get(row, "PostComm_L"),
        postcomm_r=_get(row, "PostComm_R"),
        postcode_l=_get(row, "PostCode_L"),
        postcode_r=_get(row, "PostCode_R"),
        effective=_get(row, "Effective"),
        expire=_get(row, "Expire"),
        geometry=geom,
        fid=fid,
        nguid=_get(row, "NGUID"),
    )


def _row_to_boundary(row: pd.Series) -> ServiceBoundary:
    nguid = _get(row, "NGUID")
    if not nguid:
        raise ValueError(f"Boundary record at row {row.name!r} has a missing or empty NGUID field")
    return ServiceBoundary(
        service_urn=_get(row, "ServiceURN") or "",
        effective=_get(row, "Effective"),
        expires=_get(row, "Expire"),
        last_updated=_get(row, "DateUpdate"),
        source=_get(row, "Source"),
        source_id=_get(row, "SourceId"),
        nguid=nguid,
        agency_id=_get(row, "Agency_ID"),
        service_uri=_get(row, "ServiceURI"),
        service_num=_get(row, "ServiceNum"),
        display_name=_get(row, "DsplayName"),
        geometry=_geom_or_none(row),
    )


def _build_default_mapping(service_urn: str) -> MappingElement:
    return MappingElement(
        service_urn=service_urn,
        expires="NO-EXPIRATION",
        last_updated=_SERVER_START_TIME,
        source=_server_uri,
        source_id=_default_mapping_source_id,
        service_uri=None,
        service_num=None,
        display_name="VALIDATION RESULT ONLY",
        display_name_lang=_display_name_lang,
    )



def _load_gis_data(gpkg_path: str) -> None:
    """Load SSAP, RCL, and all boundary layers from a GeoPackage into memory."""
    global _ssap, _rcl, _boundaries, _geodetic_coverage, _civic_coverage

    pickle_path = os.path.splitext(gpkg_path)[0] + ".pickle"

    if os.path.exists(pickle_path):
        gpkg_mtime = os.path.getmtime(gpkg_path)
        if os.path.getmtime(pickle_path) >= gpkg_mtime:
            try:
                log.info("Cache hit — loading GIS data from pickle: %s", pickle_path)
                with open(pickle_path, "rb") as f:
                    data = pickle.load(f)
                _ssap              = data["ssap"]
                _rcl               = data["rcl"]
                _boundaries        = data["boundaries"]
                _civic_coverage    = data["civic_coverage"]
                _geodetic_coverage = data["geodetic_coverage"]
                log.info(
                    "Loaded from pickle: %d SSAP, %d RCL, %d boundaries, "
                    "%d civic coverage entries, %d geodetic URN(s)",
                    len(_ssap), len(_rcl), len(_boundaries),
                    len(_civic_coverage), len(_geodetic_coverage),
                )
                return
            except Exception as exc:
                log.warning(
                    "Pickle load failed (%s) — falling back to GPKG and rebuilding cache",
                    exc,
                )
        else:
            log.info("Cache miss — GPKG is newer than pickle, rebuilding: %s", pickle_path)
    else:
        log.info("Cache miss — no pickle found, building for the first time: %s", pickle_path)

    ssap_layer      = os.environ.get("LVF_SSAP_LAYER",     "SiteStructureAddressPoint")
    rcl_layer       = os.environ.get("LVF_RCL_LAYER",      "RoadCenterLine")
    boundary_layers = [
        name.strip()
        for name in os.environ.get("LVF_BOUNDARY_LAYERS", "PsapPolygon").split(",")
        if name.strip()
    ]

    for layer_name, converter, store_name in [
        (ssap_layer, _row_to_ssap, "SSAP"),
        (rcl_layer,  _row_to_rcl,  "RCL"),
    ]:
        try:
            gdf = gpd.read_file(gpkg_path, layer=layer_name, engine="pyogrio")
            if store_name == "RCL":
                records = [converter(row, idx) for idx, row in gdf.iterrows()]
                _rcl = records
            else:
                records = [converter(row) for _, row in gdf.iterrows()]
                _ssap = records
            log.info("Loaded %d %s records from '%s'", len(records), store_name, layer_name)
        except Exception as exc:
            log.warning("Could not load %s layer '%s': %s", store_name, layer_name, exc)

    _boundaries = []
    for layer_name in boundary_layers:
        try:
            gdf = gpd.read_file(gpkg_path, layer=layer_name, engine="pyogrio")
            records = [_row_to_boundary(row) for _, row in gdf.iterrows()]
            _boundaries.extend(records)
            log.info("Loaded %d boundary records from '%s'", len(records), layer_name)
        except Exception as exc:
            log.warning("Could not load boundary layer '%s': %s", layer_name, exc)

    _derive_geodetic_coverage()
    _derive_civic_coverage()

    try:
        with open(pickle_path, "wb") as f:
            pickle.dump(
                {
                    "ssap":              _ssap,
                    "rcl":               _rcl,
                    "boundaries":        _boundaries,
                    "civic_coverage":    _civic_coverage,
                    "geodetic_coverage": _geodetic_coverage,
                    "gpkg_mtime":        os.path.getmtime(gpkg_path),
                },
                f,
            )
        log.info("GIS data cached to pickle: %s", pickle_path)
    except Exception as exc:
        log.warning("Could not write pickle cache: %s", exc)


def _watch_gpkg(gpkg_path: str) -> None:
    """Daemon thread: poll the GPKG file for changes and reload GIS data when detected."""
    global _reloading
    interval = int(os.environ.get("LVF_GPKG_POLL_INTERVAL_SECONDS", "60"))
    try:
        baseline_mtime = os.path.getmtime(gpkg_path)
    except OSError:
        log.warning("GPKG watcher: cannot stat %s — watcher exiting", gpkg_path)
        return

    while True:
        time.sleep(interval)
        try:
            current_mtime = os.path.getmtime(gpkg_path)
        except OSError:
            log.warning("GPKG watcher: cannot stat %s — skipping this poll", gpkg_path)
            continue

        if current_mtime > baseline_mtime:
            log.info("New GPKG detected at %s — reloading GIS data", gpkg_path)
            with _reloading_lock:
                _reloading = True
            try:
                _load_gis_data(gpkg_path)
                baseline_mtime = current_mtime
                with _reloading_lock:
                    _reloading = False
                log.info("GIS data reload complete — resuming normal service")
            except Exception:
                log.error(
                    "GIS data reload failed — service remains unavailable", exc_info=True
                )


def initialize(gpkg_path: str | None = None) -> None:
    """Load GIS data for use by handle_find_service(). Call once before the first request."""
    global _schema
    _schema = _load_schema()
    if _schema is None:
        log.warning("Operating without XML schema validation")

    path = gpkg_path or os.environ.get("LVF_GPKG_PATH")
    if path:
        _load_gis_data(path)
    else:
        log.warning("No LVF_GPKG_PATH configured — GIS data not loaded")


def _derive_geodetic_coverage() -> None:
    global _geodetic_coverage
    from collections import defaultdict
    by_urn: dict[str, list] = defaultdict(list)
    for b in _boundaries:
        if b.geometry is not None:
            by_urn[b.service_urn].append(b.geometry)
    _geodetic_coverage = {
        urn: unary_union(geoms)
        for urn, geoms in by_urn.items()
        if geoms
    }
    log.info("Derived geodetic coverage region for %d service URN(s)", len(_geodetic_coverage))


def _derive_civic_coverage() -> None:
    """
    Derive the civic coverage region lookup table from RCL and service boundaries.

    For each RCL record and each side (L/R):
    - Compute perpendicular test point 0.0001 degrees from segment midpoint
    - Point-in-polygon test against service boundaries
    - If inside: record (admin_tuple, boundary) association

    Then aggregate at three levels ("apply the same aggregation logic for
    A4 and A5 where present"):
    - A3: if ALL A3 values within (country, A1, A2) map to the same boundary →
           wildcard entry (A3=None, A4=None, A5=None)
    - A4: for remaining specific-A3 groups, if ALL A4 values within
           (country, A1, A2, A3) map to the same boundary →
           wildcard entry (A4=None, A5=None)
    - A5: for remaining specific-A4 groups, if ALL A5 values within
           (country, A1, A2, A3, A4) map to the same boundary →
           wildcard entry (A5=None); otherwise specific A5 entries
    """
    global _civic_coverage
    from collections import defaultdict

    now = datetime.datetime.now(datetime.timezone.utc)
    active_rcl = [r for r in _rcl if _is_temporally_active(r.effective, r.expire, now)]
    active_boundaries = [b for b in _boundaries if _is_temporally_active(b.effective, b.expires, now)]

    raw: list[tuple[dict, ServiceBoundary]] = []

    for record in active_rcl:
        for side in ("L", "R"):
            geom = record.geometry
            if geom is None:
                continue
            point = response_assembly._rcl_representative_point(geom, side)
            if point is None:
                continue
            containing = None
            for b in active_boundaries:
                if b.geometry is not None and b.geometry.contains(point):
                    containing = b
                    break
            if containing is None:
                continue
            suffix = "_l" if side == "L" else "_r"
            t = {
                "country": getattr(record, f"country{suffix}"),
                "a1":      getattr(record, f"a1{suffix}"),
                "a2":      getattr(record, f"a2{suffix}"),
                "a3":      getattr(record, f"a3{suffix}"),
                "a4":      getattr(record, f"a4{suffix}"),
                "a5":      getattr(record, f"a5{suffix}"),
            }
            if not all([t["country"], t["a1"], t["a2"]]):
                continue
            raw.append((t, containing))

    # Normalize all admin values to uppercase once for consistent grouping
    norm_raw = [
        ({k: v.upper() if v else None for k, v in t.items()}, b)
        for t, b in raw
    ]

    raw_entries: list[CivicCoverageEntry] = []

    # ---- A3 aggregation: group by (country, a1, a2, boundary) ----
    a3_grp: dict = defaultdict(set)
    a3_bnd: dict = {}
    for t, b in norm_raw:
        key = (t["country"], t["a1"], t["a2"], id(b))
        a3_grp[key].add(t["a3"])
        a3_bnd[key] = b

    all_a3: dict = defaultdict(set)
    for (country, a1, a2, _), a3s in a3_grp.items():
        all_a3[(country, a1, a2)].update(a3s)

    a3_wildcarded: set = set()  # (country, a1, a2, bid) that produced a wildcard A3 entry

    for (country, a1, a2, bid), a3s in a3_grp.items():
        b = a3_bnd[(country, a1, a2, bid)]
        if a3s == all_a3[(country, a1, a2)]:
            a3_wildcarded.add((country, a1, a2, bid))
            raw_entries.append(CivicCoverageEntry(
                country=country, a1=a1, a2=a2, a3=None, a4=None, a5=None, boundary=b,
            ))
        # specific A3 values are resolved in the A4 aggregation step below

    # ---- A4 aggregation: within each specific (country, a1, a2, a3, boundary) ----
    a4_grp: dict = defaultdict(set)
    a4_bnd: dict = {}
    for t, b in norm_raw:
        country, a1, a2 = t["country"], t["a1"], t["a2"]
        if (country, a1, a2, id(b)) in a3_wildcarded:
            continue  # already covered by the A3 wildcard entry
        key = (country, a1, a2, t["a3"], id(b))
        a4_grp[key].add(t["a4"])
        a4_bnd[key] = b

    all_a4: dict = defaultdict(set)
    for (country, a1, a2, a3, _), a4s in a4_grp.items():
        all_a4[(country, a1, a2, a3)].update(a4s)

    a4_wildcarded: set = set()  # (country, a1, a2, a3, bid) that produced a wildcard A4 entry

    for (country, a1, a2, a3, bid), a4s in a4_grp.items():
        b = a4_bnd[(country, a1, a2, a3, bid)]
        if a4s == all_a4[(country, a1, a2, a3)]:
            a4_wildcarded.add((country, a1, a2, a3, bid))
            raw_entries.append(CivicCoverageEntry(
                country=country, a1=a1, a2=a2, a3=a3, a4=None, a5=None, boundary=b,
            ))
        # specific A4 values are resolved in the A5 aggregation step below

    # ---- A5 aggregation: within each specific (country, a1, a2, a3, a4, boundary) ----
    a5_grp: dict = defaultdict(set)
    a5_bnd: dict = {}
    for t, b in norm_raw:
        country, a1, a2, a3 = t["country"], t["a1"], t["a2"], t["a3"]
        bid = id(b)
        if (country, a1, a2, bid) in a3_wildcarded:
            continue
        if (country, a1, a2, a3, bid) in a4_wildcarded:
            continue  # already covered by the A4 wildcard entry
        key = (country, a1, a2, a3, t["a4"], bid)
        a5_grp[key].add(t["a5"])
        a5_bnd[key] = b

    all_a5: dict = defaultdict(set)
    for (country, a1, a2, a3, a4, _), a5s in a5_grp.items():
        all_a5[(country, a1, a2, a3, a4)].update(a5s)

    for (country, a1, a2, a3, a4, bid), a5s in a5_grp.items():
        b = a5_bnd[(country, a1, a2, a3, a4, bid)]
        if a5s == all_a5[(country, a1, a2, a3, a4)]:
            raw_entries.append(CivicCoverageEntry(
                country=country, a1=a1, a2=a2, a3=a3, a4=a4, a5=None, boundary=b,
            ))
        else:
            for a5_val in a5s:
                raw_entries.append(CivicCoverageEntry(
                    country=country, a1=a1, a2=a2, a3=a3, a4=a4, a5=a5_val, boundary=b,
                ))

    # Deduplicate on the final output tuple — same 8 fields the endpoint returns
    dedup: dict = {}
    for e in raw_entries:
        b_name = e.boundary.display_name if e.boundary is not None else None
        b_urn  = e.boundary.service_urn  if e.boundary is not None else None
        key = (e.country, e.a1, e.a2, e.a3, e.a4, e.a5, b_name, b_urn)
        if key not in dedup:
            dedup[key] = e

    _civic_coverage = list(dedup.values())
    log.info("Derived civic coverage region: %d entries", len(_civic_coverage))


def lookup_civic_coverage(
    country: Optional[str],
    a1: Optional[str],
    a2: Optional[str],
    a3: Optional[str] = None,
    a4: Optional[str] = None,
    a5: Optional[str] = None,
) -> Optional[CivicCoverageEntry]:
    """
    Longest-prefix match against the civic coverage region.
    Returns the most specific matching entry, or None if no match.
    Comparison is case-insensitive.
    """
    if not all([country, a1, a2]):
        return None

    def norm(v): return v.upper() if v else None

    c, s, co = norm(country), norm(a1), norm(a2)
    a3n, a4n, a5n = norm(a3), norm(a4), norm(a5)

    best: Optional[CivicCoverageEntry] = None
    best_specificity = -1
    conflict = False

    for entry in _civic_coverage:
        if entry.country != c or entry.a1 != s or entry.a2 != co:
            continue
        if entry.a3 is not None and entry.a3 != a3n:
            continue
        if entry.a4 is not None and entry.a4 != a4n:
            continue
        if entry.a5 is not None and entry.a5 != a5n:
            continue
        specificity = (
            (1 if entry.a3 is not None else 0) +
            (1 if entry.a4 is not None else 0) +
            (1 if entry.a5 is not None else 0)
        )
        if specificity > best_specificity:
            best_specificity = specificity
            best = entry
            conflict = False
        elif specificity == best_specificity:
            best_nguid = best.boundary.nguid if best else None
            entry_nguid = entry.boundary.nguid
            if best_nguid is None or entry_nguid is None or best_nguid != entry_nguid:
                conflict = True

    if conflict:
        return None
    return best


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _parse_request(body: bytes) -> ValidationRequest:
    """
    Parse a LoST findService XML request (RFC 5222) into a ValidationRequest.

    Preserves the omitted-vs-empty distinction: elements absent from the
    PIDF-LO produce None; elements present but empty produce "".
    Raises ValueError for malformed or structurally incomplete requests.
    """
    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Malformed XML: {exc}") from exc

    service_el = root.find(f"{{{_NS_LOST}}}service")
    if service_el is None:
        raise ValueError("Missing 'service' element in findService request")
    service_urn = (service_el.text or "").strip()
    if not service_urn:
        raise ValueError("'service' element is empty")

    # civicAddress may be nested anywhere under <location>
    civic_el = root.find(f".//{{{_NS_CA}}}civicAddress")
    if civic_el is None:
        raise ValueError("Missing 'civicAddress' element in findService request")

    fields: dict[str, str] = {}
    for child in civic_el:
        ca_field = _CLARK_TO_FIELD.get(child.tag)
        if ca_field is not None:
            # Present but empty text → "" (empty); absent tag → not in fields → None
            fields[ca_field] = child.text if child.text is not None else ""

    as_of_el = root.find(f"{{{_NS_PLANNED}}}asOf")
    if as_of_el is not None:
        raise ValueError(
            "This LVF does not support 'asOf' queries. "
            "Remove the planned:asOf element and resubmit to validate against the current active dataset."
        )

    validate_location = root.get("validateLocation", "false")

    return ValidationRequest(
        service_urn=service_urn,
        civic_address=CivicAddress(**fields),
        validate_location=validate_location,
    )


def _parse_return_additional_location(body: bytes) -> str:
    """
    Extract rli:returnAdditionalLocation from a findService request.

    Absent attribute means "generate completeLocation by default" — returns "complete".
    Only explicit "none" suppresses completeLocation. "similar" also suppresses it
    (client requested similar locations only, which are not yet implemented).
    Unrecognised values and parse errors default to "complete".
    """
    _VALID = {"none", "similar", "complete", "any"}
    try:
        root = etree.fromstring(body)
        val = root.get(f"{{{_NS_RLI}}}returnAdditionalLocation")
        if val is None:
            return "complete"
        return val if val in _VALID else "complete"
    except Exception:
        return "complete"


# ---------------------------------------------------------------------------
# XML serialization
# ---------------------------------------------------------------------------

def _mapping_element(parent: etree._Element, mapping) -> None:
    """Append a <mapping> child element to parent (RFC 5222 §8.4.1)."""
    m = etree.SubElement(parent, f"{{{_NS_LOST}}}mapping")

    # Required attributes — fall back to spec-defined defaults when GIS data absent
    m.set("expires",     mapping.expires     or "NO-EXPIRATION")
    m.set("lastUpdated", mapping.last_updated or
          datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    m.set("source",   mapping.source or _server_uri)
    m.set("sourceId", mapping.source_id or "unknown")

    if mapping.display_name:
        dn = etree.SubElement(m, f"{{{_NS_LOST}}}displayName")
        dn.set("{http://www.w3.org/XML/1998/namespace}lang",
               mapping.display_name_lang or _display_name_lang)
        dn.text = mapping.display_name

    svc = etree.SubElement(m, f"{{{_NS_LOST}}}service")
    svc.text = mapping.service_urn

    if mapping.service_uri:
        uri_el = etree.SubElement(m, f"{{{_NS_LOST}}}uri")
        uri_el.text = mapping.service_uri

    if mapping.service_num:
        sn = etree.SubElement(m, f"{{{_NS_LOST}}}serviceNumber")
        sn.text = mapping.service_num


def _serialize_find_service_response(resp) -> etree._Element:
    """Build a <findServiceResponse> element for locationValidation outcomes."""
    root = etree.Element(
        f"{{{_NS_LOST}}}findServiceResponse",
        nsmap=_RESPONSE_NSMAP,
    )
    for mapping in resp.mapping:
        _mapping_element(root, mapping)

    lv = resp.location_validation
    lv_el = etree.SubElement(root, f"{{{_NS_LOST}}}locationValidation")

    if lv.valid:
        el = etree.SubElement(lv_el, f"{{{_NS_LOST}}}valid")
        el.text = " ".join(lv.valid)
    if lv.invalid:
        el = etree.SubElement(lv_el, f"{{{_NS_LOST}}}invalid")
        el.text = lv.invalid
    if lv.unchecked:
        el = etree.SubElement(lv_el, f"{{{_NS_LOST}}}unchecked")
        el.text = " ".join(lv.unchecked)

    planned_el = etree.SubElement(
        lv_el,
        f"{{{_NS_PLANNED}}}revalidateAfter",
        nsmap={"planned": _NS_PLANNED},
    )
    planned_el.text = resp.revalidate_after or "NO-EXPIRATION"

    if resp.complete_location_record is not None:
        _serialize_complete_location(lv_el, resp.complete_location_record)

    if resp.default_mapping_returned:
        warnings_elem = etree.SubElement(root, f"{{{_NS_LOST}}}warnings")
        dmr = etree.SubElement(warnings_elem, f"{{{_NS_LOST}}}defaultMappingReturned")
        dmr.set("message",
                "Mapping is present for RFC 5222 protocol compliance only. "
                "No geographic authority for submitted address. "
                "Do not use for provisioning decisions.")
        dmr.set("{http://www.w3.org/XML/1998/namespace}lang", "en")

    path_el = etree.SubElement(root, f"{{{_NS_LOST}}}path")
    via_el  = etree.SubElement(path_el, f"{{{_NS_LOST}}}via")
    via_el.set("source", _server_uri)

    return root


def _serialize_redirect(resp) -> etree._Element:
    """Build a <redirect> element per RFC 5222 §8.6."""
    root = etree.Element(f"{{{_NS_LOST}}}redirect", nsmap={None: _NS_LOST})
    root.set("target", resp.target)
    root.set("source", resp.source)
    msg = etree.SubElement(root, f"{{{_NS_LOST}}}message")
    msg.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    msg.text = resp.message
    return root


def _serialize_errors(resp) -> etree._Element:
    """Build an <errors> element for notFound, locationInvalid, serviceNotImplemented."""
    root = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
    root.set("source", _server_uri)
    err  = etree.SubElement(root, f"{{{_NS_LOST}}}{resp.type}")
    err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    err.text = {
        "badRequest":             getattr(resp, "message", None) or "Request does not conform to the LoST findService schema",
        "forbidden":              "This server is provisioned as a Location Validation Function (LVF). Only requests with validateLocation='true' are accepted.",
        "notFound":               getattr(resp, "message", None) or "No matching address record found",
        "locationInvalid":        getattr(resp, "message", None) or "Required element missing or empty",
        "serviceNotImplemented":  "Requested service URN has no provisioned boundary",
    }.get(resp.type, resp.type)
    return root


def _to_xml_response(resp, status: int) -> Response:
    if resp.type == "locationValidation":
        tree = _serialize_find_service_response(resp)
    elif resp.type == "redirect":
        tree = _serialize_redirect(resp)
    elif resp.type == "locationValidationUnavailable":
        # Warning — wrap in findServiceResponse with <warnings>
        root = etree.Element(
            f"{{{_NS_LOST}}}findServiceResponse",
            nsmap=_RESPONSE_NSMAP,
        )
        w = etree.SubElement(root, f"{{{_NS_LOST}}}warnings")
        w.set("source", _server_uri)
        lvu = etree.SubElement(w, f"{{{_NS_LOST}}}locationValidationUnavailable")
        lvu.set("message", getattr(resp, "message", "LVF temporarily cannot fulfill validation request"))
        lvu.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
        path_el = etree.SubElement(root, f"{{{_NS_LOST}}}path")
        via_el  = etree.SubElement(path_el, f"{{{_NS_LOST}}}via")
        via_el.set("source", _server_uri)
        tree = root
    else:
        tree = _serialize_errors(resp)

    body = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, status_code=status, media_type="application/xml")


def _serialize_complete_location(parent: etree._Element, data) -> None:
    """
    Append <rli:completeLocation> to parent for either an SSAP or RCL match.

    No-ops when the submitted address already contains every non-null field present
    on the matched GIS record (nothing to add). The rli namespace is declared on the
    <rli:completeLocation> element itself so it only appears when content is returned.
    """
    if data.layer == "SSAP":
        _complete_location_ssap(parent, data)
    else:
        _complete_location_rcl(parent, data)


def _complete_location_ssap(parent: etree._Element, data) -> None:
    """Build completeLocation elements from an SSAPRecord."""
    record = data.record
    address = data.address
    elements: list[tuple[str, str, str]] = []  # (clark, field, value)

    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked:
            continue
        clark = _pidf_lo_to_clark(elem.pidf_lo)
        field = elem.civic_address_field

        if field == "hno":
            val = record.add_number
            if val is not None:
                elements.append((clark, field, str(val)))
            continue

        ssap_attr = _SSAP_ATTR.get(field)
        if ssap_attr is None:
            continue
        val = getattr(record, ssap_attr, None)
        if val is not None:
            elements.append((clark, field, str(val)))

    if not elements:
        return

    # Suppress only when every submitted value exactly matches the canonical GIS value
    # (case-sensitive). Gate 2 comparison is case-insensitive; this check is not.
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
    """Build completeLocation elements from an RCLRecord."""
    record = data.record
    side = data.side or "L"
    address = data.address
    valid_set = set(data.valid_pidf_lo or [])
    suffix = "_l" if side == "L" else "_r"
    elements: list[tuple[str, str, str]] = []

    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked or elem.rcl_unchecked:
            continue
        clark = _pidf_lo_to_clark(elem.pidf_lo)
        field = elem.civic_address_field

        if field in _ADMIN_FIELDS:
            if elem.pidf_lo not in valid_set:
                continue
            val = getattr(record, f"{field}{suffix}", None)
        elif field == "hno":
            val = address.hno if address is not None else None
        elif field in _RCL_SHARED_STREET:
            val = getattr(record, _RCL_SHARED_STREET[field], None)
        elif field in _RCL_SIDE_SPECIFIC_BASE:
            val = getattr(record, f"{_RCL_SIDE_SPECIFIC_BASE[field]}{suffix}", None)
        else:
            continue

        if val is not None:
            elements.append((clark, field, str(val)))

    if not elements:
        return

    # Suppress only when every submitted value exactly matches the canonical GIS value
    # (case-sensitive). Gate 2 comparison is case-insensitive; this check is not.
    if address is not None and all(
        getattr(address, field, None) == gis_val for _, field, gis_val in elements
    ):
        return

    _emit_complete_location(parent, elements)


def _emit_complete_location(parent: etree._Element, elements: list[tuple[str, str, str]]) -> None:
    """Write the <rli:completeLocation> wrapper and civicAddress child elements."""
    cl = etree.SubElement(parent, f"{{{_NS_RLI}}}completeLocation", nsmap={"rli": _NS_RLI})
    loc = etree.SubElement(cl, f"{{{_NS_LOST}}}location")
    loc.set("id", "complete")
    loc.set("profile", "civic")
    ca_el = etree.SubElement(loc, f"{{{_NS_CA}}}civicAddress")
    for clark, _, val in elements:
        e = etree.SubElement(ca_el, clark)
        e.text = val


# ---------------------------------------------------------------------------
# Out-of-coverage admin redirect helper
# ---------------------------------------------------------------------------

def _check_ooc_admin(address: CivicAddress, g2):
    """
    Return a redirect or notFound response when Gate 2 stop-on-first-invalid
    fired at an admin element (country/A1–A5) that lies outside this LVF's
    civic coverage region.  Returns None for:
      - non-admin invalid elements (existing invalid behavior unchanged)
      - admin invalid elements that ARE within civic coverage (genuine
        validation failures — e.g. A3=NoSuchCity within a covered county)
    """
    if g2.outcome != "invalid" or g2.state.invalid not in _ADMIN_PIDF_LO:
        return None
    coverage = lookup_civic_coverage(
        address.country, address.a1, address.a2,
        address.a3, address.a4, address.a5,
    )
    if coverage is not None:
        return None  # in coverage — existing invalid behavior applies
    if _parent_uri:
        return RedirectResponse(target=_parent_uri, source=_server_uri)
    return NotFoundResponse(
        message="Location is outside this LVF's coverage area and no parent LVF is configured"
    )


# ---------------------------------------------------------------------------
# Programmatic entry point (for test harnesses — bypasses HTTP)
# ---------------------------------------------------------------------------

def handle_find_service(xml_bytes: bytes) -> bytes:
    """
    Process a raw LoST findService XML request and return raw XML response bytes.

    Mirrors the /validate endpoint without requiring an HTTP request object.
    Returns <badRequest> bytes for malformed or structurally incomplete XML.
    Call initialize() once before using this.
    """
    with _reloading_lock:
        if _reloading:
            return _to_xml_response(
                LocationValidationUnavailableResponse(
                    message="LVF is reloading GIS data — service will resume automatically"
                ),
                status=200,
            ).body

    schema_error = _validate_schema(xml_bytes)
    if schema_error is not None:
        return _to_xml_response(BadRequestResponse(message=schema_error), status=200).body

    try:
        req = _parse_request(xml_bytes)
    except ValueError as exc:
        return _to_xml_response(BadRequestResponse(message=str(exc)), status=200).body

    if req.validate_location != "true":
        return _to_xml_response(ForbiddenResponse(), status=200).body

    effective_urn, is_alias = _resolve_service_urn(req.service_urn)

    now = datetime.datetime.now(datetime.timezone.utc)
    g0 = gate0.check(effective_urn, _boundaries, now)
    if g0 is not None:
        return _to_xml_response(g0, status=200).body

    g1 = gate1.check(req.civic_address)
    if g1 is not None:
        return _to_xml_response(g1, status=200).body

    matched_boundaries = [
        b for b in _boundaries
        if b.service_urn.lower() == effective_urn.lower()
        and _is_temporally_active(b.effective, b.expires, now)
    ]
    ral = _parse_return_additional_location(xml_bytes)
    g2 = gate2.run(req.civic_address, _ssap, _rcl, now)

    ooc = _check_ooc_admin(req.civic_address, g2)
    if ooc is not None:
        return _to_xml_response(ooc, status=200).body

    final = response_assembly.assemble(
        g2,
        matched_boundaries,
        service_urn=req.service_urn,
        address=req.civic_address,
        civic_coverage_lookup=lookup_civic_coverage,
        default_mapping_factory=_build_default_mapping,
        return_additional_location=ral,
        server_uri=_server_uri,
        display_name_lang=_display_name_lang,
    )
    # Alias URN: override service_urn in all mapping elements to echo the requested URN
    if is_alias and final.type == "locationValidation":
        for m in final.mapping:
            m.service_urn = req.service_urn
    return _to_xml_response(final, status=200).body


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _schema
    _schema = _load_schema()
    if _schema is None:
        log.warning("Operating without XML schema validation")

    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    if gpkg_path:
        _load_gis_data(gpkg_path)
        poll_interval = int(os.environ.get("LVF_GPKG_POLL_INTERVAL_SECONDS", "60"))
        if poll_interval > 0:
            threading.Thread(
                target=_watch_gpkg, args=(gpkg_path,), daemon=True
            ).start()
            log.info("GPKG watcher started (poll interval: %ds)", poll_interval)
    else:
        log.warning("LVF_GPKG_PATH not set — starting with empty GIS data")
    yield


app = FastAPI(title="LVF Service", lifespan=_lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ssap_records": len(_ssap),
        "rcl_records": len(_rcl),
        "boundaries": len(_boundaries),
        "civic_coverage_entries": len(_civic_coverage),
    }


@app.get("/coverage/geodetic")
async def geodetic_coverage():
    import json
    from shapely.geometry import mapping
    return {
        urn: json.loads(json.dumps(mapping(geom)))
        for urn, geom in _geodetic_coverage.items()
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
        for e in sorted(_civic_coverage, key=entry_sort_key)
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
    """
    Diagnostic: return RCL segment IDs whose 0.0001° perpendicular offset test
    point lands inside the named service boundary AND whose side-specific admin
    attributes match the submitted parameters.  a3/a4/a5 are wildcards when
    omitted or set to "*".
    """
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
        b for b in _boundaries
        if b.display_name is not None and b.display_name.lower() == bnd_lower
    ]

    nguids: list = []
    seen: set = set()

    for i, record in enumerate(_rcl):
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


@app.post("/validate")
async def validate(request: Request) -> Response:
    """
    Accept a LoST findService request (RFC 5222) as XML and return a
    findServiceResponse or <errors> element.

    Gate 0 → Gate 1 → Gate 2 → response assembly.
    Service boundaries passed to response assembly are pre-filtered to those
    matching the requested URN, since Gate 0 already confirmed at least one
    exists.
    """
    body = await request.body()

    with _reloading_lock:
        if _reloading:
            return _to_xml_response(
                LocationValidationUnavailableResponse(
                    message="LVF is busy loading newer GIS data — service will resume automatically"
                ),
                status=200,
            )

    schema_error = _validate_schema(body)
    if schema_error is not None:
        return _to_xml_response(BadRequestResponse(message=schema_error), status=200)

    try:
        req = _parse_request(body)
    except ValueError as exc:
        return _to_xml_response(BadRequestResponse(message=str(exc)), status=200)

    if req.validate_location != "true":
        return _to_xml_response(ForbiddenResponse(), status=200)

    # Resolve alias URN to the effective provisioned URN
    effective_urn, is_alias = _resolve_service_urn(req.service_urn)

    # Single datetime.now(UTC) captured once per request
    now = datetime.datetime.now(datetime.timezone.utc)

    # Gate 0 — service URN / boundary check
    g0 = gate0.check(effective_urn, _boundaries, now)
    if g0 is not None:
        return _to_xml_response(g0, status=200)

    # Gate 1 — structural conformance
    g1 = gate1.check(req.civic_address)
    if g1 is not None:
        return _to_xml_response(g1, status=200)

    # Gate 2 — progressive filter
    # Pre-filter boundaries to the effective URN for response assembly
    matched_boundaries = [
        b for b in _boundaries
        if b.service_urn.lower() == effective_urn.lower()
        and _is_temporally_active(b.effective, b.expires, now)
    ]
    ral = _parse_return_additional_location(body)
    g2 = gate2.run(req.civic_address, _ssap, _rcl, now)

    ooc = _check_ooc_admin(req.civic_address, g2)
    if ooc is not None:
        return _to_xml_response(ooc, status=200)

    final = response_assembly.assemble(
        g2,
        matched_boundaries,
        service_urn=req.service_urn,
        address=req.civic_address,
        civic_coverage_lookup=lookup_civic_coverage,
        default_mapping_factory=_build_default_mapping,
        return_additional_location=ral,
        server_uri=_server_uri,
        display_name_lang=_display_name_lang,
    )
    # Alias URN: override service_urn in all mapping elements to echo the requested URN
    if is_alias and final.type == "locationValidation":
        for m in final.mapping:
            m.service_urn = req.service_urn
    return _to_xml_response(final, status=200)
