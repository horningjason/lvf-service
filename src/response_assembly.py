"""
Response Assembly.

Converts a Gate2Result into a FindServiceResponse, including hierarchical
ordering of element lists and point-in-polygon mapping selection.
"""

from __future__ import annotations

import datetime
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

# Canonical position of each PIDF-LO element name for sorting
_PIDF_LO_ORDER: dict[str, int] = {
    e.pidf_lo: i for i, e in enumerate(ELEMENT_HIERARCHY)
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sort_by_hierarchy(names: list[str]) -> list[str]:
    """Sort PIDF-LO element names into hierarchical order."""
    return sorted(names, key=lambda n: _PIDF_LO_ORDER.get(n, 999))


def _build_location_validation(state) -> LocationValidation:
    """
    Assemble LocationValidation from a FilterState.

    All lists are sorted to hierarchical order. invalid is either None
    (conforming result) or a single element name (non-conforming).
    """
    return LocationValidation(
        valid=_sort_by_hierarchy(state.valid),
        invalid=state.invalid,
        unchecked=_sort_by_hierarchy(state.unchecked),
    )


def _boundary_to_mapping(b: ServiceBoundary, server_uri: str, display_name_lang: str) -> MappingElement:
    return MappingElement(
        service_urn=b.service_urn,
        expires=b.expires,
        last_updated=b.last_updated,
        source=server_uri,
        source_id=b.nguid or b.source_id,
        service_uri=b.service_uri,
        service_num=b.service_num,
        display_name=b.display_name,
        display_name_lang=display_name_lang,
    )


def _min_expire(*candidates: Optional[str]) -> str:
    """
    Return the earliest parseable ISO datetime from candidates, or
    'NO-EXPIRATION' if none qualify. Ignores None and 'NO-EXPIRATION' values.
    """
    earliest: Optional[datetime.datetime] = None
    for s in candidates:
        if not s or s == "NO-EXPIRATION":
            continue
        try:
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            if earliest is None or dt < earliest:
                earliest = dt
        except ValueError:
            pass
    return earliest.isoformat() if earliest is not None else "NO-EXPIRATION"


# ---------------------------------------------------------------------------
# Representative point and mapping selection
# ---------------------------------------------------------------------------

def _rcl_representative_point(
    line_geom,
    side: Literal["L", "R"],
) -> Optional[Point]:
    """
    Derive a representative point by offsetting 0.0001 degrees perpendicularly
    from the line midpoint toward the determined side.

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

    # Overall bearing from start to end
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
    server_uri: str,
    display_name_lang: str,
) -> list[MappingElement]:
    """
    Return a MappingElement for every boundary polygon that contains point.
    Multiple elements are returned when the point falls inside more than one
    polygon — per NENA-STA-010 the client MUST have local policy to handle
    this. Returns an empty list if point is None or no boundary contains it.
    """
    if point is None:
        return []
    return [
        _boundary_to_mapping(b, server_uri, display_name_lang)
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
    server_uri: str = "",
    display_name_lang: str = "en",
) -> FindServiceResponse:
    """
    Assemble a findServiceResponse from a Gate2Result and the list of service
    boundaries pre-filtered to the requested URN (Gate 0 already confirmed at
    least one matches).

    Outcome mapping:
        not_found → NotFoundResponse
        invalid   → LocationValidationResponse with invalid element; mapping
                    uses civic coverage lookup when available, falling back to
                    a synthetic default mapping (defaultMappingReturned warning
                    emitted) when lookup does not resolve
        match     → LocationValidationResponse; mapping via point-in-polygon
                    test; if the test finds no containing boundary, a data
                    integrity failure is assumed and NotFoundResponse is returned

    All element lists in LocationValidation are sorted to hierarchical order.
    """
    if result.outcome == "not_found":
        return NotFoundResponse()

    location_validation = _build_location_validation(result.state)

    if result.outcome == "invalid":
        # Non-conforming result — no matched GIS record, so geometric mapping
        # selection is not applicable. Use civic coverage lookup to select the
        # most specific boundary for the validated admin prefix, falling back
        # to the Gate 0 candidate if unavailable.
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
                mapping = [_boundary_to_mapping(entry.boundary, server_uri, display_name_lang)]

        if not mapping:
            if default_mapping_factory is not None:
                mapping = [default_mapping_factory(service_urn)]
                default_mapping_returned = True

        revalidate_after = _min_expire(*[m.expires for m in mapping])
        return LocationValidationResponse(
            mapping=mapping,
            location_validation=location_validation,
            revalidate_after=revalidate_after,
            default_mapping_returned=default_mapping_returned,
        )

    # outcome == "match" — derive representative point for point-in-polygon selection
    record = result.record
    if result.layer == "SSAP":
        # SSAP: the address point geometry is the representative point
        point: Optional[Point] = getattr(record, "geometry", None)
    else:
        # RCL: perpendicular offset 0.0001° toward the determined side
        geom = getattr(record, "geometry", None)
        point = (
            _rcl_representative_point(geom, result.side)
            if result.side is not None
            else None
        )

    mappings = _select_mappings(point, boundaries, server_uri, display_name_lang)

    if not mappings:
        # Point-in-polygon found no containing boundary: this indicates a data
        # integrity failure between the coverage region and service boundary
        # data. Return notFound rather than a validation result.
        return NotFoundResponse()

    complete_record = (
        record
        if result.layer == "SSAP" and return_additional_location == "complete"
        else None
    )
    revalidate_after = _min_expire(
        getattr(record, "expire", None),
        *[m.expires for m in mappings],
    )
    return LocationValidationResponse(
        mapping=mappings,
        location_validation=location_validation,
        revalidate_after=revalidate_after,
        complete_location_record=complete_record,
    )


def unavailable() -> LocationValidationUnavailableResponse:
    """
    Return a locationValidationUnavailable response for system-level failures
    where the LVF temporarily cannot fulfill the validation request.
    """
    return LocationValidationUnavailableResponse()
