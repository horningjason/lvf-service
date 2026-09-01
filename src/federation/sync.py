"""
RFC 6739 LoST-Sync protocol — coverage mapping XML construction,
pushMappings/getMappingsRequest handling, and the push/pull background
sync loop (startup sync, retry-with-backoff, opportunistic repush after
GIS reload).

Depends on src.runtime_state, src.gis.provisioning,
src.lost.wire.gml_xml, src.lost.wire.lost_xml, src.federation.recursion, and
src.federation.coverage. No dependency on src.lost.find_service.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx
from fastapi import Response
from lxml import etree

from src.lost.wire import gml_xml
from src.lost.wire import lost_xml
from src import runtime_state
from src.federation import coverage as fed_coverage
from src.federation import recursion as fed_recursion
from src.gis import provisioning as gis_provisioning
from src.utils import outbound_ssl_context
from src.logging.log_events import generate_query_id
from src.logging.logger import emit_log_event, make_query_event, make_response_event

log = logging.getLogger(__name__)

# Long-running LoST-Sync retry tasks spawned by _startup_sync() (one per
# configured child, plus parent-push and/or Forest-Guide-push). Each retries
# indefinitely (exponential backoff up to _SYNC_BACKOFF_CAP seconds) while its
# target is unreachable, so they must be cancelled explicitly on shutdown
# (lifespan_shutdown()) rather than left to keep the event loop alive past
# gunicorn's graceful_timeout.
_background_sync_tasks: list[asyncio.Task] = []


# ---------------------------------------------------------------------------
# LoST-Sync — coverage region mapping builders
# ---------------------------------------------------------------------------

def _gis_last_updated_str() -> str:
    last = gis_provisioning._gis_last_loaded
    return last.strftime("%Y-%m-%dT%H:%M:%SZ") if last else runtime_state._SERVER_START_TIME


def _build_civic_coverage_mapping_xml() -> Optional[str]:
    src_id = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    if not src_id or not gis_provisioning._civic_coverage:
        return None

    mapping_el = etree.Element(f"{{{lost_xml._NS_LOST}}}mapping")
    mapping_el.set("expires",     "NO-EXPIRATION")
    mapping_el.set("lastUpdated", _gis_last_updated_str())
    mapping_el.set("source",      runtime_state._server_uri)
    mapping_el.set("sourceId",    src_id)

    dn = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}displayName")
    dn.set("{http://www.w3.org/XML/1998/namespace}lang", runtime_state._display_name_lang)
    dn.text = f"{runtime_state._server_uri} civic coverage"

    svc = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}service")
    svc.text = "urn:service:sos"

    sb = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}serviceBoundary")
    sb.set("profile", "civic")

    seen: set = set()
    for entry in gis_provisioning._civic_coverage:
        key = (entry.country, entry.a1, entry.a2, entry.a3, entry.a4, entry.a5)
        if key in seen:
            continue
        seen.add(key)
        ca = etree.SubElement(sb, f"{{{lost_xml._NS_CA}}}civicAddress")
        _e = etree.SubElement(ca, f"{{{lost_xml._NS_CA}}}country")
        _e.text = entry.country
        _e = etree.SubElement(ca, f"{{{lost_xml._NS_CA}}}A1")
        _e.text = entry.a1
        _e = etree.SubElement(ca, f"{{{lost_xml._NS_CA}}}A2")
        _e.text = entry.a2
        for field, val in (("A3", entry.a3), ("A4", entry.a4), ("A5", entry.a5)):
            if val is not None and val != "*":
                _e = etree.SubElement(ca, f"{{{lost_xml._NS_CA}}}{field}")
                _e.text = val

    etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}uri")

    return etree.tostring(mapping_el, encoding="unicode")


def _build_geodetic_coverage_mapping_xml() -> Optional[str]:
    src_id = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
    if not src_id or not gis_provisioning._geodetic_coverage:
        return None

    geom = gis_provisioning._geodetic_coverage.get("urn:service:sos")
    if geom is None:
        geom = next(iter(gis_provisioning._geodetic_coverage.values()))

    mapping_el = etree.Element(f"{{{lost_xml._NS_LOST}}}mapping")
    mapping_el.set("expires",     "NO-EXPIRATION")
    mapping_el.set("lastUpdated", _gis_last_updated_str())
    mapping_el.set("source",      runtime_state._server_uri)
    mapping_el.set("sourceId",    src_id)

    dn = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}displayName")
    dn.set("{http://www.w3.org/XML/1998/namespace}lang", runtime_state._display_name_lang)
    dn.text = f"{runtime_state._server_uri} geodetic coverage"

    svc = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}service")
    svc.text = "urn:service:sos"

    sb = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}serviceBoundary")
    sb.set("profile", "geodetic-2d")

    try:
        gml_el = gml_xml._shapely_to_gml(geom)
        sb.append(gml_el)
    except Exception as exc:
        log.warning("LoST-Sync: could not serialize geodetic coverage to GML: %s", exc)
        return None

    etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}uri")

    return etree.tostring(mapping_el, encoding="unicode")


# ---------------------------------------------------------------------------
# LoST-Sync — mapping element parser
# ---------------------------------------------------------------------------

def _parse_sync_mapping(mapping_el: etree._Element, child_uri_hint: str = "") -> dict:
    source    = mapping_el.get("source", "")
    source_id = mapping_el.get("sourceId", "")
    last_updated = mapping_el.get("lastUpdated", "")
    expires      = mapping_el.get("expires", "NO-EXPIRATION")

    service_el = mapping_el.find(f"{{{lost_xml._NS_LOST}}}service")
    service = (service_el.text or "").strip() if service_el is not None else "urn:service:sos"

    dn_el = mapping_el.find(f"{{{lost_xml._NS_LOST}}}displayName")
    display_name = (dn_el.text or "").strip() if dn_el is not None else ""

    sb_el = mapping_el.find(f"{{{lost_xml._NS_LOST}}}serviceBoundary")
    profile           = ""
    civic_addresses   = None
    geodetic_geom_wkt = None

    if sb_el is not None:
        profile = sb_el.get("profile", "")

        if profile == "civic":
            civic_addresses = []
            for ca_el in sb_el.findall(f".//{{{lost_xml._NS_CA}}}civicAddress"):
                t: dict[str, Optional[str]] = {}
                for field, tag in [
                    ("country", "country"), ("a1", "A1"), ("a2", "A2"),
                    ("a3", "A3"),           ("a4", "A4"), ("a5", "A5"),
                ]:
                    el = ca_el.find(f"{{{lost_xml._NS_CA}}}{tag}")
                    t[field] = el.text.strip() if (el is not None and el.text) else ("*" if field in ("a3", "a4", "a5") else None)
                civic_addresses.append(t)

        elif profile == "geodetic-2d":
            try:
                from shapely.wkt import dumps as _wkt_dumps
                geom = gml_xml._gml_sb_to_shapely(sb_el)
                geodetic_geom_wkt = _wkt_dumps(geom) if geom is not None else None
            except Exception:
                geodetic_geom_wkt = None

    uri_el = mapping_el.find(f"{{{lost_xml._NS_LOST}}}uri")
    if uri_el is not None and uri_el.text and uri_el.text.strip():
        lost_server = uri_el.text.strip()
    elif child_uri_hint:
        lost_server = child_uri_hint
    else:
        if source and "://" not in source:
            lost_server = f"http://{source}/lost"
        elif source:
            lost_server = source.rstrip("/") + "/lost"
        else:
            lost_server = ""

    return {
        "source":            source,
        "source_id":         source_id,
        "last_updated":      last_updated,
        "expires":           expires,
        "service":           service,
        "display_name":      display_name,
        "profile":           profile,
        "civic_addresses":   civic_addresses,
        "geodetic_geom_wkt": geodetic_geom_wkt,
        "lost_server":       lost_server,
    }


def _child_entry_to_mapping_xml(entry: dict) -> Optional[str]:
    profile = entry.get("profile", "")

    mapping_el = etree.Element(f"{{{lost_xml._NS_LOST}}}mapping")
    mapping_el.set("expires",     entry.get("expires", "NO-EXPIRATION"))
    mapping_el.set("lastUpdated", entry.get("last_updated", ""))
    mapping_el.set("source",      entry.get("source", ""))
    mapping_el.set("sourceId",    entry.get("source_id", ""))

    dn = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}displayName")
    dn.set("{http://www.w3.org/XML/1998/namespace}lang", runtime_state._display_name_lang)
    dn.text = entry.get("display_name") or f"{entry.get('source', '')} {profile} coverage"

    svc = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}service")
    svc.text = entry.get("service", "urn:service:sos")

    sb = etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}serviceBoundary")
    sb.set("profile", profile)

    if profile == "civic":
        for t in (entry.get("civic_addresses") or []):
            ca = etree.SubElement(sb, f"{{{lost_xml._NS_CA}}}civicAddress")
            for field, tag in [
                ("country", "country"), ("a1", "A1"), ("a2", "A2"),
                ("a3", "A3"),           ("a4", "A4"), ("a5", "A5"),
            ]:
                val = t.get(field)
                if val is not None and val != "*":
                    el = etree.SubElement(ca, f"{{{lost_xml._NS_CA}}}{tag}")
                    el.text = val
    elif profile == "geodetic-2d":
        geom_wkt = entry.get("geodetic_geom_wkt")
        if not geom_wkt:
            return None
        try:
            from shapely.wkt import loads as _wkt_loads
            geom = _wkt_loads(geom_wkt)
            sb.append(gml_xml._shapely_to_gml(geom))
        except Exception as exc:
            log.warning("LoST-Sync: could not reconstruct geodetic GML for child entry source=%s: %s",
                        entry.get("source", ""), exc)
            return None
    else:
        return None

    etree.SubElement(mapping_el, f"{{{lost_xml._NS_LOST}}}uri")
    return etree.tostring(mapping_el, encoding="unicode")


# ---------------------------------------------------------------------------
# LoST-Sync — sync endpoint handlers
# ---------------------------------------------------------------------------

def _sync_error_response(error_type: str, message: str) -> Response:
    root = etree.Element(f"{{{lost_xml._NS_LOST}}}errors", nsmap={None: lost_xml._NS_LOST})
    root.set("source", runtime_state._server_uri)
    err = etree.SubElement(root, f"{{{lost_xml._NS_LOST}}}{error_type}")
    err.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
    err.text = message
    body = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, status_code=200, media_type="application/lostsync+xml")


async def _handle_push_mappings(root: etree._Element, client=None) -> Response:
    mapping_els = root.findall(f"{{{lost_xml._NS_LOST}}}mapping")
    not_deleted: list[tuple[str, str]] = []

    # Parse all mappings up front (cheap, lock-free), then apply the same
    # upsert/delete merge under the cross-process coverage write lock.
    ops: list[tuple[bool, dict]] = []
    for mapping_el in mapping_els:
        sb_el     = mapping_el.find(f"{{{lost_xml._NS_LOST}}}serviceBoundary")
        is_delete = sb_el is None
        parsed    = _parse_sync_mapping(mapping_el)
        ops.append((is_delete, parsed))

    # Peer authorization (§3.7.2, Appendix A.11). (source, sourceId) is asserted
    # by the sender and the coverage store is keyed on it, so an unconstrained
    # push lets any trust-anchored peer overwrite another child's entry or add a
    # novel one that wins routing. Reject the WHOLE request on any unauthorized
    # mapping rather than applying the remainder: a partial apply lets a forged
    # mapping ride along with a valid one and still land its side effects
    # (including the upstream propagation below).
    for _is_delete, parsed in ops:
        if not fed_coverage._is_source_permitted(parsed["source"], parsed["source_id"]):
            log.warning(
                "LoST-Sync: REJECTED pushMappings from %s — not authorized to assert "
                "source=%r sourceId=%r (see %s). No mapping in this request was applied.",
                client, parsed["source"], parsed["source_id"],
                fed_coverage._ALLOWED_SOURCES_VAR,
            )
            return _sync_error_response(
                "forbidden",
                f"Not authorized to assert coverage for source={parsed['source']!r} "
                f"sourceId={parsed['source_id']!r}.",
            )

    def _apply(coverage: list[dict]) -> tuple[set, Optional[set]]:
        changed_ids: set = set()
        # LoST profiles ("civic" / "geodetic-2d") this request actually changed.
        # None is the widening sentinel meaning "could not tell — treat every
        # profile as changed", so an unparseable profile degrades to the old
        # push-both behavior rather than silently suppressing propagation.
        changed_profiles: Optional[set] = set()

        def _note_profile(profile: str) -> None:
            nonlocal changed_profiles
            if changed_profiles is None:
                return
            if profile:
                changed_profiles.add(profile)
            else:
                changed_profiles = None

        for is_delete, parsed in ops:
            source    = parsed["source"]
            source_id = parsed["source_id"]
            if is_delete:
                found = False
                for i, entry in enumerate(coverage):
                    if entry.get("source") == source and entry.get("source_id") == source_id:
                        # A delete mapping carries no <serviceBoundary>, so the
                        # profile comes from the entry being removed, not from
                        # the request.
                        _note_profile(entry.get("profile", ""))
                        coverage.pop(i)
                        found = True
                        changed_ids.add(source_id)
                        log.info(
                            "LoST-Sync: deleted coverage entry source=%s sourceId=%s",
                            source, source_id,
                        )
                        break
                if not found:
                    log.warning(
                        "LoST-Sync: delete requested for unknown entry source=%s sourceId=%s",
                        source, source_id,
                    )
                    not_deleted.append((source, source_id))
            else:
                if fed_coverage._upsert_child_coverage(parsed, coverage):
                    changed_ids.add(source_id)
                    _note_profile(parsed.get("profile", ""))
        return changed_ids, changed_profiles

    # Which sourceId(s) actually changed the store — not just which mappings
    # were present in this request — and which PROFILE(s) those changes touched.
    #
    # The profile set is what is threaded through to _push_coverage_to_fg(), so a
    # request that only touches one profile doesn't re-push the other, unchanged
    # one upstream.  It is deliberately NOT the sourceId set: the ids here are
    # the CHILD's (asserted in the mapping it pushed), whereas the FG push selects
    # this node's OWN aggregate entry by LVF_SYNC_SOURCE_ID_CIVIC/GEODETIC.  Those
    # two namespaces never intersect, so filtering the push by child sourceId
    # would suppress every propagation rather than just the redundant half.
    changed_source_ids, changed_profiles = fed_coverage._with_coverage_write(_apply)

    if changed_source_ids:
        changed_str = ", ".join(sorted(changed_source_ids))
        if runtime_state._root_ams and fed_coverage._root_ams_active:
            log.info(
                "Coverage propagation triggered by child push from %s, pushing upstream to %s",
                changed_str, runtime_state._forest_guide_uri,
            )
            asyncio.create_task(_push_coverage_to_fg(changed_profiles))
        elif not runtime_state._root_ams and runtime_state._parent_uri and not runtime_state._forest_guide_mode:
            log.info(
                "Coverage propagation triggered by child push from %s, pushing upstream to %s",
                changed_str, _get_parent_sync_uri(),
            )
            asyncio.create_task(_push_coverage_to_parent())

    if not_deleted:
        root_err = etree.Element(f"{{{lost_xml._NS_LOST}}}errors", nsmap={None: lost_xml._NS_LOST})
        root_err.set("source", runtime_state._server_uri)
        for src, sid in not_deleted:
            nd = etree.SubElement(root_err, f"{{{lost_xml._NS_LOST}}}notDeleted")
            nd.set("{http://www.w3.org/XML/1998/namespace}lang", "en")
            nd.text = f"No mapping found for source={src!r} sourceId={sid!r}"
        body = etree.tostring(root_err, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        return Response(content=body, status_code=200, media_type="application/lostsync+xml")

    body = etree.tostring(
        etree.Element(f"{{{lost_xml._NS_SYNC}}}pushMappingsResponse"),
        xml_declaration=True, encoding="UTF-8",
    )
    return Response(content=body, status_code=200, media_type="application/lostsync+xml")


async def _handle_get_mappings(root: etree._Element) -> Response:
    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
    ams_source_ids = {s for s in (sync_source_id_civic, sync_source_id_geodetic) if s}

    exists_el = root.find(f"{{{lost_xml._NS_SYNC}}}exists")
    mapping_xml_list: list[str] = []

    if exists_el is None:
        if not runtime_state._root_ams:
            if sync_source_id_civic:
                civic_xml = _build_civic_coverage_mapping_xml()
                if civic_xml:
                    mapping_xml_list.append(civic_xml)
            if sync_source_id_geodetic:
                geo_xml = _build_geodetic_coverage_mapping_xml()
                if geo_xml:
                    mapping_xml_list.append(geo_xml)
        own_count = len(mapping_xml_list)
        for entry in fed_coverage._child_coverage:
            if runtime_state._root_ams and entry.get("source_id") not in ams_source_ids:
                continue
            xml = _child_entry_to_mapping_xml(entry)
            if xml:
                mapping_xml_list.append(xml)
    else:
        fingerprints: dict[str, str] = {}
        for fp in exists_el.findall(f"{{{lost_xml._NS_SYNC}}}mapping-fingerprint"):
            sid = fp.get("sourceId")
            lu  = fp.get("lastUpdated", "")
            if sid:
                fingerprints[sid] = lu

        my_lu = _gis_last_updated_str()

        if not runtime_state._root_ams:
            if sync_source_id_civic:
                fp_lu = fingerprints.get(sync_source_id_civic)
                if fp_lu is None or fed_coverage._compare_timestamps(my_lu, fp_lu) > 0:
                    civic_xml = _build_civic_coverage_mapping_xml()
                    if civic_xml:
                        mapping_xml_list.append(civic_xml)

            if sync_source_id_geodetic:
                fp_lu = fingerprints.get(sync_source_id_geodetic)
                if fp_lu is None or fed_coverage._compare_timestamps(my_lu, fp_lu) > 0:
                    geo_xml = _build_geodetic_coverage_mapping_xml()
                    if geo_xml:
                        mapping_xml_list.append(geo_xml)

        own_count = len(mapping_xml_list)
        for entry in fed_coverage._child_coverage:
            if runtime_state._root_ams and entry.get("source_id") not in ams_source_ids:
                continue
            fp_lu = fingerprints.get(entry.get("source_id", ""))
            entry_lu = entry.get("last_updated", "")
            if fp_lu is None or fed_coverage._compare_timestamps(entry_lu, fp_lu) > 0:
                xml = _child_entry_to_mapping_xml(entry)
                if xml:
                    mapping_xml_list.append(xml)

    log.debug(
        "LoST-Sync: getMappingsResponse includes %d mapping(s) (%d own, %d child)",
        len(mapping_xml_list), own_count, len(mapping_xml_list) - own_count,
    )

    resp_root = etree.Element(f"{{{lost_xml._NS_SYNC}}}getMappingsResponse")
    for xml_str in mapping_xml_list:
        try:
            resp_root.append(etree.fromstring(xml_str))
        except Exception as exc:
            log.warning("LoST-Sync: could not include mapping in getMappingsResponse: %s", exc)

    body = etree.tostring(resp_root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, status_code=200, media_type="application/lostsync+xml")


# ---------------------------------------------------------------------------
# LoST-Sync — outbound push / pull
# ---------------------------------------------------------------------------

def _get_parent_sync_uri() -> str:
    if not runtime_state._parent_uri:
        return ""
    base = fed_recursion._resolve_lost_url(runtime_state._parent_uri).rstrip("/")
    if base.endswith("/validate") or base.endswith("/lost"):
        base = base.rsplit("/", 1)[0]
    return base + "/sync"


def _get_fg_sync_uri() -> str:
    if not runtime_state._forest_guide_uri:
        return ""
    base = fed_recursion._resolve_lost_url(runtime_state._forest_guide_uri).rstrip("/")
    if base.endswith("/sync") or base.endswith("/lost"):
        base = base.rsplit("/", 1)[0]
    return base + "/sync"


async def _push_coverage_to_parent() -> bool:
    if gis_provisioning.is_reloading():
        log.warning("LoST-Sync: skipping push — GIS reload in progress")
        return False

    parent_sync_uri = _get_parent_sync_uri()
    if not parent_sync_uri:
        return False

    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")

    attempted = False
    all_ok    = True

    for label, mapping_getter, src_id in [
        ("civic",    _build_civic_coverage_mapping_xml,    sync_source_id_civic),
        ("geodetic", _build_geodetic_coverage_mapping_xml, sync_source_id_geodetic),
    ]:
        if not src_id:
            continue

        mapping_xml = mapping_getter()
        if not mapping_xml:
            continue

        push_root = etree.Element(f"{{{lost_xml._NS_SYNC}}}pushMappings")
        try:
            push_root.append(etree.fromstring(mapping_xml))
        except Exception as exc:
            log.warning("LoST-Sync: could not parse %s mapping for push: %s", label, exc)
            all_ok = False
            continue

        push_body = etree.tostring(push_root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        log.info("LoST-Sync: pushing %s coverage to parent %s", label, parent_sync_uri)

        attempted = True
        _push_query_id = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=runtime_state.now(),
            query_id=_push_query_id,
            direction="outgoing",
            query_adapter=push_body.decode("utf-8", errors="replace"),
        ))
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=outbound_ssl_context()) as client:
                resp = await client.post(
                    parent_sync_uri,
                    content=push_body,
                    headers={"Content-Type": "application/lostsync+xml"},
                )
            emit_log_event(make_response_event(
                timestamp=runtime_state.now(),
                response_id=_push_query_id,
                direction="incoming",
                response_adapter=resp.content.decode("utf-8", errors="replace"),
                response_status=str(resp.status_code),
            ))
            if resp.status_code == 200:
                try:
                    resp_root = etree.fromstring(resp.content)
                    if resp_root.tag == f"{{{lost_xml._NS_SYNC}}}pushMappingsResponse":
                        log.info(
                            "LoST-Sync: successfully pushed %s coverage to parent %s",
                            label, parent_sync_uri,
                        )
                    else:
                        log.warning(
                            "LoST-Sync: unexpected response pushing %s to parent: %s",
                            label, resp_root.tag,
                        )
                        all_ok = False
                except Exception:
                    log.warning("LoST-Sync: parent returned unparseable XML when pushing %s", label)
                    all_ok = False
            else:
                log.warning(
                    "LoST-Sync: push %s to parent %s returned HTTP %d",
                    label, parent_sync_uri, resp.status_code,
                )
                all_ok = False
        except Exception as exc:
            log.warning(
                "LoST-Sync: failed to push %s coverage to parent %s: %s",
                label, parent_sync_uri, exc,
            )
            all_ok = False

    return attempted and all_ok


async def _push_coverage_to_fg(changed_profiles: Optional[set] = None) -> bool:
    """Push AMS coverage to the Forest Guide.

    `changed_profiles`, when given, is the set of LoST profiles ("civic",
    "geodetic-2d") an inbound change actually touched, and restricts the push
    to the matching label(s) — used by _handle_push_mappings() so a pushMappings
    request touching only one profile doesn't re-push the other, unchanged one.
    None (the default, used by startup sync and the post-reload re-push, and the
    widening sentinel when a change's profile could not be determined) pushes
    every configured label, unchanged from prior behavior.

    Filtering is on profile rather than sourceId on purpose: the sourceIds an
    inbound push carries are the CHILD's, while the entries selected below are
    this node's own aggregate, keyed by LVF_SYNC_SOURCE_ID_CIVIC/GEODETIC. The
    two never match, so a sourceId filter would suppress the push entirely.
    """
    if not fed_coverage._root_ams_active or not runtime_state._forest_guide_uri:
        return False

    if gis_provisioning.is_reloading():
        log.warning("AMS: skipping FG push — GIS reload in progress")
        return False

    fg_sync_uri = _get_fg_sync_uri()

    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")

    attempted = False
    all_ok    = True

    for label, profile, src_id in [
        ("civic",    "civic",       sync_source_id_civic),
        ("geodetic", "geodetic-2d", sync_source_id_geodetic),
    ]:
        if not src_id:
            continue
        if changed_profiles is not None and profile not in changed_profiles:
            continue

        entry = next(
            (e for e in fed_coverage._child_coverage
             if e.get("source_id") == src_id and e.get("profile") == profile),
            None,
        )
        if not entry:
            log.warning("AMS: could not find %s coverage entry in child store for FG push (no data?)", label)
            all_ok = False
            continue
        mapping_xml = _child_entry_to_mapping_xml(entry)
        if not mapping_xml:
            log.warning("AMS: could not build %s coverage mapping for FG push (no data?)", label)
            all_ok = False
            continue

        push_root = etree.Element(f"{{{lost_xml._NS_SYNC}}}pushMappings")
        try:
            push_root.append(etree.fromstring(mapping_xml))
        except Exception as exc:
            log.warning("AMS: could not parse %s mapping for FG push: %s", label, exc)
            all_ok = False
            continue

        push_body = etree.tostring(push_root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
        log.info("AMS: pushing %s coverage to Forest Guide %s", label, runtime_state._forest_guide_uri)

        attempted = True
        _fg_query_id = generate_query_id()
        emit_log_event(make_query_event(
            timestamp=runtime_state.now(),
            query_id=_fg_query_id,
            direction="outgoing",
            query_adapter=push_body.decode("utf-8", errors="replace"),
        ))
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=outbound_ssl_context()) as client:
                resp = await client.post(
                    fg_sync_uri,
                    content=push_body,
                    headers={"Content-Type": "application/lostsync+xml"},
                )
            emit_log_event(make_response_event(
                timestamp=runtime_state.now(),
                response_id=_fg_query_id,
                direction="incoming",
                response_adapter=resp.content.decode("utf-8", errors="replace"),
                response_status=str(resp.status_code),
            ))
            if resp.status_code == 200:
                try:
                    resp_root = etree.fromstring(resp.content)
                    if resp_root.tag == f"{{{lost_xml._NS_SYNC}}}pushMappingsResponse":
                        log.info("AMS: successfully pushed %s coverage to Forest Guide %s", label, runtime_state._forest_guide_uri)
                    else:
                        log.warning("AMS: unexpected response pushing %s to Forest Guide: %s", label, resp_root.tag)
                        all_ok = False
                except Exception:
                    log.warning("AMS: Forest Guide returned unparseable XML when pushing %s", label)
                    all_ok = False
            else:
                log.warning("AMS: push %s to Forest Guide %s returned HTTP %d", label, runtime_state._forest_guide_uri, resp.status_code)
                all_ok = False
        except Exception as exc:
            log.warning("AMS: failed to push %s coverage to Forest Guide %s: %s", label, runtime_state._forest_guide_uri, exc)
            all_ok = False

    return attempted and all_ok


async def _pull_from_child(child_entry: str) -> bool:
    if "://" not in child_entry:
        child_sync_url = fed_recursion._resolve_lost_url(child_entry).rstrip("/") + "/sync"
    else:
        child_sync_url = child_entry

    if gis_provisioning.is_reloading():
        log.warning("LoST-Sync: skipping pull from %s — GIS reload in progress", child_sync_url)
        return False

    base = child_sync_url.rstrip("/")
    child_lost_url = (base[: -len("/sync")] if base.endswith("/sync") else base) + "/lost"

    get_body = etree.tostring(
        etree.Element(f"{{{lost_xml._NS_SYNC}}}getMappingsRequest"),
        xml_declaration=True, encoding="UTF-8",
    )
    log.info("LoST-Sync: sending getMappingsRequest to %s", child_sync_url)

    _pull_query_id = generate_query_id()
    emit_log_event(make_query_event(
        timestamp=runtime_state.now(),
        query_id=_pull_query_id,
        direction="outgoing",
        query_adapter=get_body.decode("utf-8", errors="replace"),
    ))
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=outbound_ssl_context()) as client:
            resp = await client.post(
                child_sync_url,
                content=get_body,
                headers={"Content-Type": "application/lostsync+xml"},
            )
        emit_log_event(make_response_event(
            timestamp=runtime_state.now(),
            response_id=_pull_query_id,
            direction="incoming",
            response_adapter=resp.content.decode("utf-8", errors="replace"),
            response_status=str(resp.status_code),
        ))
        if resp.status_code != 200:
            log.warning(
                "LoST-Sync: getMappingsRequest to %s returned HTTP %d",
                child_sync_url, resp.status_code,
            )
            return False

        try:
            resp_root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as exc:
            log.warning(
                "LoST-Sync: getMappingsResponse from %s has malformed XML: %s",
                child_sync_url, exc,
            )
            return False

        if resp_root.tag != f"{{{lost_xml._NS_SYNC}}}getMappingsResponse":
            log.warning(
                "LoST-Sync: unexpected response element from %s: %s",
                child_sync_url, resp_root.tag,
            )
            return False

        parsed_list = [
            _parse_sync_mapping(mapping_el, child_uri_hint=child_lost_url)
            for mapping_el in resp_root.findall(f"{{{lost_xml._NS_LOST}}}mapping")
        ]

        # Peer authorization (§3.7.2, Appendix A.11). The pull path ingests
        # exactly as the push path does, and a configured child can return
        # mappings claiming to be any OTHER child, so the same constraint
        # applies here. Skip unauthorized mappings rather than abandoning the
        # pull: this sync is our own initiative against a configured peer, and
        # discarding its legitimate mappings over one bad one would be a
        # self-inflicted outage.
        permitted_list = []
        for parsed in parsed_list:
            if fed_coverage._is_source_permitted(parsed["source"], parsed["source_id"]):
                permitted_list.append(parsed)
            else:
                log.warning(
                    "LoST-Sync: SKIPPED mapping from %s — not authorized to assert "
                    "source=%r sourceId=%r (see %s)",
                    child_sync_url, parsed["source"], parsed["source_id"],
                    fed_coverage._ALLOWED_SOURCES_VAR,
                )
        count = len(permitted_list)

        if count > 0:
            def _apply(coverage: list[dict]) -> bool:
                for parsed in permitted_list:
                    fed_coverage._upsert_child_coverage(parsed, coverage)
                return True

            fed_coverage._with_coverage_write(_apply)
            log.info("LoST-Sync: stored %d mapping(s) received from %s", count, child_sync_url)
        else:
            log.info(
                "LoST-Sync: no mappings stored from %s (%d received, %d skipped by %s)",
                child_sync_url, len(parsed_list), len(parsed_list) - count,
                fed_coverage._ALLOWED_SOURCES_VAR,
            )
        return True

    except Exception as exc:
        log.warning("LoST-Sync: failed to pull from %s: %s", child_sync_url, exc)
        return False


_SYNC_BACKOFF_INITIAL = 5   # seconds before first retry
_SYNC_BACKOFF_CAP     = 60  # maximum seconds between retries


async def _retry_sync_op(label: str, coro_fn) -> None:
    """Call coro_fn() with exponential backoff until it returns True.

    Backoff sequence: 5s, 10s, 20s, 40s, 60s, 60s, ...
    A False return is treated as failure and triggers a retry, the same as an
    exception would.  Exits immediately and silently on asyncio.CancelledError.
    Logs a WARNING on each failed attempt with attempt number and next delay.
    """
    delay = _SYNC_BACKOFF_INITIAL
    attempt = 0
    while True:
        attempt += 1
        try:
            ok = await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "LoST-Sync: %s failed (attempt %d): %s — retrying in %ds",
                label, attempt, exc, delay,
            )
        else:
            if ok:
                return
            log.warning(
                "LoST-Sync: %s failed (attempt %d) — retrying in %ds",
                label, attempt, delay,
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, _SYNC_BACKOFF_CAP)


async def _startup_sync() -> None:
    """Launch independent retry tasks for each startup sync operation.

    Child pulls run concurrently. The AMS Forest Guide push waits for all
    child pulls to succeed before attempting, fixing the previous race where
    FG push fired before child coverage was populated.
    """
    await asyncio.sleep(1)

    # One event per child entry — set when that child's pull succeeds.
    child_done_events: list[asyncio.Event] = []

    for child_entry in runtime_state._sync_children:
        done = asyncio.Event()
        child_done_events.append(done)

        async def _child_task(entry=child_entry, event=done) -> None:
            await _retry_sync_op(
                f"pull from {entry}",
                lambda e=entry: _pull_from_child(e),
            )
            event.set()

        _background_sync_tasks.append(
            asyncio.create_task(_child_task(), name=f"lvf-sync-pull-{child_entry}")
        )

    if not runtime_state._root_ams and not runtime_state._forest_guide_mode:
        sync_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC",    "")
        sync_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
        if (sync_civic or sync_geodetic) and runtime_state._parent_uri:
            _background_sync_tasks.append(
                asyncio.create_task(
                    _retry_sync_op("push to parent", _push_coverage_to_parent),
                    name="lvf-sync-push-parent",
                )
            )

    if runtime_state._root_ams and fed_coverage._root_ams_active:
        async def _fg_task() -> None:
            for event in child_done_events:
                await event.wait()
            await _retry_sync_op("push to Forest Guide", _push_coverage_to_fg)

        _background_sync_tasks.append(
            asyncio.create_task(_fg_task(), name="lvf-sync-push-fg")
        )


def _maybe_schedule_repush() -> None:
    loop = runtime_state._event_loop
    if loop is None or not loop.is_running():
        log.warning("LoST-Sync: no running event loop — cannot schedule re-push after reload")
        return

    if runtime_state._root_ams:
        if fed_coverage._root_ams_active:
            try:
                asyncio.run_coroutine_threadsafe(_push_coverage_to_fg(), loop)
                log.info("AMS: scheduled FG re-push after GIS reload")
            except Exception as exc:
                log.warning("AMS: could not schedule FG re-push: %s", exc)
        return

    sync_source_id_civic    = os.environ.get("LVF_SYNC_SOURCE_ID_CIVIC", "")
    sync_source_id_geodetic = os.environ.get("LVF_SYNC_SOURCE_ID_GEODETIC", "")
    if not (sync_source_id_civic or sync_source_id_geodetic) or not runtime_state._parent_uri or runtime_state._forest_guide_mode:
        return
    try:
        asyncio.run_coroutine_threadsafe(_push_coverage_to_parent(), loop)
        log.info("LoST-Sync: scheduled coverage re-push to parent after GIS reload")
    except Exception as exc:
        log.warning("LoST-Sync: could not schedule re-push: %s", exc)


# ---------------------------------------------------------------------------
# LoST-Sync HTTP handler (extracted from server.sync_endpoint)
# ---------------------------------------------------------------------------

async def handle_sync(body: bytes, client) -> Response:
    """Handle an incoming LoST-Sync request body. Returns a Response directly."""
    try:
        root = etree.fromstring(body, lost_xml._XML_PARSER)
    except etree.XMLSyntaxError as exc:
        log.error("Sync request: XML parse failed: %s", exc)
        return _sync_error_response("badRequest", "Malformed XML.")

    _sync_query_id = generate_query_id()
    emit_log_event(make_query_event(
        timestamp=runtime_state.now(),
        query_id=_sync_query_id,
        direction="incoming",
        query_adapter=body.decode("utf-8", errors="replace"),
    ))

    if root.tag == f"{{{lost_xml._NS_SYNC}}}pushMappings":
        log.info("LoST-Sync: received pushMappings from %s", client)
        response = await _handle_push_mappings(root, client)
    elif root.tag == f"{{{lost_xml._NS_SYNC}}}getMappingsRequest":
        log.info("LoST-Sync: received getMappingsRequest from %s", client)
        response = await _handle_get_mappings(root)
    else:
        response = _sync_error_response(
            "badRequest",
            f"Unexpected root element {root.tag!r}; "
            "expected {urn:ietf:params:xml:ns:lostsync1}pushMappings "
            "or {urn:ietf:params:xml:ns:lostsync1}getMappingsRequest",
        )

    emit_log_event(make_response_event(
        timestamp=runtime_state.now(),
        response_id=_sync_query_id,
        direction="outgoing",
        response_adapter=response.body.decode("utf-8", errors="replace"),
    ))
    return response
