"""
Integration tests for LVF recursive mode (RFC 5222 §10).

These tests make REAL HTTP calls to the parent LVF configured via LVF_PARENT_URI.
The parent LVF must be running and reachable on the network for these tests to pass.
All tests are automatically skipped when LVF_PARENT_URI is not set.

Run with:
    pytest tests/test_recursion.py -v

Prerequisites:
    - LVF_PARENT_URI set to the base URL of a running parent LVF instance
      (e.g. LVF_PARENT_URI=http://192.168.1.69:8001 in .env)
    - The parent LVF must be provisioned and accepting requests at /lost
    - The test addresses (Cass County, ND) must be outside THIS LVF's coverage
      region so that the OOC redirect/recurse path is exercised
    - The provisioned address tests (G2-RECURSE-KNOWN-*) require the parent to
      hold the specific Cass County SSAP record listed below (NGUID
      {D2E0AD84-A4B9-4282-A83A-ECA05AE12E6A}, 1522 8th Street North, Fargo)
"""

import pytest
from lxml import etree

from src.server import handle_find_service, initialize, _parent_uri, _server_uri

_NS_LOST = "urn:ietf:params:xml:ns:lost1"

# Skip every test in this module when no parent LVF is configured.
pytestmark = pytest.mark.skipif(
    not _parent_uri,
    reason="LVF_PARENT_URI not configured — set it in .env to run recursion integration tests",
)

# Generic OOC address (Cass County, fictitious street) — verifies connectivity
# and basic structure without depending on a specific provisioned record.
_OOC_RECURSIVE = b"""<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true" recursive="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country>
      <A1>ND</A1>
      <A2>Cass County</A2>
      <A3>Fargo</A3>
      <RD>Main</RD>
      <STS>Avenue</STS>
      <HNO>100</HNO>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>"""

# Provisioned Cass County SSAP record — 1522 8th Street North, Fargo, ND 58102.
# NGUID: {D2E0AD84-A4B9-4282-A83A-ECA05AE12E6A}
# Source: casscountynd.gov, effective 2023-08-18.
# The parent LVF holds this record, so it should return a findServiceResponse
# with all submitted elements in <valid>. Used to exercise the full recursion
# path including <via> prepending and validation result forwarding.
_KNOWN_1522_8TH = b"""<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true" recursive="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country>
      <A1>ND</A1>
      <A2>Cass County</A2>
      <A3>Fargo</A3>
      <RD>8th</RD>
      <STS>Street</STS>
      <POD>North</POD>
      <HNO>1522</HNO>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>"""

# Same provisioned record with postal community and ZIP added.
_KNOWN_1522_8TH_WITH_PC = b"""<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true" recursive="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country>
      <A1>ND</A1>
      <A2>Cass County</A2>
      <A3>Fargo</A3>
      <RD>8th</RD>
      <STS>Street</STS>
      <POD>North</POD>
      <HNO>1522</HNO>
      <PCN>Fargo</PCN>
      <PC>58102</PC>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>"""


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    initialize()


def _assert_parent_reachable(root: etree._Element) -> None:
    """Fail with a clear message if the response indicates the parent was unreachable."""
    if root.tag != f"{{{_NS_LOST}}}errors":
        return
    err_types = [child.tag.split("}")[-1] for child in root]
    if any(t in ("serverError", "serverTimeout") for t in err_types):
        pytest.fail(
            f"Parent LVF at {_parent_uri.rstrip('/')}/lost was unreachable "
            f"(got {err_types}). Ensure the parent is running and LVF_PARENT_URI is correct."
        )


def test_recursive_call_returns_lost_response():
    """
    Outbound recursive call to the parent completes and returns parseable LoST XML.

    Requires: parent LVF running at LVF_PARENT_URI/lost.
    """
    result = handle_find_service(_OOC_RECURSIVE)
    root = etree.fromstring(result)
    _assert_parent_reachable(root)
    assert _NS_LOST in root.tag, \
        f"Expected a LoST namespace element, got {root.tag}"


