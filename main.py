"""
LVF Service launcher — reads LVF_TLS_MODE and configures Uvicorn SSL accordingly.
Run with: python main.py
"""
import os
import ssl
import sys

from dotenv import load_dotenv
load_dotenv()

import uvicorn

from src import metrics

# Single-process dev mode never goes through prewarm.py — clear/recreate the
# Prometheus multiprocess directory here so /metrics works correctly even
# without gunicorn. Safe to do unconditionally: this is the only process.
metrics.clear_multiproc_dir()


def main() -> None:
    tls_mode = os.environ.get("LVF_TLS_MODE", "disabled").lower()

    kwargs: dict = {
        "app":  "src.server:app",
        "host": "0.0.0.0",
        "port": 8000,
    }

    if tls_mode in ("tls", "mtls"):
        cert_file = os.environ.get("LVF_TLS_CERT_FILE", "")
        key_file  = os.environ.get("LVF_TLS_KEY_FILE",  "")

        if not cert_file or not os.path.exists(cert_file):
            print(
                f"ERROR: LVF_TLS_CERT_FILE must be set and the file must exist "
                f"(got: {cert_file!r})",
                file=sys.stderr,
            )
            sys.exit(1)
        if not key_file or not os.path.exists(key_file):
            print(
                f"ERROR: LVF_TLS_KEY_FILE must be set and the file must exist "
                f"(got: {key_file!r})",
                file=sys.stderr,
            )
            sys.exit(1)

        kwargs["ssl_certfile"] = cert_file
        kwargs["ssl_keyfile"]  = key_file

        if tls_mode == "mtls":
            ca_file = os.environ.get("LVF_TLS_CA_FILE", "")
            if not ca_file or not os.path.exists(ca_file):
                print(
                    f"ERROR: LVF_TLS_CA_FILE must be set and the file must exist "
                    f"for mtls mode (got: {ca_file!r})",
                    file=sys.stderr,
                )
                sys.exit(1)
            kwargs["ssl_ca_certs"]  = ca_file
            kwargs["ssl_cert_reqs"] = ssl.CERT_OPTIONAL

    uvicorn.run(**kwargs)


if __name__ == "__main__":
    main()
