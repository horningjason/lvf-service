"""
Core data models for the LVF civic address validation service.

Spec: NG9-1-1 LVF Civic Address Validation Algorithm Specification
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from shapely.geometry import LineString, MultiPolygon, Point, Polygon


# ---------------------------------------------------------------------------
# PIDF-LO Civic Address (Input)
# ---------------------------------------------------------------------------

class CivicAddress(BaseModel):
    """
    PIDF-LO civic address submitted in a LoST findService request.

    All fields are Optional[str] to preserve the omitted-vs-empty distinction
    required by INF-027 §2.5.7:
      - None  → element absent (omitted tag)
      - ""    → element present but empty

    Elements are grouped and ordered by the 33-position evaluation hierarchy.
    The five always-unchecked elements are included for completeness but are
    never passed to the progressive filter.

    Retired elements (A6, LMK, LMKP, POBOX, ADDCODE, RDSEC, RDBR, RDSUBBR,
    UNIT, NAM) are excluded per NENA-STA-004.2-2024 §1.4.
    """

    # Place name elements (hierarchy positions 1–6)
    country: Optional[str] = None   # ca:country  — Gate 1 required
    a1:      Optional[str] = None   # ca:A1       — Gate 1 required
    a2:      Optional[str] = None   # ca:A2       — Gate 1 required
    a3:      Optional[str] = None   # ca:A3
    a4:      Optional[str] = None   # ca:A4
    a5:      Optional[str] = None   # ca:A5

    # Street name elements (hierarchy positions 7–14)
    rd:   Optional[str] = None      # ca:RD   — Gate 1 required
    prm:  Optional[str] = None      # cae:PRM
    prd:  Optional[str] = None      # ca:PRD
    stp:  Optional[str] = None      # cae:STP
    stps: Optional[str] = None      # cdx2:STPS
    sts:  Optional[str] = None      # ca:STS
    pod:  Optional[str] = None      # ca:POD
    pom:  Optional[str] = None      # cae:POM

    # Address number elements (hierarchy positions 15–18)
    hno: Optional[str] = None       # ca:HNO  — Gate 1 required; integer value as string
    hnp: Optional[str] = None       # cae:HNP
    hns: Optional[str] = None       # ca:HNS  — RCL unchecked
    mp:  Optional[str] = None       # cae:MP  — always unchecked (§6.5.1)

    # Named location elements (hierarchy positions 19–30; all RCL unchecked)
    site:         Optional[str] = None  # cdx2:SITE
    subsite:      Optional[str] = None  # cdx2:SUBSITE
    bld:          Optional[str] = None  # ca:BLD (Structure)
    wing:         Optional[str] = None  # cdx2:WING
    flr:          Optional[str] = None  # ca:FLR
    unit_pretype: Optional[str] = None  # cdx2:UNIT_PRETYPE
    unit_value:   Optional[str] = None  # cdx2:UNIT_VALUE
    room:         Optional[str] = None  # ca:ROOM
    section:      Optional[str] = None  # cdx2:SECTION
    row:          Optional[str] = None  # cdx2:ROW
    seat:         Optional[str] = None  # ca:SEAT
    pn:           Optional[str] = None  # cae:PN (Location Marker per STA-004.2 §3.4.15)

    # Postal elements (hierarchy positions 31–33)
    pcn: Optional[str] = None       # ca:PCN
    pc:  Optional[str] = None       # ca:PC
    pce: Optional[str] = None       # cae:PCE — RCL unchecked

    # Always-unchecked elements (never enter the progressive filter)
    dt:  Optional[str] = None       # cdx2:DT  — Direction of Travel
    hnc: Optional[str] = None       # cdx2:HNC — Address Number Complete
    loc: Optional[str] = None       # ca:LOC   — Additional Location Information
    plc: Optional[str] = None       # ca:PLC   — Place Type


# ---------------------------------------------------------------------------
# Element hierarchy metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ElementInfo:
    """
    Metadata for one PIDF-LO element as it participates in the algorithm.

    Positions 0–32 in ELEMENT_HIERARCHY correspond to the 33-element evaluable
    sequence. Positions 33–36 are the always-unchecked elements,
    which gate2.py skips entirely.
    """
    civic_address_field: str    # attribute name on CivicAddress
    pidf_lo: str                # namespaced element name used in RFC 5222 response lists
    always_unchecked: bool = False  # skip on all layers, add to <unchecked>
    rcl_unchecked: bool = False     # skip on RCL only, add to <unchecked>
    null_unchecked: bool = False    # null GIS field → element unchecked (not invalid)


ELEMENT_HIERARCHY: tuple[ElementInfo, ...] = (
    # Place name
    ElementInfo("country",       "ca:country"),
    ElementInfo("a1",            "ca:A1"),
    ElementInfo("a2",            "ca:A2"),
    ElementInfo("a3",            "ca:A3"),
    ElementInfo("a4",            "ca:A4"),
    ElementInfo("a5",            "ca:A5"),
    # Street name
    ElementInfo("rd",            "ca:RD"),
    ElementInfo("prm",           "cae:PRM"),
    ElementInfo("prd",           "ca:PRD"),
    ElementInfo("stp",           "cae:STP"),
    ElementInfo("stps",          "cdx1:STPS"),
    ElementInfo("sts",           "ca:STS"),
    ElementInfo("pod",           "ca:POD"),
    ElementInfo("pom",           "cae:POM"),
    # Address number
    ElementInfo("hno",           "ca:HNO"),
    ElementInfo("hnp",           "cae:HNP"),
    ElementInfo("hns",           "ca:HNS",           rcl_unchecked=True),
    # Named location — all RCL unchecked
    ElementInfo("site",          "cdx2:SITE",         rcl_unchecked=True),
    ElementInfo("subsite",       "cdx2:SUBSITE",      rcl_unchecked=True),
    ElementInfo("bld",           "ca:BLD",            rcl_unchecked=True),
    ElementInfo("wing",          "cdx2:WING",         rcl_unchecked=True),
    ElementInfo("flr",           "ca:FLR",            rcl_unchecked=True),
    ElementInfo("unit_pretype",  "cdx2:UNIT_PRETYPE", rcl_unchecked=True),
    ElementInfo("unit_value",    "cdx2:UNIT_VALUE",   rcl_unchecked=True),
    ElementInfo("room",          "ca:ROOM",           rcl_unchecked=True),
    ElementInfo("section",       "cdx2:SECTION",      rcl_unchecked=True),
    ElementInfo("row",           "cdx2:ROW",          rcl_unchecked=True),
    ElementInfo("seat",          "ca:SEAT",           rcl_unchecked=True),
    ElementInfo("pn",            "cae:PN",            rcl_unchecked=True),
    # Postal
    ElementInfo("pcn",           "ca:PCN",           null_unchecked=True),
    ElementInfo("pc",            "ca:PC",            null_unchecked=True),
    ElementInfo("pce",           "cdx2:PCE",           rcl_unchecked=True),
    # Always-unchecked (outside the 33-position filter sequence)
    ElementInfo("dt",            "cdx2:DT",           always_unchecked=True),
    ElementInfo("hnc",           "cdx2:HNC",          always_unchecked=True),
    ElementInfo("loc",           "ca:LOC",            always_unchecked=True),
    ElementInfo("plc",           "ca:PLC",            always_unchecked=True),
    ElementInfo("mp",            "cae:MP",            always_unchecked=True),
)



# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class ValidationRequest(BaseModel):
    """
    A LoST findService request as received by the LVF.

    service_urn maps to the <service> element in the LoST request.
    Expected to be urn:service:sos or urn:service:test.sos per NENA-STA-010 §3.2.
    validate_location mirrors the validateLocation attribute on <findService>.
    """
    service_urn:       str
    civic_address:     CivicAddress
    validate_location: str = "false"


# ---------------------------------------------------------------------------
# GIS Layer Records (STA-006.3 standardized field names)
# ---------------------------------------------------------------------------

class SSAPRecord(BaseModel):
    """
    A SiteStructureAddressPoint record from the provisioned GIS layer.

    Field names match STA-006.3 standardized names exactly. The LVF must
    reference only these standardized names — no field-name mapping or
    configuration is permitted (STA-006.3 §3.2).
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Administrative
    country: Optional[str] = None
    a1:      Optional[str] = None
    a2:      Optional[str] = None
    a3:      Optional[str] = None
    a4:      Optional[str] = None
    a5:      Optional[str] = None

    # Street name
    st_name:   Optional[str] = None
    st_premod: Optional[str] = None
    st_predir: Optional[str] = None
    st_pretyp: Optional[str] = None
    st_presep: Optional[str] = None
    st_postyp: Optional[str] = None
    st_posdir: Optional[str] = None
    st_posmod: Optional[str] = None

    # Address number — exact integer comparison against Add_Number
    add_number: Optional[int] = None
    addnum_pre: Optional[str] = None
    addnum_suf: Optional[str] = None
    distmarker: Optional[str] = None

    # Named location
    site:       Optional[str] = None
    subsite:    Optional[str] = None
    structure:  Optional[str] = None    # ca:BLD maps to STA-006.3 field 'Structure'
    wing:       Optional[str] = None
    floor:      Optional[str] = None
    unitpretyp: Optional[str] = None
    unitvalue:  Optional[str] = None
    room:       Optional[str] = None
    section:    Optional[str] = None
    row:        Optional[str] = None
    seat:       Optional[str] = None
    locmarker:  Optional[str] = None

    # Postal
    post_comm:  Optional[str] = None
    post_code:  Optional[str] = None
    postcodeex: Optional[str] = None

    # Temporal validity (STA-006.3 §3.8)
    effective: Optional[str] = None
    expire:    Optional[str] = None

    geometry: Optional[Point] = None


