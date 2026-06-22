"""
The findService request/response handler (RFC 5222 §8) plus app
startup/shutdown lifecycle.

Extracted from server.py so that server.py can be a thin FastAPI router.
All public symbols needed by tests (handle_find_service, initialize,
_parent_uri, _server_uri) are defined here and re-exported by server.py.

GIS loading lives in src/provisioning/gis/; recursion, AMS child coverage, and LoST-Sync
now live in src/federation/. This file's __getattr__ forwards legacy
find_service.<name> attribute access for those to keep server.py and
src/lost/list_services*.py working unmodified.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import NamedTuple, Optional

from dotenv import load_dotenv
load_dotenv()

# Shared runtime singletons/config (NTP client, server identity, etc.) now
# live in src.runtime_state — imported here first (before any other src.*
# import) so the "src" logger handler it configures is in place before any
# other src.* module logs anything at import time.
from src import runtime_state
from src.lost.wire import lost_xml
from src.lost.wire import gml_xml

import datetime
from fastapi import Response
from lxml import etree

from src.observability.logging_events.logger import emit_log_event, make_query_event, make_response_event
from src.observability.logging_events.log_events import generate_query_id
from src.provisioning.discrepancy.discrepancy_report import file_lost_dr, LoSTProblem, LoSTQuery
from src.provisioning.gis import provisioning as gis_provisioning
from src.provisioning.gis import records as gis_records
from src.federation import recursion as fed_recursion
from src.federation import coverage as fed_coverage
from src.federation import sync as fed_sync
from src.app import lifecycle
from src.lost.wire import response_xml
from src.validation import gate0, gate1, gate2, response_assembly
from src.utils import _is_temporally_active
from src.validation.models import (
    BadRequestResponse,
    CivicAddress,
    ForbiddenResponse,
    LocationValidationUnavailableResponse,
    MappingElement,
    NotFoundResponse,
    RedirectResponse,
    ServiceNotImplementedResponse,
    ValidationRequest,
)

log = logging.getLogger(__name__)

# GIS data (loaded at startup) now lives in src.provisioning.gis.provisioning. Forward
# find_service.<name> attribute access for these names there, since
# src/server.py and src/lost/list_services*.py read them that way.
_GIS_STATE_ATTRS: frozenset[str] = frozenset({
    "_ssap", "_rcl", "_boundaries", "_geodetic_coverage", "_civic_coverage",
    "_ssap_index", "_rcl_index", "_reloading", "_reloading_lock", "_gis_last_loaded",
})

# Shared runtime singletons/config now live in src.runtime_state. Forward
# find_service.<name> attribute access for these there too, since
# src/server.py, src/lost/list_services*.py, and src/provisioning/discrepancy/
# discrepancy_report.py all read these as find_service.<name>.
_RUNTIME_STATE_ATTRS: frozenset[str] = frozenset({
    "_src_logger", "_server_uri", "_display_name_lang", "_parent_uri",
    "_sync_children", "_default_mapping_source_id", "_SERVER_START_TIME",
    "_event_loop", "_ntp_client", "_forest_guide_mode",
})

# XML namespace constants and the safe XML parser config now live in
# src.lost.wire.lost_xml. Forward find_service.<name> attribute access for these too —
# src/server.py reads _NS_LOST and _XML_PARSER this way (confirmed via the
# test suite, not just inspection: removing this shim broke test_recursion.py
# at collection time with AttributeError, since server.py does
# `_NS_LOST = _fs._NS_LOST` and `etree.fromstring(body, _fs._XML_PARSER)`
# at module scope).
_LOST_XML_ATTRS: frozenset[str] = frozenset({
    "_XML_PARSER", "_NS_LOST", "_NS_EXT_IDS", "_NS_CA", "_NS_CAE",
    "_NS_CDX1", "_NS_CDX2", "_NS_RLI", "_NS_PLANNED", "_NS_SYNC", "_NS_GML",
})

# AMS child coverage state and provisioning-file handling now live in
# src.federation.coverage. Forward find_service.<name> attribute access for
# these too — confirmed via the test suite, not just grep, that this is
# needed: server.py reads _is_leader this way (_maybe_start_sip), and
# list_services_by_location.py reads _child_coverage and
# _lookup_child_coverage this way (routing-only mode lookup).
_COVERAGE_ATTRS: frozenset[str] = frozenset({
    "_child_coverage", "_coverage_lock", "_ams_provisioning_cache",
    "_leader_lock_fd", "_is_leader", "_root_ams_active",
    "_child_coverage_path", "_read_child_coverage_file", "_load_child_coverage",
    "_save_child_coverage_list", "_coverage_file_lock",
    "_with_coverage_write", "_reapply_ams_provisioning", "_watch_child_coverage",
    "_start_coverage_watcher", "_leader_lock_path", "_acquire_leadership",
    "_parse_iso_timestamp", "_compare_timestamps", "_upsert_child_coverage",
    "_lookup_child_coverage", "_ams_provisioning_dir", "_validate_ams_civic_file",
    "_validate_ams_geodetic_file", "_load_ams_provisioning",
})

# The RFC 6739 LoST-Sync protocol now lives in src.federation.sync. Forward
# find_service.<name> attribute access for these too — confirmed via the
# test suite, not just grep: server.py's /sync route handler calls
# _fs.handle_sync(...) this way.
_SYNC_ATTRS: frozenset[str] = frozenset({
    "_gis_last_updated_str", "_build_civic_coverage_mapping_xml",
    "_build_geodetic_coverage_mapping_xml", "_parse_sync_mapping",
    "_child_entry_to_mapping_xml", "_sync_error_response",
    "_handle_push_mappings", "_handle_get_mappings", "_get_parent_sync_uri",
    "_get_fg_sync_uri", "_push_coverage_to_parent", "_push_coverage_to_fg",
    "_pull_from_child", "_retry_sync_op", "_startup_sync",
    "_maybe_schedule_repush", "handle_sync", "_background_sync_tasks",
    "_SYNC_BACKOFF_INITIAL", "_SYNC_BACKOFF_CAP",
})

# Server lifecycle (schema/NTP setup, GIS initialization, FastAPI lifespan
# startup/shutdown) now lives in src.app.lifecycle. Forward
# find_service.<name> attribute access for these too — server.py imports
# initialize/_validate_schema and calls _fs.lifespan_startup()/
# lifespan_shutdown(), prewarm.py calls find_service.prewarm(), and tests
# call _fs.initialize() / read _fs._routing_only this way.
_LIFECYCLE_ATTRS: frozenset[str] = frozenset({
    "initialize", "prewarm", "lifespan_startup", "lifespan_shutdown",
    "_routing_only",
})


def __getattr__(name: str):
    # Read-only forwarding: a write like find_service._ssap = ... would NOT
    # come through here (module __getattr__ only fires on a failed lookup,
    # never on assignment) — it would silently create a stale local
    # attribute on this module instead of writing through to
    # gis_provisioning/runtime_state/lost_xml/fed_coverage/fed_sync,
    # bypassing this shim entirely.
    if name in _GIS_STATE_ATTRS:
        return getattr(gis_provisioning, name)
    if name in _RUNTIME_STATE_ATTRS:
        return getattr(runtime_state, name)
    if name in _LOST_XML_ATTRS:
        return getattr(lost_xml, name)
    if name in _COVERAGE_ATTRS:
        return getattr(fed_coverage, name)
    if name in _SYNC_ATTRS:
        return getattr(fed_sync, name)
    if name in _LIFECYCLE_ATTRS:
        return getattr(lifecycle, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

class RequestContext(NamedTuple):
    call_id: Optional[str]
    incident_tracking_id: Optional[str]


# ---------------------------------------------------------------------------
# GIS field mapping (CLARK_TO_FIELD, PIDF_PREFIX_NS, SSAP_ATTR,
# pidf_lo_to_clark) and row/dict conversion are in src.provisioning.gis.records; the GIS
# data store itself is in src.provisioning.gis.provisioning (see _GIS_STATE_ATTRS above).
# ---------------------------------------------------------------------------

# Load shedding (spec §3.11.5) — both disabled (0) by default so existing
# deployments are unaffected until explicitly tuned. Per-worker, not
# cross-worker accurate: effective capacity scales with LVF_WORKERS.
_max_concurrent_requests:   int = int(os.environ.get("LVF_MAX_CONCURRENT_REQUESTS", "0"))
_rate_limit_per_source:     int = int(os.environ.get("LVF_RATE_LIMIT_PER_SOURCE", "0"))
_rate_limit_window_seconds: int = int(os.environ.get("LVF_RATE_LIMIT_WINDOW_SECONDS", "60"))

_ADMIN_PIDF_LO: frozenset[str] = frozenset({
    "ca:country", "ca:A1", "ca:A2", "ca:A3", "ca:A4", "ca:A5",
})

# LoST-Sync protocol state (retry tasks, backoff constants) is in
# src.federation.sync; child coverage / AMS provisioning / leader-election
# state is in src.federation.coverage (see _SYNC_ATTRS / _COVERAGE_ATTRS
# above for their respective __getattr__ shim entries). Server lifecycle
# (_routing_only, _schema/_load_schema, initialize, prewarm, lifespan_*) is
# in src.app.lifecycle (see _LIFECYCLE_ATTRS above).


def _validate_schema(body: bytes) -> Optional[str]:
    if lifecycle._schema is None:
        return None
    try:
        doc = etree.fromstring(body, lost_xml._XML_PARSER)
    except etree.XMLSyntaxError as exc:
        log.error("Schema validation: XML parse failed: %s", exc)
        return "Malformed XML."
    if lifecycle._schema.validate(doc):
        return None
    error_log = lifecycle._schema.error_log
    if error_log:
        first = error_log[0]
        return f"{first.message} (line {first.line})"
    return "Request does not conform to the LoST findService schema"


_sos_alias_urns: frozenset[str] = frozenset(
    urn.strip().lower()
    for urn in os.environ.get("LVF_SOS_ALIAS_URNS", "").split(",")
    if urn.strip()
)


def _resolve_service_urn(requested_urn: str) -> tuple[str, bool]:
    if requested_urn.lower() in _sos_alias_urns:
        return "urn:service:sos", True
    return requested_urn, False


# ---------------------------------------------------------------------------
# GeoPackage row helpers, attribute index, and _load_gis_data/_watch_gpkg —
# now in src.provisioning.gis.records / src.provisioning.gis.provisioning.
# ---------------------------------------------------------------------------


def _default_mapping_factory(service_urn: str) -> MappingElement:
    return gis_records.build_default_mapping(
        service_urn,
        server_start_time=runtime_state._SERVER_START_TIME,
        server_uri=runtime_state._server_uri,
        default_mapping_source_id=runtime_state._default_mapping_source_id,
        display_name_lang=runtime_state._display_name_lang,
    )


# lookup_civic_coverage() now lives in src.provisioning.gis.provisioning.

# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _parse_request(body: bytes) -> tuple[ValidationRequest, Optional[datetime.datetime]]:
    try:
        root = etree.fromstring(body, lost_xml._XML_PARSER)
    except etree.XMLSyntaxError as exc:
        log.error("Request parse: XML syntax error: %s", exc)
        raise ValueError("Malformed XML.") from exc

    if root.tag != f"{{{lost_xml._NS_LOST}}}findService":
        local = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        raise ValueError(
            f"Expected findService; received {local!r} — "
            "use POST /lost for listServices and listServicesByLocation"
        )

    service_el = root.find(f"{{{lost_xml._NS_LOST}}}service")
    if service_el is None:
        raise ValueError("Missing 'service' element in findService request")
    service_urn = (service_el.text or "").strip()
    if not service_urn:
        raise ValueError("'service' element is empty")

    civic_el = root.find(f".//{{{lost_xml._NS_CA}}}civicAddress")
    if civic_el is None:
        raise ValueError("Missing 'civicAddress' element in findService request")

    fields: dict[str, str] = {}
    for child in civic_el:
        ca_field = gis_records.CLARK_TO_FIELD.get(child.tag)
        if ca_field is not None:
            fields[ca_field] = (child.text or "").strip()

    as_of: Optional[datetime.datetime] = None
    as_of_el = root.find(f"{{{lost_xml._NS_PLANNED}}}asOf")
    if as_of_el is not None and as_of_el.text:
        try:
            text = as_of_el.text.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            ts = datetime.datetime.fromisoformat(text)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            as_of = ts
        except ValueError:
            pass

    validate_location = root.get("validateLocation", "false")

    return ValidationRequest(
        service_urn=service_urn,
        civic_address=CivicAddress(**fields),
        validate_location=validate_location,
    ), as_of


def _parse_return_additional_location(body: bytes) -> str:
    _VALID = {"none", "similar", "complete", "any"}
    try:
        root = etree.fromstring(body, lost_xml._XML_PARSER)
        val = root.get(f"{{{lost_xml._NS_RLI}}}returnAdditionalLocation")
        if val is None:
            return "complete"
        return val if val in _VALID else "complete"
    except Exception:
        return "complete"


def _parse_recursive(body: bytes) -> bool:
    try:
        root = etree.fromstring(body, lost_xml._XML_PARSER)
        return root.get("recursive", "false").lower() == "true"
    except Exception:
        return False


def _extract_call_incident_ids(root: etree._Element) -> tuple[Optional[str], Optional[str]]:
    """
    Extract callId and incidentTrackingId from an <emergencyCallIncidentId>
    extension element in any LoST request (namespace urn:emergency:xml:ns:lostExt:Ids).
    Returns (call_id, incident_tracking_id) — either or both may be None if absent.
    """
    el = root.find(f".//{{{lost_xml._NS_EXT_IDS}}}emergencyCallIncidentId")
    if el is None:
        return None, None
    return el.get("callId") or None, el.get("incidentTrackingId") or None


# ---------------------------------------------------------------------------
# XML serialization
# ---------------------------------------------------------------------------

# _mapping_element, _serialize_find_service_response, _serialize_redirect,
# _serialize_errors, _serialize_complete_location, _complete_location_ssap,
# _complete_location_rcl, _emit_complete_location, and _RESPONSE_NSMAP /
# _RCL_SHARED_STREET / _RCL_SIDE_SPECIFIC_BASE / _ADMIN_FIELDS now live in
# src.lost.wire.response_xml.

def _to_xml_response(
    resp,
    status: int,
    as_of_used: Optional[datetime.datetime] = None,
) -> Response:
    if resp.type == "locationValidation":
        tree = response_xml._serialize_find_service_response(resp, as_of_used=as_of_used)
    elif resp.type == "redirect":
        tree = response_xml._serialize_redirect(resp)
    elif resp.type == "locationValidationUnavailable":
        root = etree.Element(
            f"{{{lost_xml._NS_LOST}}}findServiceResponse",
            nsmap=response_xml._RESPONSE_NSMAP,
        )
        w = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}warnings")
        w.set("source", runtime_state._server_uri)
        lvu = etree.SubElement(w, f"{{{lost_xml._NS_LOST}}}locationValidationUnavailable")
        lvu.set("message", getattr(resp, "message", "LVF temporarily cannot fulfill validation request"))
        lvu.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
        path_el = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}path")
        via_el  = etree.SubElement(path_el, f"{{{lost_xml._NS_LOST}}}via")
        via_el.set("source", runtime_state._server_uri)
        tree = root
    else:
        tree = response_xml._serialize_errors(resp)

    body = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, status_code=status, media_type="application/xml")


# ---------------------------------------------------------------------------
# Out-of-coverage admin redirect helper
# ---------------------------------------------------------------------------

def _check_ooc_admin(address: CivicAddress, g2):
    if g2.outcome != "invalid" or g2.state.invalid not in _ADMIN_PIDF_LO:
        return None
    invalid_field = g2.state.invalid
    a3 = address.a3 if invalid_field not in ("ca:A3", "ca:A4", "ca:A5") else None
    a4 = address.a4 if invalid_field not in ("ca:A4", "ca:A5") else None
    a5 = address.a5 if invalid_field != "ca:A5" else None
    coverage = gis_provisioning.lookup_civic_coverage(
        address.country, address.a1, address.a2,
        a3, a4, a5,
    )
    if coverage is not None:
        return None
    if runtime_state._parent_uri:
        return RedirectResponse(target=runtime_state._parent_uri, source=runtime_state._server_uri)
    return NotFoundResponse(
        message="Location is outside this LVF's coverage area and no parent LVF is configured"
    )


# ---------------------------------------------------------------------------
# Federation logic (RFC 5222 §10 recursion, RFC 6739 LoST-Sync, AMS child
# coverage) all moved to src/federation/ this session:
#   - recursion (loop detection, Via headers, recursive POST) -> recursion.py
#   - child coverage store, leader election, AMS provisioning -> coverage.py
#   - mapping builders/parser, sync endpoint handlers, push/pull,
#     startup sync, handle_sync() -> sync.py
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Programmatic entry point (for test harnesses — bypasses HTTP)
# ---------------------------------------------------------------------------

def handle_find_service(xml_bytes: bytes) -> bytes:
    """
    Process a raw LoST findService XML request and return raw XML response bytes.

    Mirrors the /lost endpoint without requiring an HTTP request object.
    Returns <badRequest> bytes for malformed or structurally incomplete XML.
    Call initialize() once before using this.
    """
    query_id = generate_query_id()
    timestamp = runtime_state._ntp_client.get_current_time()
    call_id: Optional[str] = None
    incident_tracking_id: Optional[str] = None

    def _respond(result: bytes, *, response_status: Optional[str] = None) -> bytes:
        emit_log_event(make_response_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            response_id=query_id,
            direction="outgoing",
            response_adapter=result.decode("utf-8", errors="replace"),
            response_status=response_status,
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        return result

    def _recurse_outgoing(request_body: bytes) -> bytes:
        out_qid = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            query_id=out_qid,
            direction="outgoing",
            query_adapter=request_body.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        result = fed_recursion._do_recurse_sync(request_body)
        emit_log_event(make_response_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            response_id=out_qid,
            direction="incoming",
            response_adapter=result.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        return result

    def _recurse_to_uri_outgoing(request_body: bytes, uri: str) -> bytes:
        out_qid = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            query_id=out_qid,
            direction="outgoing",
            query_adapter=request_body.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        result = fed_recursion._do_recurse_to_uri_sync(request_body, uri)
        emit_log_event(make_response_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            response_id=out_qid,
            direction="incoming",
            response_adapter=result.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        return result

    with gis_provisioning._reloading_lock:
        if gis_provisioning._reloading:
            return _respond(_to_xml_response(
                LocationValidationUnavailableResponse(
                    message="LVF is reloading GIS data — service will resume automatically"
                ),
                status=200,
            ).body)

    schema_error = _validate_schema(xml_bytes)
    if schema_error is not None:
        emit_log_event(make_query_event(
            timestamp=timestamp,
            query_id=query_id,
            direction="incoming",
            malformed_query=xml_bytes.decode("utf-8", errors="replace")[:2048],
        ))
        return _respond(
            _to_xml_response(BadRequestResponse(message=schema_error), status=200).body,
            response_status="400",
        )

    try:
        req, as_of_raw = _parse_request(xml_bytes)
    except ValueError as exc:
        emit_log_event(make_query_event(
            timestamp=timestamp,
            query_id=query_id,
            direction="incoming",
            malformed_query=xml_bytes.decode("utf-8", errors="replace")[:2048],
        ))
        return _respond(
            _to_xml_response(BadRequestResponse(message=str(exc)), status=200).body,
            response_status="400",
        )

    recursive = _parse_recursive(xml_bytes)

    _req_root = etree.fromstring(xml_bytes, lost_xml._XML_PARSER)
    call_id, incident_tracking_id = _extract_call_incident_ids(_req_root)
    ctx = RequestContext(call_id=call_id, incident_tracking_id=incident_tracking_id)
    if call_id or incident_tracking_id:
        log.debug("LoST request: callId=%s incidentTrackingId=%s", call_id, incident_tracking_id)

    emit_log_event(make_query_event(
        timestamp=timestamp,
        query_id=query_id,
        direction="incoming",
        query_adapter=xml_bytes.decode("utf-8", errors="replace"),
        call_id=call_id,
        incident_id=incident_tracking_id,
    ))

    if req.validate_location != "true":
        return _respond(_to_xml_response(ForbiddenResponse(), status=200).body)

    if runtime_state._forest_guide_mode:
        effective_urn, _ = _resolve_service_urn(req.service_urn)
        if effective_urn.lower() != "urn:service:sos":
            return _respond(_to_xml_response(ServiceNotImplementedResponse(), status=200).body)
        child_match = fed_coverage._lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("lost_server", "")
            if child_uri:
                return _respond(_to_xml_response(
                    RedirectResponse(target=child_uri, source=runtime_state._server_uri), status=200
                ).body)
            log.warning(
                "Forest Guide: child coverage match found but lost_server is empty "
                "(source=%s) — returning notFound",
                child_match.get("source", ""),
            )
        return _respond(_to_xml_response(
            NotFoundResponse(message="No authoritative LVF tree found for this location. The Forest Guide has no registered coverage region matching the submitted civic address."),
            status=200,
        ).body)

    if fed_coverage._child_coverage and lifecycle._routing_only:
        child_match = fed_coverage._lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("lost_server", "")
            if child_uri:
                if recursive:
                    return _respond(_recurse_to_uri_outgoing(xml_bytes, child_uri))
                return _respond(_to_xml_response(
                    RedirectResponse(target=child_uri, source=runtime_state._server_uri), status=200
                ).body)
            log.warning(
                "LoST-Sync: child coverage match found but lost_server is empty "
                "(source=%s) — falling through to local processing",
                child_match.get("source", ""),
            )

    if lifecycle._routing_only:
        if runtime_state._parent_uri:
            if recursive:
                return _respond(_recurse_outgoing(xml_bytes))
            return _respond(_to_xml_response(
                RedirectResponse(target=runtime_state._parent_uri, source=runtime_state._server_uri), status=200
            ).body)
        return _respond(_to_xml_response(
            LocationValidationUnavailableResponse(
                message="This node has no GIS data and no configured parent for this location"
            ),
            status=200,
        ).body)

    effective_urn, is_alias = _resolve_service_urn(req.service_urn)

    now = runtime_state._ntp_client.get_current_time()
    as_of_used: Optional[datetime.datetime] = None
    if as_of_raw is not None and as_of_raw > now:
        now = as_of_raw
        as_of_used = as_of_raw

    g0 = gate0.check(effective_urn, gis_provisioning._boundaries, now)
    if g0 is not None:
        return _respond(_to_xml_response(g0, status=200).body)

    g1 = gate1.check(req.civic_address)
    if g1 is not None:
        return _respond(_to_xml_response(g1, status=200).body)

    matched_boundaries = [
        b for b in gis_provisioning._boundaries
        if b.service_urn.lower() == effective_urn.lower()
        and _is_temporally_active(b.effective, b.expires, now)
    ]
    ral = _parse_return_additional_location(xml_bytes)
    _ssap_cand, _rcl_cand = gis_provisioning._candidates_for(req.civic_address)
    g2 = gate2.run(req.civic_address, _ssap_cand, _rcl_cand, now)

    ooc = _check_ooc_admin(req.civic_address, g2)
    if ooc is not None:
        if recursive and isinstance(ooc, RedirectResponse):
            return _respond(_recurse_outgoing(xml_bytes))
        return _respond(_to_xml_response(ooc, status=200).body)

    final = response_assembly.assemble(
        g2,
        matched_boundaries,
        service_urn=req.service_urn,
        address=req.civic_address,
        civic_coverage_lookup=gis_provisioning.lookup_civic_coverage,
        default_mapping_factory=_default_mapping_factory,
        return_additional_location=ral,
        server_uri=runtime_state._server_uri,
        display_name_lang=runtime_state._display_name_lang,
    )
    if is_alias and final.type == "locationValidation":
        for m in final.mapping:
            m.service_urn = req.service_urn
    return _respond(_to_xml_response(final, status=200, as_of_used=as_of_used).body)


# ---------------------------------------------------------------------------
# Async entry point (for HTTP /lost endpoint)
# ---------------------------------------------------------------------------

async def handle_find_service_async(xml_bytes: bytes, client_addr: Optional[str] = None) -> bytes:
    """Async variant of handle_find_service — uses async HTTP for recursion calls."""
    query_id = generate_query_id()
    timestamp = runtime_state._ntp_client.get_current_time()
    call_id: Optional[str] = None
    incident_tracking_id: Optional[str] = None

    def _respond(result: bytes, *, response_status: Optional[str] = None) -> bytes:
        emit_log_event(make_response_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            response_id=query_id,
            direction="outgoing",
            response_adapter=result.decode("utf-8", errors="replace"),
            response_status=response_status,
            call_id=call_id,
            incident_id=incident_tracking_id,
            ip_address_port=client_addr,
        ))
        return result

    async def _recurse_out(request_body: bytes) -> bytes:
        out_qid = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            query_id=out_qid,
            direction="outgoing",
            query_adapter=request_body.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        result = await fed_recursion._do_recurse_async(request_body)
        emit_log_event(make_response_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            response_id=out_qid,
            direction="incoming",
            response_adapter=result.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        return result

    async def _recurse_to_uri_out(request_body: bytes, uri: str) -> bytes:
        out_qid = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            query_id=out_qid,
            direction="outgoing",
            query_adapter=request_body.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        result = await fed_recursion._do_recurse_to_uri_async(request_body, uri)
        emit_log_event(make_response_event(
            timestamp=runtime_state._ntp_client.get_current_time(),
            response_id=out_qid,
            direction="incoming",
            response_adapter=result.decode("utf-8", errors="replace"),
            call_id=call_id,
            incident_id=incident_tracking_id,
        ))
        return result

    with gis_provisioning._reloading_lock:
        if gis_provisioning._reloading:
            return _respond(_to_xml_response(
                LocationValidationUnavailableResponse(
                    message="LVF is busy loading newer GIS data — service will resume automatically"
                ),
                status=200,
            ).body)

    schema_error = _validate_schema(xml_bytes)
    if schema_error is not None:
        emit_log_event(make_query_event(
            timestamp=timestamp,
            query_id=query_id,
            direction="incoming",
            malformed_query=xml_bytes.decode("utf-8", errors="replace")[:2048],
            ip_address_port=client_addr,
        ))
        return _respond(
            _to_xml_response(BadRequestResponse(message=schema_error), status=200).body,
            response_status="400",
        )

    try:
        req, as_of_raw = _parse_request(xml_bytes)
    except ValueError as exc:
        emit_log_event(make_query_event(
            timestamp=timestamp,
            query_id=query_id,
            direction="incoming",
            malformed_query=xml_bytes.decode("utf-8", errors="replace")[:2048],
            ip_address_port=client_addr,
        ))
        return _respond(
            _to_xml_response(BadRequestResponse(message=str(exc)), status=200).body,
            response_status="400",
        )

    recursive = _parse_recursive(xml_bytes)

    _req_root = etree.fromstring(xml_bytes, lost_xml._XML_PARSER)
    call_id, incident_tracking_id = _extract_call_incident_ids(_req_root)
    ctx = RequestContext(call_id=call_id, incident_tracking_id=incident_tracking_id)
    if call_id or incident_tracking_id:
        log.debug("LoST request: callId=%s incidentTrackingId=%s", call_id, incident_tracking_id)

    emit_log_event(make_query_event(
        timestamp=timestamp,
        query_id=query_id,
        direction="incoming",
        query_adapter=xml_bytes.decode("utf-8", errors="replace"),
        call_id=call_id,
        incident_id=incident_tracking_id,
        ip_address_port=client_addr,
    ))

    if req.validate_location != "true":
        return _respond(_to_xml_response(ForbiddenResponse(), status=200).body)

    if runtime_state._forest_guide_mode:
        effective_urn, _ = _resolve_service_urn(req.service_urn)
        if effective_urn.lower() != "urn:service:sos":
            return _respond(_to_xml_response(ServiceNotImplementedResponse(), status=200).body)
        child_match = fed_coverage._lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("lost_server", "")
            if child_uri:
                return _respond(_to_xml_response(
                    RedirectResponse(target=child_uri, source=runtime_state._server_uri), status=200
                ).body)
            log.warning(
                "Forest Guide: child coverage match found but lost_server is empty "
                "(source=%s) — returning notFound",
                child_match.get("source", ""),
            )
        return _respond(_to_xml_response(
            NotFoundResponse(message="No authoritative LVF tree found for this location. The Forest Guide has no registered coverage region matching the submitted civic address."),
            status=200,
        ).body)

    effective_urn, is_alias = _resolve_service_urn(req.service_urn)

    if fed_coverage._child_coverage and lifecycle._routing_only:
        child_match = fed_coverage._lookup_child_coverage(
            req.civic_address.country, req.civic_address.a1, req.civic_address.a2,
            req.civic_address.a3, req.civic_address.a4, req.civic_address.a5,
        )
        if child_match:
            child_uri = child_match.get("lost_server", "")
            if child_uri:
                if recursive:
                    return _respond(await _recurse_to_uri_out(xml_bytes, child_uri))
                return _respond(_to_xml_response(
                    RedirectResponse(target=child_uri, source=runtime_state._server_uri), status=200
                ).body)
            log.warning(
                "LoST-Sync: child coverage match found but lost_server is empty "
                "(source=%s) — falling through to local processing",
                child_match.get("source", ""),
            )

    if lifecycle._routing_only:
        if runtime_state._parent_uri:
            if recursive:
                return _respond(await _recurse_out(xml_bytes))
            return _respond(_to_xml_response(
                RedirectResponse(target=runtime_state._parent_uri, source=runtime_state._server_uri), status=200
            ).body)
        return _respond(_to_xml_response(
            LocationValidationUnavailableResponse(
                message="This node has no GIS data and no configured parent for this location"
            ),
            status=200,
        ).body)

    now = runtime_state._ntp_client.get_current_time()
    as_of_used: Optional[datetime.datetime] = None
    if as_of_raw is not None and as_of_raw > now:
        now = as_of_raw
        as_of_used = as_of_raw

    g0 = gate0.check(effective_urn, gis_provisioning._boundaries, now)
    if g0 is not None:
        return _respond(_to_xml_response(g0, status=200).body)

    g1 = gate1.check(req.civic_address)
    if g1 is not None:
        return _respond(_to_xml_response(g1, status=200).body)

    matched_boundaries = [
        b for b in gis_provisioning._boundaries
        if b.service_urn.lower() == effective_urn.lower()
        and _is_temporally_active(b.effective, b.expires, now)
    ]
    ral = _parse_return_additional_location(xml_bytes)
    _ssap_cand, _rcl_cand = gis_provisioning._candidates_for(req.civic_address)
    g2 = gate2.run(req.civic_address, _ssap_cand, _rcl_cand, now)

    ooc = _check_ooc_admin(req.civic_address, g2)
    if ooc is not None:
        if recursive and isinstance(ooc, RedirectResponse):
            return _respond(await _recurse_out(xml_bytes))
        return _respond(_to_xml_response(ooc, status=200).body)

    final = response_assembly.assemble(
        g2,
        matched_boundaries,
        service_urn=req.service_urn,
        address=req.civic_address,
        civic_coverage_lookup=gis_provisioning.lookup_civic_coverage,
        default_mapping_factory=_default_mapping_factory,
        return_additional_location=ral,
        server_uri=runtime_state._server_uri,
        display_name_lang=runtime_state._display_name_lang,
    )
    if is_alias and final.type == "locationValidation":
        for m in final.mapping:
            m.service_urn = req.service_urn
    response_bytes = _to_xml_response(final, status=200, as_of_used=as_of_used).body
    if final.type == "notFound":
        asyncio.create_task(file_lost_dr(
            query=LoSTQuery.findService,
            request_xml=xml_bytes.decode("utf-8", errors="replace"),
            response_xml=response_bytes.decode("utf-8", errors="replace"),
            problem=LoSTProblem.BelievedValid,
        ))
    return _respond(response_bytes)


# prewarm, _on_gpkg_reload, lifespan_startup, lifespan_shutdown, initialize,
# _setup_ntp_and_schema, _load_schema, _schema, and _routing_only now live in
# src.app.lifecycle (see _LIFECYCLE_ATTRS above).
