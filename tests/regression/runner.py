"""
LVF regression test runner.

Discovers all *.xml files in tests/, submits each through handle_find_service(),
and compares the result against the corresponding golden file in golden/.

Comparison is semantic (parsed XML), not a string diff. Checked fields:
  - Outcome type (locationValidation / notFound / locationInvalid / serviceNotImplemented)
  - valid, invalid, unchecked element lists (order-independent for valid/unchecked)
  - mapping sourceId (only when present in the golden file)

Usage:
    python -m tests.regression.runner                    # run all tests
    python -m tests.regression.runner --test validate_2  # run one test by name
"""

import argparse
import sys
from pathlib import Path

from lxml import etree

from src.server import handle_find_service, initialize

TESTS_DIR = Path(__file__).parent.parent
GOLDEN_DIR = Path(__file__).parent / "golden"

_NS_LOST = "urn:ietf:params:xml:ns:lost1"


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

    xml_files = sorted(TESTS_DIR.glob("*.xml"))
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
            actual_bytes = handle_find_service(xml_path.read_bytes())
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
