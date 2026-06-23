# CLAUDE.md — OME MCP v5 Implementation Guide

This file is read by AI agents to understand how to use and extend this MCP server.

---

## Server Identity

- **MCP Server Name**: `ome_mcp_v5`
- **Transport**: Streaming HTTP (stateless JSON, not SSE)
- **Endpoint**: `http://<host>:8000/mcp`
- **Backend**: Dell OpenManage Enterprise REST API at `https://192.168.1.145/api`
- **Auth**: OME session token — obtained automatically at startup, never exposed to tools

---

## All Available Tools

### Read-Only (Safe to call freely)

| Tool | Key Parameters | Returns |
|------|----------------|---------|
| `ome_list_devices` | top, skip, filter | Paginated device list |
| `ome_get_device` | device_id | Full device record |
| `ome_get_device_inventory` | device_id | Hardware inventory list |
| `ome_get_device_subsystem_health` | device_id | Subsystem health objects |
| `ome_get_device_network_adapters` | device_id | NIC/port inventory |
| `ome_list_groups` | top, skip, filter | Group list |
| `ome_get_group_devices` | group_id | Devices in group |
| `ome_list_alerts` | top, skip, filter | Alert records |
| `ome_list_jobs` | top, skip, filter | Job list |
| `ome_get_job` | job_id | Job detail |
| `ome_get_job_execution_history` | job_id | Run history |
| `ome_list_discovery_jobs` | top, skip | Discovery configs |
| `ome_list_firmware_catalogs` | top, skip | Catalog list |
| `ome_list_firmware_baselines` | top, skip | Baseline list |
| `ome_get_firmware_baseline_compliance` | baseline_id | Component compliance |
| `ome_list_templates` | top, skip, filter | Template list |
| `ome_get_template` | template_id | Template detail |
| `ome_list_config_baselines` | top, skip | Config baseline list |
| `ome_get_config_baseline_compliance` | baseline_id | Device compliance |
| `ome_get_system_info` | (none) | OME appliance info |
| `ome_list_active_sessions` | top, skip | Active sessions |
| `ome_get_audit_logs` | top, skip, filter | Audit entries |
| `ome_get_warranty_info` | top, skip, filter | Warranty records |

### Write / Action Tools (Use with caution)

| Tool | Key Parameters | Effect |
|------|----------------|--------|
| `ome_device_power_action` | device_ids, action | Power on/off/restart servers |
| `ome_acknowledge_alerts` | alert_ids | Mark alerts acknowledged |
| `ome_run_job_now` | job_id | Run an OME job immediately |
| `ome_create_discovery_job` | name, ip_range, protocol | Discover new devices |
| `ome_refresh_device_inventory` | device_id | Refresh hardware inventory |
| `ome_remediate_firmware_baseline` | baseline_id, device_ids, stage_only, job_name | Push firmware updates to meet baseline |

**Power action values**: `PowerOn`, `PowerOff`, `GracefulShutdown`, `GracefulRestart`, `MasterBusReset`, `PowerCycle`

---

## Pagination Pattern

All list tools return `count`, `value`, and `next_skip`. Use these to page through large datasets:

```json
// Call 1 — first 50 devices
{"top": 50, "skip": 0}

// Call 2 — next 50
{"top": 50, "skip": 50}
```

Stop when `len(value) < top` or `skip >= count`.

---

## OData Filter Examples

```
# Filter by model
"Model eq 'PowerEdge R640'"

# Critical alerts only
"Severity eq 'Critical'"

# Failed jobs only
"LastRunStatus/Name eq 'Failed'"

# New alerts
"StatusType eq 'New'"

# Filter by service tag
"DeviceServiceTag eq 'ABC1234'"
```

---

## Common Workflows

### Inventory all servers

```
1. ome_list_devices(top=100, skip=0)
2. Repeat with increasing skip until results are exhausted
3. For each device, call ome_get_device_inventory(device_id=<id>)
```

### Check critical alerts

```
1. ome_list_alerts(filter="Severity eq 'Critical'", top=50)
2. Review alert records
3. ome_acknowledge_alerts(alert_ids=[...]) when resolved
```

### Firmware compliance check

```
1. ome_list_firmware_baselines(top=20)
2. ome_get_firmware_baseline_compliance(baseline_id=<id>)
3. Look for ComplianceStatus != "OK" in the results
```

### Firmware remediation (update non-compliant devices)

```
1. ome_list_firmware_baselines(top=20)            — find baseline_id
2. ome_get_firmware_baseline_compliance(baseline_id=<id>)  — identify non-compliant device_ids
3. ome_remediate_firmware_baseline(
       baseline_id=<id>,
       device_ids=[<id1>, <id2>],
       stage_only=False,          # False=graceful reboot if needed, True=stage only
       job_name="My Update Job"
   )
4. ome_get_job(job_id=<returned_id>)              — poll for completion
```

The tool internally fetches the baseline (for repositoryId/catalogId) and per-device compliance
reports (to build the component Data strings). It posts to `JobService/Jobs` directly — the
`UpdateService/Actions/UpdateService.UpdateFirmware` endpoint does not exist in OME.
`rebootType`: 2 = graceful reboot (stage_only=False), 3 = stage for next reboot (stage_only=True).

### Discover new lab servers

```
1. ome_create_discovery_job(name="Lab Scan", ip_range="192.168.1.100-192.168.1.200")
2. ome_list_jobs(filter="JobType/Name eq 'Discovery'", top=10) to monitor progress
3. ome_list_devices() after discovery completes
```

---

## Response Format

All tools return JSON strings. Parse with `json.loads()` if needed.

Errors are returned as `{"error": "<message>"}` — check for this key before processing.

Success responses mirror OME API structure: top-level `value` array + `@odata.count`.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OME_IP` | `192.168.1.145` | OME appliance IP |
| `OME_PORT` | `443` | OME HTTPS port |
| `OME_USER` | (required) | OME API username |
| `OME_PASSWORD` | (required) | OME API password |
| `OME_VERIFY_SSL` | `false` | `true` to verify TLS cert |
| `MCP_HOST` | `0.0.0.0` | MCP server bind address |
| `MCP_PORT` | `8000` | MCP server port |

---

## Extension Notes

When adding tools:
- Use `_ome_get(path, params)` for GET requests
- Use `_ome_post(path, payload)` for POST/action requests
- Define a Pydantic `BaseModel` with `model_config = ConfigDict(extra="forbid")`
- Use `Field(..., description="...")` for all required fields
- Always return `_ok(data)` on success or `_handle(exc)` on exception
- Session management is automatic — do not call the Sessions endpoint directly
