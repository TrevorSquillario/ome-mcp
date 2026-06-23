#!/usr/bin/env python3
"""
OME MCP v5 - Dell OpenManage Enterprise MCP Server
Manages server lab environments via OME REST API using Streaming HTTP transport.

Version: 5.2.0
"""

__version__ = "5.2.0"

import os
import sys
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from enum import Enum

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP, Context

# ── Logging (always stderr) ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ome_mcp_v5")


def _patch_transport_security() -> None:
    """Neutralize FastMCP host-header validation so external IPs are accepted.

    FastMCP's transport_security module rejects Host headers that don't match
    its allowed-hosts list.  We inspect the module at runtime and replace every
    callable that looks like a host validator with a passthrough, making this
    robust across SDK versions without depending on undocumented kwargs.
    """
    import inspect
    try:
        import mcp.server.transport_security as _ts

        # Log the module source at DEBUG level so it shows up in logs if needed
        try:
            logger.debug("transport_security source:\n%s", inspect.getsource(_ts))
        except Exception:
            pass

        # --- patch module-level callables ---
        for attr_name in dir(_ts):
            if attr_name.startswith("__"):
                continue
            obj = getattr(_ts, attr_name, None)
            if callable(obj) and any(kw in attr_name.lower() for kw in ("host", "valid", "allow", "check")):
                logger.info("Patching transport_security.%s", attr_name)
                setattr(_ts, attr_name, lambda *a, **kw: True)

        # --- patch class methods ---
        TARGET_METHODS = {
            "check_host", "_check_host", "validate_host", "_validate_host",
            "is_valid_host", "_is_valid_host", "is_allowed", "_is_allowed",
        }
        for cls_name, cls in inspect.getmembers(_ts, inspect.isclass):
            for meth_name in TARGET_METHODS:
                if hasattr(cls, meth_name):
                    logger.info("Patching %s.%s", cls_name, meth_name)
                    setattr(cls, meth_name, lambda *a, **kw: True)

        logger.info("transport_security patched — all hosts accepted")
    except Exception as exc:
        logger.warning("Could not patch transport_security (non-fatal): %s", exc)

# ── Configuration from environment ───────────────────────────────────────────
OME_IP       = os.environ.get("OME_IP",       "192.168.1.145")
OME_USER     = os.environ.get("OME_USER",     "")
OME_PASSWORD = os.environ.get("OME_PASSWORD", "")
OME_PORT     = int(os.environ.get("OME_PORT", "443"))
OME_VERIFY_SSL = os.environ.get("OME_VERIFY_SSL", "false").lower() == "true"
MCP_HOST     = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT     = int(os.environ.get("MCP_PORT", "8000"))

BASE_URL = f"https://{OME_IP}:{OME_PORT}/api"

# ── Session State (per-server lifecycle) ─────────────────────────────────────
_session_token: Optional[str] = None
_session_id: Optional[str] = None
_session_lock = asyncio.Lock()


# ── OME HTTP Client ───────────────────────────────────────────────────────────
def _make_client() -> httpx.AsyncClient:
    """Return a configured async HTTP client for OME."""
    return httpx.AsyncClient(
        verify=OME_VERIFY_SSL,
        timeout=httpx.Timeout(60.0, connect=10.0),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )


async def _get_token() -> str:
    """Return a valid session token, creating one if needed."""
    global _session_token, _session_id
    async with _session_lock:
        if _session_token:
            return _session_token
        if not OME_USER or not OME_PASSWORD:
            raise RuntimeError(
                "OME_USER and OME_PASSWORD environment variables must be set."
            )
        payload = {"UserName": OME_USER, "Password": OME_PASSWORD, "SessionType": "API"}
        async with _make_client() as client:
            r = await client.post(f"{BASE_URL}/SessionService/Sessions", json=payload)
            r.raise_for_status()
            _session_token = r.headers.get("X-Auth-Token", "")
            _session_id = r.json().get("Id", "")
            logger.info("OME session established (id=%s)", _session_id)
        return _session_token


