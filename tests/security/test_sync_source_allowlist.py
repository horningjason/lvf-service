"""LoST-Sync peer authorization — LVF_SYNC_ALLOWED_SOURCES (§3.7.2, Appendix A.11).

RFC 6739 §5.2 merges coverage mappings keyed on the (source, sourceId) pair that
the *sending peer asserts* in the mapping element. The coverage store is keyed on
that pair, and child coverage drives routing before Gate 0 (§3.7.3) with the
mapping's own <uri> naming the destination. Without a constraint, any peer whose
certificate chains to LVF_TLS_CA_FILE can overwrite another child's entry, or add
a novel more-specific entry that wins the longest-prefix match, and so choose
where validation queries are sent.

These are the first tests over the sync ingestion path. They cover BOTH ingestion
points — the inbound push (_handle_push_mappings) and the outbound pull
(_pull_from_child), which upserts identically and was the easier one to miss.

SCOPE — what these tests do and do not prove. The allowlist constrains WHICH
(source, sourceId) pairs this node accepts. It does not bind a pair to the
transport identity of the peer asserting it, because uvicorn does not implement
the ASGI TLS extension and the client certificate verified at the handshake is
unreachable from the application layer. The final test pins that residual
deliberately, so this suite is never misread as proving more than it does.
Appendix A.11 records what closes it.
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from src import runtime_state
from src.federation import coverage as fed_coverage
from src.federation import sync as fed_sync

NS_SYNC = "urn:ietf:params:xml:ns:lostsync1"
NS_LOST = "urn:ietf:params:xml:ns:lost1"
NS_CA = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"

CHILD1 = "child1.lvf.example.com"
CHILD1_ID = "{11111111-1111-1111-1111-111111111111}"
CHILD2 = "child2.lvf.example.com"
CHILD2_ID = "{22222222-2222-2222-2222-222222222222}"
ATTACKER = "attacker.lvf.example.com"
ATTACKER_ID = "{99999999-9999-9999-9999-999999999999}"

CHILD1_URI = "https://child1.lvf.example.com/lost"
EVIL_URI = "https://evil.example.com/lost"

LEGIT_PEER = ("10.0.0.5", 44444)
EVIL_PEER = ("203.0.113.66", 44444)


class _NullLoggingClient:
    """runtime_state.logging_client is built during app startup; these tests
    drive handle_sync() directly, so stand in for it. emit_log_event() only
    needs an awaitable emit()."""

    async def emit(self, event):
        return None


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Isolate the coverage store and neutralise everything outside the guard.

    LVF_GPKG_PATH decides where _child_coverage_path() puts the store, so
    pointing it into tmp_path keeps each test on its own file. _parent_uri and
    _root_ams are cleared because a successful upsert otherwise schedules a real
    upstream push (sync.py's cascade); runtime_state reads the developer's .env
    at import, so these are genuinely set in a normal dev shell.
    """
    monkeypatch.setenv("LVF_GPKG_PATH", str(tmp_path / "data.gpkg"))
    monkeypatch.delenv("LVF_FOREST_GUIDE_MODE", raising=False)
    monkeypatch.delenv("LVF_SYNC_ALLOWED_SOURCES", raising=False)
    monkeypatch.setattr(runtime_state, "logging_client", _NullLoggingClient())
    monkeypatch.setattr(runtime_state, "_root_ams", False)
    monkeypatch.setattr(runtime_state, "_parent_uri", "")
    monkeypatch.setattr(runtime_state, "_forest_guide_mode", False)
    monkeypatch.setattr(fed_coverage, "_child_coverage", [])
    # runtime_state.py sets propagate=False on the "src" logger so uvicorn's
    # handler does not double-log. caplog attaches to the ROOT logger, so without
    # re-enabling propagation these records never reach it and every log
    # assertion below would silently pass against an empty string. monkeypatch
    # restores the flag after each test.
    monkeypatch.setattr(logging.getLogger("src"), "propagate", True)
    return tmp_path


def allow(monkeypatch, *records: str) -> None:
    """Set LVF_SYNC_ALLOWED_SOURCES to the given 'source|sourceId' records."""
    monkeypatch.setenv("LVF_SYNC_ALLOWED_SOURCES", ",".join(records))


