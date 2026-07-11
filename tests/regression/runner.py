"""
LVF regression test runner.

Discovers all *.xml files in tests/requests/, submits each through the appropriate
handler based on the root element, and compares the result against the corresponding
golden file in golden/.

Supported root elements:
  - findService              → handle_find_service()
  - listServices             → list_services.handle()
  - listServicesByLocation   → list_services_by_location.handle()  (async)

Comparison is semantic (parsed XML), not a string diff. Checked fields:
  - findService: outcome type, valid/invalid/unchecked element lists, mapping sourceId
  - listServices/listServicesByLocation: sorted serviceList URNs, locationUsed id

Usage:
    python -m tests.regression.runner                           # run all tests
    python -m tests.regression.runner --test G2-SSAP-VALID-002  # run one test by name
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

from lxml import etree

from src.server import handle_find_service, initialize
from src.lost import list_services, list_services_by_location

TESTS_DIR = Path(__file__).parent.parent / "requests"
GOLDEN_DIR = Path(__file__).parent / "golden"

# Only pick up files whose stem matches the test ID convention: WORD-WORD-...-NNN
_TEST_ID_RE = re.compile(r'^[A-Z0-9]+(?:-[A-Z0-9]+)+-\d{3}$')

_NS_LOST = "urn:ietf:params:xml:ns:lost1"
_NS_RLI = "urn:ietf:params:xml:ns:lost-rli1"
_NS_CA  = "urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr"

# NOTE: _NS_CA intentionally matches whatever civicAddress namespace the
# response serializer actually emits. If completeLocation comparisons show
# spurious "no civicAddress found" diffs, verify this string against
# src/lost/wire/lost_xml.py's _NS_CA — they must be identical.


def _dispatch(xml_bytes: bytes) -> bytes:
    """Route to the correct handler based on the root element."""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError:
        return handle_find_service(xml_bytes)
    tag = root.tag
    if tag == f"{{{_NS_LOST}}}listServices":
        return list_services.handle(xml_bytes)
    if tag == f"{{{_NS_LOST}}}listServicesByLocation":
        return asyncio.run(list_services_by_location.handle(xml_bytes))
    return handle_find_service(xml_bytes)


def _extract_path_vias(root: etree._Element) -> list[str]:
    """Return the ordered list of <path><via source="..."/> values, or
    an empty list if no <path> element is present. <path> is legal on
    findServiceResponse, listServicesResponse, and
    listServicesByLocationResponse per the RFC 5222 schema
    (commonResponsePattern) — NOT on redirect or errors, which never
    carry a <path> at all."""
    path_el = root.find(f"{{{_NS_LOST}}}path")
    if path_el is None:
        return []
    return [
        via.get("source", "")
        for via in path_el.findall(f"{{{_NS_LOST}}}via")
    ]


def _extract_complete_location(root: etree._Element) -> dict | None:
    """Extract completeLocation structure for regression comparison, or
    None if absent. Captures the properties that distinguish the
    conformant shape (draft-ietf-ecrit-similar-location-19: profile on
    completeLocation, civicAddress as its DIRECT child) from the prior
    defect (an intervening <lost:location> wrapper):

      - profile:        the 'profile' attribute on <completeLocation>
      - has_location_wrapper: True if a <lost:location> element sits
                        between completeLocation and civicAddress (the
                        defect); must be False for a conformant response
      - fields:         ordered list of (localname, value) for each
                        civicAddress child, wherever civicAddress is found
    """
    lv = root.find(f"{{{_NS_LOST}}}locationValidation")
    if lv is None:
        return None
    cl = lv.find(f"{{{_NS_RLI}}}completeLocation")
    if cl is None:
        return None

    location_wrapper = cl.find(f"{{{_NS_LOST}}}location")
    has_location_wrapper = location_wrapper is not None

    # Find civicAddress wherever it lives (direct child = conformant;
    # under a <location> wrapper = defect) so we can still report fields
    # in both cases rather than silently reporting none.
    ca = cl.find(f"{{{_NS_CA}}}civicAddress")
    if ca is None and location_wrapper is not None:
        ca = location_wrapper.find(f"{{{_NS_CA}}}civicAddress")

    fields: list[tuple[str, str]] = []
    if ca is not None:
        for child in ca:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            fields.append((local, (child.text or "").strip()))

    return {
        "profile": cl.get("profile"),
        "has_location_wrapper": has_location_wrapper,
        "fields": fields,
    }


def _parse_outcome(xml_bytes: bytes) -> dict:
    """Extract comparable fields from a response XML blob."""
    root = etree.fromstring(xml_bytes)

    if root.tag == f"{{{_NS_LOST}}}findServiceResponse":
        lv = root.find(f"{{{_NS_LOST}}}locationValidation")
        if lv is not None:
            valid_el     = lv.find(f"{{{_NS_LOST}}}valid")
            invalid_el   = lv.find(f"{{{_NS_LOST}}}invalid")
            unchecked_el = lv.find(f"{{{_NS_LOST}}}unchecked")
            mapping_el   = root.find(f"{{{_NS_LOST}}}mapping")
            location_used_el = root.find(f"{{{_NS_LOST}}}locationUsed")
            return {
                "outcome":      "locationValidation",
                "valid":        sorted((valid_el.text or "").split())     if valid_el     is not None else [],
                "invalid":      (invalid_el.text or "").strip()           if invalid_el   is not None else None,
                "unchecked":    sorted((unchecked_el.text or "").split()) if unchecked_el is not None else [],
                "source_id":    mapping_el.get("sourceId")                if mapping_el   is not None else None,
                "location_used": location_used_el.get("id")               if location_used_el is not None else None,
                "path_vias":    _extract_path_vias(root),
                "complete_location": _extract_complete_location(root),
            }
        warnings = root.find(f"{{{_NS_LOST}}}warnings")
        if warnings is not None and warnings.find(f"{{{_NS_LOST}}}locationValidationUnavailable") is not None:
            return {
                "outcome": "locationValidationUnavailable",
                "path_vias": _extract_path_vias(root),
            }
        return {
            "outcome": "findServiceResponse_unknown",
            "path_vias": _extract_path_vias(root),
        }

    if root.tag == f"{{{_NS_LOST}}}redirect":
        return {
            "outcome": "redirect",
            "target":  root.get("target", ""),
        }

    if root.tag == f"{{{_NS_LOST}}}listServicesResponse":
        sl = root.find(f"{{{_NS_LOST}}}serviceList")
        urns = sorted((sl.text or "").split()) if sl is not None else []
        return {
            "outcome": "listServicesResponse",
            "service_list": urns,
            "path_vias": _extract_path_vias(root),
        }

    if root.tag == f"{{{_NS_LOST}}}listServicesByLocationResponse":
        sl = root.find(f"{{{_NS_LOST}}}serviceList")
        urns = sorted((sl.text or "").split()) if sl is not None else []
        lu = root.find(f"{{{_NS_LOST}}}locationUsed")
        return {
            "outcome": "listServicesByLocationResponse",
            "service_list": urns,
            "location_used": lu.get("id") if lu is not None else None,
            "path_vias": _extract_path_vias(root),
        }

    if root.tag == f"{{{_NS_LOST}}}errors":
        for child in root:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            return {"outcome": local}

    return {"outcome": "unknown"}


def _diff(actual: dict, golden: dict) -> list[str]:
    """Return a list of human-readable differences; empty means match."""
    diffs: list[str] = []

    if actual.get("outcome") != golden.get("outcome"):
        diffs.append(
            f"outcome: got '{actual.get('outcome')}', expected '{golden.get('outcome')}'"
        )
        return diffs  # sub-fields are meaningless when the outcome type differs

    if actual.get("outcome") == "redirect":
        if actual.get("target") != golden.get("target"):
            diffs.append(
                f"redirect target: got '{actual.get('target')}', expected '{golden.get('target')}'"
            )
        return diffs

    if actual.get("outcome") in ("listServicesResponse", "listServicesByLocationResponse"):
        if actual.get("service_list") != golden.get("service_list"):
            diffs.append(
                f"service_list: got {actual.get('service_list')}, expected {golden.get('service_list')}"
            )
        if actual.get("outcome") == "listServicesByLocationResponse":
            if actual.get("location_used") != golden.get("location_used"):
                diffs.append(
                    f"location_used: got '{actual.get('location_used')}', expected '{golden.get('location_used')}'"
                )
        if golden.get("path_vias") is not None:
            if actual.get("path_vias") != golden.get("path_vias"):
                diffs.append(
                    f"path_vias: got {actual.get('path_vias')}, expected {golden.get('path_vias')}"
                )
        return diffs

    if actual.get("valid") != golden.get("valid"):
        diffs.append(f"valid: got {actual.get('valid')}, expected {golden.get('valid')}")
    if actual.get("invalid") != golden.get("invalid"):
        diffs.append(
            f"invalid: got '{actual.get('invalid')}', expected '{golden.get('invalid')}'"
        )
    if actual.get("unchecked") != golden.get("unchecked"):
        diffs.append(
            f"unchecked: got {actual.get('unchecked')}, expected {golden.get('unchecked')}"
        )
    # Only compare sourceId when the golden file recorded one — a None golden sourceId
    # means "don't care" (e.g. the golden was seeded before a real mapping was present).
    if golden.get("source_id") is not None:
        if actual.get("source_id") != golden.get("source_id"):
            diffs.append(
                f"mapping sourceId: got '{actual.get('source_id')}', "
                f"expected '{golden.get('source_id')}'"
            )
    if actual.get("location_used") != golden.get("location_used"):
        diffs.append(
            f"location_used: got '{actual.get('location_used')}', "
            f"expected '{golden.get('location_used')}'"
        )
    if golden.get("path_vias") is not None:
        if actual.get("path_vias") != golden.get("path_vias"):
            diffs.append(
                f"path_vias: got {actual.get('path_vias')}, expected {golden.get('path_vias')}"
            )

    # completeLocation (draft-ietf-ecrit-similar-location-19). Compared only
    # when the golden recorded one (None golden = "don't care", consistent
    # with the source_id convention).
    if golden.get("complete_location") is not None:
        a_cl = actual.get("complete_location")
        g_cl = golden.get("complete_location")
        if a_cl is None:
            diffs.append("complete_location: got none, expected present")
        else:
            if a_cl.get("has_location_wrapper"):
                diffs.append(
                    "complete_location: non-conformant <location> wrapper present "
                    "between completeLocation and civicAddress (should be absent "
                    "per draft-ietf-ecrit-similar-location-19)"
                )
            if a_cl.get("profile") != g_cl.get("profile"):
                diffs.append(
                    f"complete_location profile: got '{a_cl.get('profile')}', "
                    f"expected '{g_cl.get('profile')}'"
                )
            if a_cl.get("fields") != g_cl.get("fields"):
                diffs.append(
                    f"complete_location fields: got {a_cl.get('fields')}, "
                    f"expected {g_cl.get('fields')}"
                )
    return diffs


_HEADER_WIDTH = 56


def _pretty_xml(xml_bytes: bytes) -> str:
    """Return indented XML string, falling back to raw text on parse error."""
    try:
        root = etree.fromstring(xml_bytes)
        return etree.tostring(root, pretty_print=True).decode()
    except Exception:
        return xml_bytes.decode(errors="replace")


def run_tests(test_names: list[str] | None = None) -> int:
    initialize()

    all_xml_files = sorted(f for f in TESTS_DIR.glob("*.xml") if _TEST_ID_RE.match(f.stem))

    # Auto-seed only when every discoverable test lacks a golden file (first-run scenario).
    # Partial absence (some goldens present, some not) is intentional and left as SKIP.
    if all_xml_files and not any(
        (GOLDEN_DIR / f"{f.stem}.golden.xml").exists() for f in all_xml_files
    ):
        print("No golden files found — seeding baseline automatically...")
        from tests.regression import seed as _seed_mod
        _seed_mod.seed(names=None, force=False)
        print("Seeding complete — running tests...")

    xml_files = all_xml_files
    if test_names:
        xml_files = [f for f in xml_files if f.stem in test_names]
        missing = set(test_names) - {f.stem for f in xml_files}
        if missing:
            for m in sorted(missing):
                print(f"ERROR: no test file found for '{m}'")
            return 1

    passed = failed = errors = skipped = 0
    results: list[tuple[str, str]] = []  # (name, "PASS" | "FAIL" | "ERROR" | "SKIP")

    for xml_path in xml_files:
        name = xml_path.stem
        golden_path = GOLDEN_DIR / f"{name}.golden.xml"

        print(f"\n{'═' * _HEADER_WIDTH}")
        print(f"TEST: {name}")
        print(f"{'═' * _HEADER_WIDTH}")

        if not golden_path.exists():
            print("SKIP  (no golden file — run seed.py first)")
            skipped += 1
            results.append((name, "SKIP"))
            continue

        request_bytes = xml_path.read_bytes()
        print(f"\nREQUEST:\n{_pretty_xml(request_bytes)}")

        try:
            actual_bytes = _dispatch(request_bytes)
        except Exception as exc:
            print(f"ACTUAL RESPONSE:\n(handler raised: {exc})")
            print("\nERROR")
            errors += 1
            results.append((name, "ERROR"))
            continue

        print(f"ACTUAL RESPONSE:\n{_pretty_xml(actual_bytes)}")

        try:
            actual = _parse_outcome(actual_bytes)
            golden = _parse_outcome(golden_path.read_bytes())
        except Exception as exc:
            print(f"(could not parse XML: {exc})")
            print("\nERROR")
            errors += 1
            results.append((name, "ERROR"))
            continue

        diffs = _diff(actual, golden)
        if diffs:
            print(f"EXPECTED:\n{_pretty_xml(golden_path.read_bytes())}")
            print("Differences:")
            for d in diffs:
                print(f"  {d}")
            print("\nFAIL")
            failed += 1
            results.append((name, "FAIL"))
        else:
            print("\nPASS")
            passed += 1
            results.append((name, "PASS"))

    # --- summary table ---
    print("\n--- RESULTS ---")
    for name, status in results:
        print(f"  {status:<5}  {name}")

    total = passed + failed + errors + skipped
    parts = [f"{passed}/{total} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if errors:
        parts.append(f"{errors} errors")
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"\n{', '.join(parts)}")

    non_passing = [(n, s) for n, s in results if s != "PASS"]
    if non_passing:
        print("\nFailed / non-passing tests:")
        for name, status in non_passing:
            print(f"  {status:<5}  {name}")

    return 0 if (failed == 0 and errors == 0) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="LVF regression test runner")
    parser.add_argument(
        "--test",
        metavar="NAME",
        help="Run only this test (XML file stem, e.g. validate_2)",
    )
    args = parser.parse_args()
    sys.exit(run_tests([args.test] if args.test else None))


if __name__ == "__main__":
    main()
