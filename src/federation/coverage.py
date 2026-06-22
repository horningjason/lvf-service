"""
AMS (Additional Mapping Service) child coverage state management per
NENA-INF-027 — dynamic coverage store (load/save/watch), cross-process
leader election, and operator-authored AMS provisioning file
validation/loading.

Depends on src.runtime_state; no dependency on src.lost.find_service.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Optional

try:
    import fcntl as _fcntl  # POSIX only — used for cross-process file locks
except ImportError:  # pragma: no cover - Windows dev path
    _fcntl = None  # type: ignore[assignment]

from src import runtime_state

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_child_coverage: list[dict] = []

# Guards reassignment of the _child_coverage global (in-process). The list is
# only ever swapped wholesale for a fresh list — never mutated in place once
# published — so readers can take the current reference without locking and
# always see a complete, consistent snapshot.
_coverage_lock = threading.Lock()

# Most-recently loaded operator-authored AMS provisioning entries (root-AMS
# only). Cached by _load_ams_provisioning() so they can be re-asserted on top of
# the dynamic coverage file after every reload (manual provisioning always wins).
_ams_provisioning_cache: list[dict] = []

# Single-leader election: exactly one worker process on a machine acquires this
# lock and runs the singletons (SIP notifier + boot-time startup sync). The file
# descriptor is held open for the process lifetime so the lock is not released.
_leader_lock_fd: Optional[int] = None
_is_leader: bool = False

_root_ams_active: bool = False


# ---------------------------------------------------------------------------
# LoST-Sync (RFC 6739) — child coverage store
# ---------------------------------------------------------------------------

def _child_coverage_path() -> str:
    forest_guide_mode = os.environ.get("LVF_FOREST_GUIDE_MODE", "").lower() == "true"
    filename = "fg_tree_coverage.json" if forest_guide_mode else "lvf_child_coverage.json"
    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    if gpkg_path:
        return os.path.join(os.path.dirname(gpkg_path) or ".", filename)
    return os.path.join("data", filename)


def _read_child_coverage_file() -> list[dict]:
    """Read and parse the coverage file into a FRESH list.

    Returns an empty list if the file is absent, unreadable, or not a JSON array.
    Does not touch any global — callers decide whether/how to publish the result.
    """
    path = _child_coverage_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("LoST-Sync: could not load child coverage store from %s: %s", path, exc)
        return []


def _load_child_coverage() -> None:
    global _child_coverage
    path = _child_coverage_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fresh = data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("LoST-Sync: could not load child coverage store from %s: %s", path, exc)
        return
    with _coverage_lock:
        _child_coverage = fresh
    log.info("LoST-Sync: loaded %d child coverage entries from %s", len(fresh), path)


def _save_child_coverage_list(entries: list[dict]) -> None:
    """Atomically persist `entries` to the coverage file (temp + os.replace)."""
    path = _child_coverage_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("LoST-Sync: could not save child coverage store to %s: %s", path, exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


@contextmanager
def _coverage_file_lock():
    """Exclusive cross-process lock guarding the read-modify-write of the coverage
    file. The lock is taken on a dedicated `.lock` sidecar file (never the data
    file, which is replaced via os.replace). On platforms without fcntl (Windows
    dev), this is a no-op — multi-worker deployment targets POSIX/containers."""
    if _fcntl is None:
        yield
        return
    lock_path = _child_coverage_path() + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    _t0 = time.monotonic()
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        _wait = time.monotonic() - _t0
        if _wait > 0.05:
            log.debug("Coverage file lock: acquired after waiting %.3fs", _wait)
        yield
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _with_coverage_write(apply_fn) -> bool:
    """Serialize a read-modify-write of the coverage store across processes.

    1. Acquire the exclusive cross-process file lock (blocking).
    2. Reload the latest coverage from disk into a FRESH private list.
    3. Call apply_fn(fresh_list) which performs the upsert/delete operations
       against that private list (returns truthy when coverage changed).
    4. On root-AMS nodes, re-assert operator provisioning on top (always wins).
    5. Atomically save the fresh list.
    6. Publish it as the new _child_coverage under _coverage_lock.

    Returns whatever apply_fn returned.
    """
    global _child_coverage
    with _coverage_file_lock():
        fresh = _read_child_coverage_file()
        result = apply_fn(fresh)
        if runtime_state._root_ams:
            _reapply_ams_provisioning(fresh)
        _save_child_coverage_list(fresh)
        with _coverage_lock:
            _child_coverage = fresh
    return result


def _reapply_ams_provisioning(target: list[dict]) -> None:
    """Re-assert the cached operator-authored AMS provisioning entries on top of
    `target` (matching/superseding by source_id), so manual provisioning always
    wins after any reload of the dynamic coverage file. No-op when not root-AMS
    or when no provisioning has been loaded."""
    if not runtime_state._root_ams or not _ams_provisioning_cache:
        return
    for entry in _ams_provisioning_cache:
        source_id = entry.get("source_id", "")
        for i, existing in enumerate(target):
            if existing.get("source_id") == source_id:
                target[i] = entry
                break
        else:
            target.append(entry)


def _watch_child_coverage() -> None:
    """Poll the coverage file mtime so sibling workers converge on each other's
    writes. Pure read-view refresh: never triggers an upstream push or startup
    sync. On root-AMS nodes, re-asserts provisioning after each reload."""
    global _child_coverage
    interval = int(os.environ.get("LVF_COVERAGE_POLL_INTERVAL_SECONDS", "15"))
    if interval <= 0:
        return
    path = _child_coverage_path()
    try:
        baseline = os.path.getmtime(path) if os.path.exists(path) else 0.0
    except OSError:
        baseline = 0.0

    while True:
        time.sleep(interval)
        try:
            current = os.path.getmtime(path) if os.path.exists(path) else 0.0
        except OSError:
            continue
        if current != baseline:
            baseline = current
            fresh = _read_child_coverage_file()
            if runtime_state._root_ams:
                _reapply_ams_provisioning(fresh)
            with _coverage_lock:
                _child_coverage = fresh
            log.debug(
                "LoST-Sync: coverage file changed — reloaded %d entries (read-view only)",
                len(fresh),
            )


def _start_coverage_watcher() -> None:
    """Start the coverage-file watcher thread (section 4), unless disabled."""
    interval = int(os.environ.get("LVF_COVERAGE_POLL_INTERVAL_SECONDS", "15"))
    if interval <= 0:
        log.info("Coverage watcher disabled (LVF_COVERAGE_POLL_INTERVAL_SECONDS=0)")
        return
    threading.Thread(target=_watch_child_coverage, daemon=True).start()
    log.info("Coverage watcher started (poll interval: %ds)", interval)


def _leader_lock_path() -> str:
    gpkg_path = os.environ.get("LVF_GPKG_PATH")
    data_dir = (os.path.dirname(gpkg_path) or ".") if gpkg_path else "data"
    return os.path.join(data_dir, ".lvf_leader.lock")


def _acquire_leadership() -> bool:
    """Try to become the single leader worker via a non-blocking exclusive file
    lock. The leader runs the SIP notifier and boot-time startup sync; others
    skip them. The lock fd is kept open for the process lifetime so the lock is
    not released. On platforms without fcntl (Windows dev = single process),
    this node is always the leader."""
    global _leader_lock_fd, _is_leader
    if _fcntl is None:
        _is_leader = True
        return True
    path = _leader_lock_path()
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        log.warning("Leader election: could not open %s (%s) — assuming leader", path, exc)
        _is_leader = True
        return True
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        _is_leader = False
        return False
    _leader_lock_fd = fd  # keep open for process lifetime — do not close/GC
    _is_leader = True
    return True


def _parse_iso_timestamp(ts: str) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _compare_timestamps(a: str, b: str) -> int:
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


def _upsert_child_coverage(parsed: dict, target: Optional[list] = None) -> bool:
    coverage = _child_coverage if target is None else target
    source    = parsed.get("source", "")
    source_id = parsed.get("source_id", "")
    new_lu    = parsed.get("last_updated", "")

    for i, entry in enumerate(coverage):
        if entry.get("source") == source and entry.get("source_id") == source_id:
            if _compare_timestamps(new_lu, entry.get("last_updated", "")) > 0:
                coverage[i] = parsed
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

    coverage.append(parsed)
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
        tuples = entry.get("civic_addresses") or []

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
# Root AMS mode — provisioned coverage region for Forest Guide push
# ---------------------------------------------------------------------------

def _ams_provisioning_dir() -> str:
    gpkg_path = os.environ.get("LVF_GPKG_PATH", "")
    d = os.path.dirname(gpkg_path) if gpkg_path else ""
    return d or "."


def _validate_ams_civic_file(path: str) -> Optional[list]:
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

    _ENTRY_REQUIRED = {"source", "source_id", "last_updated", "expires", "service", "profile", "lost_server"}
    _ADDR_REQUIRED  = {"country", "a1", "a2"}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            log.error("AMS: ams_civic_coverage.json entry %d is not an object", i)
            return None
        if "lost_server" not in entry and "child_uri" in entry:
            log.warning(
                'AMS: ams_civic_coverage.json entry %d uses deprecated "child_uri" — rename to "lost_server"', i
            )
            entry["lost_server"] = entry.pop("child_uri")
        if "civic_addresses" not in entry and "civic_tuples" in entry:
            log.warning(
                'AMS: ams_civic_coverage.json entry %d uses deprecated "civic_tuples" — rename to "civic_addresses"', i
            )
            entry["civic_addresses"] = entry.pop("civic_tuples")
        missing = _ENTRY_REQUIRED - entry.keys()
        if missing:
            log.error("AMS: ams_civic_coverage.json entry %d missing required key(s): %s", i, missing)
            return None
        if entry.get("profile") != "civic":
            log.error('AMS: ams_civic_coverage.json entry %d has profile %r — expected "civic"', i, entry.get("profile"))
            return None
        addrs = entry.get("civic_addresses")
        if not isinstance(addrs, list) or not addrs:
            log.error("AMS: ams_civic_coverage.json entry %d must have a non-empty civic_addresses array", i)
            return None
        for j, t in enumerate(addrs):
            if not isinstance(t, dict):
                log.error("AMS: ams_civic_coverage.json entry %d civic_addresses[%d] is not an object", i, j)
                return None
            amissing = _ADDR_REQUIRED - t.keys()
            if amissing:
                log.error("AMS: ams_civic_coverage.json entry %d civic_addresses[%d] missing required key(s): %s", i, j, amissing)
                return None
    return data


def _validate_ams_geodetic_file(path: str) -> Optional[list]:
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

    _ENTRY_REQUIRED = {"source", "source_id", "last_updated", "expires", "service", "profile", "lost_server"}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            log.error("AMS: ams_geodetic_coverage.json entry %d is not an object", i)
            return None
        if "lost_server" not in entry and "child_uri" in entry:
            log.warning(
                'AMS: ams_geodetic_coverage.json entry %d uses deprecated "child_uri" — rename to "lost_server"', i
            )
            entry["lost_server"] = entry.pop("child_uri")
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
    global _root_ams_active, _ams_provisioning_cache, _child_coverage

    if not runtime_state._forest_guide_uri:
        log.warning(
            "LVF_ROOT_AMS=true but LVF_FOREST_GUIDE_URI is not set — "
            "FG push suppressed; programmatic push to LVF_PARENT_URI is also suppressed"
        )
        _root_ams_active = False
        return False

    base_dir = _ams_provisioning_dir()
    civic_path    = os.path.join(base_dir, "ams_civic_coverage.json")
    geodetic_path = os.path.join(base_dir, "ams_geodetic_coverage.json")

    civic_entries = _validate_ams_civic_file(civic_path)
    if civic_entries is None:
        _root_ams_active = False
        return False

    geodetic_entries = _validate_ams_geodetic_file(geodetic_path)
    if geodetic_entries is None:
        _root_ams_active = False
        return False

    # Cache the entries so they can be re-asserted on top of the dynamic coverage
    # file after every reload (manual provisioning always wins — section 6).
    _ams_provisioning_cache = list(civic_entries + geodetic_entries)

    # Merge onto a fresh copy and publish by swap (never mutate the live list in
    # place) so lock-free readers always see a complete snapshot.
    merged = list(_child_coverage)
    for entry in _ams_provisioning_cache:
        source    = entry.get("source", "")
        source_id = entry.get("source_id", "")
        for i, existing in enumerate(merged):
            if existing.get("source_id") == source_id:
                log.info(
                    "AMS: provisioning entry supersedes cached entry source=%s sourceId=%s",
                    source, source_id,
                )
                merged[i] = entry
                break
        else:
            merged.append(entry)
    with _coverage_lock:
        _child_coverage = merged

    _root_ams_active = True
    civic_tuple_count = sum(len(e.get("civic_addresses") or []) for e in civic_entries)
    log.info(
        "AMS: provisioning files loaded (%d civic entry/entries, %d civic address(es), "
        "%d geodetic entry/entries) — FG push active targeting %s",
        len(civic_entries), civic_tuple_count, len(geodetic_entries), runtime_state._forest_guide_uri,
    )
    return True