def mapping_xml(
    source: str,
    source_id: str,
    *,
    a2: str = "CASS",
    a3: str | None = None,
    uri: str = CHILD1_URI,
    last_updated: str = "2026-01-01T00:00:00Z",
    delete: bool = False,
) -> str:
    """One <mapping>. delete=True omits <serviceBoundary>, which is how RFC 6739
    signals a delete and how _handle_push_mappings classifies the op."""
    if delete:
        boundary = ""
    else:
        a3_el = "<ca:A3>" + a3 + "</ca:A3>" if a3 else ""
        boundary = (
            '<lost:serviceBoundary profile="civic">'
            "<ca:civicAddress>"
            "<ca:country>US</ca:country><ca:A1>ND</ca:A1>"
            "<ca:A2>" + a2 + "</ca:A2>" + a3_el +
            "</ca:civicAddress>"
            "</lost:serviceBoundary>"
        )
    return (
        '<lost:mapping expires="NO-EXPIRATION" lastUpdated="' + last_updated + '" '
        'source="' + source + '" sourceId="' + source_id + '">'
        '<lost:displayName xml:lang="en">' + source + " civic coverage</lost:displayName>"
        "<lost:service>urn:service:sos</lost:service>"
        + boundary +
        "<lost:uri>" + uri + "</lost:uri>"
        "</lost:mapping>"
    )


def push_body(*mappings: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<pushMappings xmlns="' + NS_SYNC + '" xmlns:lost="' + NS_LOST + '" '
        'xmlns:ca="' + NS_CA + '">'
        + "".join(mappings)
        + "</pushMappings>"
    ).encode()


def get_mappings_response(*mappings: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<getMappingsResponse xmlns="' + NS_SYNC + '" xmlns:lost="' + NS_LOST + '" '
        'xmlns:ca="' + NS_CA + '">'
        + "".join(mappings)
        + "</getMappingsResponse>"
    ).encode()


def run_sync(body: bytes, client=LEGIT_PEER):
    """Drive the coroutine the /sync route delegates to. server.py's handler is a
    one-line `return await _fs.handle_sync(body, request.client)`, so this is the
    endpoint's entire behaviour, with the same peer-address argument."""
    return asyncio.run(fed_sync.handle_sync(body, client))


def store() -> list[dict]:
    """Read the PERSISTED store, not the in-memory list, so the cross-process
    write path (_with_coverage_write) is exercised too."""
    return fed_coverage._read_child_coverage_file()


def keys(entries: list[dict]) -> set:
    return {(e.get("source", ""), e.get("source_id", "")) for e in entries}


def seed_store(entries: list[dict]) -> None:
    fed_coverage._save_child_coverage_list(entries)
    fed_coverage._child_coverage = entries


def coverage_entry(source: str, source_id: str, a2: str, uri: str) -> dict:
    return {
        "source": source,
        "source_id": source_id,
        "last_updated": "2026-01-01T00:00:00Z",
        "expires": "NO-EXPIRATION",
        "service": "urn:service:sos",
        "display_name": source + " civic coverage",
        "profile": "civic",
        "civic_addresses": [
            {"country": "US", "a1": "ND", "a2": a2, "a3": "*", "a4": "*", "a5": "*"}
        ],
        "geodetic_geom_wkt": None,
        "lost_server": uri,
    }


def messages(caplog, needle: str) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if needle in r.getMessage())


# ===========================================================================
# 1. Control — an allowlisted pair is accepted
# ===========================================================================

def test_permitted_pair_is_accepted(sync_env, monkeypatch):
    """The guard must not be blanket-blocking: a pair the operator allowlisted
    is stored exactly as it was before the control existed."""
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)

    response = run_sync(push_body(mapping_xml(CHILD1, CHILD1_ID)))

    assert response.status_code == 200
    assert b"pushMappingsResponse" in response.body
    assert b"forbidden" not in response.body

    entries = store()
    assert keys(entries) == {(CHILD1, CHILD1_ID)}
    assert entries[0]["profile"] == "civic"
    assert entries[0]["lost_server"] == CHILD1_URI


# ===========================================================================
# 2. Impersonation of an allowlisted child is rejected
# ===========================================================================

