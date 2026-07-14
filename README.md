# LVF — Location Validation Function

This repository contains an open reference implementation of the NG9-1-1 Location Validation Function (LVF) 
as specified in `references/LVF_Algorithm_Specification_v79.docx`. Validates civic PIDF-LO addresses against provisioned
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

> **Linux / macOS volume permissions:** The container process runs as `lvfuser` (UID 1000). If
> Docker cannot read your host `data/` directory as that user, the service will fail to load GIS
> data. Fix it once before the first `docker compose up`:
> ```bash
> sudo chown -R 1000:1000 ./data
> ```
> Windows (Docker Desktop with WSL 2) and macOS (Docker Desktop) handle volume ownership
> transparently — no `chown` is needed on those platforms.

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

# 5. Start the server (dev — single process)
python main.py
# ...or for auto-reload during development:
uvicorn src.server:app --reload --host 0.0.0.0 --port 8000
```

**Prerequisites:** Python 3.10 or later.

---

## Working with i3-fe-core

LVF's cross-cutting i3 obligations — NTP/timestamps, ElementState/ServiceState, LogEvent
logging, SIP SUBSCRIBE/NOTIFY, and Discrepancy Reporting — live in a separate shared library,
**[`i3-fe-core`](https://github.com/horningjason/i3-fe-core)**, not in this repo. LVF consumes
it as a pinned dependency and wires it in via `src/core_components.py`; it keeps its own FastAPI
app and all LoST/GIS domain logic. See `CLAUDE.md` → *Architecture — Shared i3 Core* for how the
wiring works.

**Standard install (what `pip install -r requirements.txt` does).** `requirements.txt` pins core
directly off GitHub:

```
i3-fe-core @ git+https://github.com/horningjason/i3-fe-core.git@v0.3.0
```

No separate clone or setup step is needed — `pip install -r requirements.txt` resolves it like
any other dependency. This does require network access to GitHub at install time. `horningjason/i3-fe-core`
is public today, so no credentials are needed; if it's ever made private, installing will need
auth for that host (e.g. a PAT embedded in the URL, or an SSH deploy key with the requirement
rewritten to `git+ssh://`).

**Developing against an unreleased core change.** If a change spans both repos (e.g. LVF needs a
new core notifier before it's tagged), point `pip` at a local checkout instead of the pinned tag:

```powershell
# Clone i3-fe-core as a sibling directory of lvf-service, then:
pip uninstall i3-fe-core
pip install -e ../i3-fe-core
```

`-e` installs core in editable mode, so edits in the sibling checkout take effect on the next
process restart — no reinstall needed. **Do not commit `requirements.txt` pointed at a local
path** — revert it to the `git+https://...@vX.Y.Z` line before committing.

**Upgrading the pinned tag.**

1. Tag the desired commit in `i3-fe-core` (or use an existing tag).
2. Update the `@vX.Y.Z` suffix in `requirements.txt`.
3. `pip install -r requirements.txt` (or rebuild the Docker image).
4. Run `python -m tests.regression.runner` — it exercises every core-backed code path (logging,
   state notifications, SIP, discrepancy reporting) indirectly through `handle_find_service()`.
5. Smoke-test what the regression suite doesn't cover directly: `tests/smoke/sip_smoke.py` (SIP
   SUBSCRIBE/NOTIFY) and `tests/smoke/dr_smoke.py` (`/dr` Discrepancy Reporting) against a
   running instance.

---

## Running: Single Worker vs. Multiple Workers

The LVF supports running multiple worker processes on a single machine. Every node type
**except the Forest Guide** can run multi-worker. With `LVF_WORKERS` unset or `=1`, the
service behaves **exactly** like the previous single-process deployment — same validation,
routing, recursion, and coverage results.

> **Platform requirements**
> - **Single-worker** (uvicorn / `python main.py`) runs on **Linux, macOS, and Windows** —
>   this is the cross-platform path.
> - **Multi-worker** (gunicorn / `LVF_WORKERS=N`) requires **Linux or Docker**. gunicorn is
>   POSIX-only (it depends on `os.fork` and `fcntl`) and does **not** run natively on Windows
>   — there it fails with "command not found" if absent, or a runtime error if installed.
>   On Windows, use the single-worker dev command below; run multi-worker inside the
>   **Docker stack** instead.
> - The application still runs single-process on Windows: the `fcntl`-based cross-process
>   coverage lock and leader election degrade to no-ops there (the lone process is always the
>   leader), so Windows is fully supported for single-process development.

