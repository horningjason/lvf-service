"""LVF discrepancy report generation per NENA-STA-010.3.1 §4.9, §3.7.1, §3.7.5, §3.7.11."""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import time
import uuid
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProblemSeverity(Enum):
    Minor = "Minor"
    Moderate = "Moderate"
    Degraded = "Degraded"
    Impaired = "Impaired"
    Severe = "Severe"
    Critical = "Critical"


class LoSTQuery(Enum):
    findService = "findService"
    getServiceBoundary = "getServiceBoundary"
    listServices = "listServices"
    listServicesByLocation = "listServicesByLocation"


class LoSTProblem(Enum):
    BelievedValid = "BelievedValid"
    BelievedInvalid = "BelievedInvalid"
    NoSuchLocation = "NoSuchLocation"
    RouteIncorrect = "RouteIncorrect"
    MultipleMappings = "MultipleMappings"
    ServiceBoundaryIncorrect = "ServiceBoundaryIncorrect"
    ServiceNumberIncorrect = "ServiceNumberIncorrect"
    DataExpired = "DataExpired"
    IncorrectURI = "IncorrectURI"
    LocationErrorInError = "LocationErrorInError"
    OtherLoST = "OtherLoST"


class GISProblem(Enum):
    Gap = "Gap"
    Overlap = "Overlap"
    IncorrectLoST = "IncorrectLoST"
    BadGeometry = "BadGeometry"
    DuplicateAttribute = "DuplicateAttribute"
    OmittedField = "OmittedField"
    IncorrectDataType = "IncorrectDataType"
    AddressRange = "AddressRange"
    GeneralProvisioning = "GeneralProvisioning"
    MalformedURI = "MalformedURI"
    DisplayData = "DisplayData"
    OtherGIS = "OtherGIS"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class DiscrepancyReportBase:
    resolution_uri: str
    report_type: str  # "LoST" or "GIS"
    discrepancy_report_submittal_timestamp: datetime.datetime
    discrepancy_report_id: str
    reporting_agency_name: str
    reporting_contact_jcard: str
    problem_severity: ProblemSeverity
    problem_service: str
    problem_comments: Optional[str] = None
    reporting_agent_id: Optional[str] = None


@dataclasses.dataclass
class LoSTDiscrepancyReport(DiscrepancyReportBase):
    query: LoSTQuery = dataclasses.field(default=None)
    request: str = ""
    response: str = ""
    problem: LoSTProblem = dataclasses.field(default=None)


@dataclasses.dataclass
class GISDiscrepancyReport(DiscrepancyReportBase):
    problem: GISProblem = dataclasses.field(default=None)
    layer_ids: Optional[str] = None
    location: Optional[str] = None
    lost_uri: Optional[str] = None
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# jCard construction
# ---------------------------------------------------------------------------

_jcard_warned: bool = False


def _build_jcard() -> str:
    global _jcard_warned
    contact_name  = os.environ.get("LVF_DR_CONTACT_NAME", "")
    contact_email = os.environ.get("LVF_DR_CONTACT_EMAIL", "")

    if not _jcard_warned and (not contact_name or not contact_email):
        _jcard_warned = True
        if not contact_name:
            log.warning(
                "LVF_DR_CONTACT_NAME is not set — using 'LVF Administrator' in DR jCard"
            )
        if not contact_email:
            log.warning(
                "LVF_DR_CONTACT_EMAIL is not set — DR jCard contact email will be empty"
            )

    return json.dumps([
        "vcard",
        [
            ["fn",    {}, "text", contact_name  or "LVF Administrator"],
            ["email", {}, "text", contact_email or ""],
        ],
    ])


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_rate_limit_cache: dict[tuple, float] = {}
_RATE_LIMIT_SECONDS = 60.0


def _is_rate_limited(report_type: str, problem_value: str) -> bool:
    key = (report_type, problem_value)
    now = time.monotonic()
    last = _rate_limit_cache.get(key)
    if last is not None and (now - last) < _RATE_LIMIT_SECONDS:
        return True
    _rate_limit_cache[key] = now
    return False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _default_serializer(obj):
    if isinstance(obj, datetime.datetime):
        return obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _dr_to_dict(dr: DiscrepancyReportBase) -> dict:
    d = dataclasses.asdict(dr)
    # Convert enums in place
    for k, v in list(d.items()):
        if isinstance(v, Enum):
            d[k] = v.value
        elif isinstance(v, datetime.datetime):
            d[k] = v.strftime("%Y-%m-%dT%H:%M:%SZ")
    return d