def test_recursive_response_prepends_our_via():
    """
    When the parent returns a findServiceResponse, our server's <via> appears
    first in the response <path>.

    Requires: parent LVF running at LVF_PARENT_URI/lost AND the parent
    returns a findServiceResponse (i.e. it has data for the submitted address
    or produces a notFound/locationValidation outcome rather than a redirect).
    """
    result = handle_find_service(_OOC_RECURSIVE)
    root = etree.fromstring(result)
    _assert_parent_reachable(root)

    if root.tag != f"{{{_NS_LOST}}}findServiceResponse":
        outcome = root.tag.split("}")[-1]
        pytest.skip(
            f"Parent returned <{outcome}> instead of <findServiceResponse> — "
            "cannot verify <via> prepending. This is expected if the parent also "
            "redirects or returns an error for Cass County."
        )

    path_el = root.find(f".//{{{_NS_LOST}}}path")
    assert path_el is not None, "<findServiceResponse> must contain <path>"

    vias = path_el.findall(f"{{{_NS_LOST}}}via")
    assert len(vias) >= 1, "<path> must contain at least one <via>"
    assert vias[0].get("source") == _server_uri, (
        f"First <via> must identify this server ({_server_uri}), "
        f"got '{vias[0].get('source')}'"
    )


# ---------------------------------------------------------------------------
# Tests against the known provisioned SSAP record — 1522 8th St N, Fargo
# NGUID: {D2E0AD84-A4B9-4282-A83A-ECA05AE12E6A}
# ---------------------------------------------------------------------------

def _get_valid_elements(root: etree._Element) -> set[str]:
    """Return the set of element QNames from the <valid> element, or empty set."""
    lv = root.find(f".//{{{_NS_LOST}}}locationValidation")
    if lv is None:
        return set()
    valid_el = lv.find(f"{{{_NS_LOST}}}valid")
    if valid_el is None or not valid_el.text:
        return set()
    return set(valid_el.text.split())


def test_recursive_known_address_returns_valid():
    """
    1522 8th Street North, Fargo — parent has this SSAP record and should
    return locationValidation with all submitted elements in <valid>.

    Requires: parent LVF running and provisioned with Cass County GIS data
    including NGUID {D2E0AD84-A4B9-4282-A83A-ECA05AE12E6A}.
    """
    result = handle_find_service(_KNOWN_1522_8TH)
    root = etree.fromstring(result)
    _assert_parent_reachable(root)

    assert root.tag == f"{{{_NS_LOST}}}findServiceResponse", (
        f"Expected findServiceResponse for provisioned address, got "
        f"<{root.tag.split('}')[-1]}>. "
        "Verify the parent holds NGUID {D2E0AD84-A4B9-4282-A83A-ECA05AE12E6A}."
    )

    valid = _get_valid_elements(root)
    expected = {"ca:country", "ca:A1", "ca:A2", "ca:A3", "ca:RD", "ca:STS", "ca:POD", "ca:HNO"}
    missing = expected - valid
    assert not missing, (
        f"Expected all submitted elements in <valid>, but {missing} were absent. "
        f"Full <valid> set: {valid}"
    )


def test_recursive_known_address_via_prepended():
    """
    1522 8th Street North, Fargo — our <via> must be the first element in
    the forwarded response <path>.

    Requires: parent LVF running and provisioned with Cass County GIS data.
    """
    result = handle_find_service(_KNOWN_1522_8TH)
    root = etree.fromstring(result)
    _assert_parent_reachable(root)

    assert root.tag == f"{{{_NS_LOST}}}findServiceResponse", (
        f"Expected findServiceResponse, got <{root.tag.split('}')[-1]}>."
    )

    path_el = root.find(f".//{{{_NS_LOST}}}path")
    assert path_el is not None, "<findServiceResponse> must contain <path>"

    vias = path_el.findall(f"{{{_NS_LOST}}}via")
    assert len(vias) >= 1, "<path> must contain at least one <via>"
    assert vias[0].get("source") == _server_uri, (
        f"First <via> must be this server ({_server_uri}), "
        f"got '{vias[0].get('source')}'"
    )


def test_recursive_known_address_with_postal_elements():
    """
    1522 8th Street North, Fargo — with PCN and PC added to the submission.
    Both should appear in <valid> since they match the provisioned SSAP record
    (Post_Comm=Fargo, Post_Code=58102).

    Requires: parent LVF running and provisioned with Cass County GIS data.
    """
    result = handle_find_service(_KNOWN_1522_8TH_WITH_PC)
    root = etree.fromstring(result)
    _assert_parent_reachable(root)

    assert root.tag == f"{{{_NS_LOST}}}findServiceResponse", (
        f"Expected findServiceResponse, got <{root.tag.split('}')[-1]}>."
    )

    valid = _get_valid_elements(root)
    expected = {
        "ca:country", "ca:A1", "ca:A2", "ca:A3",
        "ca:RD", "ca:STS", "ca:POD", "ca:HNO",
        "ca:PCN", "ca:PC",
    }
    missing = expected - valid
    assert not missing, (
        f"Expected all submitted elements in <valid>, but {missing} were absent. "
        f"Full <valid> set: {valid}"
    )
