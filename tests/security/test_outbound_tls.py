"""Live-handshake verification for LVF's OUTBOUND (egress) TLS to peer nodes.

Companion to test_mtls_handshake.py, which covers the inbound listener. Until
this module existed, tests/security/ was inbound-only: every LVF call to a peer
— child->parent sync push, Forest Guide push, parent->child pull, recursion,
and listServicesByLocation forwarding — was entirely untested, in a service
whose defining role is federation.

These tests follow the same philosophy as the inbound suite: assert on the
OBSERVED handshake outcome against a real TLS peer, not on ssl.SSLContext
attributes. The one deliberate exception is test_client_identity_..._tls_mode,
which pins a DECISION rather than a mechanism (see its docstring); it is backed
by behavioral tests of the same property, so it is not the only thing standing
between that decision and a silent regression.

Unlike the gunicorn cases in test_mtls_handshake.py, everything here is
pure-client code plus a local TLS socket server, so it runs on Windows as well
as Linux/Docker. That partially closes the platform gap in the inbound suite,
where the gunicorn cases skip and A SKIP IS NOT A PASS.

Async call sites are driven with asyncio.run() rather than pytest-asyncio,
which is not declared in requirements.txt; this suite adds no new test
dependency.
"""
from __future__ import annotations

import asyncio
import http.server
import ssl
import threading
from pathlib import Path

import httpx
import pytest

_PEER_RESPONSE = b'<listServicesByLocationResponse xmlns="urn:ietf:params:xml:ns:lost1"/>'

# Handshake rejection surfaces differently depending on whether the peer
# refuses during the handshake (TLS 1.2) or post-handshake (TLS 1.3 client
# authentication, RFC 8446 §4.3.2) — both are real rejections, just shaped
# differently. See test_mtls_handshake.py, which documents the same split.
_REJECTED = (httpx.HTTPError, ssl.SSLError)


class _PeerHandler(http.server.BaseHTTPRequestHandler):
    """Minimal LoST-shaped peer: 200 + parseable XML for any POST."""

    def do_POST(self) -> None:  # noqa: N802  (stdlib naming)
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/lost+xml")
        self.send_header("Content-Length", str(len(_PEER_RESPONSE)))
        self.end_headers()
        self.wfile.write(_PEER_RESPONSE)

    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # Rejected handshakes are the POINT of several tests below; a
        # traceback per rejection would drown the real output.
        pass


