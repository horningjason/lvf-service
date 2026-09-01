"""Root-AMS -> Forest Guide push scope: only the profile that changed is re-pushed.

An inbound child pushMappings on a root-AMS node cascades: the node re-pushes
its own aggregate coverage to the Forest Guide.  That aggregate has two halves —
civic and geodetic-2d — and a child push touches at most one of them, so
re-pushing both is a wasted round trip and a spurious lastUpdated bump on the FG
for a region that did not change.

The scoping is on PROFILE, and these tests exist mostly to keep it that way.
The obvious-looking alternative — scope by the sourceId(s) the request changed —
is silently wrong: the sourceIds in an inbound push are the CHILD's, asserted in
the mapping it sent, while _push_coverage_to_fg() selects this node's OWN
aggregate entries by LVF_SYNC_SOURCE_ID_CIVIC / LVF_SYNC_SOURCE_ID_GEODETIC
(README § Root AMS Provisioning Files requires the provisioning file's source_id
to equal them).  Those two id spaces never intersect, so a sourceId filter
doesn't narrow the push — it suppresses it entirely, and the failure is invisible
because a suppressed push logs nothing.  test_civic_push_scopes_to_civic_only
fails under BOTH mistakes: zero pushes under the sourceId filter, two under an
unscoped loop.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from lxml import etree

from src import runtime_state
from src.federation import coverage as fed_coverage
from src.federation import sync as fed_sync

NS_SYNC = "urn:ietf:params:xml:ns:lostsync1"
NS_LOST = "urn:ietf:params:xml:ns:lost1"
NS_CA = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"

AMS_SOURCE = "root-ams.lvf.example.com"
AMS_CIVIC_ID = "{aaaaaaaa-0000-0000-0000-000000000001}"
AMS_GEODETIC_ID = "{aaaaaaaa-0000-0000-0000-000000000002}"

CHILD = "child1.lvf.example.com"
CHILD_CIVIC_ID = "{11111111-1111-1111-1111-111111111111}"
CHILD_GEODETIC_ID = "{11111111-1111-1111-1111-111111111112}"
CHILD_URI = "https://child1.lvf.example.com/lost"

FG_URI = "https://fg.example.com/lost"


class _NullLoggingClient:
    """runtime_state.logging_client is built during app startup; these tests
    drive the sync handlers directly, so stand in for it."""

    async def emit(self, event):
        return None


class _RecordingAsyncClient:
    """Stands in for httpx.AsyncClient, capturing every FG push body.

    Only the two methods _push_coverage_to_fg() uses are implemented: the async
    context manager protocol and post().
    """

    posts: list[tuple[str, bytes]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        type(self).posts.append((url, content))

        class _Resp:
            status_code = 200
            content = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<pushMappingsResponse xmlns="' + NS_SYNC + '"/>'
            ).encode()

        return _Resp()


def _ams_entry(profile: str) -> dict:
    """One half of this node's operator-provisioned aggregate coverage."""
    if profile == "civic":
        return {
            "source": AMS_SOURCE,
            "source_id": AMS_CIVIC_ID,
            "last_updated": "2026-01-01T00:00:00Z",
            "expires": "NO-EXPIRATION",
            "service": "urn:service:sos",
            "profile": "civic",
            "lost_server": "https://root-ams.lvf.example.com/lost",
            "civic_addresses": [{"country": "US", "a1": "ND", "a2": "BURLEIGH COUNTY"}],
        }
    return {
        "source": AMS_SOURCE,
        "source_id": AMS_GEODETIC_ID,
        "last_updated": "2026-01-01T00:00:00Z",
        "expires": "NO-EXPIRATION",
        "service": "urn:service:sos",
        "profile": "geodetic-2d",
        "lost_server": "https://root-ams.lvf.example.com/lost",
        "geodetic_geom_wkt": (
            "POLYGON ((-102.5 46.4, -100.0 46.4, -100.0 48.6, -102.5 48.6, -102.5 46.4))"
        ),
    }


@pytest.fixture
def ams_env(tmp_path, monkeypatch):
    """A root-AMS node with both aggregate halves provisioned and the FG stubbed."""
    monkeypatch.setenv("LVF_GPKG_PATH", str(tmp_path / "data.gpkg"))
    monkeypatch.setenv("LVF_SYNC_SOURCE_ID_CIVIC", AMS_CIVIC_ID)
    monkeypatch.setenv("LVF_SYNC_SOURCE_ID_GEODETIC", AMS_GEODETIC_ID)
    monkeypatch.setenv(
        "LVF_SYNC_ALLOWED_SOURCES",
        f"{CHILD}|{CHILD_CIVIC_ID},{CHILD}|{CHILD_GEODETIC_ID}",
    )
    monkeypatch.delenv("LVF_FOREST_GUIDE_MODE", raising=False)

    monkeypatch.setattr(runtime_state, "logging_client", _NullLoggingClient())
    monkeypatch.setattr(runtime_state, "_root_ams", True)
    monkeypatch.setattr(runtime_state, "_forest_guide_uri", FG_URI)
    monkeypatch.setattr(runtime_state, "_forest_guide_mode", False)
    monkeypatch.setattr(runtime_state, "_parent_uri", "")
    monkeypatch.setattr(fed_coverage, "_root_ams_active", True)
    # Post-_load_ams_provisioning() state: the aggregate lives in the coverage
    # store, which is where _push_coverage_to_fg() looks it up. The cache below
    # is what re-asserts it on top of the store on every coverage write (manual
    # provisioning always wins), which is how it survives the read-modify-write
    # in _with_coverage_write().
    monkeypatch.setattr(
        fed_coverage,
        "_child_coverage",
        [_ams_entry("civic"), _ams_entry("geodetic-2d")],
    )
    monkeypatch.setattr(
        fed_coverage,
        "_ams_provisioning_cache",
        [_ams_entry("civic"), _ams_entry("geodetic-2d")],
    )
    monkeypatch.setattr(logging.getLogger("src"), "propagate", True)

    _RecordingAsyncClient.posts = []
    monkeypatch.setattr(fed_sync.httpx, "AsyncClient", _RecordingAsyncClient)
    return tmp_path