async def _logout() -> None:
    """Invalidate the current OME session."""
    global _session_token, _session_id
    if not _session_token or not _session_id:
        return
    try:
        async with _make_client() as client:
            headers = {"X-Auth-Token": _session_token}
            await client.delete(
                f"{BASE_URL}/SessionService/Sessions/{_session_id}", headers=headers
            )
        logger.info("OME session %s closed", _session_id)
    except Exception as exc:
        logger.warning("Logout error: %s", exc)
    finally:
        _session_token = None
        _session_id = None


async def _invalidate_token() -> None:
    """Clear the cached session token so the next call re-authenticates."""
    global _session_token, _session_id
    async with _session_lock:
        logger.info("Invalidating expired OME session token (id=%s)", _session_id)
        _session_token = None
        _session_id = None


async def _ome_get(path: str, params: dict = None) -> Any:
    """GET from OME API, returns parsed JSON. Auto-retries once on 401."""
    for attempt in range(2):
        token = await _get_token()
        async with _make_client() as client:
            r = await client.get(
                f"{BASE_URL}/{path.lstrip('/')}",
                headers={"X-Auth-Token": token},
                params=params or {},
            )
            if r.status_code == 401 and attempt == 0:
                await _invalidate_token()
                continue
            _raise_for_status(r)
            return r.json()


async def _ome_post(path: str, payload: dict) -> Any:
    """POST to OME API, returns parsed JSON. Auto-retries once on 401."""
    for attempt in range(2):
        token = await _get_token()
        async with _make_client() as client:
            r = await client.post(
                f"{BASE_URL}/{path.lstrip('/')}",
                headers={"X-Auth-Token": token},
                json=payload,
            )
            if r.status_code == 401 and attempt == 0:
                await _invalidate_token()
                continue
            _raise_for_status(r)
            try:
                return r.json()
            except Exception:
                return {"status": r.status_code, "text": r.text}


async def _ome_delete(path: str) -> Any:
    """DELETE on OME API. Auto-retries once on 401."""
    for attempt in range(2):
        token = await _get_token()
        async with _make_client() as client:
            r = await client.delete(
                f"{BASE_URL}/{path.lstrip('/')}",
                headers={"X-Auth-Token": token},
            )
            if r.status_code == 401 and attempt == 0:
                await _invalidate_token()
                continue
            _raise_for_status(r)
            return {"status": r.status_code}


def _raise_for_status(r: httpx.Response) -> None:
    """Raise with a meaningful message on HTTP errors, including OME ExtendedInfo."""
    if r.status_code >= 400:
        try:
            body = r.json()
            err = body.get("error", {})
            parts = [err.get("message", "")]
            # OME puts the real detail in error.@Message.ExtendedInfo[].Message
            for info in err.get("@Message.ExtendedInfo", []):
                detail = info.get("Message", "")
                if detail and detail not in parts:
                    parts.append(detail)
            msg = " | ".join(p for p in parts if p) or r.text
        except Exception:
            msg = r.text
        raise httpx.HTTPStatusError(
            f"OME API error {r.status_code}: {msg}", request=r.request, response=r
        )


