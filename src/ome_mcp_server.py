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
from typing import Any, Dict, List, Optional
import asyncio
from contextlib import asynccontextmanager
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from starlette.responses import JSONResponse

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict

from utils.logging_config import configure_logging
from models import *
from ome_client import OMEClient

configure_logging()
logger = logging.getLogger(__name__)

# ── Configuration from environment ───────────────────────────────────────────
OME_IP       = os.environ.get("OME_IP",       "192.168.1.145")
OME_USER     = os.environ.get("OME_USER",     "")
OME_PASSWORD = os.environ.get("OME_PASSWORD", "")
OME_PORT     = int(os.environ.get("OME_PORT", "443"))
OME_VERIFY_SSL = os.environ.get("OME_VERIFY_SSL", "false").lower() == "true"
OME_MCP_HOST     = os.environ.get("OME_MCP_HOST", "0.0.0.0")
OME_MCP_PORT     = int(os.environ.get("OME_MCP_PORT", "8000"))

BASE_URL = f"https://{OME_IP}:{OME_PORT}/api"

# Alert / status mapping dictionaries 
SEVERITY_TYPE_MAP = {
    "WARNING": "8",
    "CRITICAL": "16",
    "INFO": "2",
    "NORMAL": "4",
    "UNKNOWN": "1",
}

STATUS_TYPE_MAP = {
    "NORMAL": "1000",
    "UNKNOWN": "2000",
    "WARNING": "3000",
    "CRITICAL": "4000",
    "NOSTATUS": "5000",
}

ALERT_DEVICE_TYPE_MAP = {
    "SERVER": "1000",
    "CHASSIS": "2000",
    "NETWORK_CONTROLLER": "9000",
    "NETWORK_IOM": "4000",
    "STORAGE": "3000",
    "STORAGE_IOM": "8000",
}

CATEGORY_ID_MAP = {
    "AUDIT": 4,
    "MISCELLANEOUS": 7,
    "STORAGE": 2,
    "SYSTEM_HEALTH": 1,
    "UPDATES": 3,
    "WORK_NOTES": 6,
    "CONFIGURATION": 5,
}

# Create a shared OMEClient instance and expose thin wrappers so the
# rest of this module (many `ome_*` tool functions) can continue to call
# the familiar helper names without further changes.
_ome_client = OMEClient()


_session_lock = _ome_client._session_lock


def _get_client() -> httpx.AsyncClient:
    return _ome_client.get_client()


async def _close_client() -> None:
    await _ome_client.close()


async def _get_token() -> str:
    return await _ome_client.get_token()


async def _logout() -> None:
    await _ome_client.logout()


async def _invalidate_token() -> None:
    await _ome_client.invalidate_token()


def _build_query_string(params: Optional[dict]) -> str:
    return _ome_client.build_query_string(params)


async def _ome_get(path: str, params: dict = None, include: Optional[List[str]] = None) -> Any:
    return await _ome_client.ome_get(path, params=params, include=include)


async def _ome_post(path: str, payload: dict) -> Any:
    return await _ome_client.ome_post(path, payload)


async def _ome_delete(path: str) -> Any:
    return await _ome_client.ome_delete(path)


def _raise_for_status(r: httpx.Response) -> None:
    return _ome_client.raise_for_status(r)


def _ok(data: Any) -> str:
    """Serialize data to formatted JSON string."""
    return json.dumps(data, indent=2, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": str(msg)}, indent=2)


def _handle(exc: Exception) -> str:
    logger.error("Tool error: %s", exc, exc_info=True)
    return _err(exc)

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
    await _close_client()


# ── MCP Server ────────────────────────────────────────────────────────────────
mcp = FastMCP("OME (OpenManage Enterprise) MCP Server", lifespan=lifespan)


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICE TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool(
    name="ome_list_devices",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
)
async def ome_list_devices(params: DeviceInput) -> str:
    """List all managed infrastructure devices in OpenManage Enterprise (OME).

    Use this tool when a user wants to inventory hardware, find specific machine details, 
    check server models, find a device by service tag, or check system health/power states.

    This tool returns detailed attributes for each device including:
    Id, Type, DeviceName, Model, DeviceServiceTag, PowerState, ManagedState, Health, 
    DeviceManagement, LastInventoryTime, and LastStatusTime.

    Args:
        params: The pagination constraints and explicit OData filter parameters.

    Returns:
        str: A JSON payload containing:
            - 'count' (int): Total records matching the criteria.
            - 'value' (list): Array of device metadata objects.
            - 'next_skip' (int): The calculated offset marker for the next page.
    """
    try:
        include_fields = ["Id", "Type", "DeviceName", "Model", "DeviceServiceTag", "PowerState", "ManagedState", "Health", "DeviceManagement", "LastInventoryTime", "LastStatusTime"]
        qs: dict = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("DeviceService/Devices", qs, include=include_fields)
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

    Filters Available: ("Name", "TypeId", "Id")

    Returns:
        str: JSON object with count and value (list of groups).
    """
    try:
        include_fields = ["Id", "ParentId", "TypeId", "Name", "CreationTime", "UpdatedTime", "CreatedBy", "UpdatedBy"]
        qs = {"$top": params.top, "$skip": params.skip}
        if params.filter.strip():
            qs["$filter"] = params.filter
        data = await _ome_get("GroupService/Groups", qs, include=include_fields)
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
        include_fields = ["Id", "Type", "DeviceName", "Model", "DeviceServiceTag"]
        data = await _ome_get(f"GroupService/Groups({params.group_id})/Devices", include=include_fields)
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
    """List OpenManage Enterprise (OME) alerts with optional pagination and filters.

    Use this tool when users want to view, check, list, or filter system logs and hardware alerts.
    You must construct a valid OData string in the `filter` argument using the value maps below.

    Valid Mappings (Convert user text to these integer/string IDs in the filter string):
    - SeverityType: WARNING='8', CRITICAL='16', INFO='2', NORMAL='4', UNKNOWN='1'
    - StatusType: NORMAL='1000', UNKNOWN='2000', WARNING='3000', CRITICAL='4000', NOSTATUS='5000'
    - AlertDeviceType: SERVER='1000', CHASSIS='2000', NETWORK_CONTROLLER='9000', NETWORK_IOM='4000', STORAGE='3000', STORAGE_IOM='8000'
    - CategoryName: AUDIT=4, MISCELLANEOUS=7, STORAGE=2, SYSTEM_HEALTH=1, UPDATES=3, WORK_NOTES=6, CONFIGURATION=5

    Args:
        params: The pagination and OData filter parameters object.

    Returns:
        str: A JSON string containing the count and a list of matching alert records.
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

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "ok"})

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Choose transport mode from environment: "http" (default) or "stdio".
    TRANSPORT_MODE = os.getenv("MCP_TRANSPORT_MODE", "http").lower()
    OME_MCP_PORT = int(os.getenv("OME_MCP_PORT", "8080"))
    logger.info("starting MCP with TRANSPORT_MODE=%s", TRANSPORT_MODE)
    if TRANSPORT_MODE == "stdio":
        # Run over stdio (useful for running as a direct process)
        mcp.run(transport="stdio")
    else:
        # Default: run over HTTP so the container keeps running and listens on port 8080.
        mcp.run(transport="http", host="0.0.0.0", port=OME_MCP_PORT)