class RCLRecord(BaseModel):
    """
    A RoadCenterLine record from the provisioned GIS layer.

    Administrative and postal fields are side-specific (_L / _R). Street name
    fields are shared. Address number evaluation uses range + parity +
    validation flag logic, not exact match. Side determination during HNO
    evaluation governs which suffix is used for all subsequent side-specific
    comparisons.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Administrative — side-specific
    country_l: Optional[str] = None
    country_r: Optional[str] = None
    a1_l: Optional[str] = None
    a1_r: Optional[str] = None
    a2_l: Optional[str] = None
    a2_r: Optional[str] = None
    a3_l: Optional[str] = None
    a3_r: Optional[str] = None
    a4_l: Optional[str] = None
    a4_r: Optional[str] = None
    a5_l: Optional[str] = None
    a5_r: Optional[str] = None

    # Street name — shared across both sides
    st_name:   Optional[str] = None
    st_premod: Optional[str] = None
    st_predir: Optional[str] = None
    st_pretyp: Optional[str] = None
    st_presep: Optional[str] = None
    st_postyp: Optional[str] = None
    st_posdir: Optional[str] = None
    st_posmod: Optional[str] = None

    # Address number — range, parity (E/O/B), validation flags (Y/N)
    fromaddr_l: Optional[int] = None
    toaddr_l:   Optional[int] = None
    fromaddr_r: Optional[int] = None
    toaddr_r:   Optional[int] = None
    parity_l:   Optional[Literal["E", "O", "B", "Z"]] = None
    parity_r:   Optional[Literal["E", "O", "B", "Z"]] = None
    valid_l:    Optional[Literal["Y", "N"]] = None
    valid_r:    Optional[Literal["Y", "N"]] = None
    adnumpre_l: Optional[str] = None    # cae:HNP side-specific field
    adnumpre_r: Optional[str] = None

    # Postal — side-specific
    postcomm_l: Optional[str] = None
    postcomm_r: Optional[str] = None
    postcode_l: Optional[str] = None
    postcode_r: Optional[str] = None

    # Temporal validity (STA-006.3 §3.8)
    effective: Optional[str] = None
    expire:    Optional[str] = None

    geometry: Optional[LineString] = None
    fid:      Optional[int] = None   # GeoPackage feature ID (diagnostic use)
    nguid:    Optional[str] = None   # STA-006.3 NGUID


# ---------------------------------------------------------------------------
# Service Boundary
# ---------------------------------------------------------------------------

class ServiceBoundary(BaseModel):
    """
    A provisioned service boundary polygon associated with a ServiceURN.

    Gate 0 checks for a URN match. At response assembly the geometry is used
    in a point-in-polygon test to select the <mapping> element — the only use
    of geometry in the entire algorithm.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    service_urn:  str
    effective:    Optional[str] = None  # STA-006.3 §3.8 Effective DATETIME
    expires:      Optional[str] = None  # RFC 5222 §8.4.1 dateTime / STA-006.3 Expire
    last_updated: Optional[str] = None
    source:       Optional[str] = None
    source_id:    Optional[str] = None
    nguid:        Optional[str] = None  # STA-006.3 NGUID — used as sourceId on <mapping>
    agency_id:    Optional[str] = None  # STA-006.3 Agency_ID — used as source on <mapping>
    service_uri:  Optional[str] = None  # STA-006.3 ServiceURI — <uri> child of <mapping>
    service_num:  Optional[str] = None  # STA-006.3 ServiceNum — <serviceNumber> child of <mapping>
    display_name: Optional[str] = None  # STA-006.3 DsplayName — <displayName> child of <mapping>
    geometry:     Optional[Polygon | MultiPolygon] = None


