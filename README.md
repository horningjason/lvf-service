# LVF — Location Validation Function

A reference implementation of the NG9-1-1 Location Validation Function (LVF) as specified in
`LVF_Algorithm_Specification_v48.docx`. Validates civic PIDF-LO addresses against provisioned
GIS data using the LoST protocol (RFC 5222).

> **Note:** This is a reference implementation intended for 911 Authorities, GIS staff,
> LVF vendors and LIS vendors evaluating LVF conformance. It is not production-hardened.

---

## Quick Start — Docker (Recommended)

Docker is the easiest way to run the LVF on any platform (Windows, macOS, Linux).
[Docker Desktop](https://www.docker.com/products/docker-desktop/) must be installed.

```bash
# 1. Clone the repository
git clone https://github.com/horningjason/lvf-service
cd lvf-service

# 2. Configure environment
cp .env.example .env
# Edit .env as needed — defaults work with the included child_lvf_data.gpkg

# 3. Build and start
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
uvicorn src.server:app --reload
```

**Prerequisites:** Python 3.10 or later.

---

## GIS Data

The repository includes `data/child_lvf_data.gpkg` — a sample GeoPackage provisioned for
Burleigh County, ND. This is sufficient to run the service and evaluate LVF behavior out of
the box.

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
| `LVF_GPKG_PATH` | No† | — | Path to the GeoPackage file. Absent or missing file → routing-only mode (no GIS lookup; requests are routed via child coverage store or `LVF_PARENT_URI`) |
| `LVF_DEFAULT_MAPPING_SOURCE_ID` | No† | — | UUID used as `sourceId` on the synthetic default mapping. Recommended: `{00000000-0000-0000-0000-000000000000}`. Required when a GPKG is present; not needed in routing-only mode |
| `LVF_SSAP_LAYER` | No | `SiteStructureAddressPoint` | GeoPackage layer name for SSAP |
| `LVF_RCL_LAYER` | No | `RoadCenterLine` | GeoPackage layer name for RCL |
| `LVF_BOUNDARY_LAYERS` | No | `PsapPolygon` | Comma-separated boundary layer name(s) |
| `LVF_LOG_LEVEL` | No | `INFO` | Log level for all LVF loggers (`src.*`). Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Does not affect uvicorn's own access log. `DEBUG` surfaces every gate decision and sync push/pull detail; `INFO` covers startup progress and GIS load counts; `WARNING` limits output to anomalies and recoverable failures only |
| `LVF_SERVER_URI` | No | `lostserver.example.com` | Server URI in `<path>` and `<errors source>` |
| `LVF_DISPLAY_NAME_LANG` | No | `en` | `xml:lang` on `<displayName>` elements |
| `LVF_SOS_ALIAS_URNS` | No | — | Comma-separated URN aliases for `urn:service:sos` |
| `LVF_PARENT_URI` | No | — | DNS name of a parent LoST server. When set, out-of-coverage admin-level queries return `<redirect>` instead of `<notFound>` |
| `LVF_GPKG_POLL_INTERVAL_SECONDS` | No | `60` | How often (seconds) to check for GeoPackage updates. Set to `0` to disable |
| `LVF_SYNC_CHILDREN` | No | — | Comma-separated child LVF `/sync` URLs to pull coverage from on startup. Makes this node a LoST-Sync parent |
| `LVF_SYNC_SOURCE_ID_CIVIC` | No | — | Stable UUID for this node's civic coverage region push to parent. Required to push; unused if `LVF_PARENT_URI` is unset |
| `LVF_SYNC_SOURCE_ID_GEODETIC` | No | — | Stable UUID for this node's geodetic coverage region push to parent. Required to push; unused if `LVF_PARENT_URI` is unset |

† Required when `LVF_GPKG_PATH` points to an existing file; not needed in routing-only mode.

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
| `POST` | `/validate` | Submit a civic address for LVF validation (`Content-Type: application/xml`) |
| `POST` | `/sync` | LoST-Sync (RFC 6739) — accepts `pushMappings` and `getMappingsRequest` (`Content-Type: application/lostsync+xml`) |
| `GET` | `/health` | GIS layer record counts |
| `GET` | `/coverage/geodetic` | GeoJSON of the unioned service boundary coverage polygon |
| `GET` | `/coverage/civic` | Civic coverage lookup table |
| `GET` | `/coverage/civic/explain` | Diagnose RCL segment coverage for a given admin hierarchy |

---

## Project Structure

```
src/                    Application source
  server.py             FastAPI entry point, startup, /validate endpoint
  gate0.py              Gate 0 — service URN / boundary check
  gate1.py              Gate 1 — structural conformance check
  gate2.py              Gate 2 — progressive filter (SSAP then RCL)
  response_assembly.py  <mapping> selection and response XML construction
  models.py             Data models: SSAPRecord, RCLRecord, FilterState, etc.
  utils.py              Shared utilities
schemas/                XSD files for XML schema validation
data/                   GeoPackage data files
  child_lvf_data.gpkg   Sample data — Burleigh County, ND
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
