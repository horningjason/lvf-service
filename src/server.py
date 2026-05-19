"""
FastAPI server — LVF service entry point.

Accepts LoST findService requests as XML (RFC 5222), runs the three-gate
algorithm, and returns RFC 5222 compliant XML.

Environment variables:
    LVF_GPKG_PATH       Path to the GeoPackage file (optional — absent means routing-only mode)
    LVF_SSAP_LAYER      GeoPackage layer name for SSAP (default: SiteStructureAddressPoint)
    LVF_RCL_LAYER       GeoPackage layer name for RCL  (default: RoadCenterLine)
    LVF_BOUNDARY_LAYERS Comma-separated GeoPackage layer names for service boundaries
                        (default: PsapPolygon).
    LVF_SYNC_CHILDREN   Comma-separated child LVF /sync endpoint URLs to pull from on startup
    LVF_SYNC_SOURCE_ID_CIVIC     Stable UUID for this node's civic coverage region mapping push
    LVF_SYNC_SOURCE_ID_GEODETIC  Stable UUID for this node's geodetic coverage region mapping push
    LVF_ROOT_AMS        When 'true', suppresses programmatic push to LVF_PARENT_URI and activates
                        provisioned file push to LVF_FOREST_GUIDE_URI instead.
                        Requires ams_civic_coverage.json and ams_geodetic_coverage.json in the
                        GPKG directory (or working directory if no GPKG).
    LVF_FOREST_GUIDE_URI  Full /sync URL of the Forest Guide (e.g. http://host:8002/sync).
                        Only used when LVF_ROOT_AMS=true.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv()

# Apply LVF_LOG_LEVEL immediately after dotenv so it covers both uvicorn and
# test-harness usage. Scoped to the 'src' package to avoid overriding uvicorn's
# own log config. Defaults to INFO; invalid values fall back to INFO with a warning.
_log_level_name = os.environ.get("LVF_LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, None)
if not isinstance(_log_level, int):
    logging.warning("LVF_LOG_LEVEL=%r is not a valid level — defaulting to INFO", _log_level_name)
    _log_level = logging.INFO
logging.getLogger("src").setLevel(_log_level)

import datetime
import geopandas as gpd
import pandas as pd
from shapely.ops import transform, unary_union
from fastapi import FastAPI, Request, Response
from lxml import etree
import httpx

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
    ServiceNotImplementedResponse,
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
_NS_SYNC    = "urn:ietf:params:xml:ns:lostsync1"
_NS_GML     = "http://www.opengis.net/gml"

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

_routing_only: bool = False          # True when no GeoPackage is loaded
_forest_guide_mode: bool = os.environ.get("LVF_FOREST_GUIDE_MODE", "").lower() == "true"
_gis_last_loaded: Optional[datetime.datetime] = None  # UTC time of last successful GIS load

# Root AMS mode — operator-declared coverage pushed to Forest Guide instead of
# programmatically derived coverage pushed to parent LVF.
_root_ams:          bool = os.environ.get("LVF_ROOT_AMS", "").lower() == "true"
_forest_guide_uri:  str  = os.environ.get("LVF_FOREST_GUIDE_URI", "")
_root_ams_active:   bool = False   # True only when all activation conditions are met

_server_uri:        str = os.environ.get("LVF_SERVER_URI",         "lostserver.example.com")
_display_name_lang: str = os.environ.get("LVF_DISPLAY_NAME_LANG",  "en")
_parent_uri:        str = os.environ.get("LVF_PARENT_URI",          "")

_sync_children: list[str] = [
    url.strip()
    for url in os.environ.get("LVF_SYNC_CHILDREN", "").split(",")
    if url.strip()
]

# PIDF-LO names of the six admin-level elements subject to the OOC redirect rule
_ADMIN_PIDF_LO: frozenset[str] = frozenset({
    "ca:country", "ca:A1", "ca:A2", "ca:A3", "ca:A4", "ca:A5",
})

# LVF_DEFAULT_MAPPING_SOURCE_ID validation is deferred to _lifespan / initialize()
# so that routing-only nodes (no GPKG) are not required to set this env var.
_default_mapping_source_id: str = os.environ.get("LVF_DEFAULT_MAPPING_SOURCE_ID", "")

_SERVER_START_TIME: str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# XML schema (loaded once at startup; None disables validation rather than refusing all requests)
_schema: Optional[etree.XMLSchema] = None

# Running asyncio event loop captured at startup — used by the GPKG watcher thread
# to schedule coroutines (e.g. re-push after hot reload).
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# LoST-Sync child coverage store — in-memory; persisted to lvf_child_coverage.json
_child_coverage: list[dict] = []


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
    global _ssap, _rcl, _boundaries, _geodetic_coverage, _civic_coverage, _gis_last_loaded

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
                _gis_last_loaded   = datetime.datetime.now(datetime.timezone.utc)
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
    _gis_last_loaded = datetime.datetime.now(datetime.timezone.utc)

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
                if _root_ams:
                    _load_ams_provisioning()  # re-validate provisioned files from disk
                _maybe_schedule_repush()
            except Exception:
                log.error(
                    "GIS data reload failed — service remains unavailable", exc_info=True
                )


def initialize(gpkg_path: str | None = None) -> None:
    """Load GIS data for use by handle_find_service(). Call once before the first request."""
    global _schema, _routing_only
    _schema = _load_schema()
    if _schema is None:
        log.warning("Operating without XML schema validation")

    if _forest_guide_mode:
        log.info(
            "Forest Guide mode active (LVF_FOREST_GUIDE_MODE=true): "
            "this node routes requests via redirect or notFound — no GIS validation"
        )
        if gpkg_path or os.environ.get("LVF_GPKG_PATH"):
            log.warning(
                "LVF_GPKG_PATH is set but ignored in Forest Guide mode — no GIS data will be loaded"
            )
        if os.environ.get("LVF_PARENT_URI"):
            log.warning(
                "LVF_PARENT_URI is set but ignored in Forest Guide mode — "
                "Forest Guides have no parent (RFC 5582 §8)"
            )
        _routing_only = True
        return

    path = gpkg_path or os.environ.get("LVF_GPKG_PATH")
    if path:
        if not _default_mapping_source_id:
            raise RuntimeError(
                "LVF_DEFAULT_MAPPING_SOURCE_ID is required but not set. "
                "Recommended value: {00000000-0000-0000-0000-000000000000}"
            )
        _load_gis_data(path)
        _routing_only = False
    else:
        _routing_only = True
        log.info("Routing-only mode active: no GeoPackage path provided")

    if _root_ams:
        _load_ams_provisioning()
    elif os.path.exists(os.path.join(_ams_provisioning_dir(), "ams_civic_coverage.json")):
        log.debug("AMS provisioning files found but LVF_ROOT_AMS is not set — no behavior change")


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
    - If inside: record the exact admin tuple from that side of the record

    Deduplicates on the full 8-field key. No wildcard collapsing.
    """
    global _civic_coverage

    now = datetime.datetime.now(datetime.timezone.utc)
    active_rcl = [r for r in _rcl if _is_temporally_active(r.effective, r.expire, now)]
    active_boundaries = [b for b in _boundaries if _is_temporally_active(b.effective, b.expires, now)]

    dedup: dict = {}

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

            country = (getattr(record, f"country{suffix}") or "").strip() or None
            a1      = (getattr(record, f"a1{suffix}") or "").strip() or None
            a2      = (getattr(record, f"a2{suffix}") or "").strip() or None
            if not all([country, a1, a2]):
                continue
            a3 = (getattr(record, f"a3{suffix}") or "").strip() or None
            a4 = (getattr(record, f"a4{suffix}") or "").strip() or None
            a5 = (getattr(record, f"a5{suffix}") or "").strip() or None

            key = (country, a1, a2, a3, a4, a5, containing.display_name, containing.service_urn)
            if key not in dedup:
                dedup[key] = CivicCoverageEntry(
                    country=country, a1=a1, a2=a2, a3=a3, a4=a4, a5=a5, boundary=containing,
                )

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
        if norm(entry.country) != c or norm(entry.a1) != s or norm(entry.a2) != co:
            continue
        if entry.a3 is not None and norm(entry.a3) != a3n:
            continue
        if entry.a4 is not None and norm(entry.a4) != a4n:
            continue
        if entry.a5 is not None and norm(entry.a5) != a5n:
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

