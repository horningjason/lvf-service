"""Custom UvicornWorker for the gunicorn+UvicornWorker deployment path.

WHY THIS EXISTS

uvicorn.workers.UvicornWorker.__init__ builds its own uvicorn.Config from
gunicorn's ssl_options — certfile, keyfile, cert_reqs, ca_certs, ssl_version,
ciphers, all read from gunicorn.conf.py's module-level variables — and hands
them to uvicorn.Config as discrete ssl_certfile/ssl_cert_reqs/... kwargs.
uvicorn's own create_ssl_context() (uvicorn/config.py) never sets a TLS
minimum_version and has no PFS-only cipher default, so i3 §2.8.1's TLS-1.2
floor and i3-fe-core's PFS cipher suite cannot be expressed through that
surface regardless of how it's populated — the same limitation main.py's
plain-uvicorn path has (see that module's docstring). LvfUvicornWorker
instead builds the context via i3_fe_core.security.tls.make_server_ssl_context()
and assigns it directly to the uvicorn.Config instance the base class already
built, bypassing gunicorn's ssl_options -> uvicorn ssl_* kwargs ->
create_ssl_context() chain entirely.

WHY gunicorn_mode=False (genuine CERT_REQUIRED) IS THE SHIPPED DEFAULT

This repo already tried and abandoned a fix for the same underlying problem.
Commit b36d4f8 ("mTLS gunicorn", 2026-06-22 17:25) built an explicit
ssl.SSLContext with verify_mode = ssl.CERT_REQUIRED and assigned it to
gunicorn.conf.py's own module-level `ssl_context` variable. d22c312
("Reverting mTLS work") backed it out six minutes later, at 17:31.

THAT MITIGATION COULD NOT HAVE WORKED, for reasons independent of whether
CERT_REQUIRED is reliable under gunicorn+UvicornWorker. Verified against this
repo's own pins (gunicorn==23.0.0, uvicorn==0.46.0):

  - gunicorn's `ssl_context` is not an object slot. It is a SERVER HOOK:
    class NewSSLContext (gunicorn/config.py:2017) declares
    `validator = validate_callable(2)` and `type = callable`, and expects
    `(config, default_ssl_context_factory) -> SSLContext`. b36d4f8 assigned a
    bare ssl.SSLContext *instance*.
  - Under UvicornWorker — the worker this service actually runs — the setting
    is never read at all. uvicorn/workers.py:57-63 reads only
    `cfg.ssl_options` (keyfile, certfile, cert_reqs, ca_certs, ciphers). The
    assignment was a silent no-op.
  - Under the workers that DO consult it (sync.py:131, gthread.py:55,
    geventlet.py:155 via sock.ssl_wrap_socket; ggevent.py:61, gtornado.py:143
    via sock.ssl_context), gunicorn would have rejected it at config load:
    validate_callable raises `TypeError("Value is not callable: %s")` at
    config.py:449-450, and an ssl.SSLContext instance is not callable.

So the revert removed something that was non-functional two ways over. It is
NOT evidence that handshake-level CERT_REQUIRED is unachievable here, and the
history must not be read that way. It is also not evidence against *this*
mechanism — direct injection onto uvicorn.Config.ssl, bypassing gunicorn's
ssl_options forwarding entirely — which b36d4f8 never attempted.

Two further points of chronology matter when reading that revert:

  - It predates this repo's adoption of i3-fe-core. The revert is 2026-06-22;
    the first pinned core dependency lands in 7fdf906 on 2026-07-05. The
    library that supplies make_server_ssl_context() — and with it the §2.8.1
    TLS floor and PFS cipher policy this module injects — did not exist in
    this repo when the decision to revert was made.
  - A working mechanism now exists and is verified on the exact pins above;
    see VERIFICATION PROVENANCE below.

That revert is why the code sat at ssl.CERT_OPTIONAL (with a comment
conceding "this is not equivalent to enforcing mTLS") until this module.

VERIFICATION PROVENANCE

This mechanism (direct context injection onto uvicorn.Config.ssl) is
identical to gcs-service's GcsUvicornWorker, where it was verified against a
live handshake: 100 sequential no-certificate connection attempts against 4
workers produced 0 genuine accepts (63 handshake-level exceptions, 37
post-handshake empty-response rejections — both real rejections, just shaped
differently, since client-certificate authentication in TLS 1.3 is a
post-handshake exchange per RFC 8446 §4.3.2). 80 concurrent attempts (20
threads) against 4 workers produced 0 genuine accepts. 180 total attempts, 0
false accepts, across 1/3/4-worker configurations. Those pins
(uvicorn==0.46.0, gunicorn==23.0.0, Python 3.14) match this repository's
(see requirements.txt).

That verification has since been reproduced in THIS repository by
tests/security/test_mtls_handshake.py, which commits the measurement rather
than leaving it as a one-off: 13/13 cases passed on Linux, including all
seven gunicorn+LvfUvicornWorker cases — no certificate rejected both
sequentially (24 attempts / 3 workers) and concurrently (80 attempts / 20
threads / 4 workers), untrusted certificate rejected, CA-signed certificate
accepted, TLS 1.1 rejected, non-PFS cipher (AES128-SHA) rejected, and the
CERT_OPTIONAL fallback below confirmed to genuinely NOT enforce. Run
2026-08-22 in a Debian 13 (trixie) container: Python 3.14.7, OpenSSL 3.5.6,
uvicorn==0.46.0, gunicorn==23.0.0.

Two standing caveats on that result. It was produced in a container on a
development machine, not on the deployment host — re-run the suite there
before relying on this default in production. And it says nothing about
Windows, where gunicorn cannot run at all (fcntl/os.fork): the
gunicorn-marked cases SKIP there, and a skip is not a pass. The
plain-uvicorn cases do run on Windows and pass.

LVF_GUNICORN_CERT_OPTIONAL_FALLBACK reverts to core's own gunicorn_mode=True
acknowledgment (CERT_OPTIONAL, no handshake-level enforcement) for an
operator who hits a real, reproduced problem with direct injection in their
own environment. It IS documented (.env.example's transport-security
section, alongside the other LVF_TLS_* variables) because
an undocumented switch that silently disables i3 §5.4 enforcement is the same
species of problem this module exists to fix: a service that reads as
compliant while something quietly turns compliance off. Setting it disables
i3 §5.4 mutual authentication under gunicorn+UvicornWorker entirely and MUST
NOT happen in production — see .env.example for the full warning text, which
this module's startup log deliberately echoes.
"""
from __future__ import annotations

