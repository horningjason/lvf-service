# LVF Vendor Integration Guide

This document is written for companies building Location Validation Function (LVF) implementations
who want to evaluate their own LVF behavior against this reference, understand the algorithm's 
expected behavior, or use the test suite to identify divergence between implementations.

---

## 1. Why This Exists — The Consistency Problem

NENA-INF-027.1-2018 identified a fundamental problem: because the LVF algorithm was never fully
specified, two different LVF implementations can produce different validation results for the same
address. This is an immediate problem for nationwide carriers fulfilling "phase 2" location
requests under FCC Report and Order 24-78, and a longer-term problem for any 911 authority that
might switch LVF providers and find that validation results change.

This repository is an attempt to close that gap. The algorithm is specified in detail in
`LVF_Algorithm_Specification_v67.docx`, and this codebase is a normative implementation of that
specification. Where the code and the spec conflict, the spec governs.

---

## 2. The Algorithm Specification

`LVF_Algorithm_Specification_v67.docx` (in this repository) is the authoritative description of
the algorithm. It defines:

- The three-gate structure (Pre-Gate-0 → Gate 0 → Gate 1 → Gate 2)
- The 33-position element evaluation hierarchy
- The progressive filter logic and stop-on-first-invalid rule
- The SSAP-to-RCL fallthrough conditions
- Out-of-coverage admin-level redirect behavior
- Response assembly and mapping element selection

Vendors evaluating their LVF implementations against this implementation should treat this 
document as the primary reference, not this codebase. The codebase exists to make the 
specification executable and testable.

---

## 3. GIS Data Requirements

The algorithm operates against three GIS layers. Field names are standardized per
**NENA-STA-006.3-2026** and used verbatim — no field mapping or configuration is supported.

### SiteStructureAddressPoint (SSAP)

Point layer. Key fields used in validation:

| Field | Type | Used for |
|---|---|---|
| `Country`, `A1`–`A5` | String | Admin hierarchy matching |
| `St_Name`, `St_PreDir`, `St_PreTyp`, `St_PreSep`, `St_PreMod`, `St_PosTyp`, `St_PosDir`, `St_PosMod` | String | Street name element matching |
| `Add_Number` | Integer | HNO exact integer comparison |
| `AddNum_Pre`, `AddNum_Suf` | String | HNP, HNS matching |
| `Site`, `SubSite`, `Structure`, `Wing`, `Floor`, `UnitPreTyp`, `UnitValue`, `Room`, `Section`, `Row`, `Seat`, `LocMarker` | String | Named location elements |
| `Post_Comm`, `Post_Code`, `PostCodeEx` | String | Postal elements (PCN, PC, PCE) |
| `Effective`, `Expire` | ISO 8601 datetime | Temporal filtering |

### RoadCenterLine (RCL)

Line layer. Administrative and postal fields are side-specific (`_L` / `_R`). Street name fields
are shared.

| Field | Type | Used for |
|---|---|---|
| `Country_L/R`, `A1_L/R`–`A5_L/R` | String | Admin hierarchy matching (side-specific) |
| `St_Name`, `St_PreDir`, etc. | String | Street name matching (shared) |
| `FromAddr_L/R`, `ToAddr_L/R` | Integer | HNO range check |
| `Parity_L/R` | `E`, `O`, or `B` | HNO parity check |
| `Valid_L/R` | `Y` or `N` | HNO validation flag |
| `AdNumPre_L/R` | String | HNP (post-HNO, side-specific) |
| `PostComm_L/R`, `PostCode_L/R` | String | PCN, PC (side-specific) |
| `Effective`, `Expire` | ISO 8601 datetime | Temporal filtering |
| `NGUID` | String | Segment identifier (diagnostic) |

### Service Boundary

Polygon layer. One polygon per PSAP service area.

| Field | Used for |
|---|---|
| `ServiceURN` | Gate 0 URN match; `urn:service:sos` required |
| `NGUID` | `sourceId` attribute on `<mapping>` |
| `Agency_ID` | `source` attribute on `<mapping>` |
| `ServiceURI` | `<uri>` child of `<mapping>` |
| `ServiceNum` | `<serviceNumber>` child of `<mapping>` |
| `DsplayName` | `<displayName>` child of `<mapping>` |
| `Expire`, `DateUpdate` | `expires`, `lastUpdated` attributes on `<mapping>` |
| `Effective`, `Expire` | Temporal filtering (Gate 0 active boundary check) |

---

## 4. Protocol — Request and Response

### POST /lost — LoST protocol endpoint

