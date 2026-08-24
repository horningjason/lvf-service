"""Plain-uvicorn test server for tests/security's live-handshake tests.

Mirrors main.py's TLS wiring exactly (build_tls_settings() ->
make_server_ssl_context(gunicorn_mode=False) -> Config.load() -> override
.ssl -> Server.run()) but serves tests/security/_tls_test_app.py instead of
src.server:app, and reads the bind port from LVF_TEST_PORT so the test suite
can pick a free one. Run as a subprocess:

    python -m tests.security._run_uvicorn_test_server
"""
import os
import sys

import uvicorn
from i3_fe_core.security.tls import make_server_ssl_context

from src.core_components import build_tls_settings, validate_tls_files


def main() -> None:
    tls_settings = build_tls_settings()
    error = validate_tls_files(tls_settings)
    if error is not None:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    ssl_context = make_server_ssl_context(tls_settings, gunicorn_mode=False)

    port = int(os.environ.get("LVF_TEST_PORT", "8443"))
    config = uvicorn.Config(
        "tests.security._tls_test_app:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    config.load()
    if ssl_context is not None:
        config.ssl = ssl_context

    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()