import logging
import os
import sys

from gunicorn.arbiter import Arbiter
from i3_fe_core.security.tls import make_server_ssl_context
from uvicorn.server import Server
from uvicorn.workers import UvicornWorker

from src.core_components import build_tls_settings

log = logging.getLogger(__name__)

_FALLBACK_VAR = "LVF_GUNICORN_CERT_OPTIONAL_FALLBACK"
_CERT_OPTIONAL_FALLBACK = os.environ.get(_FALLBACK_VAR, "").strip().lower() == "true"
if _CERT_OPTIONAL_FALLBACK:
    # Fires once per gunicorn worker process (module import time), matching
    # src/app/lifecycle.py's house style for loud config-validation warnings.
    log.warning(
        "%s=true — i3 §5.4 mutual authentication is NOT enforced on this "
        "worker. The gunicorn+UvicornWorker listener is running CERT_OPTIONAL "
        "instead of CERT_REQUIRED: a client presenting no certificate is "
        "accepted. This is a break-glass fallback for a reproduced problem "
        "with direct injection, not a supported deployment configuration — "
        "MUST NOT be set in production. See .env.example and this module's "
        "docstring.",
        _FALLBACK_VAR,
    )


class LvfUvicornWorker(UvicornWorker):
    """UvicornWorker with a directly-injected i3_fe_core TLS context.

    TLS injection happens in _serve() (after self.config.app is assigned the
    real ASGI app), NOT in __init__. self.config.app is still None at
    __init__ time — the base UvicornWorker only sets self.config.app =
    self.wsgi inside _serve(). Calling self.config.load() any earlier loads a
    None app: uvicorn's loader wraps it as ASGI2Middleware(None) and marks
    config.loaded = True, which then causes Server._serve()'s own
    `if not config.loaded: config.load()` to skip reloading once the real app
    is assigned — permanently poisoning config.loaded_app with the broken
    None-wrapped version. Both the ASGI lifespan handler and every HTTP
    request read from config.loaded_app, not config.app, so this broke
    startup (silently — "ASGI 'lifespan' protocol appears unsupported") AND
    every single request (TypeError: 'NoneType' object is not callable, from
    uvicorn/middleware/asgi2.py's `instance = self.app(scope)`).

    See module docstring for why the load()-then-inject-ssl shape exists at
    all (Config.load() resets self.ssl to None when ssl_certfile is unset,
    which is why plain __init__-time assignment without a load() afterward
    doesn't work either) — the fix is purely about WHEN that shape runs, not
    whether it's the right shape.
    """

    async def _serve(self) -> None:
        self.config.app = self.wsgi

        tls_settings = build_tls_settings()
        gunicorn_mode = _CERT_OPTIONAL_FALLBACK
        ssl_context = make_server_ssl_context(tls_settings, gunicorn_mode=gunicorn_mode)

        self.config.load()
        if ssl_context is not None:
            self.config.ssl = ssl_context
            log.info(
                "LvfUvicornWorker: injected i3_fe_core TLS context "
                "(gunicorn_mode=%s, verify_mode=%s)",
                gunicorn_mode,
                ssl_context.verify_mode,
            )

        server = Server(config=self.config)
        self._install_sigquit_handler()
        await server.serve(sockets=self.sockets)
        if not server.started:
            sys.exit(Arbiter.WORKER_BOOT_ERROR)
