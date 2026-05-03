# LVF Civic Address Validation Service

## Project Purpose
This is an implementation of the NG9-1-1 Location Validation Function (LVF) as specified in
`LVF_Algorithm_Specification_current.docx`. The algorithm validates civic PIDF-LO addresses
against provisioned GIS data using the LoST protocol (RFC 5222). 

The spec and code together are designed to address the LVF consistency problem identified in 
NENA-INF-027, where two different LVF implementations can produce different validation results 
for the same address because the algorithm was never fully specified. This is both an immediate
problem for nationwide carriers working to fulfill "phase 2" requests under FCC Report and 
Order 24-78 and will become a future problem for both the 9-1-1 authority and OSP community 
should a 911 authority decide to switch their LVF provider.

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
- Service Boundary — a polygon layer provisioned with a ServiceURN matching urn:service:sos

## Running the Service
- Start command: `uvicorn src.server:app --reload`
- GIS data is loaded at startup from the path in `LVF_GPKG_PATH` (set in `.env` to `data/lvf_template_data.gpkg`)
- VS Code launch config: `module: uvicorn`, `args: ["src.server:app", "--reload"]`, `envFile: "${workspaceFolder}/.env"`

**Environment Variables:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `LVF_GPKG_PATH` | **Yes** | — | Path to the GeoPackage file containing SSAP, RCL, and boundary layers |
| `LVF_DEFAULT_MAPPING_SOURCE_ID` | **Yes** | — | UUID used as `sourceId` on the default mapping element; server refuses to start if absent |
| `LVF_SSAP_LAYER` | No | `SiteStructureAddressPoint` | GeoPackage layer name for the SSAP layer |
| `LVF_RCL_LAYER` | No | `RoadCenterLine` | GeoPackage layer name for the RCL layer |
| `LVF_BOUNDARY_LAYERS` | No | `PsapPolygon` | Comma-separated GeoPackage layer name(s) for service boundary polygons. |
| `LVF_SERVER_URI` | No | `lostserver.example.com` | Server URI placed in `<path><via source="...">` and `<errors source="...">` |
| `LVF_DISPLAY_NAME_LANG` | No | `en` | `xml:lang` value on `<displayName>` in mapping elements |
| `LVF_ENABLE_SIMILAR_LOCATION` | No | `false` | Set to `true` to enable the experimental Similar Location Extension (Phase 1) |
| `LVF_SOS_ALIAS_URNS` | No | — | Comma-separated URN(s) treated as aliases for `urn:service:sos` (§3.6). Gate 0 accepts them; the response `<mapping>` echoes the requested URN rather than the provisioned one. Example: `urn:emergency:service:sos.psap` |
---

## Testing

### Regression Suite

The regression suite lives in `tests/regression/`. It submits each `tests/*.xml` file through
`handle_find_service()` directly (no HTTP) and compares the response to a golden file.


```powershell
# Run all tests
python -m tests.regression.runner

# Run one test by name (XML file stem)
python -m tests.regression.runner --test validate_2
```

Exit code is `0` if all pass, `1` if any fail or a golden file is missing.

**Seeding golden files (run once — do not re-run casually):**

```powershell
# Seed all tests that don't yet have a golden file
python -m tests.regression.seed

# Add a new test: drop a new XML file in tests/, then seed just that file
python -m tests.regression.seed --force validate_17_complete

# Force-reset the entire baseline after a deliberate behavior change
python -m tests.regression.seed --force
```

See `tests/regression/README.md` for the full philosophy and workflow.

---

### Similar Location Extension — Phase 1 (`completeLocation`) *(EXPERIMENTAL)*

> **Note:** This feature implements `draft-ietf-ecrit-similar-location-19`, an unpublished IETF
> draft. The namespace, element names, and behavior may change as the draft evolves. Do not
> deploy in production without understanding this limitation.

The extension is disabled by default. Enable it by setting `LVF_ENABLE_SIMILAR_LOCATION=true`
in `.env` (or the environment). When disabled, no `rli` namespace or elements appear anywhere
in the response.

**`.env` flag:**
```
LVF_ENABLE_SIMILAR_LOCATION=true
```

**How to request `completeLocation`:** add `xmlns:rli` and `rli:returnAdditionalLocation="complete"`
(or `"any"`) to the `<findService>` element. Valid values are `none`, `similar`, `complete`, `any`.
Absent or unrecognised values are treated as `none`.

`completeLocation` is only populated on an **SSAP match** (HNO appears in `<valid>`). RCL matches
and non-conforming results produce no `<rli:completeLocation>` regardless of the attribute value.

**Example request (PowerShell against a running server):**
```powershell
$body = @'
<?xml version="1.0" encoding="UTF-8"?>
<findService xmlns="urn:ietf:params:xml:ns:lost1"
             xmlns:rli="urn:ietf:params:xml:ns:lost-rli1"
             validateLocation="true"
             rli:returnAdditionalLocation="complete">
  <location id="L1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country>
      <A1>ND</A1>
      <A2>Burleigh County</A2>
      <A3>Bismarck</A3>
      <RD>Capitol</RD>
      <STS>Way</STS>
      <HNO>1661</HNO>
    </civicAddress>
  </location>
  <service>urn:service:sos</service>
</findService>
'@
Invoke-WebRequest -Uri http://localhost:8000/validate -Method POST -Body $body -ContentType "application/xml" | Select-Object -ExpandProperty Content
```

**Expected addition inside `<locationValidation>` on a successful SSAP match:**
```xml
<rli:completeLocation xmlns:rli="urn:ietf:params:xml:ns:lost-rli1">
  <location id="complete" profile="civic">
    <ca:civicAddress>
      <ca:country>US</ca:country>
      <ca:A1>ND</ca:A1>
      <ca:A2>Burleigh County</ca:A2>
      <ca:A3>Bismarck</ca:A3>
      <ca:RD>Capitol</ca:RD>
      <ca:STS>Way</ca:STS>
      <ca:HNO>1661</ca:HNO>
      <ca:PCN>Bismarck</ca:PCN>
      <ca:PC>58501</ca:PC>
    </ca:civicAddress>
  </location>
</rli:completeLocation>
```

The `rli` namespace declaration appears only on the `<rli:completeLocation>` element itself —
never on the `<findServiceResponse>` root — so it is absent from the document entirely when
no `completeLocation` is returned.

**Quick programmatic test (no server required):**
```powershell
python -c "
import os; os.environ['LVF_ENABLE_SIMILAR_LOCATION'] = 'true'
from src.server import initialize, handle_find_service
initialize()
xml = open('tests/validate_2.xml', 'rb').read()
# Inject the rli attribute by patching the bytes
xml = xml.replace(
    b'validateLocation=\"true\"',
    b'xmlns:rli=\"urn:ietf:params:xml:ns:lost-rli1\" validateLocation=\"true\" rli:returnAdditionalLocation=\"complete\"'
)
print(handle_find_service(xml).decode())
"
```

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
