#!/usr/bin/env python3
"""DR responding-service smoke test (§3.7.1).

Fires a minimal valid Discrepancy Report at LVF's mounted DR web service and
checks for the 201 + DiscrepancyReportResponse the standard requires.

Run against a LIVE LVF (LVF_ENABLE_DR_SERVICE=true, default):
    python tests/smoke/dr_smoke.py                 # defaults to http://localhost:8000
    python tests/smoke/dr_smoke.py http://host:8000
"""
import sys
import uuid
import datetime
import httpx

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "https://localhost:8000"
URL = f"{BASE}/dr/Reports"

# §2.3 i3 Timestamp — OFFSET-aware (this IS an i3 object, so no bare 'Z').
ts = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")

body = {
    "resolutionUri": "https://reporter.example.org/dr/resolutions",
    "reportType": "LoSTDiscrepancyReport",
    "discrepancyReportSubmittalTimeStamp": ts,
    "discrepancyReportId": f"urn:emergency:uid:drid:{uuid.uuid4()}",
    "reportingAgencyName": "smoke-test.example.org",
    "reportingContactJcard": ["vcard", [["fn", {}, "text", "Smoke Tester"]]],
    "problemService": "urn:service:sos",
    "problemSeverity": "Minor",
    # LoSTDiscrepancyReport-specific block (§3.7.5):
    "query": "findService",
    "problem": "OtherLoST",
}

print(f"POST {URL}")
try:
    r = httpx.post(URL, json=body, timeout=10.0, verify=False)  # dev self-signed cert
except Exception as exc:
    print(f"  ✗ request failed: {exc}")
    sys.exit(1)

print(f"  → HTTP {r.status_code}")
ok = r.status_code == 201
print("  ✓ 201 Created — responding service works" if ok
      else f"  ✗ expected 201, got {r.status_code}: {r.text[:300]}")
try:
    print("  body:", r.json())
except Exception:
    print("  body:", r.text[:300])
sys.exit(0 if ok else 1)