# ---------------------------------------------------------------------------
# Gate 2 — Internal filter state
# ---------------------------------------------------------------------------

@dataclass
class FilterState:
    """
    Mutable state accumulated during the Gate 2 progressive filter.

    valid and unchecked hold PIDF-LO element names (e.g. 'ca:country') in
    accumulation order; response_assembly.py sorts them to hierarchical order
    before serialisation.

    At most one element ever appears in invalid — enforced by the
    stop-on-first-invalid rule.

    determined_side is set during HNO evaluation on RCL and governs which
    side-specific field suffix (_L or _R) all subsequent RCL comparisons use.
    """
    valid:           list[str] = field(default_factory=list)
    invalid:         Optional[str] = None
    unchecked:       list[str] = field(default_factory=list)
    determined_side: Optional[Literal["L", "R"]] = None
    terminal:        bool = False   # True after stop-on-first-invalid fires


@dataclass
class CompleteLocationData:
    """
    Payload passed from response_assembly to the XML serializer for completeLocation.

    Carries enough context to build the <rli:completeLocation> element for either
    an SSAP or RCL match. The serializer is responsible for suppression when the
    submission already contains all non-null GIS fields.
    """
    layer:          str             # "SSAP" or "RCL"
    record:         Any             # SSAPRecord or RCLRecord
    side:           Optional[str] = None   # RCL only: "L" or "R"
    address:        Optional[Any] = None   # CivicAddress (submitted)
    valid_pidf_lo:  Optional[list] = None  # pidf_lo names in <valid>