def _parse_request(body: bytes) -> tuple[ValidationRequest, Optional[datetime.datetime]]:
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

    as_of: Optional[datetime.datetime] = None
    as_of_el = root.find(f"{{{_NS_PLANNED}}}asOf")
    if as_of_el is not None and as_of_el.text:
        try:
            text = as_of_el.text.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            ts = datetime.datetime.fromisoformat(text)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            as_of = ts
        except ValueError:
            pass  # Unparseable timestamp — treat as absent

    validate_location = root.get("validateLocation", "false")

    return ValidationRequest(
        service_urn=service_urn,
        civic_address=CivicAddress(**fields),
        validate_location=validate_location,
    ), as_of


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


def _parse_recursive(body: bytes) -> bool:
    """Return True iff the <findService> recursive attribute is 'true' (RFC 5222 §10)."""
    try:
        root = etree.fromstring(body)
        return root.get("recursive", "false").lower() == "true"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# XML serialization
# ---------------------------------------------------------------------------

def _mapping_element(parent: etree._Element, mapping, force_no_cache: bool = False) -> None:
    """Append a <mapping> child element to parent (RFC 5222 §8.4.1)."""
    m = etree.SubElement(parent, f"{{{_NS_LOST}}}mapping")

    # Required attributes — fall back to spec-defined defaults when GIS data absent
    m.set("expires",     "NO-CACHE" if force_no_cache else (mapping.expires or "NO-EXPIRATION"))
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


def _serialize_find_service_response(
    resp,
    as_of_used: Optional[datetime.datetime] = None,
) -> etree._Element:
    """Build a <findServiceResponse> element for locationValidation outcomes."""
    root = etree.Element(
        f"{{{_NS_LOST}}}findServiceResponse",
        nsmap=_RESPONSE_NSMAP,
    )
    for mapping in resp.mapping:
        _mapping_element(root, mapping, force_no_cache=(as_of_used is not None))

    # Per draft-ietf-ecrit-lost-planned-changes: echo <asOf> on future-asOf responses
    if as_of_used is not None:
        as_of_el = etree.SubElement(
            root,
            f"{{{_NS_PLANNED}}}asOf",
            nsmap={"planned": _NS_PLANNED},
        )
        as_of_el.text = as_of_used.strftime("%Y-%m-%dT%H:%M:%SZ")

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

    # Per draft-ietf-ecrit-lost-planned-changes: suppress revalidateAfter on asOf responses
    if as_of_used is None:
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


def _to_xml_response(
    resp,
    status: int,
    as_of_used: Optional[datetime.datetime] = None,
) -> Response:
    if resp.type == "locationValidation":
        tree = _serialize_find_service_response(resp, as_of_used=as_of_used)
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
# Recursion helpers (RFC 5222 §10 recursive mode)
# ---------------------------------------------------------------------------

def _has_loop(body: bytes) -> bool:
    """Return True if _server_uri already appears as a <via> source in the request <path>."""
    try:
        root = etree.fromstring(body)
        path_el = root.find(f"{{{_NS_LOST}}}path")
        if path_el is None:
            return False
        return any(
            via.get("source") == _server_uri
            for via in path_el.findall(f"{{{_NS_LOST}}}via")
        )
    except Exception:
        return False


def _add_via_to_request(body: bytes) -> bytes:
    """Add <via source=_server_uri> to the <path> in the outbound request, creating <path> if absent."""
    try:
        root = etree.fromstring(body)
        path_el = root.find(f"{{{_NS_LOST}}}path")
        if path_el is None:
            path_el = etree.SubElement(root, f"{{{_NS_LOST}}}path")
        via_el = etree.SubElement(path_el, f"{{{_NS_LOST}}}via")
        via_el.set("source", _server_uri)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    except Exception:
        return body


def _prepend_via_to_response(response_body: bytes) -> bytes:
    """Prepend <via source=_server_uri> to the front of the upstream response <path>."""
    try:
        root = etree.fromstring(response_body)
        path_el = root.find(f".//{{{_NS_LOST}}}path")
        if path_el is not None:
            via_el = etree.Element(f"{{{_NS_LOST}}}via")
            via_el.set("source", _server_uri)
            via_el.tail = path_el.text  # preserve indentation before the existing <via>
            path_el.insert(0, via_el)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    except Exception:
        return response_body


def _make_errors_xml(error_type: str, message: str = "") -> bytes:
    """Build a <errors> response containing loop, serverTimeout, or serverError."""
    root = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
    root.set("source", _server_uri)
    err = etree.SubElement(root, f"{{{_NS_LOST}}}{error_type}")
    err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    if message:
        err.text = message
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _do_recurse_sync(request_body: bytes) -> bytes:
    """Synchronous recursive LoST lookup to parent LVF. Returns raw XML response bytes."""
    return _do_recurse_to_uri_sync(request_body, _parent_uri.rstrip("/") + "/validate")


async def _do_recurse_async(request_body: bytes) -> bytes:
    """Asynchronous recursive LoST lookup to parent LVF. Returns raw XML response bytes."""
    return await _do_recurse_to_uri_async(request_body, _parent_uri.rstrip("/") + "/validate")


def _do_recurse_to_uri_sync(request_body: bytes, validate_uri: str) -> bytes:
    """Synchronous recursive LoST lookup to an arbitrary validate endpoint."""
    if _has_loop(request_body):
        return _make_errors_xml("loop", "Request loop detected — this server has already processed this request")
    modified = _add_via_to_request(request_body)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                validate_uri,
                content=modified,
                headers={"Content-Type": "application/xml"},
            )
        if resp.status_code == 200:
            try:
                result = _prepend_via_to_response(resp.content)
                etree.fromstring(result)  # verify response is parseable XML
                return result
            except Exception:
                return _make_errors_xml("serverError", "Target LVF returned unparseable XML")
        return _make_errors_xml("serverError", f"Target LVF returned HTTP {resp.status_code}")
    except httpx.TimeoutException:
        return _make_errors_xml("serverTimeout", "Request to target LVF timed out")
    except Exception as exc:
        return _make_errors_xml("serverError", f"Could not reach target LVF: {exc}")


async def _do_recurse_to_uri_async(request_body: bytes, validate_uri: str) -> bytes:
    """Asynchronous recursive LoST lookup to an arbitrary validate endpoint."""
    if _has_loop(request_body):
        return _make_errors_xml("loop", "Request loop detected — this server has already processed this request")
    modified = _add_via_to_request(request_body)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                validate_uri,
                content=modified,
                headers={"Content-Type": "application/xml"},
            )
        if resp.status_code == 200:
            try:
                result = _prepend_via_to_response(resp.content)
                etree.fromstring(result)  # verify response is parseable XML
                return result
            except Exception:
                return _make_errors_xml("serverError", "Target LVF returned unparseable XML")
        return _make_errors_xml("serverError", f"Target LVF returned HTTP {resp.status_code}")
    except httpx.TimeoutException:
        return _make_errors_xml("serverTimeout", "Request to target LVF timed out")
    except Exception as exc:
        return _make_errors_xml("serverError", f"Could not reach target LVF: {exc}")


# ---------------------------------------------------------------------------
# LoST-Sync (RFC 6739) — child coverage store
# ---------------------------------------------------------------------------

def _child_coverage_path() -> str:
    """Return the filesystem path for the child coverage JSON store."""
    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    if gpkg_path:
        return os.path.join(os.path.dirname(gpkg_path) or ".", "lvf_child_coverage.json")
    return os.path.join("data", "lvf_child_coverage.json")


def _load_child_coverage() -> None:
    """Load the child coverage store from JSON on startup."""
    global _child_coverage
    path = _child_coverage_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _child_coverage = data if isinstance(data, list) else []
        log.info("LoST-Sync: loaded %d child coverage entries from %s", len(_child_coverage), path)
    except Exception as exc:
        log.warning("LoST-Sync: could not load child coverage store from %s: %s", path, exc)