def pushed_profiles() -> list[str]:
    """The serviceBoundary profile of every mapping pushed to the Forest Guide."""
    profiles: list[str] = []
    for _url, body in _RecordingAsyncClient.posts:
        root = etree.fromstring(body)
        for sb in root.iter(f"{{{NS_LOST}}}serviceBoundary"):
            profiles.append(sb.get("profile", ""))
    return profiles


def child_mapping(profile: str, *, delete: bool = False) -> str:
    """One <mapping> as a child would push it."""
    source_id = CHILD_CIVIC_ID if profile == "civic" else CHILD_GEODETIC_ID
    if delete:
        boundary = ""
    elif profile == "civic":
        boundary = (
            '<lost:serviceBoundary profile="civic">'
            "<ca:civicAddress>"
            "<ca:country>US</ca:country><ca:A1>ND</ca:A1><ca:A2>CASS</ca:A2>"
            "</ca:civicAddress>"
            "</lost:serviceBoundary>"
        )
    else:
        boundary = (
            '<lost:serviceBoundary profile="geodetic-2d">'
            '<gml:Polygon xmlns:gml="http://www.opengis.net/gml" '
            'srsName="urn:ogc:def:crs:EPSG::4326">'
            "<gml:exterior><gml:LinearRing>"
            "<gml:pos>46.4 -97.5</gml:pos><gml:pos>46.4 -96.5</gml:pos>"
            "<gml:pos>47.2 -96.5</gml:pos><gml:pos>47.2 -97.5</gml:pos>"
            "<gml:pos>46.4 -97.5</gml:pos>"
            "</gml:LinearRing></gml:exterior>"
            "</gml:Polygon>"
            "</lost:serviceBoundary>"
        )
    return (
        '<lost:mapping expires="NO-EXPIRATION" lastUpdated="2026-06-01T00:00:00Z" '
        'source="' + CHILD + '" sourceId="' + source_id + '">'
        '<lost:displayName xml:lang="en">' + CHILD + " coverage</lost:displayName>"
        "<lost:service>urn:service:sos</lost:service>"
        + boundary +
        "<lost:uri>" + CHILD_URI + "</lost:uri>"
        "</lost:mapping>"
    )


def push_root(*mappings: str) -> etree._Element:
    body = (
        '<pushMappings xmlns="' + NS_SYNC + '" xmlns:lost="' + NS_LOST + '" '
        'xmlns:ca="' + NS_CA + '">'
        + "".join(mappings)
        + "</pushMappings>"
    ).encode()
    return etree.fromstring(body)


def push_and_settle(*mappings: str):
    """Run _handle_push_mappings and let the cascade task it schedules finish.

    asyncio.run() rather than pytest-asyncio: this repo's async tests drive
    coroutines directly (see tests/security/test_sync_source_allowlist.py) and
    the suite carries no async plugin.
    """

    async def _run():
        resp = await fed_sync._handle_push_mappings(push_root(*mappings), client=None)
        # The FG push is fired as a bare asyncio task; drain it before asserting.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        return resp

    return asyncio.run(_run())


def test_civic_push_scopes_to_civic_only(ams_env):
    """A civic-only child push re-pushes the civic aggregate — and only that."""
    resp = push_and_settle(child_mapping("civic"))
    assert resp.status_code == 200

    assert pushed_profiles() == ["civic"], (
        "expected exactly one FG push carrying the civic aggregate; "
        f"got {pushed_profiles()!r}"
    )


def test_geodetic_push_scopes_to_geodetic_only(ams_env):
    """The mirror case, so the scoping isn't accidentally civic-only."""
    push_and_settle(child_mapping("geodetic-2d"))
    assert pushed_profiles() == ["geodetic-2d"]


def test_push_touching_both_profiles_pushes_both(ams_env):
    """Scoping narrows the push; it must not drop a half that really did change."""
    push_and_settle(child_mapping("civic"), child_mapping("geodetic-2d"))
    assert sorted(pushed_profiles()) == ["civic", "geodetic-2d"]


def test_delete_scopes_to_the_removed_entrys_profile(ams_env):
    """A delete carries no <serviceBoundary>, so the profile must come from the
    entry being removed rather than from the request — otherwise every delete
    would fall back to pushing both halves."""
    push_and_settle(child_mapping("civic"))
    _RecordingAsyncClient.posts = []

    push_and_settle(child_mapping("civic", delete=True))
    assert pushed_profiles() == ["civic"]


def test_undeterminable_profile_widens_rather_than_suppresses(ams_env):
    """A <serviceBoundary> with no profile attribute pushes BOTH halves.

    Failing open here is deliberate. A redundant push costs one round trip; a
    suppressed push leaves the Forest Guide routing on stale coverage with
    nothing logged to say so.
    """
    push_and_settle(child_mapping("civic").replace(' profile="civic"', ""))
    assert sorted(pushed_profiles()) == ["civic", "geodetic-2d"]


def test_unscoped_call_still_pushes_both(ams_env):
    """Startup sync and the post-GIS-reload re-push pass no scope and must keep
    pushing the whole aggregate."""
    assert asyncio.run(fed_sync._push_coverage_to_fg()) is True
    assert sorted(pushed_profiles()) == ["civic", "geodetic-2d"]