def _ok(data: Any) -> str:
    """Serialize data to formatted JSON string."""
    return json.dumps(data, indent=2, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": str(msg)}, indent=2)


def _handle(exc: Exception) -> str:
    logger.error("Tool error: %s", exc, exc_info=True)
    return _err(exc)


# ── Pydantic Input Models ─────────────────────────────────────────────────────

class PaginationInput(BaseModel):
    """Common pagination parameters."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    top: int = Field(default=50, description="Max records to return", ge=1, le=1000)
    skip: int = Field(default=0, description="Records to skip (for pagination)", ge=0)
    filter: str = Field(default="", description="OData $filter expression, e.g. \"Model eq 'PowerEdge R640'\"")


class DeviceIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    device_id: int = Field(..., description="OME numeric device ID", ge=1)


class GroupIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    group_id: int = Field(..., description="OME numeric group ID", ge=1)


class AlertsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    top: int = Field(default=50, ge=1, le=1000, description="Max alerts to return")
    skip: int = Field(default=0, ge=0, description="Records to skip")
    filter: str = Field(default="", description="OData filter, e.g. \"Severity eq 'Critical'\"")


class JobIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: int = Field(..., description="OME numeric job ID", ge=1)


class PowerActionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    device_ids: List[int] = Field(..., description="List of OME device IDs to act on", min_length=1)
    action: str = Field(..., description="Power action: PowerOn, PowerOff, GracefulShutdown, GracefulRestart, MasterBusReset, PowerCycle")

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"PowerOn", "PowerOff", "GracefulShutdown", "GracefulRestart", "MasterBusReset", "PowerCycle"}
        if v not in allowed:
            raise ValueError(f"action must be one of: {', '.join(sorted(allowed))}")
        return v


class DiscoveryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(..., description="Name for this discovery job", min_length=1, max_length=100)
    ip_range: str = Field(..., description="IP range to discover, e.g. '192.168.1.100-192.168.1.200'", min_length=1)
    protocol: str = Field(default="HTTPS", description="Discovery protocol: HTTPS, REDFISH, or WSMAN")
    username: str = Field(default="", description="iDRAC username (leave empty to use OME default)")
    password: str = Field(default="", description="iDRAC password (leave empty to use OME default)")

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        allowed = {"HTTPS", "REDFISH", "WSMAN"}
        if v.upper() not in allowed:
            raise ValueError(f"protocol must be one of: {', '.join(sorted(allowed))}")
        return v.upper()


class TemplateIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    template_id: int = Field(..., description="OME template ID", ge=1)


class BaselineIdInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    baseline_id: int = Field(..., description="OME baseline ID", ge=1)


class AlertAckInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    alert_ids: List[int] = Field(..., description="List of alert IDs to acknowledge", min_length=1)


class RunJobInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: int = Field(..., description="OME job ID to run immediately", ge=1)


class FirmwareBaselineInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    baseline_id: int = Field(..., description="Firmware baseline ID", ge=1)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(server):
    """Establish OME session on startup, close on shutdown."""
    if not OME_USER or not OME_PASSWORD:
        logger.warning(
            "OME_USER / OME_PASSWORD not set — tools will fail until env vars are provided."
        )
    else:
        try:
            await _get_token()
        except Exception as exc:
            logger.error("Could not establish initial OME session: %s", exc)
    yield
    await _logout()


# ── MCP Server ────────────────────────────────────────────────────────────────
mcp = FastMCP("ome_mcp_v5", lifespan=lifespan)


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_devices",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_devices(params: PaginationInput) -> str:
    """List all managed devices in OME with optional OData filtering and pagination.

    Returns device ID, name, model, service tag, power state, and health status.

    Args:
        params (PaginationInput): top (max records), skip (offset), filter (OData expression)

    Returns:
        str: JSON object with 'count', 'value' (list of devices), and 'next_skip'.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("DeviceService/Devices", qs)
        result = {
            "count": data.get("@odata.count", len(data.get("value", []))),
            "value": data.get("value", []),
            "next_skip": params.skip + params.top,
        }
        return _ok(result)
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_device",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_device(params: DeviceIdInput) -> str:
    """Get detailed information for a single OME-managed device by its device ID.

    Returns full device record including model, service tag, firmware, and health.

    Args:
        params (DeviceIdInput): device_id (int)

    Returns:
        str: JSON object with all device fields from OME.
    """
    try:
        data = await _ome_get(f"DeviceService/Devices({params.device_id})")
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_device_inventory",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_device_inventory(params: DeviceIdInput) -> str:
    """Retrieve full hardware inventory for a device (CPUs, memory, NICs, HDDs, PSUs).

    Args:
        params (DeviceIdInput): device_id (int)

    Returns:
        str: JSON list of inventory category objects returned by OME.
    """
    try:
        data = await _ome_get(f"DeviceService/Devices({params.device_id})/InventoryDetails")
        value = data.get("value", data)
        if not value:
            return _err(
                f"Device {params.device_id} returned empty inventory. "
                "OME may not have collected it yet — "
                f"run ome_refresh_device_inventory with device_id={params.device_id} then retry."
            )
        return _ok(value)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _err(
                f"No inventory found for device {params.device_id} (OME returned 404). "
                "This usually means inventory has not been collected yet. "
                f"Run ome_refresh_device_inventory with device_id={params.device_id}, "
                "wait for the job to complete, then retry. "
                "You can also verify the device exists with ome_list_devices."
            )
        return _handle(exc)
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_device_subsystem_health",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_device_subsystem_health(params: DeviceIdInput) -> str:
    """Get subsystem health (CPU, memory, storage, network) for a specific device.

    Args:
        params (DeviceIdInput): device_id (int)

    Returns:
        str: JSON list of subsystem health objects from OME.
    """
    try:
        data = await _ome_get(f"DeviceService/Devices({params.device_id})/SubSystemHealth")
        return _ok(data.get("value", data))
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_device_network_adapters",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_device_network_adapters(params: DeviceIdInput) -> str:
    """List all network adapters and their port details for a device.

    Args:
        params (DeviceIdInput): device_id (int)

    Returns:
        str: JSON list of network adapter inventory entries from OME.
    """
    try:
        data = await _ome_get(
            f"DeviceService/Devices({params.device_id})/InventoryDetails",
            {"$filter": "InventoryType eq 'serverNetworkInterfaces'"},
        )
        return _ok(data.get("value", data))
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# POWER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_device_power_action",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
)
async def ome_device_power_action(params: PowerActionInput) -> str:
    """Execute a power action on one or more OME-managed devices via OME (no direct iDRAC).

    Supported actions: PowerOn, PowerOff, GracefulShutdown, GracefulRestart, MasterBusReset, PowerCycle.

    Args:
        params (PowerActionInput): device_ids (list[int]), action (str)

    Returns:
        str: JSON object with job ID returned by OME for the power action.
    """
    try:
        payload = {
            "DeviceIds": params.device_ids,
            "PrimaryOperation": params.action,
            "Targets": [],
        }
        data = await _ome_post("DeviceService/Actions/DeviceService.PerformDeviceAction", payload)
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_groups",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_groups(params: PaginationInput) -> str:
    """List all device groups in OME with optional filter and pagination.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and value (list of groups).
    """
    try:
        qs = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("GroupService/Groups", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_group_devices",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_group_devices(params: GroupIdInput) -> str:
    """List all devices that belong to a specific OME group.

    Args:
        params (GroupIdInput): group_id (int)

    Returns:
        str: JSON object with device list for the group.
    """
    try:
        data = await _ome_get(f"GroupService/Groups({params.group_id})/Devices")
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_alerts",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_alerts(params: AlertsInput) -> str:
    """List OME alerts with optional severity/status filter and pagination.

    Filter examples: \"Severity eq 'Critical'\", \"StatusType eq 'New'\".

    Args:
        params (AlertsInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and list of alert records.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("AlertService/Alerts", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_acknowledge_alerts",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def ome_acknowledge_alerts(params: AlertAckInput) -> str:
    """Acknowledge one or more OME alerts by their alert IDs.

    Args:
        params (AlertAckInput): alert_ids (list[int])

    Returns:
        str: JSON with OME response for the acknowledge action.
    """
    try:
        payload = {"AlertIds": params.alert_ids, "Comments": ""}
        data = await _ome_post("AlertService/Actions/AlertService.Acknowledge", payload)
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# JOB TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_jobs",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_jobs(params: PaginationInput) -> str:
    """List OME jobs with optional filter and pagination.

    Filter examples: \"JobType/Name eq 'Firmware Update Task'\", \"LastRunStatus/Name eq 'Failed'\".

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and job list.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("JobService/Jobs", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_job",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_job(params: JobIdInput) -> str:
    """Get details and last execution status for a specific OME job.

    Args:
        params (JobIdInput): job_id (int)

    Returns:
        str: JSON object with full job details from OME.
    """
    try:
        data = await _ome_get(f"JobService/Jobs({params.job_id})")
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_run_job_now",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def ome_run_job_now(params: RunJobInput) -> str:
    """Immediately trigger execution of an existing OME job.

    Args:
        params (RunJobInput): job_id (int)

    Returns:
        str: JSON with OME job execution response.
    """
    try:
        payload = {"JobIds": [params.job_id]}
        data = await _ome_post("JobService/Actions/JobService.RunNow", payload)
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_job_execution_history",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_job_execution_history(params: JobIdInput) -> str:
    """Retrieve the execution history for a specific OME job.

    Args:
        params (JobIdInput): job_id (int)

    Returns:
        str: JSON list of execution history entries.
    """
    try:
        data = await _ome_get(f"JobService/Jobs({params.job_id})/ExecutionHistories")
        return _ok(data.get("value", data))
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# DISCOVERY TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_discovery_jobs",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_discovery_jobs(params: PaginationInput) -> str:
    """List all device discovery configuration groups in OME.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and list of discovery config groups.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        data = await _ome_get("DiscoveryConfigService/DiscoveryConfigGroups", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_create_discovery_job",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def ome_create_discovery_job(params: DiscoveryInput) -> str:
    """Create and start a new device discovery job in OME for a given IP range.

    Args:
        params (DiscoveryInput): name, ip_range, protocol, username (optional), password (optional)

    Returns:
        str: JSON with the OME discovery job creation response.
    """
    try:
        conn_profile: Dict[str, Any] = {
            "ProfileName": "",
            "Type": params.protocol,
            "Credentials": [],
        }
        if params.username.strip():
            conn_profile["Credentials"].append({
                "Type": params.protocol,
                "AuthType": "Basic",
                "Modified": False,
                "UserName": params.username,
                "Password": params.password,
            })

        payload = {
            "DiscoveryConfigGroupName": params.name,
            "DiscoveryConfigGroupDescription": "",
            "DiscoveryStatusEmailRecipient": "",
            "CreateGroup": True,
            "TrapDestination": False,
            "CommunityString": False,
            "DiscoveryConfigModels": [
                {
                    "DiscoveryConfigTargets": [
                        {"NetworkAddressDetail": params.ip_range}
                    ],
                    "ConnectionProfile": json.dumps(conn_profile),
                    "DeviceType": [1000],
                }
            ],
            "Schedule": {"RunNow": True, "Cron": "startnow"},
        }
        data = await _ome_post("DiscoveryConfigService/DiscoveryConfigGroups", payload)
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# FIRMWARE / CATALOG / BASELINE TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_firmware_catalogs",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_firmware_catalogs(params: PaginationInput) -> str:
    """List firmware update catalogs available in OME.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and catalog list.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        data = await _ome_get("UpdateService/Catalogs", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_list_firmware_baselines",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_firmware_baselines(params: PaginationInput) -> str:
    """List firmware compliance baselines configured in OME.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and baseline list.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        data = await _ome_get("UpdateService/Baselines", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_firmware_baseline_compliance",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_firmware_baseline_compliance(params: FirmwareBaselineInput) -> str:
    """Get per-component firmware compliance report for a baseline.

    Args:
        params (FirmwareBaselineInput): baseline_id (int)

    Returns:
        str: JSON list of component compliance records.
    """
    try:
        data = await _ome_get(f"UpdateService/Baselines({params.baseline_id})/DeviceComplianceReports")
        return _ok(data.get("value", data))
    except Exception as exc:
        return _handle(exc)


class FirmwareRemediationInput(BaseModel):
    """Input for remediating devices to a firmware baseline."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    baseline_id: int = Field(..., description="Firmware baseline ID to remediate against", ge=1)
    device_ids: List[int] = Field(..., description="List of OME device IDs to update", min_length=1)
    stage_only: bool = Field(default=False, description="Stage firmware without rebooting (true=stage/rebootType=3, false=graceful reboot/rebootType=2)")
    job_name: str = Field(default="Firmware Remediation", description="Name for the created OME update job", min_length=1, max_length=100)


@mcp.tool(
    name="ome_remediate_firmware_baseline",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
)
async def ome_remediate_firmware_baseline(params: FirmwareRemediationInput) -> str:
    """Push firmware updates to make devices compliant with a baseline.

    Fetches the baseline for repositoryId/catalogId, reads the compliance report to
    build per-device component Data strings, then creates a JobService/Jobs Update_Task.
    Returns the OME job ID — poll ome_get_job to track progress.

    Args:
        params (FirmwareRemediationInput): baseline_id, device_ids, stage_only, job_name

    Returns:
        str: JSON object with the created job ID and details from OME.
    """
    try:
        # 1. Fetch baseline to get repositoryId and catalogId
        baseline = await _ome_get(f"UpdateService/Baselines({params.baseline_id})")
        repo_id = str(baseline["RepositoryId"])
        catalog_id = str(baseline["CatalogId"])

        # 2. Fetch compliance report; build device_id -> Data (non-compliant SourceNames)
        compliance = await _ome_get(
            f"UpdateService/Baselines({params.baseline_id})/DeviceComplianceReports"
        )
        device_data: Dict[int, str] = {}
        for report in compliance.get("value", []):
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

        if not device_data:
            return _err(
                f"No non-compliant components found for the specified devices against "
                f"baseline {params.baseline_id}. All devices may already be compliant "
                "or the baseline compliance report has not been computed yet."
            )

        # 3. Build targets — only include devices that have non-compliant components
        targets = [
            {
                "Id": did,
                "Data": device_data[did],
                "TargetType": {"Id": 1000, "Name": "DEVICE"},
            }
            for did in params.device_ids
            if did in device_data
        ]

        # 4. POST the job directly to JobService/Jobs (UpdateService has no action endpoints)
        reboot_type = "3" if params.stage_only else "2"
        payload = {
            "JobName": params.job_name,
            "JobDescription": f"Firmware remediation against baseline {params.baseline_id}",
            "Schedule": "startnow",
            "State": "Enabled",
            "JobType": {"Id": 5, "Name": "Update_Task"},
            "Targets": targets,
            "Params": [
                {"Key": "repositoryId",       "Value": repo_id},
                {"Key": "catalogId",          "Value": catalog_id},
                {"Key": "complianceReportId", "Value": str(params.baseline_id)},
                {"Key": "operationName",      "Value": "INSTALL_FIRMWARE"},
                {"Key": "rebootType",         "Value": reboot_type},
                {"Key": "signVerify",         "Value": "true"},
                {"Key": "complianceUpdate",   "Value": "true"},
                {"Key": "stagingValue",       "Value": str(params.stage_only).lower()},
            ],
        }
        data = await _ome_post("JobService/Jobs", payload)
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# TEMPLATE TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_templates",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_templates(params: PaginationInput) -> str:
    """List configuration and deployment templates in OME.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and template list.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("TemplateService/Templates", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_template",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_template(params: TemplateIdInput) -> str:
    """Get full details of a specific OME template by its template ID.

    Args:
        params (TemplateIdInput): template_id (int)

    Returns:
        str: JSON object with template attributes.
    """
    try:
        data = await _ome_get(f"TemplateService/Templates({params.template_id})")
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION COMPLIANCE
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_config_baselines",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_config_baselines(params: PaginationInput) -> str:
    """List configuration compliance baselines in OME.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and baseline list.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        data = await _ome_get("ConfigurationManagement/Baselines", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_config_baseline_compliance",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_config_baseline_compliance(params: BaselineIdInput) -> str:
    """Get the per-device configuration compliance report for a specific baseline.

    Args:
        params (BaselineIdInput): baseline_id (int)

    Returns:
        str: JSON list of device compliance entries.
    """
    try:
        data = await _ome_get(f"ConfigurationManagement/Baselines({params.baseline_id})/DeviceConfigComplianceSummaries")
        return _ok(data.get("value", data))
    except Exception as exc:
        return _handle(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM / SESSION TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_get_system_info",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_system_info() -> str:
    """Return OME server version, license, and system health summary.

    Returns:
        str: JSON object with OME appliance info fields.
    """
    try:
        data = await _ome_get("ApplicationService/Info")
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_list_active_sessions",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_active_sessions(params: PaginationInput) -> str:
    """List active API/user sessions on the OME appliance.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and session list.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        data = await _ome_get("SessionService/Sessions", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_audit_logs",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_audit_logs(params: PaginationInput) -> str:
    """Retrieve the OME audit log with optional OData filter and pagination.

    Args:
        params (PaginationInput): top, skip, filter (OData expression)

    Returns:
        str: JSON object with count and list of audit log entries.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("AuditService/AuditLogs", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_get_warranty_info",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_get_warranty_info(params: PaginationInput) -> str:
    """Get warranty information for all OME-managed devices.

    Args:
        params (PaginationInput): top, skip, filter (OData)

    Returns:
        str: JSON object with count and warranty records.
    """
    try:
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("WarrantyService/Warranties", qs)
        return _ok({"count": data.get("@odata.count", 0), "value": data.get("value", [])})
    except Exception as exc:
        return _handle(exc)


@mcp.tool(
    name="ome_refresh_device_inventory",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def ome_refresh_device_inventory(params: DeviceIdInput) -> str:
    """Trigger OME to refresh/re-discover the inventory of a specific device.

    Args:
        params (DeviceIdInput): device_id (int)

    Returns:
        str: JSON with the OME job created for inventory refresh.
    """
    try:
        payload = {"DeviceIds": [params.device_id], "ScheduleType": "RunNow"}
        data = await _ome_post("DeviceService/Actions/DeviceService.PerformInventory", payload)
        return _ok(data)
    except Exception as exc:
        return _handle(exc)


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info(
        "Starting OME MCP v5 server — OME_IP=%s SSL_VERIFY=%s → http://%s:%s",
        OME_IP, OME_VERIFY_SSL, MCP_HOST, MCP_PORT,
    )
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Mount

    async def health(request):
        return JSONResponse({"status": "ok", "service": "ome_mcp_v5"})

    # Get the FastMCP ASGI app; it is a Starlette app whose lifespan
    # initialises the StreamableHTTP task group.  Mounting it inside a
    # second Starlette app suppresses that lifespan, causing the
    # "Task group is not initialized" RuntimeError.
    # Fix: explicitly forward the inner lifespan to the outer app, and
    # rewrite the Host header (in the ASGI scope) so FastMCP's
    # transport-security check never sees the external client IP.
    _patch_transport_security()
    inner_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def _lifespan(app):
        async with inner_app.router.lifespan_context(app):
            yield

    class _HostRewrite:
        """Rewrite Host header to 'localhost' before FastMCP security sees it."""
        def __init__(self, wrapped):
            self._wrapped = wrapped
        async def __call__(self, scope, receive, send):
            if scope.get("type") in ("http", "websocket"):
                scope["headers"] = [
                    (b"host", b"localhost") if k.lower() == b"host" else (k, v)
                    for k, v in scope["headers"]
                ]
            await self._wrapped(scope, receive, send)

    app = Starlette(
        lifespan=_lifespan,
        routes=[
            Route("/health", health),
            Mount("/", _HostRewrite(inner_app)),
        ],
    )

    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
