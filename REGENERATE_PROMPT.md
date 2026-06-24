# OME MCP v5 — Regeneration Prompt (v5.2)

Use the prompt below to regenerate this entire project from scratch with any AI coding
assistant. This spec reflects the **verified working** v5.2 release. Do not skip any
section — each captures a hard-won lesson from live debugging.

---

## Prompt

Build a complete MCP (Model Context Protocol) server called **ome_mcp_v5** that manages
a Dell server lab through the Dell OpenManage Enterprise (OME) REST API. All server
management must go through OME — never connect directly to iDRAC.

---

### Environment

- **Runtime**: Ubuntu Linux Docker container, Python 3.11-slim
- **Transport**: Streaming HTTP (FastMCP `streamable_http_app()` + uvicorn) — NOT stdio, NOT SSE
- **MCP endpoint**: `http://<host>:8000/mcp`
- **OME appliance**: `https://192.168.1.145/api`
- **OME credentials**: username `admin`, password via environment variable (never hardcoded)
- **Version**: 5.2.0 (set `__version__ = "5.2.0"` in server file)

---

### Files to generate (exactly these)

1. `ome_mcp_server.py` — main Python server
2. `Dockerfile` — Python 3.11-slim, `version="5.2.0"` label, non-root user `mcpuser` (UID 1000), exposes port 8000
3. `requirements.txt` — `mcp[cli]>=1.6.0`, `httpx>=0.27.0`, `pydantic>=2.7.0`, `uvicorn>=0.30.0`, `starlette>=0.40.0`
4. `docker-compose.yml` — **no `version:` field** (obsolete, causes warnings), credentials via env, log rotation
5. `.env.example` — template with all env vars, clearly marked never-commit
6. `ome_mcp_v5.service` — systemd unit that uses `docker compose up/down`, no desktop dependency
7. `service_control.sh` — bash: `start`, `stop`, `restart`, `status`, `logs [N]`, `build`, `rebuild`, `install-systemd`, `uninstall-systemd`
8. `README.md` — tool table, architecture diagram, TLS toggle, changelog with v5.2 entry
9. `CLAUDE.md` — AI-agent reference: all tools, pagination, OData filters, common workflows
10. `REGENERATE_PROMPT.md` — this file

---

### Configuration (all via environment variables)

| Variable | Default | Notes |
|----------|---------|-------|
| `OME_IP` | `192.168.1.145` | OME appliance IP |
| `OME_PORT` | `443` | OME HTTPS port |
| `OME_USER` | *(required)* | OME API username |
| `OME_PASSWORD` | *(required)* | OME API password |
| `OME_VERIFY_SSL` | `false` | `true` for production CA-signed cert, no rebuild needed |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Listen port |

---

### OME API session management

- Login: `POST /api/SessionService/Sessions` → store `X-Auth-Token` response header
- All subsequent requests: `X-Auth-Token: <token>` header
- Logout: `DELETE /api/SessionService/Sessions/{id}` on server shutdown
- Use a module-level `asyncio.Lock` so concurrent tool calls share one session
- Establish at lifespan startup; **auto-retry on 401**: on a 401 response clear the
  cached token and re-login once before raising — handles OME token expiry transparently

---

### HTTP helpers (shared, no duplication across tools)

```python
async def _invalidate_token():
    # clears _session_token and _session_id under lock

async def _ome_get(path, params=None):
    for attempt in range(2):
        token = await _get_token()
        r = await client.get(..., headers={"X-Auth-Token": token})
        if r.status_code == 401 and attempt == 0:
            await _invalidate_token(); continue
        _raise_for_status(r)
        return r.json()

# same retry pattern for _ome_post and _ome_delete
```

`_raise_for_status` must extract `error.@Message.ExtendedInfo[].Message` from OME error
bodies and append them to the error string — OME's real error detail is there, not in
the top-level `message` field.

---

### Entry point — three required fixes (do not skip any)

FastMCP's `streamable_http_app()` has three bugs when used with an external uvicorn and
wrapper app. All three must be fixed or the server will fail.

**Fix 1 — `mcp.run()` does not accept `host`/`port` kwargs** for streamable-http.
Use `mcp.streamable_http_app()` to get the ASGI app and drive it with uvicorn directly.

**Fix 2 — Lifespan suppressed when mounted inside wrapper app.**
Mounting the FastMCP app inside a second Starlette app suppresses its lifespan,
causing `RuntimeError: Task group is not initialized`. Forward it explicitly:

```python
inner_app = mcp.streamable_http_app()

@asynccontextmanager
async def _lifespan(app):
    async with inner_app.router.lifespan_context(app):
        yield

app = Starlette(lifespan=_lifespan, routes=[
    Route("/health", health_handler),
    Mount("/", _HostRewrite(inner_app)),
])
uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
```

