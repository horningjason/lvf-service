# LVF — Location Validation Function

This repository contains an open reference implementation of the NG9-1-1 Location Validation Function (LVF) 
as specified in `LVF_Algorithm_Specification_v67.docx`. Validates civic PIDF-LO addresses against provisioned
GIS data using the LoST protocol (RFC 5222). The implementation can be configured to run as a child, parent, 
root AMS or forest guide.  When operating in forest guide mode, the service is only configured to support
queries relevant to LVF and location validation.

> **Note:** The repository was developed principally to define the process by which an LVF evaluates an input 
> PIDF-LO against authoritative mapping data provided by a 911 authority.  It has grown from that original focus 
> to address the full lifecycle of a validateLocation request in an NG9-1-1 deployment, including tree topology,
> coverage region derivation, and response assembly. It is not intended for production, nor is it production-hardened

---

## Quick Start — Docker (Recommended)

Docker provides a simple cross-platform way to run the LVF on Windows, macOS, and Linux. Windows and macOS users typically use [Docker Desktop](https://www.docker.com/products/docker-desktop/), while Linux users can use either Docker Desktop or Docker Engine.

```bash
# 1. Clone the repository
git clone https://github.com/horningjason/lvf-service
cd lvf-service

# 2. Configure environment
cp .env.example .env
# Edit .env as needed — defaults work with the included child_lvf_data.gpkg

#3. Configure docker-compose.yml (optional)
nano docker-compose.yml
# Edit docker-compose.yml as needed - defaults work if only evaluating data within
# the child_lvf_data.gpkg.  Additional configuration necessary if running multiple
# instances of the LVF in order to simulate a comprehensive LoST architecture.

# 4. Build and start
docker compose up -d
```

The server starts on `http://localhost:8000`. Verify with:

```bash
curl http://localhost:8000/health
```

To stop:

```bash
docker compose down
```

To use your own GeoPackage, place it in the `data/` folder and update `LVF_GPKG_PATH` in `.env`.
The `data/` folder is mounted as a volume — changes are picked up at the next poll interval
without rebuilding the image.

---

## Quick Start — Python

If you prefer to run without Docker:

```bash
# 1. Clone the repository
git clone https://github.com/horningjason/lvf-service
cd lvf-service

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env as needed

# 5. Start the server
uvicorn src.server:app --reload --host 0.0.0.0 --port 8000
```

**Prerequisites:** Python 3.10 or later.

---

## GIS Data

The repository includes `data/child_lvf_data.gpkg` — a sample GeoPackage provisioned for
Burleigh County, McLean County, Mercer County and Oliver County, ND. This is sufficient to 
run the service and evaluate LVF behavior out of the box.  The provided GeoPackage follows
NENA's GeoPackage v3.0 template verbatim.

The server requires a GeoPackage containing three layer types:

| Layer | Default name | Purpose |
|---|---|---|
| Site Structure Address Point | `SiteStructureAddressPoint` | SSAP — point address records |
| Road Center Line | `RoadCenterLine` | RCL — street segment records with address ranges |
| Service Boundary | `PsapPolygon` | Polygon boundaries with `ServiceURN` field |

Layer names are configurable via `.env` (see [Environment Variables](#environment-variables)).
Field names must conform to NENA-STA-006.3 standardized names — no field mapping is performed.

---

## Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Required | Default | Description |
|---|---|---|---|
| **Server Identity** | | | |
| `LVF_SERVER_URI` | No | `lostserver.example.com` | Server URI in `<path>` and `<errors source>` |
| `LVF_AGENCY_ID` | No | — | DNS-style agency identifier (e.g. `nd911.nd.gov`). Populates `agencyId` in i3 LogEvents (NENA-STA-010.3.1 §4.12.3.1). A WARNING is logged at startup if unset |
| `LVF_DISPLAY_NAME_LANG` | No | `en` | `xml:lang` on `<displayName>` elements |
| `LVF_LOG_LEVEL` | No | `INFO` | Log level for all LVF loggers (`src.*`). Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Does not affect uvicorn's own access log. `DEBUG` surfaces every gate decision and sync push/pull detail; `INFO` covers startup progress and GIS load counts; `WARNING` limits output to anomalies and recoverable failures only |
| **GIS Data** | | | |
| `LVF_GPKG_PATH` | No† | — | Path to the GeoPackage file. Absent or missing file → routing-only mode (no GIS lookup; requests are routed via child coverage store or `LVF_PARENT_URI`) |
| `LVF_DEFAULT_MAPPING_SOURCE_ID` | No† | — | UUID used as `sourceId` on the synthetic default mapping. Recommended: `{00000000-0000-0000-0000-000000000000}`. Required when a GPKG is present; not needed in routing-only mode |
| `LVF_SSAP_LAYER` | No | `SiteStructureAddressPoint` | GeoPackage layer name for SSAP |
| `LVF_RCL_LAYER` | No | `RoadCenterLine` | GeoPackage layer name for RCL |
| `LVF_BOUNDARY_LAYERS` | No | `PsapPolygon` | Comma-separated boundary layer name(s) |
| `LVF_GPKG_POLL_INTERVAL_SECONDS` | No | `60` | How often (seconds) to check for GeoPackage updates. Set to `0` to disable |
| **LoST Service** | | | |
| `LVF_SOS_ALIAS_URNS` | No | — | Comma-separated URN aliases for `urn:service:sos` |
| **Tree Topology & LoST-Sync** | | | |
| `LVF_PARENT_URI` | No | — | DNS name of a parent LoST server. When set, out-of-coverage admin-level queries return `<redirect>` instead of `<notFound>` |
| `LVF_SYNC_CHILDREN` | No | — | Comma-separated child LVF `/sync` URLs to pull coverage from on startup. Makes this node a LoST-Sync parent |
| `LVF_SYNC_SOURCE_ID_CIVIC` | No | — | Stable UUID for this node's civic coverage region push to parent. Required to push; unused if `LVF_PARENT_URI` is unset |
| `LVF_SYNC_SOURCE_ID_GEODETIC` | No | — | Stable UUID for this node's geodetic coverage region push to parent. Required to push; unused if `LVF_PARENT_URI` is unset |
| **Root AMS Mode** | | | |
| `LVF_ROOT_AMS` | No | `false` | When `true`, activates Root AMS mode. Suppresses programmatic GIS-derived push to `LVF_PARENT_URI` and instead pushes operator-declared coverage from provisioning files to `LVF_FOREST_GUIDE_URI`. Out-of-coverage redirect/recursion via `LVF_PARENT_URI` is unaffected |
| `LVF_FOREST_GUIDE_URI` | No | — | Full `/sync` URL of the Forest Guide. Only used when `LVF_ROOT_AMS=true`. Example: `http://host.docker.internal:8002/sync` |
| **Forest Guide Mode** | | | |
| `LVF_FOREST_GUIDE_MODE` | No | `false` | When `true`, this node operates as a Forest Guide: GIS validation is skipped, all requests are redirected to the matching child LVF, and `LVF_PARENT_URI` is ignored |
| **NTP** | | | |
| `LVF_NTP_SERVER` | No | — | Hostname of the NTP server. When unset, NTP is disabled and the system clock is used. When set, time-sensitive fields use NTP; query failures log a WARNING and fall back to the system clock |
| `LVF_NTP_VERSION` | No | `3` | NTP protocol version (only used when `LVF_NTP_SERVER` is set) |
| `LVF_NTP_TIMEOUT` | No | `5.0` | NTP query timeout in seconds (only used when `LVF_NTP_SERVER` is set) |
| **i3 Logging** | | | |
| `LVF_LOGGING_SERVICE_URI` | No | — | URI of an i3 Logging Service to POST LogEvents to. When unset, events are emitted to Python standard logging only |
| **Discrepancy Reporting** | | | |
| `LVF_DR_ENDPOINT` | No | — | HTTP endpoint to POST Discrepancy Reports to (responding agency's `/Reports` service). When unset, DRs are logged locally only (NENA-STA-010.3.1 §3.7.1) |
| `LVF_DR_RESOLUTION_URI` | No | — | URI this LVF exposes for receiving DR resolution callbacks. Used as `resolutionUri` in the DR body. Required for conformant submission |
| `LVF_DR_CONTACT_NAME` | No | `LVF Administrator` | Contact name in the DR jCard (`reportingContactJcard`). A WARNING is logged at startup if unset |
| `LVF_DR_CONTACT_EMAIL` | No | — | Contact email in the DR jCard. A WARNING is logged at startup if unset |
| **SIP State Notifications** | | | |
| `LVF_SIP_HOST` | No | `0.0.0.0` | IP address or hostname to bind the SIP listener |
| `LVF_SIP_PORT` | No | `5060` | SIP port for SUBSCRIBE/NOTIFY. Set to `0` to disable the SIP listener entirely |
| `LVF_SIP_ALLOWED_SUBSCRIBERS` | No | — | Comma-separated SIP URIs permitted to subscribe (e.g. `sip:esrp.example.com`). When unset, all SUBSCRIBE requests are accepted (appropriate for ESInet trust model where network-level access control is assumed) |

† Required when `LVF_GPKG_PATH` points to an existing file; not needed in routing-only mode.

---

## Deployment Topologies

The service supports four operating modes, set by environment variables:

| Mode | Key variables | Behavior |
|---|---|---|
| **Child LVF** | `LVF_GPKG_PATH`, `LVF_PARENT_URI`, `LVF_SYNC_SOURCE_ID_CIVIC/GEODETIC` | Validates addresses against local GIS data. Pushes coverage to parent on startup and GIS reload. Out-of-coverage queries redirect to parent. |
| **Parent / Intermediate LVF** | `LVF_GPKG_PATH`, `LVF_PARENT_URI`, `LVF_SYNC_CHILDREN` | Validates locally and routes to children for addresses in their coverage. Aggregates child coverage upstream. |
| **Root AMS** | `LVF_GPKG_PATH`, `LVF_PARENT_URI` (for routing), `LVF_ROOT_AMS=true`, `LVF_FOREST_GUIDE_URI` | Validates locally. Pushes **operator-declared** civic/geodetic coverage from `ams_civic_coverage.json` and `ams_geodetic_coverage.json` to the Forest Guide instead of GIS-derived tuples. Out-of-coverage queries still escalate to `LVF_PARENT_URI`. Coverage changes cascade to the FG automatically. |
| **Forest Guide** | `LVF_FOREST_GUIDE_MODE=true`, `LVF_SYNC_CHILDREN` | No GIS validation. Routes all requests to the matching child LVF via the child coverage store. |

### Root AMS Provisioning Files

Root AMS nodes require two files in the same directory as the GeoPackage. Annotated templates are provided in `data/ams_civic_coverage.example.json` and `data/ams_geodetic_coverage.example.json` — copy and rename them to activate.

**`ams_civic_coverage.json`** — JSON array of coverage mapping entries. Each entry declares one set of civic tuples this node is authoritative for:

```json
[
  {
    "source": "root-ams.lvf.example.com",
    "source_id": "{11111111-1111-1111-1111-111111111111}",
    "last_updated": "2026-01-01T00:00:00Z",
    "expires": "NO-EXPIRATION",
    "service": "urn:service:sos",
    "profile": "civic",
    "child_uri": "http://root-ams.lvf.example.com/lost",
    "civic_tuples": [
      { "country": "US", "a1": "ND", "a2": "Burleigh County", "lost_server": "http://root-ams.lvf.example.com/lost" },
      { "country": "US", "a1": "ND", "a2": "McLean County",   "lost_server": "http://root-ams.lvf.example.com/lost" }
    ]
  }
]
```

`source_id` must match `LVF_SYNC_SOURCE_ID_CIVIC`. `child_uri` and `lost_server` should be this node's own `/lost` URL (the Forest Guide will redirect queries here). `a3`, `a4`, `a5` are optional in each tuple.

**`ams_geodetic_coverage.json`** — JSON array with a single entry containing a WKT polygon of the node's geodetic boundary:

```json
[
  {
    "source": "root-ams.lvf.example.com",
    "source_id": "{22222222-2222-2222-2222-222222222222}",
    "last_updated": "2026-01-01T00:00:00Z",
    "expires": "NO-EXPIRATION",
    "service": "urn:service:sos",
    "profile": "geodetic-2d",
    "child_uri": "http://root-ams.lvf.example.com/lost",
    "geodetic_geom_wkt": "POLYGON ((-102.5 46.4, -100.0 46.4, -100.0 48.6, -102.5 48.6, -102.5 46.4))"
  }
]
```

`source_id` must match `LVF_SYNC_SOURCE_ID_GEODETIC`. Coordinates are `(longitude latitude)`. The WKT polygon is converted to GML when pushed to the Forest Guide — it is never stored as-is on the wire.


---

## SIP State Notifications (ElementState / ServiceState)

When `LVF_SIP_HOST`/`LVF_SIP_PORT` are configured (and `LVF_SIP_PORT` is non-zero), the LVF
exposes a SIP endpoint that accepts SUBSCRIBE requests for the `emergency-ElementState` and
`emergency-ServiceState` event packages per NENA-STA-010.3.1 §2.4.1 and §2.4.2. The LVF
sends SIP NOTIFY to all active subscribers whenever its element or service state changes.

This is the i3-required notifier-side interface that allows ESInet elements (ESRPs, monitoring
systems) to subscribe to LVF health state. The SIP endpoint listens on both UDP and TCP.

In production, the SIP interface should be on the ESInet SIP network, separate from the HTTPS
LoST interface.

**To enable:** set `LVF_SIP_PORT=5060` (or another port) in `.env`.

**To disable:** set `LVF_SIP_PORT=0` (or leave unset).

---

## Running Tests

The regression suite submits each request in `tests/requests/` through the algorithm and
compares the response to a golden file in `tests/regression/golden/`.

```bash
# Run all regression tests
python -m tests.regression.runner

# Run a single test
python -m tests.regression.runner --test G2-SSAP-VALID-002
```

Exit code is `0` if all pass, `1` if any fail or a golden file is missing.

See `tests/regression/README.md` for full details on seeding golden files.

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/lost` | LoST protocol endpoint (RFC 5222) — `findService`, `listServices`, `listServicesByLocation`, `getServiceBoundary` (`Content-Type: application/lost+xml`). `findService` requires `validateLocation="true"` |
| `POST` | `/sync` | LoST-Sync (RFC 6739) — accepts `pushMappings` and `getMappingsRequest` (`Content-Type: application/lostsync+xml`) |
| `GET` | `/health` | GIS layer record counts, element state, and service state |
| `GET` | `/coverage/geodetic` | GeoJSON of the unioned service boundary coverage polygon |
| `GET` | `/coverage/civic` | Civic coverage lookup table |
| `GET` | `/coverage/civic/explain` | Diagnose RCL segment coverage for a given admin hierarchy |

---

## Project Structure

```
src/                        Application source
  server.py                 FastAPI thin router — app, lifespan, HTTP endpoints
  utils.py                  Shared utilities
  ntp.py                    NTP client — syncs time via ntplib, falls back to system clock
  lost/                     LoST protocol handlers (RFC 5222)
    find_service.py         Core LVF logic: GIS loading, gate orchestration,
                            XML helpers, LoST-Sync, handle_find_service()
    list_services.py        listServices — returns provisioned URNs, optional child-filter
    list_services_by_location.py  listServicesByLocation — geodetic-2d and civic profiles
    get_service_boundary.py getServiceBoundary stub (notFound)
  validation/               Three-gate algorithm
    gate0.py                Gate 0 — service URN / boundary check
    gate1.py                Gate 1 — structural conformance check
    gate2.py                Gate 2 — progressive filter (SSAP then RCL)
    response_assembly.py    <mapping> selection and response XML construction
    models.py               Data models: SSAPRecord, RCLRecord, FilterState, etc.
  logging_events/           Structured log event types
    log_events.py           LostQueryLogEvent, LostResponseLogEvent dataclasses
    logger.py               emit_log_event() helper
  notifications/            ElementState and ServiceState change notifiers (NENA-STA-010.3.1 §10.12–13)
  discrepancy/              Discrepancy report generation and submission (NENA-STA-010.3.1 §3.7)
schemas/                    XSD files for XML schema validation
data/                   GeoPackage data files and runtime state
  child_lvf_data.gpkg         Sample data — Burleigh, McLean, Mercer, Oliver counties
  lvf_child_coverage.json     Child coverage store (written at runtime; do not edit manually)
  ams_civic_coverage.json     Root AMS civic coverage declaration (operator-provisioned)
  ams_geodetic_coverage.json     Root AMS geodetic boundary declaration (operator-provisioned)
tests/                  Test XML inputs and regression infrastructure
  regression/
    golden/             Expected output files (committed)
    runner.py           Test runner
    seed.py             Golden file seeder
```

---

## Governing Standards

- NENA-STA-004.2-2024 — CLDXF-US element definitions
- NENA-STA-006.3-2026 — GIS layer definitions and field names
- NENA-INF-027.1-2018 — LVF evaluation logic and hierarchy
- NENA-STA-010.3.1-2026 — i3 Standard, LVF LoST requirements
- RFC 5222 — LoST protocol
- RFC 5139 — PIDF-LO civic address schema
- RFC 6848 — PIDF-LO civic address extensions

## Other Documents
- RFC 5582 - LoST mapping architecture (informational)
- RFC 6739 - LoST sync (experimental)
- draft-ietf-ecrit-similar-location-19
- draft-ietf-ecrit-lost-planned-changes-17