def _save_child_coverage() -> None:
    """Persist the child coverage store to JSON atomically."""
    path = _child_coverage_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_child_coverage, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("LoST-Sync: could not save child coverage store to %s: %s", path, exc)


def _parse_iso_timestamp(ts: str) -> Optional[datetime.datetime]:
    """Parse an ISO 8601 timestamp string to a datetime, or None on failure."""
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _compare_timestamps(a: str, b: str) -> int:
    """Compare two ISO 8601 timestamp strings. Returns >0 if a is newer, <0 if older, 0 if equal."""
    ta = _parse_iso_timestamp(a)
    tb = _parse_iso_timestamp(b)
    if ta is None and tb is None:
        return 0
    if ta is None:
        return -1
    if tb is None:
        return 1
    if ta > tb:
        return 1
    if ta < tb:
        return -1
    return 0


def _upsert_child_coverage(parsed: dict) -> bool:
    """
    Apply RFC 6739 §5.2 update rules: update if newer, add if new.
    Returns True if the store was actually modified (new entry or updated entry),
    False if the incoming data was stale and ignored.
    """
    source    = parsed.get("source", "")
    source_id = parsed.get("source_id", "")
    new_lu    = parsed.get("last_updated", "")

    for i, entry in enumerate(_child_coverage):
        if entry.get("source") == source and entry.get("source_id") == source_id:
            if _compare_timestamps(new_lu, entry.get("last_updated", "")) > 0:
                _child_coverage[i] = parsed
                log.info(
                    "LoST-Sync: updated child coverage entry source=%s sourceId=%s",
                    source, source_id,
                )
                return True
            else:
                log.info(
                    "LoST-Sync: received stale coverage for source=%s sourceId=%s — ignoring",
                    source, source_id,
                )
                return False

    _child_coverage.append(parsed)
    log.info(
        "LoST-Sync: added new child coverage entry source=%s sourceId=%s profile=%s",
        source, source_id, parsed.get("profile", ""),
    )
    return True


def _lookup_child_coverage(
    country: Optional[str],
    a1: Optional[str],
    a2: Optional[str],
    a3: Optional[str] = None,
    a4: Optional[str] = None,
    a5: Optional[str] = None,
) -> Optional[dict]:
    """
    Longest-prefix match against the child coverage store (civic profile entries only).
    Returns the matching entry dict with the highest specificity, or None if no match.
    Geodetic entries are skipped — geodetic-vs-civic matching is not defined.
    """
    def norm(v: Optional[str]) -> Optional[str]:
        return v.upper() if v else None

    c, s, co = norm(country), norm(a1), norm(a2)
    if not c or not s or not co:
        return None
    a3n, a4n, a5n = norm(a3), norm(a4), norm(a5)

    best_entry: Optional[dict] = None
    best_specificity = -1

    for entry in _child_coverage:
        if entry.get("profile") != "civic":
            continue
        tuples = entry.get("civic_tuples") or []

        entry_best = -1
        for t in tuples:
            tc  = norm(t.get("country"))
            ts  = norm(t.get("a1"))
            tco = norm(t.get("a2"))
            ta3 = norm(t.get("a3"))
            ta4 = norm(t.get("a4"))
            ta5 = norm(t.get("a5"))

            if tc != c or ts != s or tco != co:
                continue
            if ta3 is not None and ta3 != "*" and ta3 != a3n:
                continue
            if ta4 is not None and ta4 != "*" and ta4 != a4n:
                continue
            if ta5 is not None and ta5 != "*" and ta5 != a5n:
                continue

            spec = (
                (1 if (ta3 is not None and ta3 != "*") else 0) +
                (1 if (ta4 is not None and ta4 != "*") else 0) +
                (1 if (ta5 is not None and ta5 != "*") else 0)
            )
            if spec > entry_best:
                entry_best = spec

        if entry_best > best_specificity:
            best_specificity = entry_best
            best_entry = entry

    return best_entry


# ---------------------------------------------------------------------------
# LoST-Sync — GML serialization helpers
# ---------------------------------------------------------------------------

def _gml_add_ring(ring_el: etree._Element, coords) -> None:
    """Add <gml:pos> children to a LinearRing element. Swaps (lon, lat) → (lat, lon)."""
    for lon, lat in coords:
        pos = etree.SubElement(ring_el, f"{{{_NS_GML}}}pos")
        pos.text = f"{lat} {lon}"


def _gml_polygon(polygon) -> etree._Element:
    """Convert a shapely Polygon to a <gml:Polygon> element."""
    poly_el = etree.Element(
        f"{{{_NS_GML}}}Polygon",
        nsmap={"gml": _NS_GML},
    )
    poly_el.set("srsName", "urn:ogc:def::crs:EPSG::4326")
    ext_el  = etree.SubElement(poly_el, f"{{{_NS_GML}}}exterior")
    ring_el = etree.SubElement(ext_el,  f"{{{_NS_GML}}}LinearRing")
    _gml_add_ring(ring_el, polygon.exterior.coords)
    for interior in polygon.interiors:
        int_el  = etree.SubElement(poly_el, f"{{{_NS_GML}}}interior")
        iring   = etree.SubElement(int_el,  f"{{{_NS_GML}}}LinearRing")
        _gml_add_ring(iring, interior.coords)
    return poly_el


def _shapely_to_gml(geom) -> etree._Element:
    """Convert a shapely Polygon or MultiPolygon to a GML element."""
    if geom.geom_type == "MultiPolygon":
        mp = etree.Element(
            f"{{{_NS_GML}}}MultiPolygon",
            nsmap={"gml": _NS_GML},
        )
        mp.set("srsName", "urn:ogc:def::crs:EPSG::4326")
        for polygon in geom.geoms:
            pm = etree.SubElement(mp, f"{{{_NS_GML}}}polygonMember")
            pm.append(_gml_polygon(polygon))
        return mp
    return _gml_polygon(geom)


# ---------------------------------------------------------------------------
# LoST-Sync — coverage region mapping builders
# ---------------------------------------------------------------------------

def _gis_last_updated_str() -> str:
    return _gis_last_loaded.strftime("%Y-%m-%dT%H:%M:%SZ") if _gis_last_loaded else _SERVER_START_TIME


def _build_civic_coverage_mapping_xml() -> Optional[str]:
    """
    Build a <mapping> XML string for this node's civic coverage region.
    Returns None if LVF_SYNC_SOURCE_ID_CIVIC is unset or there is no civic coverage data.
    """
    src_id = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    if not src_id or not _civic_coverage:
        return None

    mapping_el = etree.Element(f"{{{_NS_LOST}}}mapping")
    mapping_el.set("expires",     "NO-EXPIRATION")
    mapping_el.set("lastUpdated", _gis_last_updated_str())
    mapping_el.set("source",      _server_uri)
    mapping_el.set("sourceId",    src_id)

    dn = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}displayName")
    dn.set("{http://www.w3.org/XML/1998/namespace}lang", _display_name_lang)
    dn.text = f"{_server_uri} civic coverage"

    svc = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}service")
    svc.text = "urn:service:sos"

    sb = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}serviceBoundary")
    sb.set("profile", "civic")

    seen: set = set()
    for entry in _civic_coverage:
        key = (entry.country, entry.a1, entry.a2, entry.a3, entry.a4, entry.a5)
        if key in seen:
            continue
        seen.add(key)
        ca = etree.SubElement(sb, f"{{{_NS_CA}}}civicAddress")
        _e = etree.SubElement(ca, f"{{{_NS_CA}}}country")
        _e.text = entry.country
        _e = etree.SubElement(ca, f"{{{_NS_CA}}}A1")
        _e.text = entry.a1
        _e = etree.SubElement(ca, f"{{{_NS_CA}}}A2")
        _e.text = entry.a2
        for field, val in (("A3", entry.a3), ("A4", entry.a4), ("A5", entry.a5)):
            if val is not None and val != "*":
                _e = etree.SubElement(ca, f"{{{_NS_CA}}}{field}")
                _e.text = val

    etree.SubElement(mapping_el, f"{{{_NS_LOST}}}uri")  # empty per RFC 6739 Figure 2

    return etree.tostring(mapping_el, encoding="unicode")


