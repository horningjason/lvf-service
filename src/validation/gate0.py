"""
Gate 0 — Service URN and Boundary Check.
"""

import datetime

from src.validation.models import ServiceBoundary, ServiceNotImplementedResponse
from src.utils import _is_temporally_active


def check(
    service_urn: str,
    boundaries: list[ServiceBoundary],
    now: datetime.datetime,
) -> ServiceNotImplementedResponse | None:
    """
    Check whether the LVF has at least one temporally active provisioned
    service boundary whose ServiceURN matches the requested service URN.

    Returns None on success (processing continues to Gate 1). Returns
    ServiceNotImplementedResponse if no match is found, terminating the
    request.

    URN comparison is case-insensitive per RFC 2141.

    RFC 5222 mapping+ requirement:
        RFC 5222 §8.4.1 requires one or more <mapping> elements in every
        findServiceResponse. Gate 0 establishes that a candidate service
        boundary exists to fulfil this requirement. The specific boundary
        returned in <mapping> is determined at response assembly once
        the matched GIS record is known — not here.
    """
    for boundary in boundaries:
        if (boundary.service_urn.lower() == service_urn.lower()
                and _is_temporally_active(boundary.effective, boundary.expires, now)):
            return None

    return ServiceNotImplementedResponse()
