"""
RFC 5222 §10 recursive-mode helpers — loop detection, Via header handling,
and the actual recursive POST to a parent or other LoST server.

Moved out of find_service.py so federation logic doesn't live inside the
findService handler file.
"""

from __future__ import annotations

import asyncio
import logging
import time

import dns.resolver
import httpx
from lxml import etree

from src.lost.wire import lost_xml
from src.observability import metrics as _metrics
from src import runtime_state
from src.provisioning.discrepancy.discrepancy_report import (
    file_lost_dr,
    LoSTProblem, LoSTQuery, ProblemSeverity,
)
from src.utils import outbound_ssl_context

log = logging.getLogger(__name__)

_resolved_urls: dict[str, str] = {}  # cache: input URI → resolved base URL


def _has_loop(body: bytes) -> bool:
    try:
        root = etree.fromstring(body, lost_xml._XML_PARSER)
        path_el = root.find(f"{{{lost_xml._NS_LOST}}}path")
        if path_el is None:
            return False
        return any(
            via.get("source") == runtime_state._server_uri
            for via in path_el.findall(f"{{{lost_xml._NS_LOST}}}via")
        )
    except Exception:
        return False


def _add_via_to_request(body: bytes) -> bytes:
    try:
        root = etree.fromstring(body, lost_xml._XML_PARSER)
        path_el = root.find(f"{{{lost_xml._NS_LOST}}}path")
        if path_el is None:
            path_el = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}path")
        via_el = etree.SubElement(path_el, f"{{{lost_xml._NS_LOST}}}via")
        via_el.set("source", runtime_state._server_uri)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    except Exception:
        return body


def _prepend_via_to_response(response_body: bytes) -> bytes:
    try:
        root = etree.fromstring(response_body)
        path_el = root.find(f".//{{{lost_xml._NS_LOST}}}path")
        if path_el is not None:
            via_el = etree.Element(f"{{{lost_xml._NS_LOST}}}via")
            via_el.set("source", runtime_state._server_uri)
            via_el.tail = path_el.text
            path_el.insert(0, via_el)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    except Exception:
        return response_body


def _make_errors_xml(error_type: str, message: str = "") -> bytes:
    root = etree.Element(f"{{{lost_xml._NS_LOST}}}errors", nsmap={None: lost_xml._NS_LOST})
    root.set("source", runtime_state._server_uri)
    err = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}{error_type}")
    err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    if message:
        err.text = message
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _resolve_lost_url(parent_uri: str) -> str:
    """Return the HTTP base URL for parent_uri, resolving via U-NAPTR if needed.

    Results are cached in _resolved_urls keyed by input, so each DNS lookup
    happens at most once per process lifetime — including for multiple children.
    """
    if parent_uri in _resolved_urls:
        return _resolved_urls[parent_uri]

    if "://" in parent_uri:
        _resolved_urls[parent_uri] = parent_uri.rstrip("/")
        return _resolved_urls[parent_uri]

    try:
        answers = dns.resolver.resolve(parent_uri, "NAPTR")
        candidates = []
        for rr in answers:
            flags   = rr.flags.decode()   if isinstance(rr.flags,   bytes) else rr.flags
            service = rr.service.decode() if isinstance(rr.service, bytes) else rr.service
            regexp  = rr.regexp.decode()  if isinstance(rr.regexp,  bytes) else rr.regexp
            if service in ("LoST:https", "LoST:http") and flags.upper() == "U":
                proto_rank = 0 if service == "LoST:https" else 1
                candidates.append((rr.order, rr.preference, proto_rank, regexp))

        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1], c[2]))
            _, _, _, regexp = candidates[0]
            delim = regexp[0]
            parts = regexp.split(delim)
            if len(parts) >= 3 and parts[2]:
                _resolved_urls[parent_uri] = parts[2].rstrip("/")
                log.info("U-NAPTR resolved %s → %s", parent_uri, _resolved_urls[parent_uri])
                return _resolved_urls[parent_uri]
            log.warning("U-NAPTR record for %s has unparseable regexp %r — falling back to http://", parent_uri, regexp)
        else:
            log.warning("U-NAPTR lookup for %s returned no LoST records — falling back to http://", parent_uri)
    except Exception as exc:
        log.warning("U-NAPTR lookup failed for %s (%s) — falling back to http://", parent_uri, exc)

    _resolved_urls[parent_uri] = f"http://{parent_uri}"
    return _resolved_urls[parent_uri]


def _do_recurse_sync(request_body: bytes) -> bytes:
    return _do_recurse_to_uri_sync(request_body, _resolve_lost_url(runtime_state._parent_uri) + "/lost")


