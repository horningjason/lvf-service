"""
Gate 2 — Progressive Filter (§5).

The filter simultaneously identifies the authoritative GIS record and evaluates
submitted PIDF-LO elements in hierarchical order. No separate GIS investigation
step precedes element evaluation — the two are the same operation (§5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from src.models import (
    ELEMENT_HIERARCHY,
    CivicAddress,
    FilterState,
    RCLRecord,
    SSAPRecord,
)


# ---------------------------------------------------------------------------
# GIS field mappings (STA-006.3 standardized names per §5.7, §6)
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
    # hno handled separately — integer comparison (§5.6.1)
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
# hno is not listed here — it uses range/parity/flag logic (§5.6.2).
# rcl_unchecked fields are not listed — they never reach this lookup.
_RCL_FIELD: dict[str, str | tuple[str, str]] = {
    "country": ("country_l", "country_r"),
    "a1":      ("a1_l",      "a1_r"),
    "a2":      ("a2_l",      "a2_r"),
    "a3":      ("a3_l",      "a3_r"),
    "a4":      ("a4_l",      "a4_r"),
    "a5":      ("a5_l",      "a5_r"),
    "rd":      "st_name",       # §6.2 — shared across both sides
    "prm":     "st_premod",
    "prd":     "st_predir",
    "stp":     "st_pretyp",
    "stps":    "st_presep",
    "sts":     "st_postyp",
    "pod":     "st_posdir",
    "pom":     "st_posmod",
    "hnp":     ("adnumpre_l", "adnumpre_r"),   # §6.3 — side-specific post-HNO
    "pcn":     ("postcomm_l", "postcomm_r"),   # §6.5 — side-specific post-HNO
    "pc":      ("postcode_l", "postcode_r"),
}

# Position of each element in ELEMENT_HIERARCHY for remainder calculation
_ELEM_INDEX: dict[str, int] = {
    e.civic_address_field: i for i, e in enumerate(ELEMENT_HIERARCHY)
}

# Reverse lookup: pidf_lo tag string → civic_address_field name
_PIDF_TO_FIELD: dict[str, str] = {
    e.pidf_lo: e.civic_address_field for e in ELEMENT_HIERARCHY
}


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

# RCL candidates carry a per-record determined side after HNO evaluation (§5.4)
_RCLCandidate = tuple[RCLRecord, Optional[Literal["L", "R"]]]


@dataclass
class Gate2Result:
    """
    Outcome of the Gate 2 progressive filter (§5).

    outcome:
        "match"     — exactly one candidate found; record and layer are set
        "invalid"   — stop-on-first-invalid fired (§5.8); state.invalid is set
        "not_found" — no single candidate on any available layer (§5.3)
    side:
        Determined RCL side when layer is "RCL" and outcome is "match".
        Used by response assembly for the §7.5 point-in-polygon test.
    """
    state:   FilterState
    outcome: Literal["match", "invalid", "not_found"]
    layer:   Optional[str] = None
    record:  Optional[SSAPRecord | RCLRecord] = None
    side:    Optional[Literal["L", "R"]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parity_ok(hno: int, parity: Optional[Literal["E", "O", "B"]]) -> bool:
    """Return True if hno satisfies the given parity constraint (§5.6.2)."""
    if parity is None or parity == "B":
        return True
    return (hno % 2 == 0) == (parity == "E")


def _str_match(submitted: str, gis: Optional[str]) -> bool:
    """
    Case-insensitive exact string comparison per §5.7.
    An empty submitted value matches an empty GIS field (INF-027 §2.5.7).
    """
    return submitted.lower() == (gis or "").lower()


def _seed_unchecked(state: FilterState, address: CivicAddress, is_rcl: bool) -> None:
    """
    Pre-populate state.unchecked with submitted elements that will never enter
    the filter loop: always-unchecked (§5.5.1) and, on RCL, rcl-only unchecked
    (§5.5.2). Called once before the filter loop begins.
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
    submitted evaluable elements into state.unchecked (§5.8).

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
# SSAP progressive filter (§5.1 Step 1, §5.6.1)
# ---------------------------------------------------------------------------

