"""
Shared, cross-cutting runtime singletons and config.

Read by src/lost/find_service.py and by all three src/federation/ modules
(recursion.py, coverage.py, sync.py). No business logic belongs here — only
state and the minimal setup needed to initialize it.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

_log_level_name = os.environ.get("LVF_LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, None)
if not isinstance(_log_level, int):
    logging.warning("LVF_LOG_LEVEL=%r is not a valid level — defaulting to INFO", _log_level_name)
    _log_level = logging.INFO
_src_logger = logging.getLogger("src")
_src_logger.setLevel(_log_level)
if not _src_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    _h.setLevel(_log_level)
    _src_logger.addHandler(_h)
    _src_logger.propagate = False  # uvicorn's handler lives on "uvicorn", not root — prevent double-logging

from i3_fe_core.time.ntp import NtpClient

log = logging.getLogger(__name__)

_server_uri:        str = os.environ.get("LVF_SERVER_URI",         "lostserver.example.com")
_display_name_lang: str = os.environ.get("LVF_DISPLAY_NAME_LANG",  "en")
_parent_uri:        str = os.environ.get("LVF_PARENT_URI",          "")

if _parent_uri:
    if "://" in _parent_uri:
        log.warning(
            "LVF_PARENT_URI=%r looks like a direct URL — U-NAPTR resolution skipped "
            "(non-conformant per RFC 5222; acceptable for dev/testing only)",
            _parent_uri,
        )
    else:
        log.info(
            "LVF_PARENT_URI=%r is a DNS name — U-NAPTR resolution will be used on first request",
            _parent_uri,
        )

_sync_children: list[str] = [
    url.strip()
    for url in os.environ.get("LVF_SYNC_CHILDREN", "").split(",")
    if url.strip()
]

_root_ams:          bool = os.environ.get("LVF_ROOT_AMS", "").lower() == "true"
_forest_guide_uri:  str  = os.environ.get("LVF_FOREST_GUIDE_URI", "")
_forest_guide_mode: bool = os.environ.get("LVF_FOREST_GUIDE_MODE", "").lower() == "true"

if _forest_guide_uri and _root_ams:
    if "://" in _forest_guide_uri:
        log.warning(
            "LVF_FOREST_GUIDE_URI=%r looks like a direct URL — U-NAPTR resolution skipped "
            "(non-conformant per RFC 5222; acceptable for dev/testing only)",
            _forest_guide_uri,
        )
    else:
        log.info(
            "LVF_FOREST_GUIDE_URI=%r is a DNS name — U-NAPTR resolution will be used on first use",
            _forest_guide_uri,
        )

_default_mapping_source_id: str = os.environ.get("LVF_DEFAULT_MAPPING_SOURCE_ID", "")

_SERVER_START_TIME: str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

_event_loop: Optional[asyncio.AbstractEventLoop] = None

_ntp_client: Optional[NtpClient] = None

state_store = None
element_notifier = None
service_notifier = None
discrepancy = None
logging_client = None


def now() -> datetime.datetime:
    """Current UTC time, corrected by the NTP client's measured offset
    (NENA-STA-010.3f-2021 §4.3.2.4). Falls back to the host clock when no
    NTP sample is available yet or NTP is unconfigured."""
    base = datetime.datetime.now(datetime.timezone.utc)
    if _ntp_client is not None and _ntp_client.offset is not None:
        base += datetime.timedelta(seconds=_ntp_client.offset)
    return base