**Single worker (unchanged behavior) — Linux, macOS, Windows:**

```bash
# Development (cross-platform):
python main.py

# Production, explicit single worker (Linux / Docker):
LVF_WORKERS=1 gunicorn -c gunicorn.conf.py src.server:app
```

**Multiple workers (per-machine) — Linux / Docker only:**

```bash
# Build the GIS cache once before forking, then run N workers.
# Work is CPU-bound — a good starting point is ~the number of CPU cores, then measure.
python prewarm.py
LVF_WORKERS=4 gunicorn -c gunicorn.conf.py src.server:app
```

> The Forest Guide does no GIS validation, so multi-worker gives it no CPU-parallelism
> benefit, but it is supported — coverage-store locking and leader election (below) apply
> uniformly regardless of node role.
>
> In production, multi-worker runs inside the Docker stack (Linux containers) — that is the
> supported environment for gunicorn / `LVF_WORKERS`.

**Pre-warm step.** `python prewarm.py` builds the GIS JSON cache once up front so the
workers all start against a warm cache instead of cold-building the GeoPackage
concurrently. It is a no-op in Forest Guide / routing-only mode. The Docker image runs it
automatically before launching gunicorn.

**Liveness vs. readiness.** `GET /health` is liveness (always 200 while the process is up).
`GET /ready` is readiness: it returns **503** while a GIS reload is in progress or before
GIS data is loaded, and **200** once records are present (or immediately, for routing-only
and Forest Guide nodes, which legitimately have no GIS records). **Load balancers should
check `/ready`.**

**Multi-worker safety.** Coverage-store writes (inbound LoST-Sync pushes/pulls) are
serialized with a cross-process file lock and the in-memory list is swapped atomically;
workers converge on each other's writes via a coverage-file watcher
(`LVF_COVERAGE_POLL_INTERVAL_SECONDS`, default 15s). Exactly one "leader" worker runs the
singleton tasks — the SIP notifier and the boot-time startup sync — while the others skip
them. On root-AMS nodes, operator-authored AMS provisioning is always re-asserted on top of
the dynamic coverage file, so manual provisioning always wins.

**Scope.** Multi-worker is **per-machine** only. To survive a machine failure, run multiple
stateless query/validation nodes behind a load balancer (active-active) and use an
active+standby setup for coordination nodes (parents/Forest Guides). Running a single
coordination node active-active across multiple machines is intentionally **not** supported
(it would require a shared store) and is out of scope.

---

## GIS Data

