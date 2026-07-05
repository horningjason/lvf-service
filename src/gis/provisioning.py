"""
GIS data loading, attribute indexing, and coverage-region derivation.

Extracted from src/lost/find_service.py as part of the GIS-code extraction
into src/gis/. Module state below (_ssap, _rcl, _boundaries, ...) is
module-private — find_service.py's module-level __getattr__ forwards legacy
find_service.<name> access to it, since src/server.py and
src/lost/list_services*.py read this state that way. Those two files
weren't modified by this refactor, but they're real consumers of this
module's state via that forwarding.

Loading and hot-reload are backed by i3_fe_core.gis.DatasetCache: LvfDatasetSpec
below is the seam that plugs LVF's GPKG format (and its derived coverage/index
state) into that shared cache/reload engine. This module has no dependency on
src.lost.find_service — reload success/failure notification (element/service
state, metrics, discrepancy reports) is the caller's job via the on_success/
on_failure callbacks passed to watch(), not this module's.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Callable, Optional

import geopandas as gpd
from shapely.ops import unary_union

from i3_fe_core.gis import DatasetCache

from src.gis import records as gis_records
from src.utils import _is_temporally_active
from src.validation import response_assembly
from src.validation.models import (
    CivicAddress,
    CivicCoverageEntry,
    RCLRecord,
    SSAPRecord,
    ServiceBoundary,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GIS data store (populated at startup)
# ---------------------------------------------------------------------------

_ssap:       list[SSAPRecord]      = []
_rcl:        list[RCLRecord]       = []
_boundaries: list[ServiceBoundary] = []
_geodetic_coverage: dict[str, Any] = {}
_civic_coverage: list[CivicCoverageEntry] = []
_ssap_index: dict[tuple, list] = {}
_rcl_index:  dict[tuple, list] = {}

_gis_last_loaded: Optional[datetime.datetime] = None
_cache: Optional[DatasetCache] = None


def _norm_key(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    s = v.strip()
    return s.upper() if s else None


def _build_attribute_index() -> None:
    global _ssap_index, _rcl_index
    ssap_idx: dict[tuple, list] = {}
    for r in _ssap:
        key = (_norm_key(r.country), _norm_key(r.a1), _norm_key(r.a2))
        ssap_idx.setdefault(key, []).append(r)
    _ssap_index = ssap_idx

    rcl_idx: dict[tuple, list] = {}
    for r in _rcl:
        l_key = (_norm_key(r.country_l), _norm_key(r.a1_l), _norm_key(r.a2_l))
        r_key = (_norm_key(r.country_r), _norm_key(r.a1_r), _norm_key(r.a2_r))
        rcl_idx.setdefault(l_key, []).append(r)
        if r_key != l_key:
            rcl_idx.setdefault(r_key, []).append(r)
    _rcl_index = rcl_idx
    log.debug(
        "Attribute index built: %d SSAP buckets, %d RCL buckets",
        len(_ssap_index), len(_rcl_index),
    )


def _candidates_for(address: CivicAddress) -> tuple[list, list]:
    key = (_norm_key(address.country), _norm_key(address.a1), _norm_key(address.a2))
    ssap_subset = _ssap_index.get(key)
    rcl_subset  = _rcl_index.get(key)
    if ssap_subset is None and rcl_subset is None:
        return _ssap, _rcl
    return (ssap_subset or [], rcl_subset or [])


class LvfDatasetSpec:
    """DatasetSpec adapter: plugs LVF's GPKG layers (SSAP/RCL/boundary) and
    derived coverage/index state into i3_fe_core.gis.DatasetCache."""

    def __init__(self, gpkg_path: str) -> None:
        self._gpkg_path = gpkg_path

    @property
    def source_path(self) -> str:
        return self._gpkg_path

    @property
    def cache_key(self) -> str:
        return "lvf"

    def populate_from_source(self, now: datetime.datetime) -> None:
        global _ssap, _rcl, _boundaries, _gis_last_loaded

        ssap_layer      = os.environ.get("LVF_SSAP_LAYER",     "SiteStructureAddressPoint")
        rcl_layer       = os.environ.get("LVF_RCL_LAYER",      "RoadCenterLine")
        boundary_layers = [
            name.strip()
            for name in os.environ.get("LVF_BOUNDARY_LAYERS", "PsapPolygon").split(",")
            if name.strip()
        ]

        for layer_name, converter, store_name in [
            (ssap_layer, gis_records.row_to_ssap, "SSAP"),
            (rcl_layer,  gis_records.row_to_rcl,  "RCL"),
        ]:
            try:
                gdf = gpd.read_file(self._gpkg_path, layer=layer_name, engine="pyogrio")
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
                gdf = gpd.read_file(self._gpkg_path, layer=layer_name, engine="pyogrio")
                records = [gis_records.row_to_boundary(row) for _, row in gdf.iterrows()]
                _boundaries.extend(records)
                log.info("Loaded %d boundary records from '%s'", len(records), layer_name)
            except Exception as exc:
                log.warning("Could not load boundary layer '%s': %s", layer_name, exc)

        _derive_geodetic_coverage()
        _derive_civic_coverage(now)
        _build_attribute_index()
        _gis_last_loaded = now

    def populate_from_cache(self, payload: dict, now: datetime.datetime) -> None:
        global _ssap, _rcl, _boundaries, _civic_coverage, _geodetic_coverage, _gis_last_loaded

        from shapely.wkt import loads as _wkt_loads
        _ssap              = [gis_records.dict_to_ssap(d) for d in payload["ssap"]]
        _rcl               = [gis_records.dict_to_rcl(d)  for d in payload["rcl"]]
        _boundaries        = [gis_records.dict_to_boundary(d) for d in payload["boundaries"]]
        _civic_coverage    = [gis_records.dict_to_civic_entry(d) for d in payload["civic_coverage"]]
        _geodetic_coverage = {urn: _wkt_loads(wkt) for urn, wkt in payload["geodetic_coverage"].items()}
        _gis_last_loaded   = now
        log.info(
            "Loaded from cache: %d SSAP, %d RCL, %d boundaries, "
            "%d civic coverage entries, %d geodetic URN(s)",
            len(_ssap), len(_rcl), len(_boundaries),
            len(_civic_coverage), len(_geodetic_coverage),
        )
        _build_attribute_index()

    def serialize(self) -> dict:
        return {
            "ssap":              [gis_records.ssap_to_dict(r) for r in _ssap],
            "rcl":               [gis_records.rcl_to_dict(r)  for r in _rcl],
            "boundaries":        [gis_records.boundary_to_dict(b) for b in _boundaries],
            "civic_coverage":    [gis_records.civic_entry_to_dict(e) for e in _civic_coverage],
            "geodetic_coverage": {urn: geom.wkt for urn, geom in _geodetic_coverage.items()},
        }


def load(gpkg_path: str, now: datetime.datetime) -> None:
    """Load GIS data (from the JSON cache if the GPKG mtime is unchanged,
    otherwise from the GeoPackage), build the attribute index, and derive
    coverage.

    LvfDatasetSpec.populate_from_source/populate_from_cache already derive
    coverage and build the attribute index, so this is a thin public entry
    point for find_service.py's startup paths — they call this instead of
    reaching into DatasetCache/DatasetSpec internals directly.
    """
    global _cache
    _cache = DatasetCache(
        LvfDatasetSpec(gpkg_path),
        poll_interval_seconds=int(os.environ.get("LVF_GPKG_POLL_INTERVAL_SECONDS", "60")),
    )
    _cache.load(now)


def is_reloading() -> bool:
    return _cache.is_reloading if _cache is not None else False


def watch(
    get_now: Callable[[], datetime.datetime],
    on_success: Callable[[], None],
    on_failure: Callable[[Exception], None],
) -> None:
    """Blocking loop (run in a daemon thread): poll the GPKG mtime and
    hot-reload on change via the DatasetCache built by load(). No-op if
    load() hasn't been called yet."""
    if _cache is not None:
        _cache.watch(get_now, on_success, on_failure)


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


def _derive_civic_coverage(now: datetime.datetime) -> None:
    global _civic_coverage

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