def test_impersonating_allowlisted_child_is_rejected(sync_env, monkeypatch, caplog):
    """The LVF4 scenario. child1 is a legitimate child of this node with a stored
    entry. An attacker pushes under child1's OWN source name, with a forged
    sourceId, claiming a more specific territory that would win the longest-prefix
    match. The allowlist keys on the PAIR, so forging either half fails.

    child1's stored entry must be untouched — not merely 'the push was refused'.
    """
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)
    run_sync(push_body(mapping_xml(CHILD1, CHILD1_ID)))
    before = store()
    assert keys(before) == {(CHILD1, CHILD1_ID)}

    with caplog.at_level(logging.WARNING, logger="src.federation.sync"):
        response = run_sync(
            push_body(mapping_xml(CHILD1, ATTACKER_ID, a3="FARGO", uri=EVIL_URI)),
            client=EVIL_PEER,
        )

    assert response.status_code == 200
    assert b"<forbidden" in response.body
    assert b"Not authorized to assert coverage" in response.body

    assert store() == before, "victim's coverage entry was modified by a forged push"
    assert keys(store()) == {(CHILD1, CHILD1_ID)}

    rejection = messages(caplog, "REJECTED")
    assert rejection, "rejection was not logged"
    assert ATTACKER_ID in rejection
    assert "LVF_SYNC_ALLOWED_SOURCES" in rejection
    assert EVIL_PEER[0] in rejection, "peer address was not threaded into the log"


# ===========================================================================
# 3. A novel pair is rejected — the specificity hijack
# ===========================================================================

def test_novel_pair_with_higher_specificity_is_rejected(sync_env, monkeypatch):
    """_lookup_child_coverage selects by SPECIFICITY, not by source, so an
    attacker never needs a victim's identifiers: a brand-new pair carrying a more
    specific A3 simply out-ranks the legitimate entry. Unknown pairs must be
    refused, not merely mismatched ones."""
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)
    run_sync(push_body(mapping_xml(CHILD1, CHILD1_ID)))
    before = store()

    response = run_sync(
        push_body(mapping_xml(ATTACKER, ATTACKER_ID, a3="FARGO", uri=EVIL_URI)),
        client=EVIL_PEER,
    )

    assert response.status_code == 200
    assert b"<forbidden" in response.body
    assert store() == before
    assert (ATTACKER, ATTACKER_ID) not in keys(store())


# ===========================================================================
# 4. Routing consequence — the lookup still resolves to the real child
# ===========================================================================

def test_rejected_push_does_not_capture_routing(sync_env, monkeypatch):
    """The reason any of this matters: _lookup_child_coverage feeds
    find_service.py's redirect/recurse decision before Gate 0. After a rejected
    hijack, the lookup for the targeted address must still resolve to the
    legitimate child and its URI."""
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)
    run_sync(push_body(mapping_xml(CHILD1, CHILD1_ID)))

    run_sync(
        push_body(mapping_xml(ATTACKER, ATTACKER_ID, a3="FARGO", uri=EVIL_URI)),
        client=EVIL_PEER,
    )

    match = fed_coverage._lookup_child_coverage("US", "ND", "CASS", "FARGO")
    assert match is not None
    assert match["source"] == CHILD1
    assert match["source_id"] == CHILD1_ID
    assert match["lost_server"] == CHILD1_URI


# ===========================================================================
# 5. Batch atomicity — one forged mapping rejects the whole request
# ===========================================================================

def test_forged_mapping_rejects_entire_batch(sync_env, monkeypatch):
    """A partial apply would let a forged mapping ride along with a valid one and
    still land its side effects, including the upstream cascade. The VALID
    mapping must also be absent — that is what makes this atomic rather than
    merely filtered."""
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)

    response = run_sync(
        push_body(
            mapping_xml(CHILD1, CHILD1_ID),
            mapping_xml(ATTACKER, ATTACKER_ID, a3="FARGO"),
        ),
        client=EVIL_PEER,
    )

    assert response.status_code == 200
    assert b"<forbidden" in response.body
    assert store() == [], "a mapping was applied from a request containing a forgery"


# ===========================================================================
# 6. The delete path is guarded too
# ===========================================================================

def test_forged_delete_is_rejected(sync_env, monkeypatch):
    """A mapping with no <serviceBoundary> is a delete. Guarding only the upsert
    would close overwrite while leaving deletion open — an attacker could erase a
    child's coverage and black-hole its territory. child2's entry is present but
    not allowlisted here, which is what a stale store looks like after an
    allowlist is narrowed."""
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)
    seed_store(
        [
            coverage_entry(CHILD1, CHILD1_ID, "CASS", CHILD1_URI),
            coverage_entry(CHILD2, CHILD2_ID, "BURLEIGH", "https://child2.lvf.example.com/lost"),
        ]
    )
    before = store()

    response = run_sync(
        push_body(mapping_xml(CHILD2, CHILD2_ID, delete=True)),
        client=EVIL_PEER,
    )

    assert response.status_code == 200
    assert b"<forbidden" in response.body
    assert b"notDeleted" not in response.body
    assert store() == before
    assert (CHILD2, CHILD2_ID) in keys(store()), "entry was deleted by a forged push"