The `/lost` endpoint accepts `POST` with `Content-Type: application/lost+xml` and handles all
RFC 5222 request types: `findService`, `listServices`, `listServicesByLocation`, and
`getServiceBoundary`.

**`findService` (LVF validation)** — The body must be a valid RFC 5222 `<findService>` element
with `validateLocation="true"` and `profile="civic"`. The service URN must be `urn:service:sos`
(or a configured alias).

The minimum civic address for Gate 1 to pass is: `country`, `A1`, `A2`, `RD`, `HNO`. Any
additional submitted elements are evaluated in hierarchical order (§7 of the spec).

### Responses

| Element returned | Meaning | RFC 5222 reference |
|---|---|---|
| `<findServiceResponse>` with `<locationValidation>` | Gate 2 completed; contains `<valid>`, `<invalid>`, and/or `<unchecked>` | §8.4.2 |
| `<errors><notFound>` | No single matching GIS record found | §13.1 |
| `<errors><locationInvalid>` | Gate 1 failure — required element missing or empty | §13.1 |
| `<errors><serviceNotImplemented>` | Gate 0 failure — no provisioned boundary for the URN | §13.1 |
| `<errors><badRequest>` | Pre-Gate-0 failure — request does not conform to schema | §13.1 |
| `<redirect>` | Out-of-coverage admin-level failure with a configured parent | §13.3 |
| `<findServiceResponse>` with `<warnings><locationValidationUnavailable>` | `validateLocation` was not `"true"` | §13.2 |

All responses are HTTP 200. Error conditions are expressed in the XML body, not HTTP status codes,
per RFC 5222.

### Key Behavioral Invariants

These are the points where vendor implementations most commonly diverge:

- **Stop-on-first-invalid.** Exactly one element ever appears in `<invalid>`. The filter stops
  immediately when the candidate set reaches zero. All submitted elements after the stop position
  go to `<unchecked>`.

- **HNO on RCL is always `<unchecked>`, never `<valid>`.** Even when HNO narrows the candidate
  set to a single record, it is placed in `<unchecked>` on an RCL match. (INF-027 §2.5.8)

- **SSAP terminal → fall through to RCL.** If SSAP exists but yields zero candidates (terminal),
  the algorithm discards the SSAP result and runs the full RCL filter. SSAP terminal does not
  produce an invalid response.

- **Element ordering in responses.** The `<valid>`, `<invalid>`, and `<unchecked>` text content
  lists elements in the canonical 33-position hierarchy order, regardless of submission order.

- **All comparisons are case-insensitive exact string match.** No fuzzy matching, no
  normalization beyond case folding.

