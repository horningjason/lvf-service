# LVF — Location Validation Function

A reference implementation of the NG9-1-1 Location Validation Function (LVF) as specified in
`LVF_Algorithm_Specification_vXX.docx`. Validates civic PIDF-LO addresses against provisioned
GIS data using the LoST protocol (RFC 5222).

---

## Prerequisites

- Python 3.10 or later
- A GeoPackage (`.gpkg`) containing the provisioned GIS layers (see [GIS Data](#gis-data) below)

---

## Quick Start

```bash
# 1. Clone the repository
git clone <repo-url>
cd <repo-name>

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
# Edit .env and set LVF_GPKG_PATH to your GeoPackage file (see below)

# 5. Start the server
uvicorn src.server:app --reload
```

The server starts on `http://localhost:8000`. Verify with:

```bash
curl http://localhost:8000/health
```

---

## GIS Data

The server requires a GeoPackage containing three layer types:

| Layer | Default name | Purpose |
|---|---|---|
| Site Structure Address Point | `SiteStructureAddressPoint` | SSAP — point address records |
| Road Center Line | `RoadCenterLine` | RCL — street segment records with address ranges |
| Service Boundary | `PsapPolygon` | Polygon boundaries with `ServiceURN` field |

Layer names are configurable via `.env` (see [Environment Variables](#environment-variables)).

Place the `.gpkg` file anywhere accessible and set `LVF_GPKG_PATH` accordingly. The recommended
location is a `data/` subdirectory at the repo root (already in `.gitignore`):

```
data/
  lvf_template_data.gpkg
```

Field names must conform to NENA-STA-006.3 standardized names — no field mapping is performed.

---

## Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Required | Default | Description |
|---|---|---|---|
| `LVF_GPKG_PATH` | **Yes** | — | Path to the GeoPackage file |
| `LVF_DEFAULT_MAPPING_SOURCE_ID` | **Yes** | — | UUID used as `sourceId` on the synthetic default mapping. Server refuses to start if absent. Recommended: `{00000000-0000-0000-0000-000000000000}` |
| `LVF_SSAP_LAYER` | No | `SiteStructureAddressPoint` | GeoPackage layer name for SSAP |
| `LVF_RCL_LAYER` | No | `RoadCenterLine` | GeoPackage layer name for RCL |
| `LVF_BOUNDARY_LAYERS` | No | `PsapPolygon` | Comma-separated boundary layer name(s) |
| `LVF_SERVER_URI` | No | `lostserver.example.com` | Server URI in `<path>` and `<errors source>` |
| `LVF_DISPLAY_NAME_LANG` | No | `en` | `xml:lang` on `<displayName>` elements |
| `LVF_ENABLE_SIMILAR_LOCATION` | No | `false` | Enable experimental Similar Location Extension |
| `LVF_SOS_ALIAS_URNS` | No | — | Comma-separated URN aliases for `urn:service:sos` |

---

## Running Tests

The regression suite submits each `tests/*.xml` file through the algorithm directly (no HTTP)
and compares the response to a golden file in `tests/regression/golden/`.

```bash
# Run all regression tests
python -m tests.regression.runner

# Run a single test
python -m tests.regression.runner --test validate_2
```

Exit code is `0` if all pass, `1` if any fail or a golden file is missing.

See `tests/regression/README.md` for full details on seeding golden files.

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/validate` | Submit a civic address for LVF validation (`Content-Type: application/xml`) |
| `GET` | `/health` | GIS layer record counts |
| `GET` | `/coverage/geodetic` | GeoJSON of the unioned service boundary coverage polygon |
| `GET` | `/coverage/civic` | Civic coverage lookup table |
| `GET` | `/coverage/civic/explain` | Diagnose RCL segment coverage for a given admin hierarchy |

See `CLAUDE.md` for full request/response examples.

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
tests/                  Test XML inputs and regression infrastructure
  regression/
    golden/             Expected output files (committed)
    runner.py           Test runner
    seed.py             Golden file seeder
CLAUDE.md               Implementation context for Claude Code
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