def _build_geodetic_coverage_mapping_xml() -> Optional[str]:
    """
    Build a <mapping> XML string for this node's geodetic coverage region.
    Returns None if LVF_SYNC_SOURCE_ID_GEODETIC is unset or there is no geodetic coverage.
    """
    src_id = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
    if not src_id or not _geodetic_coverage:
        return None

    geom = _geodetic_coverage.get("urn:service:sos")
    if geom is None:
        geom = next(iter(_geodetic_coverage.values()))

    mapping_el = etree.Element(f"{{{_NS_LOST}}}mapping")
    mapping_el.set("expires",     "NO-EXPIRATION")
    mapping_el.set("lastUpdated", _gis_last_updated_str())
    mapping_el.set("source",      _server_uri)
    mapping_el.set("sourceId",    src_id)

    dn = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}displayName")
    dn.set("{http://www.w3.org/XML/1998/namespace}lang", _display_name_lang)
    dn.text = f"{_server_uri} geodetic coverage"

    svc = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}service")
    svc.text = "urn:service:sos"

    sb = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}serviceBoundary")
    sb.set("profile", "geodetic-2d")

    try:
        gml_el = _shapely_to_gml(geom)
        sb.append(gml_el)
    except Exception as exc:
        log.warning("LoST-Sync: could not serialize geodetic coverage to GML: %s", exc)
        return None

    etree.SubElement(mapping_el, f"{{{_NS_LOST}}}uri")  # empty per RFC 6739 Figure 2

    return etree.tostring(mapping_el, encoding="unicode")


# ---------------------------------------------------------------------------
# Root AMS mode — provisioned coverage region for Forest Guide push
# ---------------------------------------------------------------------------


def _ams_provisioning_dir() -> str:
    """Return the directory where AMS provisioning files live (same dir as GPKG, or '.')."""
    gpkg_path = os.environ.get("LVF_GPKG_PATH", "")
    d = os.path.dirname(gpkg_path) if gpkg_path else ""
    return d or "."


def geojson_to_gml(geojson_feature: dict) -> etree._Element:
    """
    Convert a GeoJSON Polygon or MultiPolygon (Feature or bare geometry) to a
    <gml:Polygon> element suitable for embedding in <serviceBoundary profile="geodetic-2d">.

    For MultiPolygon input, the largest polygon by area is selected and only its
    exterior ring is used.  GeoJSON coordinates are [lon, lat]; gml:pos is "lat lon"
    — the swap is performed by the existing _gml_add_ring helper.
    """
    from shapely.geometry import shape as _shape, Polygon as _Polygon

    geom_dict = geojson_feature.get("geometry", geojson_feature) if geojson_feature.get("type") == "Feature" else geojson_feature
    geom = _shape(geom_dict)
    if geom.geom_type == "MultiPolygon":
        largest = max(geom.geoms, key=lambda p: p.area)
        geom = _Polygon(largest.exterior)
    return _gml_polygon(geom)