# ---------------------------------------------------------------------------
# Coverage Region
# ---------------------------------------------------------------------------

@dataclass
class CivicCoverageEntry:
    """
    One entry in the civic coverage region lookup table.
    Maps an admin prefix to a service boundary. A3-A5 may be None
    to represent wildcard matching at that level.
    """
    country:  str
    a1:       str
    a2:       str
    a3:       Optional[str] = None   # None = wildcard (matches any A3)
    a4:       Optional[str] = None   # None = wildcard (matches any A4)
    a5:       Optional[str] = None   # None = wildcard (matches any A5)
    boundary: Optional[ServiceBoundary] = None


# ---------------------------------------------------------------------------
# RFC 5222 Response Models
# ---------------------------------------------------------------------------

class LocationValidation(BaseModel):
    """
    The <locationValidation> element of a findServiceResponse (RFC 5222 §8.4.2).

    All lists are in hierarchical order. At most one element appears in invalid
    — the stop-on-first-invalid rule enforces this.
    """
    valid:     list[str] = Field(default_factory=list)
    invalid:   Optional[str] = None
    unchecked: list[str] = Field(default_factory=list)


class MappingElement(BaseModel):
    """
    A <mapping> element in the findServiceResponse (RFC 5222 §8.4.1).

    Selected at response assembly via point-in-polygon against provisioned
    service boundaries. Multiple instances are returned when the representative
    point falls within more than one boundary polygon.
    """
    service_urn:       str
    expires:           Optional[str] = None
    last_updated:      Optional[str] = None
    source:            Optional[str] = None
    source_id:         Optional[str] = None
    service_uri:       Optional[str] = None
    service_num:       Optional[str] = None
    display_name:      Optional[str] = None
    display_name_lang: Optional[str] = None


