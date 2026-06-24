## Changelog

### v6.0.0 — 2026-06-23

**Major restructuring: monolithic server → package layout, v5 naming dropped**

- **Source restructured** — `ome_mcp_v5_server.py` deleted; server code moved into `src/ome_mcp_server.py` under a proper package layout.
- **Logging utility added** — New `src/utils/logging_config.py` routes all logs to stderr (required for MCP stdout protocol compatibility).
- **Docker Compose renamed** — `docker-compose.yml` → `compose.yml` (modern standard); service renamed from `ome_mcp_v5` to `ome_mcp`; env vars updated to `OME_MCP_HOST`/`OME_MCP_PORT`; removed legacy logging driver config.
- **Dockerfile updated** — Now copies the entire `src/` directory instead of a single file; entrypoint changed to `ome_mcp_server.py`.
- **Dependencies slimmed** — `requirements.txt` now only lists `fastmcp`, `httpx`, and `pydantic`. `uvicorn` and `starlette` are pulled transitively by `fastmcp`; `mcp[cli]` replaced with the lighter `fastmcp`.
- **Env var renamed** — `MCP_PORT` → `OME_MCP_PORT` in `.env.example` for consistency.
- **README overhaul** — Removed "v5" branding; Docker Compose usage promoted to primary install method; TLS/SSL section moved down; duplicate changelog extracted into standalone `CHANGELOG.md`; all file references updated.
- **Regeneration prompt updated** — `REGENERATE_PROMPT.md` references `ome_mcp_server.py` instead of the old v5 filename.

### v5.2.0 — 2026-04-09

**Fixed: `ome_remediate_firmware_baseline` completely rewritten**

The tool was non-functional in v5.0/v5.1 due to three root-cause bugs:

1. **Wrong endpoint** — Code used `UpdateService/Actions/UpdateService.UpdateFirmware`, which does not exist in OME 4.6+ (`UpdateService.Actions` is null). Fixed: now POSTs directly to `JobService/Jobs`.

2. **Wrong Targets structure** — Old payload used `{"Id": id, "Type": {...}}`, missing the required `Data` field. Fixed: `{"Id": id, "Data": "<component_sources>", "TargetType": {"Id": 1000, "Name": "DEVICE"}}` where `Data` is a semicolon-joined string of component `SourceName` values for non-compliant components.

3. **Incomplete Params** — Old params list was missing required keys. Fixed: full validated param set is `repositoryId`, `catalogId`, `complianceReportId`, `operationName`, `rebootType`, `signVerify`, `complianceUpdate`, `stagingValue`.

**New execution flow:**
1. `GET UpdateService/Baselines({id})` → extract `RepositoryId`, `CatalogId`
2. `GET UpdateService/Baselines({id})/DeviceComplianceReports` → per-device, collect `SourceName` for components where `UpdateAction != UNKNOWN` and `ComplianceStatus` not in `{OK, UNKNOWN}`
3. `POST JobService/Jobs` with `operationName=INSTALL_FIRMWARE`, `rebootType=2` (graceful) or `3` (stage-only)

Returns an early error if no non-compliant components are found, preventing empty job creation.

**Validated** by live test: full firmware update of a PowerEdge R6515 (13 components including BIOS, PERC RAID, HDDs, NICs, SSDs) against Dell online catalog baseline.

---

### v5.0.0 — initial release

- 28 read/write OME tools via Streaming HTTP MCP transport
- Docker container with non-root user, systemd unit, health check
- OData filtering and pagination on all list tools
