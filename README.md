# OME MCP — Dell OpenManage Enterprise MCP Server

A Model Context Protocol (MCP) server that exposes Dell OpenManage Enterprise (OME)
management capabilities to AI agents via a Streaming HTTP transport. No direct iDRAC
connections are made — all data flows through OME's REST API.

---

## Features

| Tool | Description |
|------|-------------|
| `ome_list_devices` | List all managed devices with OData filter + pagination |
| `ome_get_device` | Full detail for one device by ID |
| `ome_get_device_inventory` | Hardware inventory (CPU/RAM/NIC/HDD/PSU) |
| `ome_get_device_subsystem_health` | Per-subsystem health status |
| `ome_get_device_network_adapters` | NIC/port details |
| `ome_device_power_action` | PowerOn/Off/Restart/Cycle via OME |
| `ome_list_groups` | List device groups |
| `ome_get_group_devices` | Devices in a specific group |
| `ome_list_alerts` | Alerts with severity/status filter |
| `ome_acknowledge_alerts` | Acknowledge one or more alerts |
| `ome_list_jobs` | List jobs with filter |
| `ome_get_job` | Job detail and status |
| `ome_run_job_now` | Trigger an existing job immediately |
| `ome_get_job_execution_history` | Job run history |
| `ome_list_discovery_jobs` | Discovery configuration groups |
| `ome_create_discovery_job` | Create & start a discovery scan |
| `ome_list_firmware_catalogs` | Firmware update catalogs |
| `ome_list_firmware_baselines` | Firmware compliance baselines |
| `ome_get_firmware_baseline_compliance` | Per-component compliance report |
| `ome_list_templates` | Configuration/deployment templates |
| `ome_get_template` | Template details |
| `ome_list_config_baselines` | Configuration compliance baselines |
| `ome_get_config_baseline_compliance` | Per-device config compliance |
| `ome_get_system_info` | OME version, license, health |
| `ome_list_active_sessions` | Active OME API sessions |
| `ome_get_audit_logs` | OME audit trail |
| `ome_get_warranty_info` | Device warranty records |
| `ome_refresh_device_inventory` | Trigger OME inventory refresh |

---

## Architecture

```
AI Agent / MCP Client
        │
        │  HTTP POST /mcp  (Streaming HTTP transport)
        ▼
┌─────────────────────┐
│   ome_mcp_v5        │   Docker container (Ubuntu/Python 3.11)
│   FastMCP server    │
│   port 8000         │
└────────┬────────────┘
         │  HTTPS REST API calls
         ▼
┌─────────────────────┐
│  OME Appliance      │   https://192.168.1.145/api
│  (192.168.1.145)    │
└────────┬────────────┘
         │  OME manages iDRAC internally
         ▼
   Dell PowerEdge Servers
```

No connections are made directly to iDRAC — OME handles that internally.

---

## Prerequisites

- Docker Engine 24+ and Docker Compose v2 on an Ubuntu host
- Network access from the host to `https://192.168.1.145` (OME appliance)
- OME admin credentials

---

## Quick Start

### 1. Clone / copy project files

### 2. Configure credentials

```bash
cd ome-mcp
cp .env.example .env
nano .env          # Set OME_USER, OME_PASSWORD
```

Never commit `.env` to version control.

### 3. Start container
```bash
docker compose up -d
```

The MCP endpoint will be available at `http://<host>:8000/mcp`.

## Service Based Install 

### Build the image

```bash
./service_control.sh build
```

### Start the server

```bash
./service_control.sh start
```

The MCP endpoint will be available at `http://<host>:8000/mcp`.

### Check status

```bash
./service_control.sh status
./service_control.sh logs 50
```

### Auto-start with systemd (optional)

```bash
# Install systemd unit (auto-updates WorkingDirectory to current path)
sudo ./service_control.sh install-systemd

# Start now
sudo systemctl start ome_mcp_v5

# View logs
sudo journalctl -u ome_mcp_v5 -f
```

To remove the systemd unit:

```bash
sudo ./service_control.sh uninstall-systemd
```

---

## TLS / SSL Toggle

| Scenario | Setting in `.env` |
|----------|-------------------|
| Lab / self-signed cert | `OME_VERIFY_SSL=false` |
| Production (trusted CA) | `OME_VERIFY_SSL=true` |
| Production (custom CA bundle) | `OME_VERIFY_SSL=/path/to/ca-bundle.crt` |

No rebuild is required — just change the env var and restart.

---

## Connecting an MCP Client

Point any MCP-compatible client at:

```
http://<host_ip>:8000/mcp
```

Example with `mcp` CLI:

```bash
mcp call --url http://192.168.1.200:8000/mcp ome_list_devices '{"top":10}'
```

Example with Python SDK:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async with streamablehttp_client("http://192.168.1.200:8000/mcp") as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        result = await session.call_tool("ome_list_devices", {"top": 5})
        print(result.content)
```

---

## Pagination

All list tools accept `top` (page size, default 50) and `skip` (offset) parameters.
Use the returned `next_skip` value to fetch subsequent pages:

```json
{"top": 25, "skip": 0}   // first page
{"top": 25, "skip": 25}  // second page
```

## OData Filtering

Pass standard OData `$filter` expressions via the `filter` parameter:

```
"Model eq 'PowerEdge R640'"
"Severity eq 'Critical'"
"LastRunStatus/Name eq 'Failed'"
"StatusType eq 'New'"
```

---

## Security Notes

- Credentials are stored in `.env` (owner-read-only, not version-controlled).
- The server runs as a non-root user (`mcpuser`, UID 1000) inside the container.
- Credentials are never logged.
- For production, set `OME_VERIFY_SSL=true` and use a valid TLS certificate on OME.
- The MCP endpoint itself is plain HTTP on port 8000; place a reverse proxy (nginx/caddy)
  with TLS in front of it if external exposure is required.

---

## Troubleshooting

**Container won't start:**
```bash
./service_control.sh logs
```

**Authentication errors:**
- Verify `OME_USER` / `OME_PASSWORD` in `.env`
- Confirm OME is reachable: `curl -k https://192.168.1.145/api/SessionService/Sessions`

**SSL errors:**
- Set `OME_VERIFY_SSL=false` for self-signed certs
- Restart after changing: `./service_control.sh restart`

**Tools return empty results:**
- Devices may not be discovered yet — use `ome_create_discovery_job`
- Check OME permissions for the API account

---

## Adding New Tools

1. Add a Pydantic `Input` model in `ome_mcp_server.py`
2. Add a function decorated with `@mcp.tool(name="ome_your_tool")`
3. Call `_ome_get` / `_ome_post` as appropriate
4. Rebuild: `./service_control.sh rebuild && ./service_control.sh restart`

---

## License

MIT License — see LICENSE file.

---

