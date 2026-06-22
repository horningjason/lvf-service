"""Shared utilities for the LVF service."""

from __future__ import annotations

import datetime
import os
from typing import Optional, Union


def _is_temporally_active(
    effective: Optional[str],
    expire: Optional[str],
    now: datetime.datetime,
) -> bool:
    """Return True if the record is temporally active at `now`."""
    if effective:
        try:
            eff_dt = datetime.datetime.fromisoformat(effective)
            if eff_dt.tzinfo is None:
                eff_dt = eff_dt.replace(tzinfo=datetime.timezone.utc)
            if eff_dt > now:
                return False
        except ValueError:
            pass  # unparseable effective date — treat as no constraint
    if expire:
        try:
            exp_dt = datetime.datetime.fromisoformat(expire)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
            if exp_dt <= now:
                return False
        except ValueError:
            pass  # unparseable expiration date — treat as no constraint
    return True


def outbound_ssl_context() -> Union[bool, str]:
    """Return the value to pass as httpx's `verify=` for outbound calls to peer LVF nodes.

    Trusts our self-signed CA (LVF_TLS_CA_FILE) when configured, so federation
    calls succeed under LVF_TLS_MODE=tls/mtls without disabling verification.
    """
    ca_file = os.environ.get("LVF_TLS_CA_FILE", "")
    if ca_file and os.path.isfile(ca_file):
        return ca_file
    return True
