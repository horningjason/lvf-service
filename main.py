"""
LVF Service launcher — reads LVF_TLS_MODE and builds the server-side TLS
context through i3_fe_core.security.tls (mirrors ../gcs-service/main.py).
Run with: python main.py

Single-process dev mode. For multi-worker use gunicorn (Linux/Docker only):
    python prewarm.py && gunicorn -c gunicorn.conf.py src.server:app

Why this doesn't call uvicorn.run(ssl_certfile=..., ssl_keyfile=...) anymore:
uvicorn.Config has no kwarg that accepts a pre-built ssl.SSLContext — only
discrete ssl_certfile/ssl_keyfile/ssl_version/ssl_cert_reqs/ssl_ca_certs/
ssl_ciphers. uvicorn's own create_ssl_context() (uvicorn/config.py) never sets
a TLS minimum_version at all, so passing discrete kwargs cannot express i3
§2.8.1's TLS-1.2 floor or i3-fe-core's PFS-only cipher suite
(security/tls.py's _PFS_CIPHERS_TLS12) — only a context built by
make_server_ssl_context() does both.

This launcher instead builds that context directly and assigns it to a
Config's `.ssl` attribute after calling `.load()` once. Config.load() only
(re)builds `.ssl` from the discrete ssl_* kwargs when `ssl_certfile` is set
(uvicorn/config.py: `if self.is_ssl: self.ssl = create_ssl_context(...)`);
since none of those kwargs are ever passed here, `.load()` leaves `.ssl` as
whatever it already is, so setting it immediately afterwards — before
`Server.serve()` runs, which skips a second `.load()` once `config.loaded` is
True (uvicorn/server.py) — is the supported way to hand an externally-built
context to an unmodified uvicorn.Server.
"""
import sys

from dotenv import load_dotenv

load_dotenv()

import uvicorn
from i3_fe_core.security.tls import make_server_ssl_context

from src.core_components import build_tls_settings, validate_tls_files
from src.observability import metrics

# Single-process dev mode never goes through prewarm.py — clear/recreate the
# Prometheus multiprocess directory here so /metrics works correctly even
# without gunicorn. Safe to do unconditionally: this is the only process.
metrics.clear_multiproc_dir()


def main() -> None:
    tls_settings = build_tls_settings()
    error = validate_tls_files(tls_settings)
    if error is not None:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    # gunicorn_mode=False: this process IS the TLS terminator (plain uvicorn,
    # no gunicorn arbiter, no reverse proxy in front) — see
    # gunicorn.conf.py / src/app/lvf_uvicorn_worker.py for the gunicorn path,
    # which is handled separately.
    ssl_context = make_server_ssl_context(tls_settings, gunicorn_mode=False)

    config = uvicorn.Config("src.server:app", host="0.0.0.0", port=8000)
    config.load()
    if ssl_context is not None:
        config.ssl = ssl_context

    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass

    if not server.started:
        sys.exit(1)


if __name__ == "__main__":
    main()
