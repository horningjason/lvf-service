"""
Gate 1 — Structural Conformance Check.
"""

from src.validation.models import CivicAddress, LocationInvalidResponse

# Required elements, in the order they are checked.
# Tuples of (CivicAddress field name, PIDF-LO element name for response messages).
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("country", "ca:country"),
    ("a1",      "ca:A1"),
    ("a2",      "ca:A2"),
    ("rd",      "ca:RD"),
    ("hno",     "ca:HNO"),
)


def check(address: CivicAddress) -> LocationInvalidResponse | None:
    """
    Verify the submitted PIDF-LO contains non-empty values for all required
    elements: ca:country, ca:A1, ca:A2, ca:RD, ca:HNO.

    Returns None on success (processing continues to Gate 2). Returns
    LocationInvalidResponse identifying the first failing element if any
    required element is absent or empty. No GIS lookup is performed.

    Omitted vs. empty distinction:
        - None  → element tag absent from the PIDF-LO (omitted)
        - ""    → element tag present but contains no value (empty)
        Both fail Gate 1 for required elements. The message distinguishes
        them to support diagnostics on the LIS side.
    """
    for field, pidf_lo in _REQUIRED:
        value = getattr(address, field)
        if not value:  # covers None (omitted) and "" (empty)
            reason = "absent" if value is None else "empty"
            return LocationInvalidResponse(
                message=f"Required element {pidf_lo} is {reason}"
            )
    return None