- **Null GIS field behavior is element-specific.** PCN and PC treat a null GIS field as
  `<unchecked>` (the LVF cannot verify what it doesn't have). Other fields treat null GIS as
  an empty string, which only matches an empty submitted value.

**`listServices`** — returns the space-separated set of provisioned service URNs. An optional
`<service>` child filters the response to immediate dot-separated children of that URN.

```xml
<listServices xmlns="urn:ietf:params:xml:ns:lost1"/>
```

```xml
<!-- with filter -->
<listServices xmlns="urn:ietf:params:xml:ns:lost1">
  <service>urn:service:sos</service>
</listServices>
```

**listServicesByLocation** — returns the URNs whose provisioned boundaries contain the given
location. Supports `profile="geodetic-2d"` (GML Point, lat lon order in `<pos>`) and
`profile="civic"` (PIDF-LO `<civicAddress>`, country/A1/A2 minimum). A3–A5 are optional;
absent fields match any value including null.

```xml
<listServicesByLocation xmlns="urn:ietf:params:xml:ns:lost1" recursive="false">
  <location id="loc1" profile="geodetic-2d">
    <Point xmlns="http://www.opengis.net/gml" srsName="urn:ogc:def:crs:EPSG::4326">
      <pos>46.828121 -100.883898</pos>
    </Point>
  </location>
</listServicesByLocation>
```

```xml
<listServicesByLocation xmlns="urn:ietf:params:xml:ns:lost1" recursive="false">
  <location id="loc1" profile="civic">
    <civicAddress xmlns="urn:ietf:params:xml:ns:pidf:geopriv10:civicAddr">
      <country>US</country><A1>ND</A1><A2>Burleigh County</A2>
    </civicAddress>
  </location>
</listServicesByLocation>
```

When `recursive="false"` and the location is outside this node's coverage, the server returns
`<redirect>` to the parent (if configured) rather than an empty list. When `recursive="true"`,
the request is forwarded to the parent and the parent's response is returned directly.

---

## 5. Using the Test Suite for Conformance Testing

The regression suite in `tests/regression/` defines the reference behavior. Each test is a pair
of files: a `tests/requests/<TEST-ID>.xml` request and a `tests/regression/golden/<TEST-ID>.golden.xml`
expected response.

The test runner dispatches directly to the appropriate handler based on the root element
(`handle_find_service()` for `findService`, `list_services.handle()` for `listServices`,
`list_services_by_location.handle()` for `listServicesByLocation`) — no HTTP server is required.
To use it against your own implementation, adapt `tests/regression/runner.py` to POST each
request to your endpoint instead. The comparison logic is a normalized field-by-field XML diff,
not a string comparison.

The sample GeoPackage at `data/child_lvf_data.gpkg` (Burleigh, McLean, Mercer, and Oliver
counties, ND) is the dataset against which all golden files are produced. To run the suite
meaningfully against your implementation, your system must load the same GeoPackage.

## Test Case Naming Convention

Test IDs follow the pattern: `{GATE}-{LAYER}-{CATEGORY}-{SEQ}`

### Gate prefixes

| Prefix | Meaning |
|--------|---------|
| `PROTO` | Protocol / pre-gate checks (malformed XML, missing elements, validateLocation) |
| `G0` | Gate 0 — Service URN and boundary check |
| `G1` | Gate 1 — Structural conformance (minimum required elements) |
| `G2` | Gate 2 — Progressive filter (GIS evaluation) |
| `TEMP` | Temporal filtering (Effective/Expire date handling) |
| `RESP` | Response assembly (mapping element, revalidateAfter, defaultMapping) |
| `EXT` | Extensions (completeLocation, always-unchecked elements, etc.) |
| `LOST` | LoST protocol requests on `/lost` — `listServices`, `listServicesByLocation` (note: `findService` validation tests use `G0`–`G2`, `PROTO`, etc.) |

### Layer segment (G2 only)

| Segment | Meaning |
|---------|---------|
| `SSAP` | Test exercises the SiteStructureAddressPoint layer |
| `RCL` | Test exercises the RoadCenterLine layer |
| `FALL` | Test exercises SSAP-to-RCL fallthrough boundary behavior |
| `NF` | notFound result |

For non-G2 gates the layer segment describes the condition, not a GIS layer
(e.g. `G0-URN-...`, `G1-STRUCT-...`, `PROTO-REQ-...`).

### Category segment

Free-form but should be descriptive enough to understand the condition without
opening the file. Examples: `VALID`, `INVALID-A2`, `MISSING-HNO`, `EMPTY-COUNTRY`,
`RCL-ONLY-RD`, `PARITY`, `FUTURE-EFF`, `EXPIRED`, `VALIDFLAG`.

### Sequence

Zero-padded three-digit integer (`001`, `002`, ...). Variants of the same condition
get sequential numbers — e.g. `G2-FALL-RCL-ONLY-RD-001` and `G2-FALL-RCL-ONLY-RD-002`
are the same scenario with different submitted elements (one with PCN, one with PC).

---

## 6. LoST-Sync (RFC 6739)

The `/sync` endpoint accepts `pushMappings` and `getMappingsRequest` per RFC 6739. Coverage
regions are exchanged as `<mapping>` elements with `<serviceBoundary profile="civic">` or
`profile="geodetic-2d"`. The `<uri/>` child is intentionally empty on coverage region mappings
(RFC 6739 Figure 2).

If your implementation participates in a LoST hierarchy with this reference node as parent or
child, the sync protocol is the mechanism for exchanging coverage regions. See the `POST /sync`
section of the README for request/response examples.

---

## 7. Limitations of This Implementation

This is an open reference implementation intended for vendor evaluation and interoperability
testing. It is **not production-hardened**:

- No authentication or access control on the `/lost` endpoint
- No rate limiting
- Single-process; not designed for horizontal scaling
- The GeoPackage pickle cache is not secured against tampering
- No formal SLA or uptime guarantee

Vendors evaluating this implementation for production deployment should conduct their own security
review and add appropriate hardening for their environment.

---

## 8. Reporting Divergence

If your implementation produces a different result than this reference for the same input and GIS
data, open a GitHub issue tagged `conformance` with:

- The request XML
- Your implementation's response
- This reference implementation's response (from the test suite or the running service)
- Which spec section you believe your behavior follows

Divergence reports are the most valuable contribution this project can receive. The goal is not
to prove that this implementation is correct — it is to identify and resolve ambiguities in the
specification so that all implementations can agree.
