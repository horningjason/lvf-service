"""Tests for DiscrepancyReport and the /health element/service state fields.

ElementStateNotifier/ServiceStateNotifier are now owned by i3_fe_core (see
i3-fe-core/tests/state/) — no need to duplicate that coverage here.
DiscrepancyReporting's own submission/rate-limiting behavior is likewise
covered by i3-fe-core's test suite; these tests exercise only what
discrepancy_report.py itself owns: building the LoST/GIS report_specific
blocks and driving the shared component through runtime_state.discrepancy.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import runtime_state
from src.discrepancy.discrepancy_report import (
    GISProblem,
    LoSTProblem,
    LoSTQuery,
    ProblemSeverity,
    file_gis_dr,
    file_lost_dr,
)


class FakeDiscrepancyReporting:
    """Stand-in for i3_fe_core.discrepancy.DiscrepancyReporting that records
    what discrepancy_report.py asks it to do, without any real HTTP I/O."""

    def __init__(self, submit_side_effect=None):
        self.built: list = []
        self.submitted: list = []
        self._submit_side_effect = submit_side_effect

    def build_report(self, **kwargs):
        report = SimpleNamespace(discrepancy_report_id="fake-id", **kwargs)
        self.built.append(report)
        return report

    async def submit(self, report, responder_uri):
        if self._submit_side_effect is not None:
            raise self._submit_side_effect
        self.submitted.append((report, responder_uri))


# ===========================================================================
# Discrepancy Report tests
# ===========================================================================

class TestDiscrepancyReports:
    def test_file_lost_dr_builds_and_submits(self):
        """file_lost_dr builds a LoSTDiscrepancyReport-typed report via the
        shared component and submits it when LVF_DR_ENDPOINT is configured."""
        fake = FakeDiscrepancyReporting()

        with patch.object(runtime_state, "discrepancy", fake):
            with patch.dict(os.environ, {"LVF_DR_ENDPOINT": "http://dr.example.com/Reports"}):
                asyncio.run(file_lost_dr(
                    query=LoSTQuery.findService,
                    request_xml="<req/>",
                    response_xml="<resp/>",
                    problem=LoSTProblem.BelievedValid,
                ))

        assert len(fake.built) == 1
        report = fake.built[0]
        assert report.report_type == "LoSTDiscrepancyReport"
        assert report.problem_service == "urn:service:sos"
        assert report.problem_severity == ProblemSeverity.Moderate.value
        assert report.report_specific == {
            "query": "findService",
            "request": "<req/>",
            "response": "<resp/>",
            "problem": "BelievedValid",
        }

        assert len(fake.submitted) == 1
        submitted_report, responder_uri = fake.submitted[0]
        assert submitted_report is report
        assert responder_uri == "http://dr.example.com"  # "/Reports" suffix stripped

    def test_file_gis_dr_builds_and_submits(self):
        """file_gis_dr builds a GISDiscrepancyReport-typed report, omitting
        unset optional fields from report_specific."""
        fake = FakeDiscrepancyReporting()

        with patch.object(runtime_state, "discrepancy", fake):
            with patch.dict(os.environ, {"LVF_DR_ENDPOINT": "http://dr.example.com"}):
                asyncio.run(file_gis_dr(
                    problem=GISProblem.GeneralProvisioning,
                    severity=ProblemSeverity.Severe,
                    detail="GPKG parse error",
                ))

        assert len(fake.built) == 1
        report = fake.built[0]
        assert report.report_type == "GISDiscrepancyReport"
        assert report.problem_severity == ProblemSeverity.Severe.value
        assert report.report_specific == {
            "problem": "GeneralProvisioning",
            "detail": "GPKG parse error",
        }

        assert len(fake.submitted) == 1
        _, responder_uri = fake.submitted[0]
        assert responder_uri == "http://dr.example.com"

    def test_no_submit_when_endpoint_unset(self):
        """When LVF_DR_ENDPOINT is not set, the report is still built (and
        logged) but never submitted through the shared component."""
        fake = FakeDiscrepancyReporting()
        env = {k: v for k, v in os.environ.items() if k != "LVF_DR_ENDPOINT"}

        with patch.object(runtime_state, "discrepancy", fake):
            with patch.dict(os.environ, env, clear=True):
                asyncio.run(file_lost_dr(
                    query=LoSTQuery.findService,
                    request_xml="<req/>",
                    response_xml="<resp/>",
                    problem=LoSTProblem.BelievedValid,
                ))

        assert len(fake.built) == 1
        assert fake.submitted == []

    def test_no_op_when_discrepancy_component_unset(self):
        """When the shared DiscrepancyReporting component has not been
        initialized (runtime_state.discrepancy is None), filing a DR is a
        silent no-op — never raises."""
        with patch.object(runtime_state, "discrepancy", None):
            asyncio.run(file_lost_dr(
                query=LoSTQuery.findService,
                request_xml="<req/>",
                response_xml="<resp/>",
                problem=LoSTProblem.OtherLoST,
            ))

    def test_submit_failure_does_not_raise(self):
        """A submission failure inside the shared component is logged but
        never propagates to the caller."""
        fake = FakeDiscrepancyReporting(submit_side_effect=OSError("connection refused"))

        with patch.object(runtime_state, "discrepancy", fake):
            with patch.dict(os.environ, {"LVF_DR_ENDPOINT": "http://dr.example.com"}):
                asyncio.run(file_lost_dr(
                    query=LoSTQuery.findService,
                    request_xml="<req/>",
                    response_xml="<resp/>",
                    problem=LoSTProblem.OtherLoST,
                ))

        assert len(fake.built) == 1


# ===========================================================================
# /health endpoint includes elementState and serviceState
# ===========================================================================

class TestHealthEndpoint:
    def test_health_includes_element_and_service_state(self):
        """/health response includes element_state and service_state fields."""
        from fastapi.testclient import TestClient
        from src.server import app

        # Patch initialize to avoid loading real GIS data
        with patch("src.lost.find_service.initialize"):
            client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "element_state" in data
        assert "service_state" in data
        assert isinstance(data["element_state"], str)
        assert isinstance(data["service_state"], str)
