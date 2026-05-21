"""
Gate 2 — Progressive Filter.

The filter simultaneously identifies the authoritative GIS record and evaluates
submitted PIDF-LO elements in hierarchical order. No separate GIS investigation
step precedes element evaluation — the two are the same operation.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Literal, Optional

from src.utils import _is_temporally_active
from src.models import (
    ELEMENT_HIERARCHY,
    CivicAddress,
    FilterState,
    RCLRecord,
    SSAPRecord,
)


# ---------------------------------------------------------------------------
# GIS field mappings (STA-006.3 standardized names)
# ---------------------------------------------------------------------------

# CivicAddress field → SSAPRecord attribute name
_SSAP_FIELD: dict[str, str] = {
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
    # hno handled separately — integer comparison
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

# CivicAddress field → RCLRecord field mapping.
# Tuples are (left_attr, right_attr) for side-specific fields.
# Plain strings are shared fields (same value for both sides).
# hno is not listed here — it uses range/parity/flag logic.
# rcl_unchecked fields are not listed — they never reach this lookup.
_RCL_FIELD: dict[str, str | tuple[str, str]] = {
    "country": ("country_l", "country_r"),
    "a1":      ("a1_l",      "a1_r"),
    "a2":      ("a2_l",      "a2_r"),
    "a3":      ("a3_l",      "a3_r"),
    "a4":      ("a4_l",      "a4_r"),
    "a5":      ("a5_l",      "a5_r"),
    "rd":      "st_name",       # shared across both sides
    "prm":     "st_premod",
    "prd":     "st_predir",
    "stp":     "st_pretyp",
    "stps":    "st_presep",
    "sts":     "st_postyp",
    "pod":     "st_posdir",
    "pom":     "st_posmod",
    "hnp":     ("adnumpre_l", "adnumpre_r"),   # side-specific post-HNO
    "pcn":     ("postcomm_l", "postcomm_r"),   # side-specific post-HNO
    "pc":      ("postcode_l", "postcode_r"),
}

# Position of each element in ELEMENT_HIERARCHY for remainder calculation
_ELEM_INDEX: dict[str, int] = {
    e.civic_address_field: i for i, e in enumerate(ELEMENT_HIERARCHY)
}


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

# RCL candidates carry a per-record determined side after HNO evaluation
_RCLCandidate = tuple[RCLRecord, Optional[Literal["L", "R"]]]


@dataclass
class Gate2Result:
    """
    Outcome of the Gate 2 progressive filter.

    outcome:
        "match"     — exactly one candidate found; record and layer are set
        "invalid"   — stop-on-first-invalid fired; state.invalid is set
        "not_found" — no single candidate on any available layer
    side:
        Determined RCL side when layer is "RCL" and outcome is "match".
        Used by response assembly for the point-in-polygon test.
    """
    state:   FilterState
    outcome: Literal["match", "invalid", "not_found"]
    layer:   Optional[Literal["SSAP", "RCL"]] = None
    record:  Optional[SSAPRecord | RCLRecord] = None
    side:    Optional[Literal["L", "R"]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parity_ok(hno: int, parity: Optional[Literal["E", "O", "B"]]) -> bool:
    """Return True if hno satisfies the given parity constraint."""
    if parity is None or parity == "B":
        return True
    return (hno % 2 == 0) == (parity == "E")


def _str_match(submitted: str, gis: Optional[str]) -> bool:
    """
    Case-insensitive exact string comparison.
    An empty submitted value matches an empty GIS field (INF-027 §2.5.7).
    """
    return submitted.lower() == (gis or "").lower()


def _field_outcome(
    submitted: str,
    gis: Optional[str],
    null_unchecked: bool = False,
) -> Literal["match", "null", "mismatch"]:
    """
    Categorise one field comparison.

    "match"    — GIS has a value that matches submitted (case-insensitive), or
                 both are absent/empty (empty submitted matches null GIS).
    "null"     — GIS field is absent, submitted is non-empty, and null_unchecked
                 is True; the LVF cannot verify the submitted value against GIS.
    "mismatch" — GIS has a value that does not match submitted, or GIS is absent
                 and null_unchecked is False (null treated as empty-string mismatch).
    """
    if gis is None and submitted and null_unchecked:
        return "null"
    if _str_match(submitted, gis):
        return "match"
    return "mismatch"


def _seed_unchecked(state: FilterState, address: CivicAddress, is_rcl: bool) -> None:
    """
    Pre-populate state.unchecked with submitted elements that will never enter
    the filter loop: always-unchecked elements and, on RCL, rcl-only unchecked
    elements. Called once before the filter loop begins.
    """
    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked or (is_rcl and elem.rcl_unchecked):
            if getattr(address, elem.civic_address_field) is not None:
                state.unchecked.append(elem.pidf_lo)


def _flush_remaining_to_unchecked(
    state: FilterState,
    address: CivicAddress,
    stopped_at_field: str,
    is_rcl: bool,
) -> None:
    """
    After stop-on-first-invalid fires at stopped_at_field, place all remaining
    submitted evaluable elements into state.unchecked.

    Skips elements already handled: always-unchecked (pre-seeded), rcl-unchecked
    when is_rcl (pre-seeded), and elements at or before the stop position.
    """
    stop_idx = _ELEM_INDEX[stopped_at_field]
    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked:
            continue
        if is_rcl and elem.rcl_unchecked:
            continue
        if _ELEM_INDEX[elem.civic_address_field] <= stop_idx:
            continue
        if getattr(address, elem.civic_address_field) is not None:
            state.unchecked.append(elem.pidf_lo)


# ---------------------------------------------------------------------------
# SSAP progressive filter
# ---------------------------------------------------------------------------

def _filter_ssap(
    address: CivicAddress,
    records: list[SSAPRecord],
) -> tuple[FilterState, list[SSAPRecord]]:
    """
    Run the progressive filter against the SSAP layer.

    HNO uses exact integer comparison against Add_Number.
    All other evaluable fields use case-insensitive exact string match.
    """
    state = FilterState()
    _seed_unchecked(state, address, is_rcl=False)
    candidates = list(records)

    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked:
            continue
        if state.terminal:
            break

        field = elem.civic_address_field
        submitted = getattr(address, field)
        if submitted is None:
            continue  # omitted — skip, do not add to any list

        if field == "hno":
            # Integer comparison against Add_Number
            try:
                hno_int = int(submitted)
                candidates = [r for r in candidates if r.add_number == hno_int]
            except ValueError:
                candidates = []
            any_match = bool(candidates)
        else:
            attr = _SSAP_FIELD.get(field)
            if attr is None:
                continue
            any_match = False
            next_candidates: list[SSAPRecord] = []
            for r in candidates:
                outcome = _field_outcome(submitted, getattr(r, attr), elem.null_unchecked)
                if outcome != "mismatch":
                    next_candidates.append(r)
                    if outcome == "match":
                        any_match = True
            candidates = next_candidates

        if not candidates:
            state.invalid = elem.pidf_lo
            _flush_remaining_to_unchecked(state, address, field, is_rcl=False)
            state.terminal = True
            break

        if any_match:
            state.valid.append(elem.pidf_lo)
        else:
            state.unchecked.append(elem.pidf_lo)

    return state, candidates


# ---------------------------------------------------------------------------
# RCL progressive filter
# ---------------------------------------------------------------------------

def _filter_rcl(
    address: CivicAddress,
    records: list[RCLRecord],
) -> tuple[FilterState, list[_RCLCandidate]]:
    """
    Run the progressive filter against the RCL layer.

    Candidates are (RCLRecord, side) tuples. Side is None until HNO evaluation
    determines it. After HNO, side-specific fields are evaluated against the
    per-record determined side only.

    Pre-HNO side-specific elements (country, A1–A5) match if either side
    satisfies the submitted value — side is not yet known. Post-HNO side-specific
    elements (HNP, PCN, PC) use only the determined side's field.

    HNO goes to <unchecked> on a successful RCL match, never to <valid>
    (INF-027 §2.5.8). If HNO causes candidate_set = 0, stop-on-first-invalid
    applies and HNO is placed in <invalid>.
    """
    state = FilterState()
    _seed_unchecked(state, address, is_rcl=True)
    candidates: list[_RCLCandidate] = [(r, None) for r in records]
    side_determined = False

    for elem in ELEMENT_HIERARCHY:
        if elem.always_unchecked or elem.rcl_unchecked:
            continue
        if state.terminal:
            break

        field = elem.civic_address_field
        submitted = getattr(address, field)
        if submitted is None:
            continue

        if field == "hno":
            # Range + parity + validation flag; each matching side yields a
            # separate (record, side) candidate entry.
            try:
                hno_int = int(submitted)
            except ValueError:
                candidates = []
                pre_hno: list[_RCLCandidate] = []
            else:
                pre_hno = list(candidates)
                new: list[_RCLCandidate] = []
                for record, _ in candidates:
                    if (record.fromaddr_l is not None
                            and record.toaddr_l is not None
                            and record.fromaddr_l <= hno_int <= record.toaddr_l
                            and _parity_ok(hno_int, record.parity_l)
                            and record.valid_l != "N"):
                        new.append((record, "L"))
                    if (record.fromaddr_r is not None
                            and record.toaddr_r is not None
                            and record.fromaddr_r <= hno_int <= record.toaddr_r
                            and _parity_ok(hno_int, record.parity_r)
                            and record.valid_r != "N"):
                        new.append((record, "R"))
                candidates = new

            if not candidates:
                # candidate_set = 0 — stop-on-first-invalid
                state.invalid = elem.pidf_lo
                _flush_remaining_to_unchecked(state, address, field, is_rcl=True)
                state.terminal = True
                break

            # HNO retained candidates — always unchecked on RCL, never valid
            state.unchecked.append(elem.pidf_lo)
            side_determined = True

        else:
            field_spec = _RCL_FIELD.get(field)
            if field_spec is None:
                continue

            any_match = False
            next_candidates: list[_RCLCandidate] = []

            if isinstance(field_spec, str):
                # Shared field (street name elements) — same value both sides
                for r, s in candidates:
                    outcome = _field_outcome(submitted, getattr(r, field_spec), elem.null_unchecked)
                    if outcome != "mismatch":
                        next_candidates.append((r, s))
                        if outcome == "match":
                            any_match = True
            elif not side_determined:
                # Pre-HNO side-specific: retain if neither side disqualifies
                left_attr, right_attr = field_spec
                for r, s in candidates:
                    l_out = _field_outcome(submitted, getattr(r, left_attr), elem.null_unchecked)
                    r_out = _field_outcome(submitted, getattr(r, right_attr), elem.null_unchecked)
                    if l_out != "mismatch" or r_out != "mismatch":
                        next_candidates.append((r, s))
                        if l_out == "match" or r_out == "match":
                            any_match = True
            else:
                # Post-HNO: use the per-record determined side
                left_attr, right_attr = field_spec
                for r, s in candidates:
                    gis_val = getattr(r, left_attr if s == "L" else right_attr)
                    outcome = _field_outcome(submitted, gis_val, elem.null_unchecked)
                    if outcome != "mismatch":
                        next_candidates.append((r, s))
                        if outcome == "match":
                            any_match = True

            candidates = next_candidates

            if not candidates:
                state.invalid = elem.pidf_lo
                _flush_remaining_to_unchecked(state, address, field, is_rcl=True)
                state.terminal = True
                break

            if any_match:
                state.valid.append(elem.pidf_lo)
            else:
                state.unchecked.append(elem.pidf_lo)

    return state, candidates


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(
    address: CivicAddress,
    ssap_records: list[SSAPRecord],
    rcl_records: list[RCLRecord],
    now: datetime.datetime,
) -> Gate2Result:
    """
    Execute the Gate 2 progressive filter.

    Layer search order: SSAP first, RCL if SSAP does not yield exactly one
    record. If SSAP is absent or empty, proceed directly to RCL.

    notFound is returned when:
    - Both layers are absent or empty
    - All elements exhausted with candidate_set ≥ 2 on every available layer
    """
    active_ssap = [r for r in ssap_records if _is_temporally_active(r.effective, r.expire, now)]
    active_rcl  = [r for r in rcl_records  if _is_temporally_active(r.effective, r.expire, now)]

    # Step 1: SSAP
    if active_ssap:
        ssap_state, ssap_candidates = _filter_ssap(address, active_ssap)
        if ssap_state.terminal:
            pass  # discard SSAP result, fall through to RCL regardless
        elif len(ssap_candidates) == 1:
            return Gate2Result(
                state=ssap_state,
                outcome="match",
                layer="SSAP",
                record=ssap_candidates[0],
            )
        # terminal or 2+ candidates — fall through to RCL.

    # Step 2: RCL
    if active_rcl:
        rcl_state, rcl_candidates = _filter_rcl(address, active_rcl)
        if rcl_state.terminal:
            return Gate2Result(state=rcl_state, outcome="invalid", layer="RCL")
        if len(rcl_candidates) == 1:
            record, side = rcl_candidates[0]
            rcl_state.determined_side = side
            return Gate2Result(
                state=rcl_state,
                outcome="match",
                layer="RCL",
                record=record,
                side=side,
            )
        # 2+ candidates → ambiguous on all layers → notFound

    # All layers exhausted with no single candidate
    return Gate2Result(state=FilterState(), outcome="not_found")