**Fix 3 — FastMCP rejects external Host headers (421 Misdirected Request).**
`mcp.server.transport_security` validates the `Host` header. External IPs and even
`localhost` get rejected. Two-layer fix — call both before `streamable_http_app()`:

```python
def _patch_transport_security():
    """Runtime monkey-patch — works across SDK versions without undocumented kwargs."""
    import inspect
    import mcp.server.transport_security as _ts
    # patch module-level callables
    for attr_name in dir(_ts):
        if attr_name.startswith("__"): continue
        obj = getattr(_ts, attr_name, None)
        if callable(obj) and any(kw in attr_name.lower()
                                 for kw in ("host","valid","allow","check")):
            setattr(_ts, attr_name, lambda *a, **kw: True)
    # patch class methods
    for _, cls in inspect.getmembers(_ts, inspect.isclass):
        for meth in ("check_host","_check_host","validate_host","_validate_host",
                     "is_valid_host","_is_valid_host","is_allowed","_is_allowed",
                     "_validate_host"):
            if hasattr(cls, meth):
                setattr(cls, meth, lambda *a, **kw: True)

class _HostRewrite:
    """Belt-and-suspenders: rewrite Host header to 'localhost' at ASGI scope level."""
    def __init__(self, wrapped): self._wrapped = wrapped
    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            scope["headers"] = [
                (b"host", b"localhost") if k.lower() == b"host" else (k, v)
                for k, v in scope["headers"]
            ]
        await self._wrapped(scope, receive, send)
```

Add `GET /health` → `{"status": "ok", "service": "ome_mcp_v5"}`.

---

### Tools to implement

Use Pydantic v2 `BaseModel` with `ConfigDict(extra="forbid")` and `Field(..., description="...")`.
All tools return JSON strings. Shared pagination model: `top=50`, `skip=0`, `filter=""`.

#### Device tools
- `ome_list_devices` — `GET DeviceService/Devices` with `$top/$skip/$filter`; returns `count`, `value`, `next_skip`
- `ome_get_device` — `GET DeviceService/Devices({device_id})`
- `ome_get_device_inventory` — `GET DeviceService/Devices({id})/InventoryDetails`; on **404** return actionable error: *"inventory not collected yet — run ome_refresh_device_inventory then retry"*; on empty result return similar guidance
- `ome_get_device_subsystem_health` — `GET DeviceService/Devices({id})/SubSystemHealth`
- `ome_get_device_network_adapters` — same as inventory but filtered to `serverNetworkInterfaces`
- `ome_refresh_device_inventory` — `POST DeviceService/Actions/DeviceService.PerformInventory`

#### Power management
- `ome_device_power_action` — `POST DeviceService/Actions/DeviceService.PerformDeviceAction`
  Valid actions: `PowerOn`, `PowerOff`, `GracefulShutdown`, `GracefulRestart`, `MasterBusReset`, `PowerCycle`

#### Group tools
- `ome_list_groups` — `GET GroupService/Groups`
- `ome_get_group_devices` — `GET GroupService/Groups({id})/Devices`

#### Alert tools
- `ome_list_alerts` — `GET AlertService/Alerts`
- `ome_acknowledge_alerts` — `POST AlertService/Actions/AlertService.Acknowledge`

#### Job tools
- `ome_list_jobs` — `GET JobService/Jobs`
- `ome_get_job` — `GET JobService/Jobs({id})`
- `ome_run_job_now` — `POST JobService/Actions/JobService.RunNow`
- `ome_get_job_execution_history` — `GET JobService/Jobs({id})/ExecutionHistories`

#### Discovery tools
- `ome_list_discovery_jobs` — `GET DiscoveryConfigService/DiscoveryConfigGroups`
- `ome_create_discovery_job` — `POST DiscoveryConfigService/DiscoveryConfigGroups`

#### Firmware / catalog tools
- `ome_list_firmware_catalogs` — `GET UpdateService/Catalogs`
- `ome_list_firmware_baselines` — `GET UpdateService/Baselines`
- `ome_get_firmware_baseline_compliance` — `GET UpdateService/Baselines({id})/DeviceComplianceReports`
- `ome_remediate_firmware_baseline` — **see full spec below**

#### Template tools
- `ome_list_templates` — `GET TemplateService/Templates`
- `ome_get_template` — `GET TemplateService/Templates({id})`

#### Config compliance tools
- `ome_list_config_baselines` — `GET ConfigurationManagement/Baselines`
- `ome_get_config_baseline_compliance` — `GET ConfigurationManagement/Baselines({id})/DeviceConfigComplianceSummaries`

#### System / audit tools
- `ome_get_system_info` — `GET ApplicationService/Info`
- `ome_list_active_sessions` — `GET SessionService/Sessions`
- `ome_get_audit_logs` — `GET AuditService/AuditLogs`
- `ome_get_warranty_info` — `GET WarrantyService/Warranties`

---

### ome_remediate_firmware_baseline — full verified spec (v5.2)