The repository includes `data/child_lvf_data.gpkg` — a sample GeoPackage provisioned for
Burleigh County, McLean County, Mercer County, Morton County, and Oliver County, ND. This is
sufficient to run the service and evaluate LVF behavior out of the box. The provided GeoPackage
follows NENA's GeoPackage v3.0 template verbatim.

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
| `LVF_AGENCY_ID` | No | — | DNS-style agency identifier (e.g. `nd911.nd.gov`). Populates `agencyId` in i3 LogEvents (NENA-STA-010.3f-2021 §4.12.3.1). A WARNING is logged at startup if unset |
| `LVF_DISPLAY_NAME_LANG` | No | `en` | `xml:lang` on `<displayName>` elements |
| `LVF_SERVICE_DOMAIN` | No | `LVF_SERVER_URI` value | `service`/`domain`/`service_id` reported by the i3 `ServiceState` notifier (§2.4.2). Defaults to `LVF_SERVER_URI` when unset |
| `LVF_VERSION_MAJOR` / `LVF_VERSION_MINOR` | No | `1` / `0` | Version reported by the i3 §4.12 `Versions` entry points (`/lost/Versions`, `/sync/Versions`, `/dr/Versions`). Bump major (reset minor to `0`) for breaking web-service changes, minor for backwards-compatible ones |
| `LVF_BUILD_FINGERPRINT` | No | `lvf-service-dev` | Build/vendor identifier reported as `fingerprint` in `Versions` responses |
| `LVF_LOG_LEVEL` | No | `INFO` | Log level for all LVF loggers (`src.*`). Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Does not affect uvicorn's own access log. `DEBUG` surfaces every gate decision and sync push/pull detail; `INFO` covers startup progress and GIS load counts; `WARNING` limits output to anomalies and recoverable failures only |
| **Process Management** | | | |
| `LVF_WORKERS` | No | `1` | Number of gunicorn worker processes on this machine (read by `gunicorn.conf.py`). `1` == single-process behavior. A leaf/child node may use ~CPU-core count; a Forest Guide gets no CPU-parallelism benefit from more than 1 (no GIS validation) but multi-worker is supported there too. Ignored by `python main.py` (always single-process). Multi-worker requires **Linux/Docker** — gunicorn is POSIX-only and does not run on Windows |
| `LVF_WORKER_TIMEOUT` | No | `120` | Gunicorn worker timeout in seconds (read by `gunicorn.conf.py`) |
| `LVF_COVERAGE_POLL_INTERVAL_SECONDS` | No | `15` | How often (seconds) each worker polls the child-coverage file so siblings converge on each other's LoST-Sync writes. Silent read-only refresh — never triggers a push or startup sync. Set to `0` to disable |
| `LVF_MAX_CONCURRENT_REQUESTS` | No | `0` | Load shedding (§3.11.5): per-worker in-flight cap on `POST /lost` only. `0` = unlimited. Shed requests return HTTP `429`, not a LoST element. Per-worker, not cross-worker accurate — scales with `LVF_WORKERS` |
| `LVF_RATE_LIMIT_PER_SOURCE` | No | `0` | Load shedding: per-worker, per-source-IP request cap on `POST /lost` within `LVF_RATE_LIMIT_WINDOW_SECONDS`. `0` = unlimited |
| `LVF_RATE_LIMIT_WINDOW_SECONDS` | No | `60` | Sliding window (seconds) for `LVF_RATE_LIMIT_PER_SOURCE` |
| `PROMETHEUS_MULTIPROC_DIR` | No | `/tmp/lvf_prometheus_multiproc` | Directory backing `prometheus_client`'s multiprocess mode, required for `GET /metrics` to report correct totals when `LVF_WORKERS > 1`. Wiped and recreated fresh on every startup automatically — no operator action needed |
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
| `LVF_FOREST_GUIDE_URI` | No | — | U-NAPTR application unique string (DNS name) identifying the Forest Guide — e.g. `lvf-fg.example.com`. Resolved via U-NAPTR on first use; `/sync` is appended internally. Must not include a path or scheme. Only used when `LVF_ROOT_AMS=true` |
| **Forest Guide Mode** | | | |
| `LVF_FOREST_GUIDE_MODE` | No | `false` | When `true`, this node operates as a Forest Guide: GIS validation is skipped, all requests are redirected to the matching child LVF, and `LVF_PARENT_URI` is ignored |
| **NTP** | | | |
| `LVF_NTP_SERVER` | No | `pool.ntp.org` | Hostname of the NTP server. Core's `NtpClient` always starts at startup (logs `is_healthy`/`offset`); time-sensitive fields use NTP-derived time and query failures fall back to the system clock |
| **i3 Logging** | | | |
| `LVF_LOGGING_SERVICE_URI` | No | — | URI of an i3 Logging Service to POST LogEvents to. When unset, events are emitted to Python standard logging only |
| **Discrepancy Reporting** | | | |
| `LVF_ENABLE_DR_SERVICE` | No | `true` | Mounts core's §3.7 Discrepancy Reporting responding web service at `/dr`. Set to non-`true` to disable |
| `LVF_DR_ENDPOINT` | No | — | HTTP endpoint to POST Discrepancy Reports to (responding agency's `/Reports` service). When unset, DRs are logged locally only (NENA-STA-010.3f-2021 §3.7.1) |
| `LVF_DR_RESOLUTION_URI` | No | — | URI this LVF exposes for receiving DR resolution callbacks. Used as `resolutionUri` in the DR body. Required for conformant submission |
| `LVF_DR_CONTACT_NAME` | No | `LVF Administrator` | Contact name in the DR jCard (`reportingContactJcard`). A WARNING is logged at startup if unset |
| `LVF_DR_CONTACT_EMAIL` | No | — | Contact email in the DR jCard. A WARNING is logged at startup if unset |
| **SIP State Notifications** | | | |
| `LVF_ENABLE_SIP` | No | `true` | Master on/off switch for the SIP notifier. When `false`, the notifier never starts. In multi-worker mode it is a singleton that runs only in the elected leader worker |
| `LVF_SIP_HOST` | No | `0.0.0.0` | IP address or hostname to bind the SIP listener |
| `LVF_SIP_PORT` | No | `5060` | SIP port for SUBSCRIBE/NOTIFY. Set to `0` to disable the SIP listener entirely |
| `LVF_SIP_ALLOWED_SUBSCRIBERS` | No | — | Comma-separated SIP URIs permitted to subscribe (e.g. `sip:esrp.example.com`). When unset, all SUBSCRIBE requests are accepted (appropriate for ESInet trust model where network-level access control is assumed) |
| **TLS** | | | |
| `LVF_TLS_MODE` | No | `disabled` | Transport mode. `disabled` = HTTP only (default). `tls` = HTTPS with server certificate. `mtls` = HTTPS where the server requests a client certificate (`CERT_OPTIONAL`) and presents its own client certificate on outbound calls to peer nodes. **Known limitation:** client certificate presence is not currently enforced on inbound connections (gunicorn/uvicorn use `CERT_OPTIONAL`; app-level enforcement was removed) — do not rely on `mtls` alone for inbound access control. |
| `LVF_TLS_CERT_FILE` | No | — | Path to the server certificate PEM file. Required when `LVF_TLS_MODE` is `tls` or `mtls`. |
| `LVF_TLS_KEY_FILE` | No | — | Path to the server private key PEM file. Required when `LVF_TLS_MODE` is `tls` or `mtls`. |
| `LVF_TLS_CA_FILE` | No | — | Path to the CA certificate bundle PEM file. Used to verify inbound client certificates (server-side mTLS) and outbound peer server certificates on calls to other LVF nodes (sync push/pull, recursion). Required when `LVF_TLS_MODE` is `mtls`. |
| `LVF_TLS_CLIENT_CERT_FILE` | No | — | Path to the client certificate PEM file used for mTLS outbound calls. When set, this node presents this certificate when making outbound HTTPS requests to peer nodes (child→parent sync push, parent→FG push, recursion calls). Required when `LVF_TLS_MODE` is `mtls`. Not used for `tls` or `disabled`. |
| `LVF_TLS_CLIENT_KEY_FILE` | No | — | Path to the client private key PEM file used for mTLS outbound calls. Must correspond to `LVF_TLS_CLIENT_CERT_FILE`. Required when `LVF_TLS_MODE` is `mtls`. Not used for `tls` or `disabled`. |

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

**`ams_civic_coverage.json`** — JSON array of coverage mapping entries. Each entry declares one set of civic addresses this node is authoritative for:

```json
[
  {
    "source": "root-ams.lvf.example.com",
    "source_id": "{11111111-1111-1111-1111-111111111111}",
    "last_updated": "2026-01-01T00:00:00Z",
    "expires": "NO-EXPIRATION",
    "service": "urn:service:sos",
    "profile": "civic",
    "lost_server": "http://root-ams.lvf.example.com/lost",
    "civic_addresses": [
      { "country": "US", "a1": "ND", "a2": "Adams County" },
      { "country": "US", "a1": "ND", "a2": "Barnes County" },
      { "country": "US", "a1": "ND", "a2": "Benson County", "a3": "Leeds" }
    ]
  }
]
```

`source_id` must match `LVF_SYNC_SOURCE_ID_CIVIC`. `lost_server` is this node's own `/lost` URL (the Forest Guide will redirect queries here). Each address object requires `country`, `a1`, and `a2`; include `a3`, `a4`, `a5` only when they carry a real non-wildcard value (absent fields match any value).

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
    "lost_server": "http://root-ams.lvf.example.com/lost",
    "geodetic_geom_wkt": "POLYGON ((-102.5 46.4, -100.0 46.4, -100.0 48.6, -102.5 48.6, -102.5 46.4))"
  }
]
```

`source_id` must match `LVF_SYNC_SOURCE_ID_GEODETIC`. Coordinates are `(longitude latitude)`. The WKT polygon is converted to GML when pushed to the Forest Guide — it is never stored as-is on the wire.


---

## SIP State Notifications (ElementState / ServiceState)

When `LVF_SIP_HOST`/`LVF_SIP_PORT` are configured (and `LVF_SIP_PORT` is non-zero), the LVF
exposes a SIP endpoint that accepts SUBSCRIBE requests for the `emergency-ElementState` and
`emergency-ServiceState` event packages per NENA-STA-010.3f-2021 §2.4.1 and §2.4.2. The LVF
sends SIP NOTIFY to all active subscribers whenever its element or service state changes.

This is the i3-required notifier-side interface that allows ESInet elements (ESRPs, monitoring
systems) to subscribe to LVF health state. The SIP endpoint listens on both UDP and TCP.

In production, the SIP interface should be on the ESInet SIP network, separate from the HTTPS
LoST interface.

`ServiceState` also reflects sustained load shedding on `POST /lost` (§3.11.6): continuous
shedding (`LVF_RATE_LIMIT_PER_SOURCE` and/or `LVF_MAX_CONCURRENT_REQUESTS`, see
[Environment Variables](#environment-variables)) for 15s trips state to `Partial`; it returns
to `Normal` only after shedding has been fully quiet for 30s. These windows are fixed, not
configurable. `GET /health` reflects the current value.

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
| `POST` | `/lost` | LoST protocol endpoint (RFC 5222) — `findService`, `listServices`, `listServicesByLocation`, `getServiceBoundary` (`Content-Type: application/lost+xml`). `findService` requires `validateLocation="true"`. Subject to load shedding (§3.11.5, `LVF_MAX_CONCURRENT_REQUESTS` / `LVF_RATE_LIMIT_PER_SOURCE`) — shed requests return HTTP `429`, disabled by default |
| `POST` | `/sync` | LoST-Sync (RFC 6739) — accepts `pushMappings` and `getMappingsRequest` (`Content-Type: application/lostsync+xml`) |
| `GET` | `/health` | Liveness — GIS layer record counts, element state, and service state (always `200` while the process is up) |
| `GET` | `/ready` | Readiness — `503` while a GIS reload is in progress or before GIS data is loaded; `200` once records are present (or immediately for routing-only / Forest Guide nodes). Load balancers should check this |
| `GET` | `/coverage/geodetic` | GeoJSON of the unioned service boundary coverage polygon |
| `GET` | `/coverage/civic` | Civic coverage lookup table |
| `GET` | `/coverage/civic/explain` | Diagnose RCL segment coverage for a given admin hierarchy |
| `GET` | `/metrics` | Prometheus exposition-format metrics (operations tooling only — no spec/protocol impact). Correct under multi-worker via prometheus_client multiprocess mode; see [Metrics](#metrics) below |
| `GET` | `/lost/Versions`, `/sync/Versions`, `/dr/Versions` | i3 §4.12 `Versions` entry point, one per web service (`/dr/Versions` only when `LVF_ENABLE_DR_SERVICE=true`). Reports `LVF_VERSION_MAJOR.LVF_VERSION_MINOR` and `LVF_BUILD_FINGERPRINT` |

---

## Metrics

`GET /metrics` exposes Prometheus-format counters and histograms: `lvf_http_requests_total` /
`lvf_http_request_duration_seconds` (every HTTP request, by endpoint and status),
`lvf_lost_errors_total` (LoST `<errors>` responses from `/lost`, by error element name),
`lvf_recursion_total` / `lvf_recursion_duration_seconds` (outbound recursive calls, by
outcome), `lvf_reload_events_total` (GIS reloads, by trigger and outcome), and
`lvf_load_shed_total` (§3.11.5 shed requests, by reason).

**Multi-worker correctness is automatic.** Plain `prometheus_client` does not aggregate
across gunicorn workers by default — each worker would keep separate in-memory counters, so
a scrape would only reflect whichever worker happened to handle it. This service uses
`prometheus_client`'s documented [multiprocess mode](https://prometheus.github.io/client_python/multiprocess/)
instead: metrics are backed by files under `PROMETHEUS_MULTIPROC_DIR`, and `/metrics`
aggregates across every worker's files at scrape time. The directory is wiped and recreated
fresh on every startup (`prewarm.py`, `main.py`, and gunicorn's `on_starting` hook all do
this) — no operator action needed beyond the default in `.env.example`.

---

## Project Structure

```
src/                        Application source
  server.py                 FastAPI app — lifespan, HTTP endpoints, core wiring
  core_components.py        Builds the CoreComponents container from i3-fe-core (NTP, state,
                            logging, SIP, discrepancy), configured from env
  runtime_state.py          Process-wide state; mirrors the shared core notifiers + discrepancy
                            component for deep call sites
  utils.py                  Shared utilities
  app/
    lifecycle.py            Startup/shutdown orchestration (GIS load, sync, background tasks)
    role.py                 Node-role resolution (leaf / routing-only / Forest Guide / Root AMS)
  lost/                     LoST protocol handlers (RFC 5222)
    find_service.py         Core LVF logic: gate orchestration, handle_find_service()
    list_services.py        listServices — provisioned URNs, optional child-filter
    list_services_by_location.py  listServicesByLocation — geodetic-2d and civic
    get_service_boundary.py getServiceBoundary stub (notFound)
    load_shed.py            Load shedding (§3.11.5) and ServiceState debounce (§3.11.6)
    wire/                   LoST/GML XML serialization + response assembly
  validation/               Three-gate algorithm
    gate0.py / gate1.py / gate2.py   Gates 0–2 (URN/boundary, structural, progressive filter)
    response_assembly.py    <mapping> selection and response XML construction
    models.py               SSAPRecord, RCLRecord, FilterState, etc.
  federation/               Multi-node coordination
    sync.py                 LoST-Sync push/pull (RFC 6739)
    recursion.py            recursive="true" forwarding
    coverage.py             Child coverage store + routing lookup
  gis/
    provisioning.py         GeoPackage loading + coverage lookups
    records.py              SSAPRecord/RCLRecord row/dict conversion, civicAddress helpers
  discrepancy/
    discrepancy_report.py   LVF LoST/GIS problem enums; builds core DiscrepancyReports (§3.7)
  notify/
    sip_notifier.py         SipWireAdapter — SIP UDP/TCP wire over core's SipNotifier (§2.4)
  observability/
    metrics.py              Prometheus counters/histograms, multiprocess /metrics ASGI app
  logging/
    log_events.py           i3 LogEvent types (LostQueryLogEvent, LostResponseLogEvent)
    logger.py                emit_log_event() helper (§4.12)