class BadRequestResponse(BaseModel):
    """Pre-Gate-0 failure — request does not conform to the LoST findService schema."""
    type: Literal["badRequest"] = "badRequest"
    message: Optional[str] = None


class ForbiddenResponse(BaseModel):
    """Request rejected — LVF only accepts requests with validateLocation='true'."""
    type: Literal["forbidden"] = "forbidden"


class ServiceNotImplementedResponse(BaseModel):
    """Gate 0 failure — no provisioned boundary matches the requested URN."""
    type: Literal["serviceNotImplemented"] = "serviceNotImplemented"


class LocationInvalidResponse(BaseModel):
    """Gate 1 failure — required PIDF-LO elements absent or empty."""
    type:    Literal["locationInvalid"] = "locationInvalid"
    message: Optional[str] = None


class NotFoundResponse(BaseModel):
    """Gate 2 terminal — filter yielded zero or ambiguous candidates."""
    type: Literal["notFound"] = "notFound"
    message: Optional[str] = None


class RedirectResponse(BaseModel):
    """Gate 2 out-of-coverage at admin level — redirect to a parent LVF (RFC 5222 §13.3)."""
    type: Literal["redirect"] = "redirect"
    target: str
    source: str
    message: str = "Location is outside this LVF's coverage area"


class LocationValidationUnavailableResponse(BaseModel):
    """System-level failure — LVF temporarily unable to fulfill the request."""
    type: Literal["locationValidationUnavailable"] = "locationValidationUnavailable"
    message: str = "LVF temporarily cannot fulfill validation request"


class LocationValidationResponse(BaseModel):
    """
    Successful findServiceResponse with a <locationValidation> element.

    A conforming result has no invalid element. A non-conforming result has
    exactly one. RFC 5222 §8.4.1 requires at least one <mapping> in both cases.
    """
    type:                    Literal["locationValidation"] = "locationValidation"
    mapping:                 list[MappingElement]
    location_validation:     LocationValidation
    revalidate_after:        Optional[str] = None   # planned-changes revalidate hint
    default_mapping_returned: bool = False  # True → emit <defaultMappingReturned> warning
    complete_location_record: Optional[Any] = None  # CompleteLocationData; Optional[Any] avoids importing it here and keeps the model light


FindServiceResponse = Annotated[
    BadRequestResponse
    | ForbiddenResponse
    | ServiceNotImplementedResponse
    | LocationInvalidResponse
    | NotFoundResponse
    | RedirectResponse
    | LocationValidationUnavailableResponse
    | LocationValidationResponse,
    Field(discriminator="type"),
]
"""Discriminated union of all RFC 5222 findServiceResponse outcomes."""
