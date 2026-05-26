"""Tests for LostQueryLogEvent / LostResponseLogEvent wiring (NENA-STA-010.3.1 §4.12.3)."""
from __future__ import annotations

import importlib
import json
import logging


_XML_VALID = b"""<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country><A1>ND</A1><A2>Burleigh County</A2>
      <RD>Test</RD><STS>Street</STS><HNO>123</HNO>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>"""

_XML_MALFORMED = b"<not xml at all"

_XML_WITH_IDS = b"""<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country><A1>ND</A1><A2>Burleigh County</A2>
      <RD>Test</RD><STS>Street</STS><HNO>123</HNO>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
  <emergencyCallIncidentId xmlns="urn:emergency:xml:ns:lostExt:Ids"
    callId="call-99" incidentTrackingId="inc-42"/>
</findService>"""


def _setup(monkeypatch):
    monkeypatch.delenv("LVF_GPKG_PATH", raising=False)
    monkeypatch.delenv("LVF_PARENT_URI", raising=False)
    import src.lost.find_service as _fs
    _fs.initialize()
    return _fs


def test_query_and_response_events_emitted(monkeypatch, caplog):
    """A well-formed request produces both a query and a response log event at INFO."""
    _setup(monkeypatch)
    import src.lost.find_service as _fs

    with caplog.at_level(logging.INFO, logger="src.logging_events.logger"):
        _fs.handle_find_service(_XML_VALID)

    events = [r for r in caplog.records if r.message.startswith("log_event ")]
    assert len(events) == 2, f"Expected 2 log_event records, got {len(events)}: {[r.message for r in events]}"

    query_evt = json.loads(events[0].message[len("log_event "):])
    resp_evt  = json.loads(events[1].message[len("log_event "):])

    assert query_evt["logEventType"] == "LostQueryLogEvent"
    assert query_evt["direction"] == "incoming"
    assert query_evt["queryId"].startswith("urn:emergency:uid:queryid:")

    assert resp_evt["logEventType"] == "LostResponseLogEvent"
    assert resp_evt["direction"] == "outgoing"
    assert resp_evt["responseId"] == query_evt["queryId"]


def test_malformed_request_sets_malformed_query(monkeypatch, caplog):
    """A malformed XML request produces a query event with malformedQuery set."""
    _setup(monkeypatch)
    import src.lost.find_service as _fs

    with caplog.at_level(logging.INFO, logger="src.logging_events.logger"):
        _fs.handle_find_service(_XML_MALFORMED)

    events = [r for r in caplog.records if r.message.startswith("log_event ")]
    assert len(events) == 2

    query_evt = json.loads(events[0].message[len("log_event "):])
    resp_evt  = json.loads(events[1].message[len("log_event "):])

    assert query_evt["malformedQuery"] is not None
    assert resp_evt["responseStatus"] == "400"


def test_call_incident_ids_propagated_to_events(monkeypatch, caplog):
    """callId and incidentTrackingId from the extension element appear in log events."""
    _setup(monkeypatch)
    import src.lost.find_service as _fs

    with caplog.at_level(logging.INFO, logger="src.logging_events.logger"):
        _fs.handle_find_service(_XML_WITH_IDS)

    events = [r for r in caplog.records if r.message.startswith("log_event ")]
    assert events, "No log events emitted"

    query_evt = json.loads(events[0].message[len("log_event "):])
    assert query_evt.get("callId") == "call-99"
    assert query_evt.get("incidentId") == "inc-42"


def test_camel_case_keys_in_json(monkeypatch, caplog):
    """All JSON keys in emitted events use lowerCamelCase."""
    _setup(monkeypatch)
    import src.lost.find_service as _fs

    with caplog.at_level(logging.INFO, logger="src.logging_events.logger"):
        _fs.handle_find_service(_XML_VALID)

    for record in caplog.records:
        if not record.message.startswith("log_event "):
            continue
        evt = json.loads(record.message[len("log_event "):])
        for key in evt:
            assert "_" not in key, f"Key {key!r} contains underscore — expected camelCase"


def test_agency_id_warning_when_unset(monkeypatch, caplog):
    """A WARNING is logged at module load time when LVF_AGENCY_ID is not set."""
    monkeypatch.delenv("LVF_AGENCY_ID", raising=False)
    # Suppress load_dotenv during reload so .env doesn't repopulate LVF_AGENCY_ID.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
    import src.logging_events.logger as logger_mod
    with caplog.at_level(logging.WARNING, logger="src.logging_events.logger"):
        reloaded = importlib.reload(logger_mod)
    assert reloaded._agency_id == ""
    assert any("LVF_AGENCY_ID" in r.message for r in caplog.records if r.levelno == logging.WARNING)