# ---------------------------------------------------------------------------
# Core submission function
# ---------------------------------------------------------------------------

async def submit_discrepancy_report(dr: DiscrepancyReportBase) -> None:
    """Serialize and submit a discrepancy report. Never raises."""
    problem_value = (
        dr.problem.value if hasattr(dr, "problem") and dr.problem is not None else "unknown"
    )

    if _is_rate_limited(dr.report_type, problem_value):
        log.debug(
            "DR rate-limited (%s/%s) — suppressed within %ds window",
            dr.report_type, problem_value, _RATE_LIMIT_SECONDS,
        )
        return

    try:
        payload = json.dumps(_dr_to_dict(dr), default=_default_serializer, indent=2)
    except Exception as exc:
        log.warning("DR serialization failed: %s", exc)
        return

    log.info("DiscrepancyReport [%s/%s]: %s", dr.report_type, problem_value, payload)

    endpoint = os.environ.get("LVF_DR_ENDPOINT", "")
    if not endpoint:
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                endpoint,
                content=payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code in (200, 201, 202, 204):
            log.info(
                "DR submitted to %s — HTTP %d",
                endpoint, resp.status_code,
            )
        else:
            log.warning(
                "DR submission to %s returned HTTP %d",
                endpoint, resp.status_code,
            )
    except Exception as exc:
        log.warning("DR submission to %s failed: %s", endpoint, exc)


# ---------------------------------------------------------------------------
# Timestamp helper (uses NTP if available)
# ---------------------------------------------------------------------------

def _get_timestamp() -> datetime.datetime:
    try:
        from src.lost.find_service import _ntp_client  # lazy import to avoid circular
        if _ntp_client is not None:
            return _ntp_client.get_current_time()
    except (ImportError, AttributeError):
        pass
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

async def file_lost_dr(
    query: LoSTQuery,
    request_xml: str,
    response_xml: str,
    problem: LoSTProblem,
    severity: ProblemSeverity = ProblemSeverity.Moderate,
    comments: Optional[str] = None,
) -> None:
    dr = LoSTDiscrepancyReport(
        resolution_uri=os.environ.get("LVF_DR_RESOLUTION_URI", ""),
        report_type="LoST",
        discrepancy_report_submittal_timestamp=_get_timestamp(),
        discrepancy_report_id=f"urn:emergency:uid:drid:{uuid.uuid4()}",
        reporting_agency_name=os.environ.get("LVF_SERVER_URI", "lostserver.example.com"),
        reporting_contact_jcard=_build_jcard(),
        problem_severity=severity,
        problem_service="urn:service:sos",
        problem_comments=comments,
        reporting_agent_id=os.environ.get("LVF_AGENCY_ID") or None,
        query=query,
        request=request_xml,
        response=response_xml,
        problem=problem,
    )
    await submit_discrepancy_report(dr)


async def file_gis_dr(
    problem: GISProblem,
    severity: ProblemSeverity = ProblemSeverity.Moderate,
    layer_ids: Optional[str] = None,
    location: Optional[str] = None,
    detail: Optional[str] = None,
    comments: Optional[str] = None,
) -> None:
    dr = GISDiscrepancyReport(
        resolution_uri=os.environ.get("LVF_DR_RESOLUTION_URI", ""),
        report_type="GIS",
        discrepancy_report_submittal_timestamp=_get_timestamp(),
        discrepancy_report_id=f"urn:emergency:uid:drid:{uuid.uuid4()}",
        reporting_agency_name=os.environ.get("LVF_SERVER_URI", "lostserver.example.com"),
        reporting_contact_jcard=_build_jcard(),
        problem_severity=severity,
        problem_service="urn:service:sos",
        problem_comments=comments,
        reporting_agent_id=os.environ.get("LVF_AGENCY_ID") or None,
        problem=problem,
        layer_ids=layer_ids,
        location=location,
        detail=detail,
    )
    await submit_discrepancy_report(dr)