def _validate_ams_civic_file(path: str) -> Optional[list]:
    """Load and validate ams_civic_coverage.json. Returns a list of entry dicts or None on error."""
    if not os.path.exists(path):
        log.warning("AMS: ams_civic_coverage.json not found at %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("AMS: ams_civic_coverage.json is not valid JSON: %s", exc)
        return None
    if not isinstance(data, list) or not data:
        log.error("AMS: ams_civic_coverage.json must be a non-empty JSON array")
        return None

    _ENTRY_REQUIRED = {"source", "source_id", "last_updated", "expires", "service", "profile", "child_uri"}
    _TUPLE_REQUIRED = {"country", "a1", "a2", "lost_server"}
    _TUPLE_OPTIONAL = {"a3", "a4", "a5"}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            log.error("AMS: ams_civic_coverage.json entry %d is not an object", i)
            return None
        missing = _ENTRY_REQUIRED - entry.keys()
        if missing:
            log.error("AMS: ams_civic_coverage.json entry %d missing required key(s): %s", i, missing)
            return None
        if entry.get("profile") != "civic":
            log.error('AMS: ams_civic_coverage.json entry %d has profile %r — expected "civic"', i, entry.get("profile"))
            return None
        tuples = entry.get("civic_tuples")
        if not isinstance(tuples, list) or not tuples:
            log.error("AMS: ams_civic_coverage.json entry %d must have a non-empty civic_tuples array", i)
            return None
        for j, t in enumerate(tuples):
            if not isinstance(t, dict):
                log.error("AMS: ams_civic_coverage.json entry %d civic_tuples[%d] is not an object", i, j)
                return None
            tmissing = _TUPLE_REQUIRED - t.keys()
            if tmissing:
                log.error("AMS: ams_civic_coverage.json entry %d civic_tuples[%d] missing required key(s): %s", i, j, tmissing)
                return None
    return data


def _validate_ams_geodetic_file(path: str) -> Optional[list]:
    """Load and validate ams_geodetic_coverage.json. Returns a list of entry dicts or None on error."""
    from shapely.wkt import loads as _wkt_loads

    if not os.path.exists(path):
        log.warning("AMS: ams_geodetic_coverage.json not found at %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.error("AMS: ams_geodetic_coverage.json is not valid JSON: %s", exc)
        return None
    if not isinstance(data, list) or not data:
        log.error("AMS: ams_geodetic_coverage.json must be a non-empty JSON array")
        return None

    _ENTRY_REQUIRED = {"source", "source_id", "last_updated", "expires", "service", "profile", "child_uri"}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            log.error("AMS: ams_geodetic_coverage.json entry %d is not an object", i)
            return None
        missing = _ENTRY_REQUIRED - entry.keys()
        if missing:
            log.error("AMS: ams_geodetic_coverage.json entry %d missing required key(s): %s", i, missing)
            return None
        if entry.get("profile") != "geodetic-2d":
            log.error('AMS: ams_geodetic_coverage.json entry %d has profile %r — expected "geodetic-2d"', i, entry.get("profile"))
            return None
        wkt = entry.get("geodetic_geom_wkt")
        if not wkt or not isinstance(wkt, str):
            log.error("AMS: ams_geodetic_coverage.json entry %d missing or invalid geodetic_geom_wkt", i)
            return None
        try:
            _wkt_loads(wkt)
        except Exception as exc:
            log.error("AMS: ams_geodetic_coverage.json entry %d geodetic_geom_wkt is not valid WKT: %s", i, exc)
            return None
    return data


def _load_ams_provisioning() -> bool:
    """
    Load and validate AMS provisioning files; inject entries into the child coverage store.
    Returns True if all conditions are met and root AMS mode is fully activated.

    LVF spec §3.9.3 (civic provisioning file), §3.9.4 (geodetic provisioning file).
    """
    global _root_ams_active

    if not _forest_guide_uri:
        log.warning(
            "LVF_ROOT_AMS=true but LVF_FOREST_GUIDE_URI is not set — "
            "FG push suppressed; programmatic push to LVF_PARENT_URI is also suppressed"
        )
        _root_ams_active = False
        return False

    base_dir = _ams_provisioning_dir()
    civic_path    = os.path.join(base_dir, "ams_civic_coverage.json")
    geodetic_path = os.path.join(base_dir, "ams_geodetic_coverage.json")

    civic_entries    = _validate_ams_civic_file(civic_path)
    if civic_entries is None:
        _root_ams_active = False
        return False

    geodetic_entries = _validate_ams_geodetic_file(geodetic_path)
    if geodetic_entries is None:
        _root_ams_active = False
        return False

    for entry in civic_entries + geodetic_entries:
        _upsert_child_coverage(entry)

    _root_ams_active = True
    civic_tuple_count = sum(len(e.get("civic_tuples") or []) for e in civic_entries)
    log.info(
        "AMS: provisioning files loaded (%d civic entry/entries, %d civic tuple(s), "
        "%d geodetic entry/entries) — FG push active targeting %s",
        len(civic_entries), civic_tuple_count, len(geodetic_entries), _forest_guide_uri,
    )
    return True


# ---------------------------------------------------------------------------
# LoST-Sync — GML → shapely helpers (inverse of _gml_polygon / _shapely_to_gml)
# ---------------------------------------------------------------------------

def _gml_ring_coords(ring_el: etree._Element) -> list[tuple[float, float]]:
    """Extract (lon, lat) coordinate pairs from a <gml:LinearRing> element."""
    coords: list[tuple[float, float]] = []
    for pos in ring_el.findall(f"{{{_NS_GML}}}pos"):
        parts = (pos.text or "").split()
        if len(parts) >= 2:
            lat, lon = float(parts[0]), float(parts[1])
            coords.append((lon, lat))
    return coords


def _gml_polygon_to_shapely(poly_el: etree._Element):
    """Convert a <gml:Polygon> element to a shapely Polygon."""
    from shapely.geometry import Polygon as _Polygon
    ext_ring = poly_el.find(f"{{{_NS_GML}}}exterior/{{{_NS_GML}}}LinearRing")
    exterior = _gml_ring_coords(ext_ring) if ext_ring is not None else []
    interiors = [
        _gml_ring_coords(ir)
        for int_el in poly_el.findall(f"{{{_NS_GML}}}interior")
        for ir in [int_el.find(f"{{{_NS_GML}}}LinearRing")]
        if ir is not None
    ]
    return _Polygon(exterior, interiors)


def _gml_sb_to_shapely(sb_el: etree._Element):
    """
    Parse the GML geometry inside a <serviceBoundary> element to a shapely geometry.
    Returns a Polygon, MultiPolygon, or None on failure.
    """
    from shapely.geometry import MultiPolygon as _MultiPolygon
    mp_el = sb_el.find(f".//{{{_NS_GML}}}MultiPolygon")
    if mp_el is not None:
        polys = []
        for pm_el in mp_el.findall(f"{{{_NS_GML}}}polygonMember"):
            poly_el = pm_el.find(f"{{{_NS_GML}}}Polygon")
            if poly_el is not None:
                polys.append(_gml_polygon_to_shapely(poly_el))
        return _MultiPolygon(polys)
    poly_el = sb_el.find(f".//{{{_NS_GML}}}Polygon")
    if poly_el is not None:
        return _gml_polygon_to_shapely(poly_el)
    return None


# ---------------------------------------------------------------------------
# LoST-Sync — mapping element parser
# ---------------------------------------------------------------------------

def _parse_sync_mapping(mapping_el: etree._Element, child_uri_hint: str = "") -> dict:
    """
    Parse a LoST <mapping> element received via pushMappings or getMappingsResponse
    into the child coverage store dict format.

    All fields are discrete scalar or structured values — no raw XML is stored,
    so json.dump() always produces valid JSON with no embedded control characters.
    """
    source    = mapping_el.get("source", "")
    source_id = mapping_el.get("sourceId", "")
    last_updated = mapping_el.get("lastUpdated", "")
    expires      = mapping_el.get("expires", "NO-EXPIRATION")

    service_el = mapping_el.find(f"{{{_NS_LOST}}}service")
    service = (service_el.text or "").strip() if service_el is not None else "urn:service:sos"

    dn_el = mapping_el.find(f"{{{_NS_LOST}}}displayName")
    display_name = (dn_el.text or "").strip() if dn_el is not None else ""

    sb_el = mapping_el.find(f"{{{_NS_LOST}}}serviceBoundary")
    profile           = ""
    civic_tuples      = None
    geodetic_geom_wkt = None

    if sb_el is not None:
        profile = sb_el.get("profile", "")

        if profile == "civic":
            civic_tuples = []
            for ca_el in sb_el.findall(f".//{{{_NS_CA}}}civicAddress"):
                t: dict[str, Optional[str]] = {}
                for field, tag in [
                    ("country", "country"), ("a1", "A1"), ("a2", "A2"),
                    ("a3", "A3"),           ("a4", "A4"), ("a5", "A5"),
                ]:
                    el = ca_el.find(f"{{{_NS_CA}}}{tag}")
                    t[field] = el.text.strip() if (el is not None and el.text) else ("*" if field in ("a3", "a4", "a5") else None)
                civic_tuples.append(t)

        elif profile == "geodetic-2d":
            try:
                from shapely.wkt import dumps as _wkt_dumps
                geom = _gml_sb_to_shapely(sb_el)
                geodetic_geom_wkt = _wkt_dumps(geom) if geom is not None else None
            except Exception:
                geodetic_geom_wkt = None

    # Derive child validate URI
    uri_el = mapping_el.find(f"{{{_NS_LOST}}}uri")
    if uri_el is not None and uri_el.text and uri_el.text.strip():
        child_uri = uri_el.text.strip()
    elif child_uri_hint:
        child_uri = child_uri_hint
    else:
        # Derive from source attribute (typically a hostname)
        if source and "://" not in source:
            child_uri = f"http://{source}/validate"
        elif source:
            child_uri = source.rstrip("/") + "/validate"
        else:
            child_uri = ""

    if civic_tuples is not None:
        for t in civic_tuples:
            t["lost_server"] = child_uri

    return {
        "source":           source,
        "source_id":        source_id,
        "last_updated":     last_updated,
        "expires":          expires,
        "service":          service,
        "display_name":     display_name,
        "profile":          profile,
        "civic_tuples":     civic_tuples,
        "geodetic_geom_wkt": geodetic_geom_wkt,
        "child_uri":        child_uri,
    }


def _child_entry_to_mapping_xml(entry: dict) -> Optional[str]:
    """
    Reconstruct a <mapping> XML string from a child coverage store entry.
    Returns None if the entry cannot be serialised (e.g. missing geodetic geometry).
    """
    profile = entry.get("profile", "")

    mapping_el = etree.Element(f"{{{_NS_LOST}}}mapping")
    mapping_el.set("expires",     entry.get("expires", "NO-EXPIRATION"))
    mapping_el.set("lastUpdated", entry.get("last_updated", ""))
    mapping_el.set("source",      entry.get("source", ""))
    mapping_el.set("sourceId",    entry.get("source_id", ""))

    dn = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}displayName")
    dn.set("{http://www.w3.org/XML/1998/namespace}lang", _display_name_lang)
    dn.text = entry.get("display_name") or f"{entry.get('source', '')} {profile} coverage"

    svc = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}service")
    svc.text = entry.get("service", "urn:service:sos")

    sb = etree.SubElement(mapping_el, f"{{{_NS_LOST}}}serviceBoundary")
    sb.set("profile", profile)

    if profile == "civic":
        for t in (entry.get("civic_tuples") or []):
            ca = etree.SubElement(sb, f"{{{_NS_CA}}}civicAddress")
            for field, tag in [
                ("country", "country"), ("a1", "A1"), ("a2", "A2"),
                ("a3", "A3"),           ("a4", "A4"), ("a5", "A5"),
            ]:
                val = t.get(field)
                if val is not None and val != "*":
                    el = etree.SubElement(ca, f"{{{_NS_CA}}}{tag}")
                    el.text = val
    elif profile == "geodetic-2d":
        geom_wkt = entry.get("geodetic_geom_wkt")
        if not geom_wkt:
            return None
        try:
            from shapely.wkt import loads as _wkt_loads
            geom = _wkt_loads(geom_wkt)
            sb.append(_shapely_to_gml(geom))
        except Exception as exc:
            log.warning("LoST-Sync: could not reconstruct geodetic GML for child entry source=%s: %s",
                        entry.get("source", ""), exc)
            return None
    else:
        return None

    etree.SubElement(mapping_el, f"{{{_NS_LOST}}}uri")
    return etree.tostring(mapping_el, encoding="unicode")


# ---------------------------------------------------------------------------
# LoST-Sync — sync endpoint handlers
# ---------------------------------------------------------------------------

def _sync_error_response(error_type: str, message: str) -> Response:
    """Build an HTTP 200 LoST-Sync error response."""
    root = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
    root.set("source", _server_uri)
    err = etree.SubElement(root, f"{{{_NS_LOST}}}{error_type}")
    err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    err.text = message
    body = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, status_code=200, media_type="application/lostsync+xml")


