"""LVF discrepancy report generation per NENA-STA-010.3.1 §4.9, §3.7.1, §3.7.5, §3.7.11.

Report submission — the §3.7.1 reporting prolog (discrepancyReportId, jCard,
agent id), the §2.3 submittal timestamp, and the §3.7 SHOULD-level rate limit
on similar reports — is owned by i3_fe_core.discrepancy.DiscrepancyReporting,
shared via runtime_state.discrepancy (see src/core_components.py). This module
keeps only the LVF domain enums and the two convenience functions that build
and file a LoSTDiscrepancyReport (§3.7.5) / GISDiscrepancyReport (§3.7.11).
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Optional

from i3_fe_core.discrepancy import PROBLEM_SEVERITIES, DiscrepancyReport

from src import runtime_state

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


assert {s.value for s in ProblemSeverity} == set(PROBLEM_SEVERITIES), (
    "ProblemSeverity has drifted from i3_fe_core.discrepancy.PROBLEM_SEVERITIES (§3.7.1/§10.34)"
)


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
# Submission
# ---------------------------------------------------------------------------

async def _submit(report: DiscrepancyReport) -> None:
    """Log locally and, when LVF_DR_ENDPOINT is configured, submit through the
    shared DiscrepancyReporting component. Never raises."""
    log.info(
        "DiscrepancyReport [%s/%s]: %s",
        report.report_type, report.problem_severity, report.report_specific,
    )

    dr_component = runtime_state.discrepancy
    endpoint = os.environ.get("LVF_DR_ENDPOINT", "")
    if dr_component is None or not endpoint:
        return

    # LVF_DR_ENDPOINT has historically named the full .../Reports submission
    # URL; DiscrepancyReporting.submit() appends "/Reports" to responder_uri
    # itself, so strip it back off here if present.
    responder_uri = endpoint[: -len("/Reports")] if endpoint.endswith("/Reports") else endpoint

    try:
        await dr_component.submit(report, responder_uri)
    except Exception as exc:
        log.warning("DR submission to %s failed: %s", responder_uri, exc)


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
    """File a LoSTDiscrepancyReport (§3.7.5). Never raises."""
    dr_component = runtime_state.discrepancy
    if dr_component is None:
        log.debug("DR component not initialized — LoST DR not filed")
        return

    try:
        report: DiscrepancyReport = dr_component.build_report(
            report_type="LoSTDiscrepancyReport",
            problem_severity=severity.value,
            resolution_uri=os.environ.get("LVF_DR_RESOLUTION_URI", ""),
            problem_service="urn:service:sos",
            problem_comments=comments,
            report_specific={
                "query": query.value,
                "request": request_xml,
                "response": response_xml,
                "problem": problem.value,
            },
        )
    except Exception as exc:
        log.warning("Failed to build LoST DR: %s", exc)
        return

    await _submit(report)


async def file_gis_dr(
    problem: GISProblem,
    severity: ProblemSeverity = ProblemSeverity.Moderate,
    layer_ids: Optional[str] = None,
    location: Optional[str] = None,
    detail: Optional[str] = None,
    comments: Optional[str] = None,
) -> None:
    """File a GISDiscrepancyReport (§3.7.11). Never raises."""
    dr_component = runtime_state.discrepancy
    if dr_component is None:
        log.debug("DR component not initialized — GIS DR not filed")
        return

    report_specific = {"problem": problem.value}
    if layer_ids is not None:
        report_specific["layerIds"] = layer_ids
    if location is not None:
        report_specific["location"] = location
    if detail is not None:
        report_specific["detail"] = detail

    try:
        report: DiscrepancyReport = dr_component.build_report(
            report_type="GISDiscrepancyReport",
            problem_severity=severity.value,
            resolution_uri=os.environ.get("LVF_DR_RESOLUTION_URI", ""),
            problem_service="urn:service:sos",
            problem_comments=comments,
            report_specific=report_specific,
        )
    except Exception as exc:
        log.warning("Failed to build GIS DR: %s", exc)
        return

    await _submit(report)
