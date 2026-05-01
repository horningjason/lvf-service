# LVF Civic Address Validation Service

## Project Purpose
This is an implementation of the NG9-1-1 Location Validation Function (LVF) as specified in
`LVF_Algorithm_Specification_current.docx`. The algorithm validates civic PIDF-LO addresses
against provisioned GIS data using the LoST protocol (RFC 5222).

## Governing Standards
- NENA-STA-004.2-2024 — CLDXF-US element definitions and business rules
- NENA-STA-006.3-2026 — GIS layer definitions and standardized field names
- NENA-INF-027.1-2018 — LVF evaluation logic and hierarchy principles
- NENA-STA-010.3.1-2026 — i3 Standard, LVF LoST server requirements
- RFC 5222 — LoST protocol, findServiceResponse structure
- RFC 5139 — Base PIDF-LO civic address schema (ca: namespace)
- RFC 6848 — PIDF-LO civic address extensions (cae: namespace)

## Algorithm Summary
The algorithm processes a civic PIDF-LO through three sequential gates:
- Gate 0: Service URN / boundary check → returns `<serviceNotImplemented>` on failure
- Gate 1: Structural conformance check → returns `<locationInvalid>` on failure
- Gate 2: Progressive filter against SSAP then RCL GIS layers → returns `<locationValidation>`
  with `<valid>`, `<invalid>`, `<unchecked>` elements, or `<notFound>`

The full algorithm specification is in `LVF_Algorithm_Specification_current.docx`.
Always consult that document before making implementation decisions.

## Implementation Language
Python 3.x with FastAPI for the HTTP/XML service layer.

## Project Scope (Current Version)
- Leaf node LVF behavior only
- US civic addresses only (CLDXF-US profile)
- HNO required for all validation requests
- Recursion and redirection deferred to future version

## Key Implementation Notes
- GIS field names are standardized per STA-006.3 — no field mapping required
- All element comparisons are case-insensitive exact string match
- No fuzzy matching, no alias resolution
- Geometry is used only at response assembly for `<mapping>` element selection (§7.5)
- HNO against RCL is always `<unchecked>`, never `<valid>` (INF-027 §2.5.8)
- Only one element ever appears in `<invalid>` (stop-on-first-invalid rule)

## GIS Data Layers
- SiteStructureAddressPoint (SSAP) — searched first
- RoadCenterLine (RCL) — searched second if SSAP yields no single match

## Running the Service
- Start command: `uvicorn src.server:app --reload`
- GIS data is loaded at startup from the path in `LVF_GPKG_PATH` (set in `.env` to `data/lvf_template_data.gpkg`)
- VS Code launch config: `module: uvicorn`, `args: ["src.server:app", "--reload"]`, `envFile: "${workspaceFolder}/.env"`

---

## API Reference

Base URL: `http://localhost:8000`

---

### POST /validate

Submit a civic address for LVF validation. Requires `validateLocation="true"`.

**Request:** `Content-Type: application/xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1" validateLocation="true">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr" xml:lang="en-US">
      <country>US</country>
      <A1>ND</A1>
      <A2>Burleigh County</A2>
      <A3>Bismarck</A3>
      <RD>State</RD>
      <STS>Street</STS>
      <HNO>3401</HNO>
      <PC>58503</PC>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>
```

**PowerShell:**
```powershell
$body = Get-Content tests\test_request.xml -Raw
Invoke-WebRequest -Uri http://localhost:8000/validate -Method POST -Body $body -ContentType "application/xml" | Select-Object -ExpandProperty Content
```

**Possible responses:**

| Response element | Meaning |
|---|---|
| `<findServiceResponse>` with `<locationValidation>` | Gate 2 ran; contains `<valid>`, `<invalid>`, and/or `<unchecked>` elements |
| `<errors><notFound>` | No matching address record in GIS data |
| `<errors><locationInvalid>` | Gate 1 failed — required element missing or structurally invalid |
| `<errors><serviceNotImplemented>` | Gate 0 failed — no provisioned boundary for the requested service URN |
| `<findServiceResponse>` with `<warnings><locationValidationUnavailable>` | `validateLocation` was not `"true"` |

---

### GET /health

Returns record counts for all loaded GIS layers.

```powershell
Invoke-WebRequest http://localhost:8000/health | Select-Object -ExpandProperty Content
```

```json
{
  "status": "ok",
  "ssap_records": 1500,
  "rcl_records": 8200,
  "boundaries": 3,
  "civic_coverage_entries": 42
}
```

---

### GET /coverage/geodetic

Returns GeoJSON of the unioned geodetic coverage polygon for each service URN.

```powershell
Invoke-WebRequest http://localhost:8000/coverage/geodetic | Select-Object -ExpandProperty Content
```

```json
{
  "urn:service:sos": { "type": "Polygon", "coordinates": [...] }
}
```

---

### GET /coverage/civic

Returns the civic coverage lookup table (country/A1/A2/A3/A4/A5 → boundary). `*` means wildcard (any value including null matches).

```powershell
Invoke-WebRequest http://localhost:8000/coverage/civic | Select-Object -ExpandProperty Content
```

```json
[
  {
    "country": "US",
    "a1": "ND",
    "a2": "BURLEIGH COUNTY",
    "a3": "*",
    "a4": "*",
    "a5": "*",
    "boundary_display_name": "BISMARCK COMMUNICATIONS CENTER",
    "boundary_urn": "urn:service:sos"
  }
]
```

---

### GET /coverage/civic/explain

Returns the RCL segment NGUIDs whose perpendicular offset test point lands inside a named boundary with matching admin attributes. Useful for diagnosing missing or unexpected coverage entries.

**Required parameters:** `country`, `a1`, `a2`, `boundary`
**Optional parameters:** `a3`, `a4`, `a5` — omit or pass `*` to match any value including null

```powershell
Invoke-WebRequest "http://localhost:8000/coverage/civic/explain?country=US&a1=ND&a2=DIVIDE%20COUNTY&boundary=WILLIAMS%20COUNTY%20DISPATCH%20CENTER" | Select-Object -ExpandProperty Content
```

```json
{
  "query": {
    "country": "US", "a1": "ND", "a2": "DIVIDE COUNTY",
    "a3": null, "a4": null, "a5": null,
    "boundary": "WILLIAMS COUNTY DISPATCH CENTER"
  },
  "count": 42,
  "nguids": ["ND_RCL_00001", "ND_RCL_00002", "..."]
}
```

`nguids` are STA-006.3 NGUID values from the RoadCenterLine layer (falls back to GeoPackage FID if NGUID is absent). Each segment appears at most once even if both sides matched.
