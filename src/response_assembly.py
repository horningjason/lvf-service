"""
Response Assembly (§7).

Converts a Gate2Result into a FindServiceResponse, including hierarchical
ordering of element lists (§7.1) and point-in-polygon mapping selection (§7.5).
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from shapely.geometry import Point

from src.gate2 import Gate2Result
from src.models import (
    ELEMENT_HIERARCHY,
    FindServiceResponse,
    LocationValidation,
    LocationValidationResponse,
    LocationValidationUnavailableResponse,
    MappingElement,
    NotFoundResponse,
    ServiceBoundary,
)

# Canonical position of each PIDF-LO element name for sorting (§6, §7.1)
_PIDF_LO_ORDER: dict[str, int] = {
    e.pidf_lo: i for i, e in enumerate(ELEMENT_HIERARCHY)
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sort_by_hierarchy(names: list[str]) -> list[str]:
    """Sort PIDF-LO element names into hierarchical order per §7.1."""
    return sorted(names, key=lambda n: _PIDF_LO_ORDER.get(n, 999))


def _build_location_validation(state) -> LocationValidation:
    """
    Assemble LocationValidation from a FilterState.

    All lists are sorted to hierarchical order (§7.1). invalid is either None
    (conforming result, §7.2) or a single element name (non-conforming, §7.3).
    """
    return LocationValidation(
        valid=_sort_by_hierarchy(state.valid),
        invalid=state.invalid,
        unchecked=_sort_by_hierarchy(state.unchecked),
    )


def _boundary_to_mapping(b: ServiceBoundary) -> MappingElement:
    return MappingElement(
        service_urn=b.service_urn,
        expires=b.expires,
        last_updated=b.last_updated,
        source=b.agency_id or b.source,
        source_id=b.nguid or b.source_id,
        service_uri=b.service_uri,
        service_num=b.service_num,
        display_name=b.display_name,
        display_name_lang=None,  # resolved to _display_name_lang at serialization time
    )


# ---------------------------------------------------------------------------
# §7.5 — Representative point and mapping selection
# ---------------------------------------------------------------------------

def _rcl_representative_point(
    line_geom,
    side: Literal["L", "R"],
) -> Optional[Point]:
    """
    Derive a representative point by offsetting 0.0001 degrees perpendicularly
    from the line midpoint toward the determined side (§7.5, §3.5.1).

    Side convention follows the RCL digitization direction (FROM → TO node):
        L → counter-clockwise 90° rotation of the direction vector
        R → clockwise 90° rotation

    Returns None for a degenerate (zero-length) segment.
    """
    if line_geom is None:
        return None
    coords = list(line_geom.coords)
    if len(coords) < 2:
        return None

    mid: Point = line_geom.interpolate(0.5, normalized=True)

    # Overall bearing from start to end (§3.5.1: "using the segment's bearing")
    dx = coords[-1][0] - coords[0][0]   # Δlongitude
    dy = coords[-1][1] - coords[0][1]   # Δlatitude
    magnitude = math.sqrt(dx * dx + dy * dy)
    if magnitude == 0.0:
        return None

    # Perpendicular unit vector
    if side == "L":
        perp_lon, perp_lat = -dy / magnitude, dx / magnitude   # CCW
    else:
        perp_lon, perp_lat = dy / magnitude, -dx / magnitude   # CW

    return Point(mid.x + perp_lon * 0.0001, mid.y + perp_lat * 0.0001)


def _select_mappings(
    point: Optional[Point],
    boundaries: list[ServiceBoundary],
) -> list[MappingElement]:
    """
    Return a MappingElement for every boundary polygon that contains point
    (§7.5). Multiple elements are returned when the point falls inside more
    than one polygon — per NENA-STA-010 the client MUST have local policy to
    handle this (§7.5). Returns an empty list if point is None or no boundary
    contains it.
    """
    if point is None:
        return []
    return [
        _boundary_to_mapping(b)
        for b in boundaries
        if b.geometry is not None and b.geometry.contains(point)
    ]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def assemble(
    result: Gate2Result,
    boundaries: list[ServiceBoundary],
    service_urn: str = "",
    address=None,               # CivicAddress — for civic coverage lookup
    civic_coverage_lookup=None, # callable(country, a1, a2, a3, ...) → CivicCoverageEntry | None
    default_mapping_factory=None, # callable(service_urn: str) → MappingElement
    return_additional_location: str = "none",  # draft-ietf-ecrit-similar-location-19 rli: attribute
) -> FindServiceResponse:
    """
    Assemble a findServiceResponse from a Gate2Result and the list of service
    boundaries pre-filtered to the requested URN (Gate 0 already confirmed at
    least one matches).

    Outcome mapping (§7.4):
        not_found → NotFoundResponse
        invalid   → LocationValidationResponse with invalid element; mapping
                    uses civic coverage lookup (§3.5.2) when available, falling
                    back to a synthetic default mapping (defaultMappingReturned
                    warning emitted) when lookup does not resolve
        match     → LocationValidationResponse; mapping via point-in-polygon
                    test (§7.5); if the test finds no containing boundary, a
                    data integrity failure is assumed and NotFoundResponse is
                    returned (§7.5)

    All element lists in LocationValidation are sorted to hierarchical order
    (§7.1).
    """
    if result.outcome == "not_found":
        return NotFoundResponse()

    location_validation = _build_location_validation(result.state)

    if result.outcome == "invalid":
        # Non-conforming result — no matched GIS record, so geometric mapping
        # selection (§7.5) is not applicable. Use civic coverage lookup (§3.5.2)
        # to select the most specific boundary for the validated admin prefix,
        # falling back to the Gate 0 candidate (§3.2) if unavailable.
        default_mapping_returned = False
        mapping = []

        if civic_coverage_lookup is not None and address is not None:
            entry = civic_coverage_lookup(
                country=address.country if "ca:country" in result.state.valid else None,
                a1=address.a1     if "ca:A1"      in result.state.valid else None,
                a2=address.a2     if "ca:A2"      in result.state.valid else None,
                a3=address.a3     if "ca:A3"      in result.state.valid else None,
                a4=address.a4     if "ca:A4"      in result.state.valid else None,
                a5=address.a5     if "ca:A5"      in result.state.valid else None,
            )
            if entry is not None and entry.boundary is not None:
                mapping = [_boundary_to_mapping(entry.boundary)]

        if not mapping:
            if default_mapping_factory is not None:
                mapping = [default_mapping_factory(service_urn)]
                default_mapping_returned = True

        return LocationValidationResponse(
            mapping=mapping,
            location_validation=location_validation,
            default_mapping_returned=default_mapping_returned,
        )

    # outcome == "match" — derive representative point for §7.5 selection
    record = result.record
    if result.layer == "SSAP":
        # SSAP: the address point geometry is the representative point (§7.5)
        point: Optional[Point] = getattr(record, "geometry", None)
    else:
        # RCL: perpendicular offset 0.0001° toward the determined side (§7.5)
        geom = getattr(record, "geometry", None)
        point = (
            _rcl_representative_point(geom, result.side)
            if result.side is not None
            else None
        )

    mappings = _select_mappings(point, boundaries)

    if not mappings:
        # Point-in-polygon found no containing boundary (§7.5): this indicates
        # a data integrity failure between the coverage region and service
        # boundary data. Return notFound rather than a validation result.
        return NotFoundResponse()

    complete_record = (
        record
        if result.layer == "SSAP" and return_additional_location in ("complete", "any")
        else None
    )
    return LocationValidationResponse(
        mapping=mappings,
        location_validation=location_validation,
        complete_location_record=complete_record,
    )


def unavailable() -> LocationValidationUnavailableResponse:
    """
    Return a locationValidationUnavailable response for system-level failures
    where the LVF temporarily cannot fulfill the validation request (§7.4).
    """
    return LocationValidationUnavailableResponse()
