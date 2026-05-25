"""Tests for ElementStateNotifier, ServiceStateNotifier, and DiscrepancyReport."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.notifications.element_state import (
    ElementState,
    ElementStateNotifier,
    notify_degraded,
    notify_restored,
    _notifier as _element_notifier,
)
from src.notifications.service_state import (
    ServiceState,
    ServiceStateNotifier,
    _notifier as _service_notifier,
)
from src.discrepancy.discrepancy_report import (
    GISProblem,
    LoSTProblem,
    LoSTQuery,
    ProblemSeverity,
    file_gis_dr,
    file_lost_dr,
    submit_discrepancy_report,
    _rate_limit_cache,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _fresh_element_notifier() -> ElementStateNotifier:
    n = ElementStateNotifier()
    return n


def _fresh_service_notifier() -> ServiceStateNotifier:
    return ServiceStateNotifier(service_name="TestService", service_id="test.example.com")


# ===========================================================================
# ElementStateNotifier tests
# ===========================================================================

class TestElementStateNotifier:
    def test_state_change_fires_subscriber(self):
        """State change fires subscriber with correct body."""
        notifier = _fresh_element_notifier()
        received: list[dict] = []

        notifier.subscribe(lambda body: received.append(body))

        async def _run():
            notifier.set_state(ElementState.ServiceDisruption, "test reason")

        asyncio.run(_run())

        assert len(received) == 1
        assert received[0]["state"] == "ServiceDisruption"
        assert received[0]["reason"] == "test reason"

    def test_no_op_same_state(self):
        """Setting the same state twice does not fire subscribers."""
        notifier = _fresh_element_notifier()
        received: list[dict] = []
        notifier.subscribe(lambda body: received.append(body))

        async def _run():
            notifier.set_state(ElementState.Normal, "")
            notifier.set_state(ElementState.Normal, "")

        asyncio.run(_run())
        # Both are "no-op" because default is already Normal with reason ""
        assert len(received) == 0

    def test_rate_limiting_queues_second_notification(self):
        """Two rapid state changes within 1s produce only one immediate notification."""
        notifier = _fresh_element_notifier()
        # Prime last_notified so the first notification fires immediately
        notifier._last_notified = 0.0
        received: list[dict] = []
        notifier.subscribe(lambda body: received.append(body))

        async def _run():
            notifier.set_state(ElementState.ServiceDisruption, "first")
            # Second call within 1s — should be rate-limited
            notifier._last_notified = time.monotonic()  # simulate recent fire
            notifier.set_state(ElementState.Down, "second")

        asyncio.run(_run())

        # Only the first fires immediately; the second is queued
        assert len(received) == 1
        assert received[0]["state"] == "ServiceDisruption"

    def test_get_notify_body_structure(self):
        """get_notify_body returns the expected JSON structure."""
        notifier = _fresh_element_notifier()
        notifier._state = ElementState.Overloaded
        notifier._reason = "high load"

        with patch.dict(os.environ, {"LVF_SERVER_URI": "test.lvf.example"}):
            body = notifier.get_notify_body()

        assert body["elementId"] == "test.lvf.example"
        assert body["state"] == "Overloaded"
        assert body["reason"] == "high load"

    def test_notify_degraded_sets_service_disruption(self):
        """notify_degraded() sets the module-level notifier to ServiceDisruption."""
        original_state = _element_notifier._state
        original_reason = _element_notifier._reason
        try:
            _element_notifier._state = ElementState.Normal
            _element_notifier._reason = ""
            notify_degraded("database")
            assert _element_notifier._state == ElementState.ServiceDisruption
            assert "database" in _element_notifier._reason
        finally:
            _element_notifier._state = original_state
            _element_notifier._reason = original_reason

    def test_notify_restored_sets_normal(self):
        """notify_restored() sets the module-level notifier to Normal."""
        original_state = _element_notifier._state
        original_reason = _element_notifier._reason
        try:
            _element_notifier._state = ElementState.ServiceDisruption
            _element_notifier._reason = "something broken"
            notify_restored()
            assert _element_notifier._state == ElementState.Normal
            assert _element_notifier._reason == "Component restored"
        finally:
            _element_notifier._state = original_state
            _element_notifier._reason = original_reason


# ===========================================================================
# ServiceStateNotifier tests
# ===========================================================================

class TestServiceStateNotifier:
    def test_state_change_fires_subscriber(self):
        """State change fires subscriber with correct body."""
        notifier = _fresh_service_notifier()
        received: list[dict] = []
        notifier.subscribe(lambda body: received.append(body))

        async def _run():
            notifier.set_state(ServiceState.Overloaded, "capacity exceeded")

        asyncio.run(_run())

        assert len(received) == 1
        assert received[0]["serviceState"]["state"] == "Overloaded"
        assert received[0]["serviceState"]["reason"] == "capacity exceeded"

    def test_no_op_same_state(self):
        """Setting the same state twice does not fire subscribers."""
        notifier = _fresh_service_notifier()
        received: list[dict] = []
        notifier.subscribe(lambda body: received.append(body))

        async def _run():
            notifier.set_state(ServiceState.Normal, "")
            notifier.set_state(ServiceState.Normal, "")

        asyncio.run(_run())
        assert len(received) == 0

    def test_rate_limiting_queues_second_notification(self):
        """Two rapid state changes within 1s produce only one immediate notification."""
        notifier = _fresh_service_notifier()
        notifier._last_notified = 0.0
        received: list[dict] = []
        notifier.subscribe(lambda body: received.append(body))

        async def _run():
            notifier.set_state(ServiceState.GoingDown, "maintenance")
            notifier._last_notified = time.monotonic()
            notifier.set_state(ServiceState.Down, "down")

        asyncio.run(_run())

        assert len(received) == 1
        assert received[0]["serviceState"]["state"] == "GoingDown"

    def test_get_notify_body_structure(self):
        """get_notify_body returns the expected JSON structure."""
        notifier = _fresh_service_notifier()
        notifier._state = ServiceState.Partial
        notifier._reason = "partial outage"

        body = notifier.get_notify_body()

        assert body["service"] == "TestService"
        assert body["name"] == "TestService"
        assert body["serviceId"] == "test.example.com"
        assert body["serviceState"]["state"] == "Partial"
        assert body["serviceState"]["reason"] == "partial outage"


# ===========================================================================
# Discrepancy Report tests
# ===========================================================================

class TestDiscrepancyReports:
    def setup_method(self):
        _rate_limit_cache.clear()

    def test_file_lost_dr_calls_submit(self):
        """file_lost_dr builds a LoSTDiscrepancyReport and calls submit."""
        submitted: list = []

        async def fake_submit(dr):
            submitted.append(dr)

        with patch(
            "src.discrepancy.discrepancy_report.submit_discrepancy_report",
            side_effect=fake_submit,
        ):
            asyncio.run(file_lost_dr(
                query=LoSTQuery.findService,
                request_xml="<req/>",
                response_xml="<resp/>",
                problem=LoSTProblem.BelievedValid,
            ))

        assert len(submitted) == 1
        dr = submitted[0]
        assert dr.report_type == "LoST"
        assert dr.query == LoSTQuery.findService
        assert dr.problem == LoSTProblem.BelievedValid
        assert dr.request == "<req/>"
        assert dr.response == "<resp/>"

    def test_file_gis_dr_calls_submit(self):
        """file_gis_dr builds a GISDiscrepancyReport and calls submit."""
        submitted: list = []

        async def fake_submit(dr):
            submitted.append(dr)

        with patch(
            "src.discrepancy.discrepancy_report.submit_discrepancy_report",
            side_effect=fake_submit,
        ):
            asyncio.run(file_gis_dr(
                problem=GISProblem.GeneralProvisioning,
                severity=ProblemSeverity.Severe,
                detail="GPKG parse error",
            ))

        assert len(submitted) == 1
        dr = submitted[0]
        assert dr.report_type == "GIS"
        assert dr.problem == GISProblem.GeneralProvisioning
        assert dr.problem_severity == ProblemSeverity.Severe
        assert dr.detail == "GPKG parse error"

    def test_rate_limiting_suppresses_duplicate_within_60s(self):
        """Second identical DR within 60s is suppressed by submit_discrepancy_report."""
        from src.discrepancy.discrepancy_report import (
            GISDiscrepancyReport, DiscrepancyReportBase,
        )
        import datetime

        logged: list = []

        def fake_log_info(msg, *args, **kwargs):
            logged.append(msg % args if args else msg)

        dr = GISDiscrepancyReport(
            resolution_uri="",
            report_type="GIS",
            discrepancy_report_submittal_timestamp=datetime.datetime.now(datetime.timezone.utc),
            discrepancy_report_id="urn:emergency:uid:drid:test-1",
            reporting_agency_name="test",
            reporting_contact_jcard="[]",
            problem_severity=ProblemSeverity.Moderate,
            problem_service="urn:service:sos",
            problem=GISProblem.Gap,
        )

        async def _run():
            await submit_discrepancy_report(dr)
            # Second call — same report_type + problem.value → rate-limited
            await submit_discrepancy_report(dr)

        env = {k: v for k, v in os.environ.items() if k != "LVF_DR_ENDPOINT"}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "src.discrepancy.discrepancy_report.log.info",
                side_effect=fake_log_info,
            ):
                asyncio.run(_run())

        # Only the first call should produce an info log with the DR payload
        dr_logs = [m for m in logged if "DiscrepancyReport" in m]
        assert len(dr_logs) == 1

    def test_http_failure_does_not_raise(self):
        """DR submission HTTP failure is logged but never raises."""
        async def _run():
            with patch.dict(os.environ, {"LVF_DR_ENDPOINT": "http://dr.example.com/Reports"}):
                with patch(
                    "httpx.AsyncClient.post",
                    side_effect=OSError("connection refused"),
                ):
                    await file_lost_dr(
                        query=LoSTQuery.findService,
                        request_xml="<req/>",
                        response_xml="<resp/>",
                        problem=LoSTProblem.OtherLoST,
                    )

        # Should complete without raising
        asyncio.run(_run())

    def test_no_http_when_endpoint_unset(self):
        """When LVF_DR_ENDPOINT is not set, no HTTP call is made."""
        env = {k: v for k, v in os.environ.items() if k != "LVF_DR_ENDPOINT"}

        async def _run():
            with patch.dict(os.environ, env, clear=True):
                with patch("httpx.AsyncClient.post") as mock_post:
                    await file_lost_dr(
                        query=LoSTQuery.findService,
                        request_xml="<req/>",
                        response_xml="<resp/>",
                        problem=LoSTProblem.BelievedValid,
                    )
                    mock_post.assert_not_called()

        asyncio.run(_run())


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
