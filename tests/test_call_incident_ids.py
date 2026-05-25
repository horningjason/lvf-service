"""Tests for the Call/Incident Identifier LoST extension (NENA-STA-010.3.1 §3.4.10.4)."""
from __future__ import annotations

import logging

import pytest
from lxml import etree

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
    callId="test-call-1" incidentTrackingId="test-incident-2"/>
</findService>"""

_XML_WITHOUT_IDS = b"""<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country><A1>ND</A1><A2>Burleigh County</A2>
      <RD>Test</RD><STS>Street</STS><HNO>123</HNO>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>"""


def test_extract_ids_present():
    """_extract_call_incident_ids returns both IDs when the extension element is present."""
    from src.lost.find_service import _extract_call_incident_ids

    root = etree.fromstring(_XML_WITH_IDS)
    call_id, incident_id = _extract_call_incident_ids(root)
    assert call_id == "test-call-1"
    assert incident_id == "test-incident-2"


def test_extract_ids_absent():
    """_extract_call_incident_ids returns (None, None) when the extension element is absent."""
    from src.lost.find_service import _extract_call_incident_ids

    root = etree.fromstring(_XML_WITHOUT_IDS)
    call_id, incident_id = _extract_call_incident_ids(root)
    assert call_id is None
    assert incident_id is None


def test_debug_log_emitted(caplog, monkeypatch):
    """DEBUG log line appears in handle_find_service() when extension is present."""
    monkeypatch.delenv("LVF_GPKG_PATH", raising=False)
    monkeypatch.delenv("LVF_PARENT_URI", raising=False)

    import src.lost.find_service as _fs

    _fs.initialize()  # routing-only mode — no GIS I/O

    with caplog.at_level(logging.DEBUG, logger="src.lost.find_service"):
        _fs.handle_find_service(_XML_WITH_IDS)

    assert any(
        "test-call-1" in r.message and "test-incident-2" in r.message
        for r in caplog.records
    ), f"Expected debug log with IDs not found. Records: {[r.message for r in caplog.records]}"
