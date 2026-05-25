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
            return {
                "outcome":   "locationValidation",
                "valid":     sorted((valid_el.text or "").split())     if valid_el     is not None else [],
                "invalid":   (invalid_el.text or "").strip()           if invalid_el   is not None else None,
                "unchecked": sorted((unchecked_el.text or "").split()) if unchecked_el is not None else [],
                "source_id": mapping_el.get("sourceId")                if mapping_el   is not None else None,
            }
        warnings = root.find(f"{{{_NS_LOST}}}warnings")
        if warnings is not None and warnings.find(f"{{{_NS_LOST}}}locationValidationUnavailable") is not None:
            return {"outcome": "locationValidationUnavailable"}
        return {"outcome": "findServiceResponse_unknown"}

    if root.tag == f"{{{_NS_LOST}}}redirect":
        return {
            "outcome": "redirect",
            "target":  root.get("target", ""),
        }

    if root.tag == f"{{{_NS_LOST}}}listServicesResponse":
        sl = root.find(f"{{{_NS_LOST}}}serviceList")
        urns = sorted((sl.text or "").split()) if sl is not None else []
        return {"outcome": "listServicesResponse", "service_list": urns}

    if root.tag == f"{{{_NS_LOST}}}listServicesByLocationResponse":
        sl = root.find(f"{{{_NS_LOST}}}serviceList")
        urns = sorted((sl.text or "").split()) if sl is not None else []
        lu = root.find(f"{{{_NS_LOST}}}locationUsed")
        return {
            "outcome": "listServicesByLocationResponse",
            "service_list": urns,
            "location_used": lu.get("id") if lu is not None else None,
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
    return diffs


def run_tests(test_names: list[str] | None = None) -> int:
    initialize()

    xml_files = sorted(f for f in TESTS_DIR.glob("*.xml") if _TEST_ID_RE.match(f.stem))
    if test_names:
        xml_files = [f for f in xml_files if f.stem in test_names]
        missing = set(test_names) - {f.stem for f in xml_files}
        if missing:
            for m in sorted(missing):
                print(f"ERROR: no test file found for '{m}'")
            return 1

    passed = failed = errors = 0

    for xml_path in xml_files:
        name = xml_path.stem
        golden_path = GOLDEN_DIR / f"{name}.golden.xml"

        if not golden_path.exists():
            print(f"SKIP  {name}  (no golden file — run seed.py first)")
            errors += 1
            continue

        try:
            actual_bytes = _dispatch(xml_path.read_bytes())
        except Exception as exc:
            print(f"ERROR {name}: handle_find_service raised: {exc}")
            errors += 1
            continue

        try:
            actual = _parse_outcome(actual_bytes)
            golden = _parse_outcome(golden_path.read_bytes())
        except Exception as exc:
            print(f"ERROR {name}: could not parse XML: {exc}")
            errors += 1
            continue

        diffs = _diff(actual, golden)
        if diffs:
            print(f"FAIL  {name}")
            for d in diffs:
                print(f"        {d}")
            failed += 1
        else:
            print(f"PASS  {name}")
            passed += 1

    total = passed + failed + errors
    summary = f"{passed}/{total} passed"
    if failed:
        summary += f", {failed} failed"
    if errors:
        summary += f", {errors} errors/skipped"
    print(f"\n{summary}")
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