# ===========================================================================
# 7. The pull path applies the same constraint
# ===========================================================================

class _FakeResponse:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.content = content


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient inside _pull_from_child. Returns a
    getMappingsResponse from the 'child' without any network or TLS."""

    payload: bytes = b""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, content=None, headers=None):
        return _FakeResponse(type(self).payload)


def test_pull_skips_unauthorized_mappings(sync_env, monkeypatch, caplog):
    """_pull_from_child upserts every mapping in the response. A configured child
    can return mappings claiming to be any OTHER child, so the same constraint
    applies. Unlike the push path this SKIPS rather than aborting: the pull is
    our own initiative against a configured peer, and dropping its legitimate
    mappings over one bad one would be a self-inflicted outage."""
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)

    _FakeAsyncClient.payload = get_mappings_response(
        mapping_xml(CHILD1, CHILD1_ID),
        mapping_xml(ATTACKER, ATTACKER_ID, a3="FARGO", uri=EVIL_URI),
    )
    monkeypatch.setattr(fed_sync.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(fed_sync, "outbound_ssl_context", lambda: False)
    monkeypatch.setattr(fed_sync.gis_provisioning, "is_reloading", lambda: False)

    with caplog.at_level(logging.WARNING, logger="src.federation.sync"):
        ok = asyncio.run(fed_sync._pull_from_child("https://child1.lvf.example.com/sync"))

    assert ok is True, "the pull itself should succeed; only the bad mapping is dropped"
    assert keys(store()) == {(CHILD1, CHILD1_ID)}
    assert (ATTACKER, ATTACKER_ID) not in keys(store())

    skipped = messages(caplog, "SKIPPED")
    assert skipped, "skip was not logged"
    assert ATTACKER in skipped


# ===========================================================================
# 8. Unset means fail-closed, and says so at startup
# ===========================================================================

def test_unset_allowlist_is_fail_closed_and_warns(sync_env, monkeypatch, caplog):
    """Pins the default so it cannot drift silently. Unset accepts nothing, which
    from the operator's side is indistinguishable from a network fault — so the
    startup warning has to name the variable."""
    assert os.environ.get("LVF_SYNC_ALLOWED_SOURCES") is None
    assert fed_coverage._load_allowed_sources() == {}
    assert fed_coverage._is_source_permitted(CHILD1, CHILD1_ID) is False

    response = run_sync(push_body(mapping_xml(CHILD1, CHILD1_ID)))
    assert b"<forbidden" in response.body
    assert store() == []

    with caplog.at_level(logging.WARNING, logger="src.federation.coverage"):
        fed_coverage._warn_if_no_allowed_sources()
    warning = messages(caplog, "LVF_SYNC_ALLOWED_SOURCES")
    assert warning
    assert "INERT" in warning

    caplog.clear()
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID)
    with caplog.at_level(logging.WARNING, logger="src.federation.coverage"):
        fed_coverage._warn_if_no_allowed_sources()
    assert not messages(caplog, "INERT")


# ===========================================================================
# 9. Residual gap, pinned deliberately (Appendix A.11)
# ===========================================================================

def test_allowlisted_pair_accepted_from_any_peer_is_the_residual_gap(sync_env, monkeypatch):
    """NOT a passing security property — a deliberate record of what is still
    open, so this suite is never misread as proving impersonation fully solved.

    The allowlist constrains WHICH pairs are accepted, not WHO may assert them.
    A push from an unrelated peer address carrying an allowlisted pair is
    accepted, because uvicorn does not surface the client certificate to the
    application layer (no ASGI TLS extension), so _is_source_permitted() is
    called with peer=None and the allowlist's optional third field cannot be
    enforced. Closing this needs proxy-terminated mTLS re-verified with
    i3_fe_core's PeerCertVerifier, or an ASGI server that exposes the peer cert.

    When that lands, this test SHOULD start failing — that is the signal to
    replace it with an assertion that the wrong peer is rejected.
    """
    allow(monkeypatch, CHILD1 + "|" + CHILD1_ID + "|" + CHILD1)

    response = run_sync(
        push_body(mapping_xml(CHILD1, CHILD1_ID, uri=EVIL_URI)),
        client=EVIL_PEER,
    )

    assert b"pushMappingsResponse" in response.body
    assert keys(store()) == {(CHILD1, CHILD1_ID)}
    assert store()[0]["lost_server"] == EVIL_URI