schemas/                    XSD files for XML schema validation
main.py                     Single-process launcher (dev) — `python main.py`
prewarm.py                  Builds the GIS cache once before workers fork
gunicorn.conf.py            Gunicorn config — workers, timeout, TLS (multi-worker launch)
data/                       GeoPackage data files and runtime state
  child_lvf_data.gpkg         Sample data — Burleigh, McLean, Mercer, Morton, Oliver counties
  lvf_child_coverage.json     Child coverage store (written at runtime; do not edit manually)
  ams_civic_coverage.json     Root AMS civic coverage declaration (operator-provisioned)
  ams_geodetic_coverage.json  Root AMS geodetic boundary declaration (operator-provisioned)
tests/                      Test XML inputs and regression infrastructure
  smoke/                      Dev smoke tests against a live instance (dr_smoke.py — DR /dr, sip_smoke.py — SIP SUBSCRIBE)
  regression/golden/          Expected output files (committed)
  regression/runner.py        Test runner
  regression/seed.py          Golden file seeder
```

> **Shared core.** Cross-cutting i3 concerns are not in this repo — they come from the pinned
> **`i3-fe-core`** library. See [Working with i3-fe-core](#working-with-i3-fe-core) above for
> install/upgrade instructions, and `CLAUDE.md` → *Architecture — Shared i3 Core* for the full
> wiring picture.

---

## Governing Standards

- NENA-STA-004.2-2024 — CLDXF-US element definitions
- NENA-STA-006.3-2026 — GIS layer definitions and field names
- NENA-INF-027.1-2018 — LVF evaluation logic and hierarchy
- NENA-STA-010.3f-2021 — i3 Standard, LVF LoST requirements
- RFC 5222 — LoST protocol
- RFC 5139 — PIDF-LO civic address schema
- RFC 6848 — PIDF-LO civic address extensions

## Other Documents
- RFC 5582 - LoST mapping architecture (informational)
- RFC 6739 - LoST sync (experimental)
- draft-ietf-ecrit-similar-location-19
- draft-ietf-ecrit-lost-planned-changes-17