async def _do_recurse_async(request_body: bytes) -> bytes:
    return await _do_recurse_to_uri_async(request_body, _resolve_lost_url(runtime_state._parent_uri) + "/lost")


def _do_recurse_to_uri_sync(request_body: bytes, validate_uri: str) -> bytes:
    if _has_loop(request_body):
        return _make_errors_xml("loop", "Request loop detected — this server has already processed this request")
    modified = _add_via_to_request(request_body)
    _t0 = time.monotonic()
    log.debug("Recursion (sync): POST %s", validate_uri)
    try:
        with httpx.Client(timeout=10.0, verify=outbound_ssl_context()) as client:
            resp = client.post(
                validate_uri,
                content=modified,
                headers={"Content-Type": "application/xml"},
            )
        elapsed = time.monotonic() - _t0
        _metrics.recursion_duration_seconds.observe(elapsed)
        _metrics.recursion_total.labels(outcome="success").inc()
        log.debug(
            "Recursion (sync): %s responded in %.3fs (status=%d)",
            validate_uri, elapsed, resp.status_code,
        )
        if resp.status_code == 200:
            try:
                result = _prepend_via_to_response(resp.content)
                etree.fromstring(result)
                return result
            except Exception:
                return _make_errors_xml("serverError", "Target LVF returned unparseable XML")
        return _make_errors_xml("serverError", f"Target LVF returned HTTP {resp.status_code}")
    except httpx.TimeoutException:
        elapsed = time.monotonic() - _t0
        _metrics.recursion_duration_seconds.observe(elapsed)
        _metrics.recursion_total.labels(outcome="timeout").inc()
        log.warning("Recursion (sync): %s timed out after %.3fs", validate_uri, elapsed)
        return _make_errors_xml("serverTimeout", "Request to target LVF timed out")
    except Exception as exc:
        elapsed = time.monotonic() - _t0
        _metrics.recursion_duration_seconds.observe(elapsed)
        _metrics.recursion_total.labels(outcome="error").inc()
        log.error(
            "Recursion (sync) to %s failed after %.3fs: %s",
            validate_uri, elapsed, exc,
        )
        return _make_errors_xml("serverError", "An internal error occurred.")


async def _do_recurse_to_uri_async(request_body: bytes, validate_uri: str) -> bytes:
    if _has_loop(request_body):
        return _make_errors_xml("loop", "Request loop detected — this server has already processed this request")
    modified = _add_via_to_request(request_body)
    _t0 = time.monotonic()
    log.debug("Recursion (async): POST %s", validate_uri)
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=outbound_ssl_context()) as client:
            resp = await client.post(
                validate_uri,
                content=modified,
                headers={"Content-Type": "application/xml"},
            )
        elapsed = time.monotonic() - _t0
        _metrics.recursion_duration_seconds.observe(elapsed)
        _metrics.recursion_total.labels(outcome="success").inc()
        log.debug(
            "Recursion (async): %s responded in %.3fs (status=%d)",
            validate_uri, elapsed, resp.status_code,
        )
        if resp.status_code == 200:
            try:
                result = _prepend_via_to_response(resp.content)
                etree.fromstring(result)
                return result
            except Exception:
                err = _make_errors_xml("serverError", "Target LVF returned unparseable XML")
                asyncio.create_task(file_lost_dr(
                    query=LoSTQuery.findService,
                    request_xml=request_body.decode("utf-8", errors="replace"),
                    response_xml=resp.content.decode("utf-8", errors="replace"),
                    problem=LoSTProblem.OtherLoST,
                    severity=ProblemSeverity.Moderate,
                ))
                return err
        err = _make_errors_xml("serverError", f"Target LVF returned HTTP {resp.status_code}")
        asyncio.create_task(file_lost_dr(
            query=LoSTQuery.findService,
            request_xml=request_body.decode("utf-8", errors="replace"),
            response_xml=f"HTTP {resp.status_code}",
            problem=LoSTProblem.OtherLoST,
            severity=ProblemSeverity.Moderate,
        ))
        return err
    except httpx.TimeoutException:
        elapsed = time.monotonic() - _t0
        _metrics.recursion_duration_seconds.observe(elapsed)
        _metrics.recursion_total.labels(outcome="timeout").inc()
        log.warning("Recursion (async): %s timed out after %.3fs", validate_uri, elapsed)
        return _make_errors_xml("serverTimeout", "Request to target LVF timed out")
    except Exception as exc:
        elapsed = time.monotonic() - _t0
        _metrics.recursion_duration_seconds.observe(elapsed)
        _metrics.recursion_total.labels(outcome="error").inc()
        log.error(
            "Recursion (async) to %s failed after %.3fs: %s",
            validate_uri, elapsed, exc,
        )
        return _make_errors_xml("serverError", "An internal error occurred.")
