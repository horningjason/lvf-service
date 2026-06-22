"""
GIS data loading, attribute indexing, and coverage-region derivation.

Extracted from src/lost/find_service.py as part of the GIS-code extraction
into src/provisioning/gis/. Module state below (_ssap, _rcl, _boundaries, ...) is
module-private — find_service.py's module-level __getattr__ forwards legacy
find_service.<name> access to it, since src/server.py and
src/lost/list_services*.py read this state that way. Those two files
weren't modified by this refactor, but they're real consumers of this
module's state via that forwarding.

This module has no dependency on src.lost.find_service. _watch_gpkg() takes
callbacks (get_now, on_reload, on_event_loop) supplied by its caller instead
of reaching back into find_service.py for NTP time, AMS/repush behavior, and
the asyncio event loop — GIS code has no business knowing about AMS
provisioning or LoST-Sync repush scheduling.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

import geopandas as gpd
from shapely.ops import unary_union

from src.observability import metrics as _metrics
from src.provisioning.discrepancy.discrepancy_report import GISProblem, ProblemSeverity, file_gis_dr
from src.provisioning.gis import records as gis_records
from src.notifications import element_state as _element_state
from src.notifications import service_state as _service_state
from src.notifications.element_state import ElementState
from src.notifications.service_state import ServiceState
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

_reloading: bool = False
_reloading_lock = threading.Lock()
_gis_last_loaded: Optional[datetime.datetime] = None


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


def _load_gis_data(gpkg_path: str, now: datetime.datetime) -> None:
    global _ssap, _rcl, _boundaries, _geodetic_coverage, _civic_coverage, _gis_last_loaded

    cache_path = os.path.splitext(gpkg_path)[0] + ".cache.json"
    gpkg_mtime = os.path.getmtime(gpkg_path)

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("gpkg_mtime") == gpkg_mtime:
                log.info("Cache hit — loading GIS data from JSON cache: %s", cache_path)
                from shapely.wkt import loads as _wkt_loads
                _ssap              = [gis_records.dict_to_ssap(d) for d in data["ssap"]]
                _rcl               = [gis_records.dict_to_rcl(d)  for d in data["rcl"]]
                _boundaries        = [gis_records.dict_to_boundary(d) for d in data["boundaries"]]
                _civic_coverage    = [gis_records.dict_to_civic_entry(d) for d in data["civic_coverage"]]
                _geodetic_coverage = {urn: _wkt_loads(wkt) for urn, wkt in data["geodetic_coverage"].items()}
                _gis_last_loaded   = now
                log.info(
                    "Loaded from cache: %d SSAP, %d RCL, %d boundaries, "
                    "%d civic coverage entries, %d geodetic URN(s)",
                    len(_ssap), len(_rcl), len(_boundaries),
                    len(_civic_coverage), len(_geodetic_coverage),
                )
                _build_attribute_index()
                return
            else:
                log.info("Cache miss — GPKG mtime changed, rebuilding: %s", cache_path)
        except Exception as exc:
            log.warning(
                "JSON cache load failed (%s) — falling back to GPKG and rebuilding cache", exc,
            )
    else:
        log.info("Cache miss — no JSON cache found, building for the first time: %s", cache_path)

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
            records = [gis_records.row_to_boundary(row) for _, row in gdf.iterrows()]
            _boundaries.extend(records)
            log.info("Loaded %d boundary records from '%s'", len(records), layer_name)
        except Exception as exc:
            log.warning("Could not load boundary layer '%s': %s", layer_name, exc)

    _derive_geodetic_coverage()
    _derive_civic_coverage(now)
    _build_attribute_index()
    _gis_last_loaded = now

    # Atomic write (temp file + os.replace) so concurrent workers never observe a
    # half-written cache. Mirrors the pattern used by _save_child_coverage_list.
    tmp_cache = cache_path + ".tmp"
    try:
        with open(tmp_cache, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ssap":              [gis_records.ssap_to_dict(r) for r in _ssap],
                    "rcl":               [gis_records.rcl_to_dict(r)  for r in _rcl],
                    "boundaries":        [gis_records.boundary_to_dict(b) for b in _boundaries],
                    "civic_coverage":    [gis_records.civic_entry_to_dict(e) for e in _civic_coverage],
                    "geodetic_coverage": {urn: geom.wkt for urn, geom in _geodetic_coverage.items()},
                    "gpkg_mtime":        gpkg_mtime,
                },
                f,
            )
        os.replace(tmp_cache, cache_path)
        log.info("GIS data cached to JSON: %s", cache_path)
    except Exception as exc:
        log.warning("Could not write JSON cache: %s", exc)
        try:
            if os.path.exists(tmp_cache):
                os.remove(tmp_cache)
        except OSError:
            pass


def load(gpkg_path: str, now: datetime.datetime) -> None:
    """Load GIS data, build the attribute index, and derive coverage.

    _load_gis_data() already calls _derive_geodetic_coverage() and
    _derive_civic_coverage(now) internally (cache-hit loads already-derived
    coverage from the JSON cache instead), so this is a thin public entry
    point for find_service.py's startup paths — they call this instead of
    reaching into GIS internals (_load_gis_data) directly.
    """
    _load_gis_data(gpkg_path, now)


def _watch_gpkg(
    gpkg_path: str,
    get_now: Callable[[], datetime.datetime],
    on_reload: Callable[[], None],
    on_event_loop: Callable[[], Optional[asyncio.AbstractEventLoop]],
) -> None:
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
                _load_gis_data(gpkg_path, get_now())
                baseline_mtime = current_mtime
                with _reloading_lock:
                    _reloading = False
                _metrics.reload_events_total.labels(trigger="gpkg_watcher", outcome="success").inc()
                log.info("GIS data reload complete — resuming normal service")
                _element_state._notifier.set_state(ElementState.Normal, "GIS reload succeeded")
                _service_state._notifier.set_state(ServiceState.Normal, "GIS reload succeeded")
                on_reload()
            except Exception as exc:
                _metrics.reload_events_total.labels(trigger="gpkg_watcher", outcome="failure").inc()
                log.error(
                    "GIS data reload failed — service remains unavailable", exc_info=True
                )
                _element_state._notifier.set_state(
                    ElementState.ServiceDisruption, "GIS reload failed"
                )
                event_loop = on_event_loop()
                if event_loop is not None and event_loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        file_gis_dr(
                            problem=GISProblem.GeneralProvisioning,
                            severity=ProblemSeverity.Severe,
                            detail=str(exc),
                        ),
                        event_loop,
                    )


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