async def _handle_push_mappings(root: etree._Element) -> Response:
    """Handle an incoming <pushMappings> request per RFC 6739 §5.2.

    Cascades coverage changes upstream on modification per LVF spec §3.9.6.
    """
    mapping_els = root.findall(f"{{{_NS_LOST}}}mapping")
    not_deleted: list[tuple[str, str]] = []
    coverage_changed = False
    last_source_id = ""

    for mapping_el in mapping_els:
        sb_el    = mapping_el.find(f"{{{_NS_LOST}}}serviceBoundary")
        is_delete = sb_el is None

        parsed    = _parse_sync_mapping(mapping_el)
        source    = parsed["source"]
        source_id = parsed["source_id"]
        last_source_id = source_id

        if is_delete:
            found = False
            for i, entry in enumerate(_child_coverage):
                if entry.get("source") == source and entry.get("source_id") == source_id:
                    _child_coverage.pop(i)
                    found = True
                    coverage_changed = True
                    log.info(
                        "LoST-Sync: deleted coverage entry source=%s sourceId=%s",
                        source, source_id,
                    )
                    break
            if not found:
                log.warning(
                    "LoST-Sync: delete requested for unknown entry source=%s sourceId=%s",
                    source, source_id,
                )
                not_deleted.append((source, source_id))
        else:
            if _upsert_child_coverage(parsed):
                coverage_changed = True

    _save_child_coverage()

    # Cascade coverage change upstream (fire-and-forget, non-blocking)
    if coverage_changed:
        if _root_ams and _root_ams_active:
            log.info(
                "Coverage propagation triggered by child push from %s, pushing upstream to %s",
                last_source_id, _forest_guide_uri,
            )
            asyncio.create_task(_push_coverage_to_fg())
        elif not _root_ams and _parent_uri and not _forest_guide_mode:
            log.info(
                "Coverage propagation triggered by child push from %s, pushing upstream to %s",
                last_source_id, _get_parent_sync_uri(),
            )
            asyncio.create_task(_push_coverage_to_parent())

    if not_deleted:
        root_err = etree.Element(f"{{{_NS_LOST}}}errors", nsmap={None: _NS_LOST})
        root_err.set("source", _server_uri)
        for src, sid in not_deleted:
            nd = etree.SubElement(root_err, f"{{{_NS_LOST}}}notDeleted")
            nd.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
            nd.text = f"No mapping found for source={src!r} sourceId={sid!r}"
        body = etree.tostring(root_err, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        return Response(content=body, status_code=200, media_type="application/lostsync+xml")

    body = etree.tostring(
        etree.Element(f"{{{_NS_SYNC}}}pushMappingsResponse"),
        xml_declaration=True, encoding="UTF-8",
    )
    return Response(content=body, status_code=200, media_type="application/lostsync+xml")


async def _handle_get_mappings(root: etree._Element) -> Response:
    """Handle an incoming <getMappingsRequest> per RFC 6739."""
    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")

    exists_el = root.find(f"{{{_NS_SYNC}}}exists")
    mapping_xml_list: list[str] = []

    if exists_el is None:
        # Return all mappings this node can provide
        if sync_source_id_civic:
            civic_xml = _build_civic_coverage_mapping_xml()
            if civic_xml:
                mapping_xml_list.append(civic_xml)
        if sync_source_id_geodetic:
            geo_xml = _build_geodetic_coverage_mapping_xml()
            if geo_xml:
                mapping_xml_list.append(geo_xml)
        own_count = len(mapping_xml_list)
        for entry in _child_coverage:
            xml = _child_entry_to_mapping_xml(entry)
            if xml:
                mapping_xml_list.append(xml)
    else:
        # Return only mappings newer than the requester's fingerprints
        fingerprints: dict[str, str] = {}
        for fp in exists_el.findall(f"{{{_NS_SYNC}}}mapping-fingerprint"):
            sid = fp.get("sourceId")
            lu  = fp.get("lastUpdated", "")
            if sid:
                fingerprints[sid] = lu

        my_lu = _gis_last_updated_str()

        if sync_source_id_civic:
            fp_lu = fingerprints.get(sync_source_id_civic)
            if fp_lu is None or _compare_timestamps(my_lu, fp_lu) > 0:
                civic_xml = _build_civic_coverage_mapping_xml()
                if civic_xml:
                    mapping_xml_list.append(civic_xml)

        if sync_source_id_geodetic:
            fp_lu = fingerprints.get(sync_source_id_geodetic)
            if fp_lu is None or _compare_timestamps(my_lu, fp_lu) > 0:
                geo_xml = _build_geodetic_coverage_mapping_xml()
                if geo_xml:
                    mapping_xml_list.append(geo_xml)

        own_count = len(mapping_xml_list)
        for entry in _child_coverage:
            fp_lu = fingerprints.get(entry.get("source_id", ""))
            entry_lu = entry.get("last_updated", "")
            if fp_lu is None or _compare_timestamps(entry_lu, fp_lu) > 0:
                xml = _child_entry_to_mapping_xml(entry)
                if xml:
                    mapping_xml_list.append(xml)

    log.debug(
        "LoST-Sync: getMappingsResponse includes %d mapping(s) (%d own, %d child)",
        len(mapping_xml_list), own_count, len(mapping_xml_list) - own_count,
    )

    resp_root = etree.Element(f"{{{_NS_SYNC}}}getMappingsResponse")
    for xml_str in mapping_xml_list:
        try:
            resp_root.append(etree.fromstring(xml_str))
        except Exception as exc:
            log.warning("LoST-Sync: could not include mapping in getMappingsResponse: %s", exc)

    body = etree.tostring(resp_root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, status_code=200, media_type="application/lostsync+xml")


# ---------------------------------------------------------------------------
# LoST-Sync — outbound push / pull
# ---------------------------------------------------------------------------

def _get_parent_sync_uri() -> str:
    """Derive the parent's /sync endpoint URL from LVF_PARENT_URI."""
    if not _parent_uri:
        return ""
    base = _parent_uri.rstrip("/")
    if base.endswith("/validate"):
        base = base[: -len("/validate")]
    return base + "/sync"


async def _push_coverage_to_parent() -> None:
    """Push this node's civic and geodetic coverage regions to the parent LVF."""
    with _reloading_lock:
        if _reloading:
            log.warning("LoST-Sync: skipping push — GIS reload in progress")
            return

    parent_sync_uri = _get_parent_sync_uri()
    if not parent_sync_uri:
        return

    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")

    for label, mapping_getter, src_id in [
        ("civic",    _build_civic_coverage_mapping_xml,    sync_source_id_civic),
        ("geodetic", _build_geodetic_coverage_mapping_xml, sync_source_id_geodetic),
    ]:
        if not src_id:
            continue

        mapping_xml = mapping_getter()
        if not mapping_xml:
            log.warning("LoST-Sync: could not build %s coverage mapping (no data?)", label)
            continue

        push_root = etree.Element(f"{{{_NS_SYNC}}}pushMappings")
        try:
            push_root.append(etree.fromstring(mapping_xml))
        except Exception as exc:
            log.warning("LoST-Sync: could not parse %s mapping for push: %s", label, exc)
            continue

        push_body = etree.tostring(push_root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        log.info("LoST-Sync: pushing %s coverage to parent %s", label, parent_sync_uri)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    parent_sync_uri,
                    content=push_body,
                    headers={"Content-Type": "application/lostsync+xml"},
                )
            if resp.status_code == 200:
                try:
                    resp_root = etree.fromstring(resp.content)
                    if resp_root.tag == f"{{{_NS_SYNC}}}pushMappingsResponse":
                        log.info(
                            "LoST-Sync: successfully pushed %s coverage to parent %s",
                            label, parent_sync_uri,
                        )
                    else:
                        log.warning(
                            "LoST-Sync: unexpected response pushing %s to parent: %s",
                            label, resp_root.tag,
                        )
                except Exception:
                    log.warning("LoST-Sync: parent returned unparseable XML when pushing %s", label)
            else:
                log.warning(
                    "LoST-Sync: push %s to parent %s returned HTTP %d",
                    label, parent_sync_uri, resp.status_code,
                )
        except Exception as exc:
            log.warning(
                "LoST-Sync: failed to push %s coverage to parent %s: %s",
                label, parent_sync_uri, exc,
            )


async def _push_coverage_to_fg() -> None:
    """Push AMS-provisioned civic and geodetic coverage regions to the Forest Guide.

    LVF spec §3.9.5.
    """
    if not _root_ams_active or not _forest_guide_uri:
        return

    with _reloading_lock:
        if _reloading:
            log.warning("AMS: skipping FG push — GIS reload in progress")
            return

    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")

    for label, profile, src_id in [
        ("civic",    "civic",       sync_source_id_civic),
        ("geodetic", "geodetic-2d", sync_source_id_geodetic),
    ]:
        if not src_id:
            continue

        entry = next(
            (e for e in _child_coverage
             if e.get("source_id") == src_id and e.get("profile") == profile),
            None,
        )
        if not entry:
            log.warning("AMS: could not find %s coverage entry in child store for FG push (no data?)", label)
            continue
        mapping_xml = _child_entry_to_mapping_xml(entry)
        if not mapping_xml:
            log.warning("AMS: could not build %s coverage mapping for FG push (no data?)", label)
            continue

        push_root = etree.Element(f"{{{_NS_SYNC}}}pushMappings")
        try:
            push_root.append(etree.fromstring(mapping_xml))
        except Exception as exc:
            log.warning("AMS: could not parse %s mapping for FG push: %s", label, exc)
            continue

        push_body = etree.tostring(push_root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        log.info("AMS: pushing %s coverage to Forest Guide %s", label, _forest_guide_uri)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _forest_guide_uri,
                    content=push_body,
                    headers={"Content-Type": "application/lostsync+xml"},
                )
            if resp.status_code == 200:
                try:
                    resp_root = etree.fromstring(resp.content)
                    if resp_root.tag == f"{{{_NS_SYNC}}}pushMappingsResponse":
                        log.info("AMS: successfully pushed %s coverage to Forest Guide %s", label, _forest_guide_uri)
                    else:
                        log.warning("AMS: unexpected response pushing %s to Forest Guide: %s", label, resp_root.tag)
                except Exception:
                    log.warning("AMS: Forest Guide returned unparseable XML when pushing %s", label)
            else:
                log.warning("AMS: push %s to Forest Guide %s returned HTTP %d", label, _forest_guide_uri, resp.status_code)
        except Exception as exc:
            log.warning("AMS: failed to push %s coverage to Forest Guide %s: %s", label, _forest_guide_uri, exc)


async def _pull_from_child(child_sync_url: str) -> None:
    """Send a getMappingsRequest to a child LVF /sync endpoint and store received mappings."""
    with _reloading_lock:
        if _reloading:
            log.warning("LoST-Sync: skipping pull from %s — GIS reload in progress", child_sync_url)
            return

    # Derive the child's /validate URL for use as child_uri in stored entries
    base = child_sync_url.rstrip("/")
    child_validate_url = (base[: -len("/sync")] if base.endswith("/sync") else base) + "/validate"

    get_body = etree.tostring(
        etree.Element(f"{{{_NS_SYNC}}}getMappingsRequest"),
        xml_declaration=True, encoding="UTF-8",
    )
    log.info("LoST-Sync: sending getMappingsRequest to %s", child_sync_url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                child_sync_url,
                content=get_body,
                headers={"Content-Type": "application/lostsync+xml"},
            )
        if resp.status_code != 200:
            log.warning(
                "LoST-Sync: getMappingsRequest to %s returned HTTP %d",
                child_sync_url, resp.status_code,
            )
            return

        try:
            resp_root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as exc:
            log.warning(
                "LoST-Sync: getMappingsResponse from %s has malformed XML: %s",
                child_sync_url, exc,
            )
            return

        if resp_root.tag != f"{{{_NS_SYNC}}}getMappingsResponse":
            log.warning(
                "LoST-Sync: unexpected response element from %s: %s",
                child_sync_url, resp_root.tag,
            )
            return

        count = 0
        for mapping_el in resp_root.findall(f"{{{_NS_LOST}}}mapping"):
            parsed = _parse_sync_mapping(mapping_el, child_uri_hint=child_validate_url)
            _upsert_child_coverage(parsed)
            count += 1

        if count > 0:
            _save_child_coverage()
            log.info("LoST-Sync: stored %d mapping(s) received from %s", count, child_sync_url)
        else:
            log.info("LoST-Sync: no mappings received from %s", child_sync_url)

    except Exception as exc:
        log.warning("LoST-Sync: failed to pull from %s: %s", child_sync_url, exc)


async def _startup_sync() -> None:
    """Background task: push coverage to parent/FG and pull from children after startup."""
    await asyncio.sleep(1)  # allow the server to fully start before making outbound calls

    if _root_ams:
        # Root AMS: push provisioned coverage to Forest Guide; never push programmatic to parent
        if _root_ams_active:
            await _push_coverage_to_fg()
    else:
        sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
        sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
        if (sync_source_id_civic or sync_source_id_geodetic) and _parent_uri and not _forest_guide_mode:
            await _push_coverage_to_parent()

    for child_url in _sync_children:
        await _pull_from_child(child_url)


def _maybe_schedule_repush() -> None:
    """
    Schedule re-push of coverage regions after a GIS hot reload.
    Called from the GPKG watcher thread, so uses run_coroutine_threadsafe.
    Root AMS nodes push provisioned coverage to the Forest Guide;
    regular nodes push programmatic coverage to the parent LVF.
    """
    loop = _event_loop
    if loop is None or not loop.is_running():
        log.warning("LoST-Sync: no running event loop — cannot schedule re-push after reload")
        return

    if _root_ams:
        # Root AMS: push provisioned coverage to FG; never push programmatic to parent
        if _root_ams_active:
            try:
                asyncio.run_coroutine_threadsafe(_push_coverage_to_fg(), loop)
                log.info("AMS: scheduled FG re-push after GIS reload")
            except Exception as exc:
                log.warning("AMS: could not schedule FG re-push: %s", exc)
        return

    # Regular mode: push programmatic coverage to parent
    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
    if not (sync_source_id_civic or sync_source_id_geodetic) or not _parent_uri or _forest_guide_mode:
        return
    try:
        asyncio.run_coroutine_threadsafe(_push_coverage_to_parent(), loop)
        log.info("LoST-Sync: scheduled coverage re-push to parent after GIS reload")
    except Exception as exc:
        log.warning("LoST-Sync: could not schedule re-push: %s", exc)


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
        req, as_of_raw = _parse_request(xml_bytes)
    except ValueError as exc:
        return _to_xml_response(BadRequestResponse(message=str(exc)), status=200).body

    recursive = _parse_recursive(xml_bytes)

    if req.validate_location != "true":
        return _to_xml_response(ForbiddenResponse(), status=200).body

    # Forest Guide mode: URN check, then always redirect to matched child — never recurse, no parent escalation
    if _forest_guide_mode:
        effective_urn, _ = _resolve_service_urn(req.service_urn)
        if effective_urn.lower() != "urn:service:sos":
            return _to_xml_response(ServiceNotImplementedResponse(), status=200).body
        child_match = _lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("child_uri", "")
            if child_uri:
                return _to_xml_response(
                    RedirectResponse(target=child_uri, source=_server_uri), status=200
                ).body
            log.warning(
                "Forest Guide: child coverage match found but child_uri is empty "
                "(source=%s) — returning notFound",
                child_match.get("source", ""),
            )
        return _to_xml_response(
            NotFoundResponse(message="No child LVF covers this location"),
            status=200,
        ).body

    # Child coverage store lookup — before Gate 0 when the store is non-empty
    if _child_coverage:
        child_match = _lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("child_uri", "")
            if child_uri:
                if recursive:
                    return _do_recurse_to_uri_sync(xml_bytes, child_uri)
                return _to_xml_response(
                    RedirectResponse(target=child_uri, source=_server_uri), status=200
                ).body
            log.warning(
                "LoST-Sync: child coverage match found but child_uri is empty "
                "(source=%s) — falling through to local processing",
                child_match.get("source", ""),
            )

    # Routing-only mode: skip Gate 0/1/2 and route via parent or return unavailable
    if _routing_only:
        if _parent_uri:
            if recursive:
                return _do_recurse_sync(xml_bytes)
            return _to_xml_response(
                RedirectResponse(target=_parent_uri, source=_server_uri), status=200
            ).body
        return _to_xml_response(
            LocationValidationUnavailableResponse(
                message="This node has no GIS data and no configured parent for this location"
            ),
            status=200,
        ).body

    effective_urn, is_alias = _resolve_service_urn(req.service_urn)

    # KNOWN LIMITATION: civic/geodetic coverage regions are derived at load time against
    # the real datetime.now(). Out-of-coverage redirect decisions for future asOf queries
    # may not reflect future-staged records — acceptable per the draft's own caveats about
    # future query stability (draft-ietf-ecrit-lost-planned-changes).
    now = datetime.datetime.now(datetime.timezone.utc)
    as_of_used: Optional[datetime.datetime] = None
    if as_of_raw is not None and as_of_raw > now:
        now = as_of_raw
        as_of_used = as_of_raw

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
        if recursive and isinstance(ooc, RedirectResponse):
            return _do_recurse_sync(xml_bytes)
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
    return _to_xml_response(final, status=200, as_of_used=as_of_used).body


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _schema, _routing_only, _event_loop
    _schema = _load_schema()
    if _schema is None:
        log.warning("Operating without XML schema validation")

    _event_loop = asyncio.get_running_loop()

    if _forest_guide_mode:
        log.info(
            "Forest Guide mode active (LVF_FOREST_GUIDE_MODE=true): "
            "this node routes requests via redirect or notFound — no GIS validation"
        )
        gpkg_env = os.environ.get("LVF_GPKG_PATH")
        if gpkg_env:
            log.warning(
                "LVF_GPKG_PATH=%r is set but ignored in Forest Guide mode — no GIS data will be loaded",
                gpkg_env,
            )
        if os.environ.get("LVF_PARENT_URI"):
            log.warning(
                "LVF_PARENT_URI=%r is set but ignored in Forest Guide mode — "
                "Forest Guides have no parent (RFC 5582 §8)",
                os.environ["LVF_PARENT_URI"],
            )
        _routing_only = True
        _load_child_coverage()
        asyncio.create_task(_startup_sync())
        yield
        return

    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    gpkg_exists = gpkg_path and os.path.exists(gpkg_path)

    if gpkg_exists:
        if not _default_mapping_source_id:
            raise RuntimeError(
                "LVF_DEFAULT_MAPPING_SOURCE_ID is required but not set. "
                "Recommended value: {00000000-0000-0000-0000-000000000000}"
            )
        _load_gis_data(gpkg_path)
        _routing_only = False
        poll_interval = int(os.environ.get("LVF_GPKG_POLL_INTERVAL_SECONDS", "60"))
        if poll_interval > 0:
            threading.Thread(
                target=_watch_gpkg, args=(gpkg_path,), daemon=True
            ).start()
            log.info("GPKG watcher started (poll interval: %ds)", poll_interval)
    else:
        _routing_only = True
        if gpkg_path:
            log.info(
                "Routing-only mode: LVF_GPKG_PATH is set but file not found at %s",
                gpkg_path,
            )
        else:
            log.info("Routing-only mode: LVF_GPKG_PATH is not configured")
        if not _parent_uri and not _sync_children:
            log.warning(
                "Routing-only mode: neither LVF_PARENT_URI nor LVF_SYNC_CHILDREN is configured "
                "— this node cannot answer or route requests"
            )

    if _root_ams:
        _load_ams_provisioning()
    elif os.path.exists(os.path.join(_ams_provisioning_dir(), "ams_civic_coverage.json")):
        log.debug("AMS provisioning files found but LVF_ROOT_AMS is not set — no behavior change")

    _load_child_coverage()
    asyncio.create_task(_startup_sync())
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
    import json as _json
    from shapely.geometry import mapping
    return {
        urn: _json.loads(_json.dumps(mapping(geom)))
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


@app.post("/sync")
async def sync_endpoint(request: Request) -> Response:
    """
    LoST-Sync endpoint (RFC 6739).
    Accepts pushMappings and getMappingsRequest in application/lostsync+xml.
    Returns HTTP 200 for both success and protocol-level errors.
    """
    body = await request.body()
    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError as exc:
        return _sync_error_response("badRequest", f"Malformed XML: {exc}")

    if root.tag == f"{{{_NS_SYNC}}}pushMappings":
        log.info("LoST-Sync: received pushMappings from %s", request.client)
        return await _handle_push_mappings(root)
    elif root.tag == f"{{{_NS_SYNC}}}getMappingsRequest":
        log.info("LoST-Sync: received getMappingsRequest from %s", request.client)
        return await _handle_get_mappings(root)
    else:
        return _sync_error_response(
            "badRequest",
            f"Unexpected root element {root.tag!r}; "
            "expected {urn:ietf:params:xml:ns:lostsync1}pushMappings "
            "or {urn:ietf:params:xml:ns:lostsync1}getMappingsRequest",
        )


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
        req, as_of_raw = _parse_request(body)
    except ValueError as exc:
        return _to_xml_response(BadRequestResponse(message=str(exc)), status=200)

    recursive = _parse_recursive(body)

    if req.validate_location != "true":
        return _to_xml_response(ForbiddenResponse(), status=200)

    # Forest Guide mode: URN check, then always redirect to matched child — never recurse, no parent escalation
    if _forest_guide_mode:
        effective_urn, _ = _resolve_service_urn(req.service_urn)
        if effective_urn.lower() != "urn:service:sos":
            return _to_xml_response(ServiceNotImplementedResponse(), status=200)
        child_match = _lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("child_uri", "")
            if child_uri:
                return _to_xml_response(
                    RedirectResponse(target=child_uri, source=_server_uri), status=200
                )
            log.warning(
                "Forest Guide: child coverage match found but child_uri is empty "
                "(source=%s) — returning notFound",
                child_match.get("source", ""),
            )
        return _to_xml_response(
            NotFoundResponse(message="No child LVF covers this location"),
            status=200,
        )

    # Resolve alias URN to the effective provisioned URN
    effective_urn, is_alias = _resolve_service_urn(req.service_urn)

    # Child coverage store lookup — before Gate 0 when the store is non-empty
    if _child_coverage:
        child_match = _lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("child_uri", "")
            if child_uri:
                if recursive:
                    result = await _do_recurse_to_uri_async(body, child_uri)
                    return Response(content=result, status_code=200, media_type="application/xml")
                return _to_xml_response(
                    RedirectResponse(target=child_uri, source=_server_uri), status=200
                )
            log.warning(
                "LoST-Sync: child coverage match found but child_uri is empty "
                "(source=%s) — falling through to local processing",
                child_match.get("source", ""),
            )

    # Routing-only mode: skip Gate 0/1/2 and route via parent or return unavailable
    if _routing_only:
        if _parent_uri:
            if recursive:
                result = await _do_recurse_async(body)
                return Response(content=result, status_code=200, media_type="application/xml")
            return _to_xml_response(
                RedirectResponse(target=_parent_uri, source=_server_uri), status=200
            )
        return _to_xml_response(
            LocationValidationUnavailableResponse(
                message="This node has no GIS data and no configured parent for this location"
            ),
            status=200,
        )

    # KNOWN LIMITATION: civic/geodetic coverage regions are derived at load time against
    # the real datetime.now(). Out-of-coverage redirect decisions for future asOf queries
    # may not reflect future-staged records — acceptable per the draft's own caveats about
    # future query stability (draft-ietf-ecrit-lost-planned-changes).
    now = datetime.datetime.now(datetime.timezone.utc)
    as_of_used: Optional[datetime.datetime] = None
    if as_of_raw is not None and as_of_raw > now:
        now = as_of_raw
        as_of_used = as_of_raw

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
        if recursive and isinstance(ooc, RedirectResponse):
            result = await _do_recurse_async(body)
            return Response(content=result, status_code=200, media_type="application/xml")
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
    return _to_xml_response(final, status=200, as_of_used=as_of_used)
