"""
LVF golden file seeder.

Run ONCE to establish the baseline. Never re-run unless you are deliberately
resetting the baseline after an intentional behavior change.

Usage:
    python -m tests.regression.seed                               # seed all, skip existing
    python -m tests.regression.seed --force                       # overwrite all
    python -m tests.regression.seed --force G2-SSAP-VALID-002    # overwrite one by name
"""

import argparse
import asyncio
import sys
from pathlib import Path

from lxml import etree

from src.server import handle_find_service, initialize
from src.lost import list_services, list_services_by_location

TESTS_DIR = Path(__file__).parent.parent / "requests"
GOLDEN_DIR = Path(__file__).parent / "golden"

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


def seed(names: list[str] | None, force: bool) -> int:
    GOLDEN_DIR.mkdir(exist_ok=True)
    initialize()

    xml_files = sorted(TESTS_DIR.glob("*.xml"))
    if names:
        xml_files = [f for f in xml_files if f.stem in names]
        missing = set(names) - {f.stem for f in xml_files}
        for m in sorted(missing):
            print(f"ERROR: no test file found for '{m}'")
        if missing:
            return 1

    wrote = 0
    skipped = 0
    for xml_path in xml_files:
        name = xml_path.stem
        golden_path = GOLDEN_DIR / f"{name}.golden.xml"
        if golden_path.exists() and not force:
            print(f"SKIP  {name}  (already seeded — use --force to overwrite)")
            skipped += 1
            continue
        response_bytes = _dispatch(xml_path.read_bytes())
        golden_path.write_bytes(response_bytes)
        action = "OVERWROTE" if golden_path.exists() and force else "WROTE"
        print(f"{action}  {name}  ->  {golden_path.name}")
        wrote += 1

    print(f"\n{wrote} written, {skipped} skipped")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LVF golden file seeder — run once only",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing golden files")
    parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help="XML file stem(s) to seed (default: all)",
    )
    args = parser.parse_args()
    sys.exit(seed(args.names or None, args.force))


if __name__ == "__main__":
    main()