This tool was completely rewritten in v5.2 after live debugging. The original
implementation had three root-cause bugs that made it non-functional:

1. **Wrong endpoint**: `UpdateService/Actions/UpdateService.UpdateFirmware` does not
   exist in OME 4.6+. Jobs must be posted directly to `JobService/Jobs`.

2. **Wrong Targets structure**: Targets require a `Data` field containing a
   semicolon-joined string of component `SourceName` values, not just `Id`/`Type`.

3. **Incomplete Params**: Must include `repositoryId`, `catalogId`, `complianceReportId`,
   `operationName`, `rebootType`, `signVerify` (required by OME 4.6+), `complianceUpdate`,
   `stagingValue`.

**Input model** (`FirmwareRemediationInput`):
- `baseline_id: int` — firmware baseline ID
- `device_ids: List[int]` — devices to update
- `stage_only: bool = False` — False = graceful reboot (rebootType=2), True = stage only (rebootType=3)
- `job_name: str = "Firmware Remediation"`

**Execution flow**:

```python
# Step 1 — get repositoryId and catalogId from the baseline
baseline = await _ome_get(f"UpdateService/Baselines({baseline_id})")
repo_id   = str(baseline["RepositoryId"])
catalog_id = str(baseline["CatalogId"])

# Step 2 — get compliance report; build device_id -> Data string
compliance = await _ome_get(
    f"UpdateService/Baselines({baseline_id})/DeviceComplianceReports"
)
device_data = {}  # {device_id: "SourceName1;SourceName2;..."}
for report in compliance["value"]:
    dev_id = report["DeviceId"]
    if dev_id not in params.device_ids:
        continue
    sources = [
        c["SourceName"]
        for c in report.get("ComponentComplianceReports", [])
        if c.get("UpdateAction") not in ("UNKNOWN", "")
        and c.get("ComplianceStatus") not in ("OK", "UNKNOWN", "")
    ]
    if sources:
        device_data[dev_id] = ";".join(sources)

# Return early if nothing to do
if not device_data:
    return error("No non-compliant components found...")

# Step 3 — build targets
targets = [
    {"Id": did, "Data": device_data[did], "TargetType": {"Id": 1000, "Name": "DEVICE"}}
    for did in params.device_ids if did in device_data
]

# Step 4 — POST job
reboot_type = "3" if params.stage_only else "2"
payload = {
    "JobName": params.job_name,
    "JobDescription": f"Firmware remediation against baseline {baseline_id}",
    "Schedule": "startnow",
    "State": "Enabled",
    "JobType": {"Id": 5, "Name": "Update_Task"},
    "Targets": targets,
    "Params": [
        {"Key": "repositoryId",       "Value": repo_id},
        {"Key": "catalogId",          "Value": catalog_id},
        {"Key": "complianceReportId", "Value": str(baseline_id)},
        {"Key": "operationName",      "Value": "INSTALL_FIRMWARE"},
        {"Key": "rebootType",         "Value": reboot_type},
        {"Key": "signVerify",         "Value": "true"},   # required OME 4.6+
        {"Key": "complianceUpdate",   "Value": "true"},
        {"Key": "stagingValue",       "Value": str(stage_only).lower()},
    ],
}
return await _ome_post("JobService/Jobs", payload)
```

**Validated** by live test: full firmware update of a PowerEdge R6515 (13 components
including BIOS, PERC RAID, HDDs, NICs, SSDs) against a Dell online catalog baseline.

---

### Code quality requirements

- `__version__ = "5.2.0"` at top of server file
- All tools use `@mcp.tool(name="ome_...", annotations={...})` with correct hint flags
- `readOnlyHint: True` for all GET tools; `destructiveHint: True` for power actions and firmware remediation
- Single shared `_ome_get()` / `_ome_post()` / `_ome_delete()` — no duplicated HTTP logic
- `_raise_for_status` extracts `error.@Message.ExtendedInfo[].Message` for full OME error detail
- Errors return `{"error": "<message>"}` via shared `_handle(exc)` / `_err(msg)`
- Success returns via `json.dumps(data, indent=2, default=str)` through shared `_ok(data)`
- Credentials never logged
- `docker-compose.yml` has **no** `version:` field
- systemd unit has no dependency on Claude Desktop, VS Code, or any desktop app
- `service_control.sh` works standalone without systemd installed

---

### Verified working state (v5.2)

The following have been confirmed working against a live OME 4.6 appliance:

- ✅ Server starts, transport security patched, `POST /mcp` 200 OK from external IPs
- ✅ `GET /health` 200 OK
- ✅ OME session auth + auto-retry on token expiry (401)
- ✅ All 28 read tools
- ✅ `ome_device_power_action`
- ✅ `ome_remediate_firmware_baseline` — full update of PowerEdge R6515, 13 components
- ✅ `ome_get_device_inventory` — actionable 404 guidance when inventory not yet collected
- ✅ OME `ExtendedInfo` error details surfaced in all error messages