def _start_peer(
    pki,
    *,
    require_client_cert: bool,
    ciphers: str | None = None,
    maximum_version: ssl.TLSVersion | None = None,
):
    """Start a throwaway HTTPS peer on 127.0.0.1. Returns (url, shutdown)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(pki.server_cert), keyfile=str(pki.server_key))
    if require_client_cert:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=str(pki.ca_cert))
    if ciphers is not None:
        ctx.set_ciphers(ciphers)          # may raise; caller decides to skip
    if maximum_version is not None:
        ctx.maximum_version = maximum_version

    server = _QuietServer(("127.0.0.1", 0), _PeerHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # 127.0.0.1 (not "localhost") to dodge IPv6-first resolution; the fixture
    # server cert carries an IP SAN for it, so hostname verification still runs.
    return f"https://127.0.0.1:{port}/lost", shutdown


@pytest.fixture
def outbound_env(monkeypatch, tls_pki):
    """Configure LVF_TLS_* for egress and keep the cached context honest.

    outbound_ssl_context() is lru_cached, so every test that manipulates
    LVF_TLS_* must clear it on the way in AND on the way out — otherwise a
    context built from one test's environment leaks into the next.
    """
    from src.utils import outbound_ssl_context

    def configure(
        *,
        identity: bool = True,
        ca: bool = True,
        mode: str = "mtls",
        ca_path: Path | None = None,
    ) -> None:
        monkeypatch.setenv("LVF_TLS_MODE", mode)
        for var, path, enabled in (
            ("LVF_TLS_CLIENT_CERT_FILE", tls_pki.trusted_client_cert, identity),
            ("LVF_TLS_CLIENT_KEY_FILE", tls_pki.trusted_client_key, identity),
            ("LVF_TLS_CA_FILE", ca_path or tls_pki.ca_cert, ca),
        ):
            if enabled:
                monkeypatch.setenv(var, str(path))
            else:
                monkeypatch.delenv(var, raising=False)
        outbound_ssl_context.cache_clear()

    outbound_ssl_context.cache_clear()
    yield configure
    outbound_ssl_context.cache_clear()


# ---------------------------------------------------------------------------
# 1. The semantic decision: client identity is keyed on FILE EXISTENCE, not on
#    LVF_TLS_MODE == mtls.
# ---------------------------------------------------------------------------

def test_client_identity_presented_in_tls_mode(outbound_env, tls_pki):
    """LVF_TLS_MODE=tls with client cert files set MUST still load an identity.

    This is the one attribute-level assertion in this module, and it is
    deliberate: it pins a DECISION, at the exact seam where the decision is
    made, rather than a mechanism. i3-fe-core's make_client_ssl_context()
    loads a client identity only when settings.mode == MTLS, so LVF sets MTLS
    on its purpose-built client settings whenever both cert and key files
    exist — independent of LVF_TLS_MODE — to preserve the behavior of the
    outbound_client_cert() helper this replaced, which keyed purely on file
    existence.

    Aligning build_client_tls_settings() with build_tls_settings() would break
    exactly this case: LVF_TLS_MODE=tls deployments would silently stop
    presenting their client certificate, and the failure would surface at the
    PEER as a rejected handshake — a federation outage attributed to the wrong
    node. That is why this is pinned separately from the behavioral tests.
    """
    from i3_fe_core.config.settings import TLSMode
    from src.utils import build_client_tls_settings

    outbound_env(identity=True, ca=True, mode="tls")   # NOT mtls
    settings = build_client_tls_settings()

    assert settings.mode == TLSMode.MTLS, (
        "client identity was gated on LVF_TLS_MODE — see this test's docstring"
    )
    assert settings.cert_path == Path(str(tls_pki.trusted_client_cert))
    assert settings.key_path == Path(str(tls_pki.trusted_client_key))


def test_no_client_identity_when_files_absent(outbound_env):
    """With no client cert files, the context carries trust but no identity."""
    from i3_fe_core.config.settings import TLSMode
    from src.utils import build_client_tls_settings

    outbound_env(identity=False, ca=True, mode="mtls")
    settings = build_client_tls_settings()

    assert settings.mode == TLSMode.TLS
    assert settings.cert_path is None


# ---------------------------------------------------------------------------
# 2. Behavioral proof: the context actually presents the identity, and
#    actually pins trust to our CA.
# ---------------------------------------------------------------------------

def test_outbound_presents_identity_to_cert_required_peer(outbound_env, tls_pki):
    """A peer demanding CERT_REQUIRED accepts us — proof the identity is sent.

    No public API exposes a context's loaded client chain, so a successful
    handshake against a CERT_REQUIRED peer is the only real evidence that the
    certificate is presented rather than merely configured.
    """
    from src.utils import outbound_ssl_context

    outbound_env(identity=True, ca=True, mode="tls")
    url, shutdown = _start_peer(tls_pki, require_client_cert=True)
    try:
        with httpx.Client(timeout=10.0, verify=outbound_ssl_context()) as client:
            resp = client.post(url, content=b"<x/>")
        assert resp.status_code == 200
        assert resp.content == _PEER_RESPONSE
    finally:
        shutdown()


def test_cert_required_peer_rejects_us_without_identity(outbound_env, tls_pki):
    """Same peer, no client cert configured — the handshake MUST fail.

    The negative control for the test above. Without it, that test would pass
    just as happily against a peer that never actually checked.
    """
    from src.utils import outbound_ssl_context

    outbound_env(identity=False, ca=True, mode="tls")
    url, shutdown = _start_peer(tls_pki, require_client_cert=True)
    try:
        with httpx.Client(timeout=10.0, verify=outbound_ssl_context()) as client:
            with pytest.raises(_REJECTED):
                client.post(url, content=b"<x/>")
    finally:
        shutdown()


def test_outbound_rejects_peer_not_signed_by_our_ca(outbound_env, tls_pki):
    """Trust is pinned to LVF_TLS_CA_FILE — an unrelated CA is refused."""
    from src.utils import outbound_ssl_context

    # Point LVF_TLS_CA_FILE at the UNTRUSTED self-signed cert, which did not
    # sign the peer's server certificate. Routed through the fixture so
    # monkeypatch still restores the environment afterwards.
    outbound_env(
        identity=True, ca=True, mode="mtls",
        ca_path=Path(str(tls_pki.untrusted_client_cert)),
    )

    url, shutdown = _start_peer(tls_pki, require_client_cert=False)
    try:
        with httpx.Client(timeout=10.0, verify=outbound_ssl_context()) as client:
            with pytest.raises(_REJECTED):
                client.post(url, content=b"<x/>")
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# 3. Regression: _forward() had NO verify= at all.
# ---------------------------------------------------------------------------

def test_forward_reaches_peer_signed_by_our_self_signed_ca(outbound_env, tls_pki):
    """listServicesByLocation forwarding works against a self-signed-CA peer.

    THIS TEST MUST FAIL BEFORE THE FIX IT GUARDS. _forward() previously built
    a bare httpx.AsyncClient(timeout=10.0) with no verify= and no cert=, so
    httpx verified the peer against certifi's bundle. LVF's federation CA is
    self-signed and is not in that bundle, so the handshake raised
    SSLCertVerificationError, which _forward()'s bare `except Exception`
    converted into <errors><serverError>"Could not reach remote server: ...".

    That means the path was BROKEN in LVF_TLS_MODE=tls/mtls, not merely
    unenforced — it failed closed, with an error string that points operators
    at network reachability rather than at TLS configuration. It went
    unnoticed because LVF_TLS_MODE defaults to disabled and the regression
    suite drives handle_find_service() directly with no HTTP.
    """
    from src.lost.list_services_by_location import _forward

    outbound_env(identity=True, ca=True, mode="tls")
    url, shutdown = _start_peer(tls_pki, require_client_cert=False)
    try:
        result = asyncio.run(_forward(b"<x/>", url, "lvf.test"))
        assert result == _PEER_RESPONSE
        assert b"serverError" not in result
    finally:
        shutdown()


def test_forward_presents_identity_to_cert_required_peer(outbound_env, tls_pki):
    """_forward() carries the client identity too, not just CA trust.

    _forward() targets peer LVF nodes, which under LVF_TLS_MODE=mtls enforce
    CERT_REQUIRED at the handshake (test_mtls_handshake.py). Verifying only CA
    trust here would leave that half of the fix unproven.
    """
    from src.lost.list_services_by_location import _forward

    outbound_env(identity=True, ca=True, mode="mtls")
    url, shutdown = _start_peer(tls_pki, require_client_cert=True)
    try:
        result = asyncio.run(_forward(b"<x/>", url, "lvf.test"))
        assert result == _PEER_RESPONSE
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# 4. §2.8.1: the constraints this migration exists to apply.
# ---------------------------------------------------------------------------

def test_outbound_context_enforces_tls12_floor(outbound_env):
    """TLS 1.2 floor per i3 §2.8.1."""
    from src.utils import outbound_ssl_context

    outbound_env(identity=True, ca=True, mode="mtls")
    assert outbound_ssl_context().minimum_version == ssl.TLSVersion.TLSv1_2


def test_outbound_context_offers_only_pfs_tls12_ciphers(outbound_env):
    """No non-PFS TLS 1.2 suite is offered.

    This is the control the previous `verify=<ca path>` did NOT provide: that
    form routed through httpx to ssl.create_default_context(), whose TLS 1.2
    cipher list includes static-RSA key exchange. TLS 1.3 suites are always
    PFS and are not configurable, so they are excluded from this assertion.
    """
    from src.utils import outbound_ssl_context

    outbound_env(identity=True, ca=True, mode="mtls")
    non_pfs = [
        c["name"]
        for c in outbound_ssl_context().get_ciphers()
        if c["protocol"] == "TLSv1.2" and not c["name"].startswith(("ECDHE", "DHE"))
    ]
    assert not non_pfs, f"non-PFS TLS 1.2 ciphers offered: {non_pfs}"


def test_outbound_rejects_non_pfs_only_peer(outbound_env, tls_pki):
    """Behavioral counterpart: a peer offering only static-RSA is refused."""
    from src.utils import outbound_ssl_context

    outbound_env(identity=True, ca=True, mode="tls")
    try:
        url, shutdown = _start_peer(
            tls_pki,
            require_client_cert=False,
            ciphers="AES128-SHA:@SECLEVEL=0",
            maximum_version=ssl.TLSVersion.TLSv1_2,
        )
    except ssl.SSLError as exc:
        pytest.skip(f"platform OpenSSL will not offer a non-PFS suite: {exc}")

    try:
        with httpx.Client(timeout=10.0, verify=outbound_ssl_context()) as client:
            with pytest.raises(_REJECTED):
                client.post(url, content=b"<x/>")
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# 5. core_components.py: Logging Service / DR egress reuse the SAME outbound
#    path as federation traffic (see src/core_components.py's module
#    docstring for the design rationale and the LVF_TLS_CA_FILE-scope
#    caveat this wiring accepts).
# ---------------------------------------------------------------------------

def test_core_components_wires_tls_when_enabled(outbound_env, tls_pki, monkeypatch):
    """LVF_TLS_MODE=tls/mtls: both LoggingClient and DiscrepancyReporting get
    a real, TLS-configured http_client instead of falling back to their own
    internal plaintext-capable default.

    Reaches into both components' private client attributes because neither
    exposes its configured transport publicly. LoggingClient falls back to
    `http_client or httpx.AsyncClient()` at construction time, so a non-None
    `_http_client` here proves build_core_components() supplied one — it
    would be non-None either way once *used*, but never before first use if
    left to its own lazy default (DiscrepancyReporting's `_http`, checked
    below, IS None until first use when nothing is supplied — see
    test_core_components_no_tls_client_when_disabled).
    """
    from src.core_components import build_core_components

    monkeypatch.setenv("LVF_SERVER_URI", "lvf.test")
    outbound_env(identity=True, ca=True, mode="tls")

    core = build_core_components()

    assert core.logging_client._http_client is not None
    assert core.discrepancy._http is not None


def test_core_components_no_tls_client_when_disabled(outbound_env, monkeypatch):
    """LVF_TLS_MODE=disabled: neither component is handed a pre-built client.

    DiscrepancyReporting stores exactly what it is given (`self._http =
    http_client`), with no constructor-time fallback — so `_http is None`
    here is direct proof that build_core_components() supplied nothing,
    which is the correct behavior when the deployment has not opted into
    TLS: LVF_LOGGING_SERVICE_URI / LVF_DR_ENDPOINT may legitimately be plain
    http:// in that configuration, and building a TLS-enforcing client for
    them would be pointless work, not a correctness issue (verify= is
    ignored for http:// requests either way).
    """
    from src.core_components import build_core_components

    monkeypatch.setenv("LVF_SERVER_URI", "lvf.test")
    outbound_env(identity=False, ca=False, mode="disabled")

    core = build_core_components()

    assert core.discrepancy._http is None


def test_core_components_logging_and_dr_share_cached_context(outbound_env, monkeypatch):
    """Logging and DR each get their OWN AsyncClient (separate connection
    pools, matching gcs-service's own _outbound_http / _dr_http split) but
    both wrap the SAME cached ssl.SSLContext object — outbound_ssl_context()
    is lru_cached, so this costs nothing beyond the two connection pools
    already required by having two independent clients.

    Reaches into httpx's private transport/pool internals to compare the
    live ssl.SSLContext identity, since httpx exposes no public API to
    introspect a constructed client's `verify=`. This repo pins
    httpx==0.28.1 exactly (requirements.txt), so this coupling is no more
    fragile than the version pin itself.
    """
    from src.core_components import build_core_components
    from src.utils import outbound_ssl_context

    monkeypatch.setenv("LVF_SERVER_URI", "lvf.test")
    outbound_env(identity=True, ca=True, mode="mtls")

    core = build_core_components()

    logging_ctx = core.logging_client._http_client._transport._pool._ssl_context
    dr_ctx = core.discrepancy._http._transport._pool._ssl_context

    assert logging_ctx is dr_ctx
    assert logging_ctx is outbound_ssl_context()