def _filter_ssap(
    address: CivicAddress,
    records: list[SSAPRecord],
) -> tuple[FilterState, list[SSAPRecord]]:
    """
    Run the progressive filter against the SSAP layer.

    HNO uses exact integer comparison against Add_Number (§5.6.1).
    All other evaluable fields use case-insensitive exact string match (§5.7).
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
            continue  # omitted — §5.2: skip, do not add to any list

        if field == "hno":
            # §5.6.1 — integer comparison against Add_Number
            try:
                hno_int = int(submitted)
                candidates = [r for r in candidates if r.add_number == hno_int]
            except ValueError:
                candidates = []
        else:
            attr = _SSAP_FIELD.get(field)
            if attr is None:
                continue
            candidates = [
                r for r in candidates
                if _str_match(submitted, getattr(r, attr))
            ]

        if not candidates:
            state.invalid = elem.pidf_lo
            _flush_remaining_to_unchecked(state, address, field, is_rcl=False)
            state.terminal = True
            break

        # §5.2: non-zero candidate set → element evaluated successfully → <valid>
        state.valid.append(elem.pidf_lo)

    return state, candidates


# ---------------------------------------------------------------------------
# RCL progressive filter (§5.1 Step 2, §5.4, §5.6.2)
# ---------------------------------------------------------------------------

def _filter_rcl(
    address: CivicAddress,
    records: list[RCLRecord],
) -> tuple[FilterState, list[_RCLCandidate]]:
    """
    Run the progressive filter against the RCL layer.

    Candidates are (RCLRecord, side) tuples. Side is None until HNO evaluation
    determines it (§5.4). After HNO, side-specific fields are evaluated against
    the per-record determined side only.

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
            # §5.6.2 — range + parity + validation flag; each matching side
            # yields a separate (record, side) candidate entry.
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
                for record, _ in pre_hno:
                    if (record.fromaddr_l is not None
                            and record.toaddr_l is not None
                            and record.fromaddr_l <= hno_int <= record.toaddr_l
                            and _parity_ok(hno_int, record.parity_l)):
                        state.all_flags_n = True
                        break
                    if (record.fromaddr_r is not None
                            and record.toaddr_r is not None
                            and record.fromaddr_r <= hno_int <= record.toaddr_r
                            and _parity_ok(hno_int, record.parity_r)):
                        state.all_flags_n = True
                        break
                # candidate_set = 0 — stop-on-first-invalid (§5.8)
                state.invalid = elem.pidf_lo
                _flush_remaining_to_unchecked(state, address, field, is_rcl=True)
                state.terminal = True
                break

            # HNO retained candidates — always unchecked on RCL, never valid (§5.6.2)
            state.unchecked.append(elem.pidf_lo)
            side_determined = True

        else:
            field_spec = _RCL_FIELD.get(field)
            if field_spec is None:
                continue

            if isinstance(field_spec, str):
                # Shared field (street name elements) — same value both sides
                candidates = [
                    (r, s) for r, s in candidates
                    if _str_match(submitted, getattr(r, field_spec))
                ]
            elif not side_determined:
                # Pre-HNO side-specific: retain if either side matches (§5.4)
                left_attr, right_attr = field_spec
                candidates = [
                    (r, s) for r, s in candidates
                    if (_str_match(submitted, getattr(r, left_attr))
                        or _str_match(submitted, getattr(r, right_attr)))
                ]
            else:
                # Post-HNO: use the per-record determined side (§5.4)
                left_attr, right_attr = field_spec
                candidates = [
                    (r, s) for r, s in candidates
                    if _str_match(
                        submitted,
                        getattr(r, left_attr if s == "L" else right_attr),
                    )
                ]

            if not candidates:
                state.invalid = elem.pidf_lo
                _flush_remaining_to_unchecked(state, address, field, is_rcl=True)
                state.terminal = True
                break

            # §5.2: non-zero candidate set → element evaluated successfully → <valid>
            state.valid.append(elem.pidf_lo)

    return state, candidates


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(
    address: CivicAddress,
    ssap_records: list[SSAPRecord],
    rcl_records: list[RCLRecord],
) -> Gate2Result:
    """
    Execute the Gate 2 progressive filter per §5.

    Layer search order: SSAP first, RCL if SSAP does not yield exactly one
    record (§5.1). If SSAP is absent or empty, proceed directly to RCL.

    notFound is returned when (§5.3):
    - Both layers are absent or empty
    - All elements exhausted with candidate_set ≥ 2 on every available layer
    """
    # Step 1: SSAP (§5.1)
    if ssap_records:
        ssap_state, ssap_candidates = _filter_ssap(address, ssap_records)
        if ssap_state.terminal:
            failing_idx = _ELEM_INDEX[_PIDF_TO_FIELD[ssap_state.invalid]]
            if failing_idx < _ELEM_INDEX["hno"]:
                # Pre-HNO failure is authoritative — RCL cannot help (§5.1)
                return Gate2Result(state=ssap_state, outcome="invalid", layer="SSAP")
            # HNO or below — discard SSAP result and let RCL decide (§5.1)
        if len(ssap_candidates) == 1:
            return Gate2Result(
                state=ssap_state,
                outcome="match",
                layer="SSAP",
                record=ssap_candidates[0],
            )
        # terminal at HNO or below — discarded, fall through to RCL per §5.1. 2+ means ambiguous.

    # Step 2: RCL (§5.1)
    if rcl_records:
        rcl_state, rcl_candidates = _filter_rcl(address, rcl_records)
        if rcl_state.terminal:
            if rcl_state.all_flags_n:
                return Gate2Result(state=FilterState(), outcome="not_found")
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
        # 2+ candidates → ambiguous on all layers → notFound (§5.3)

    # All layers exhausted with no single candidate (§5.3)
    return Gate2Result(state=FilterState(), outcome="not_found")
