
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict

# ── Pydantic Input Models ─────────────────────────────────────────────────────
class PaginationInput(BaseModel):
    """Common pagination parameters."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    top: int = Field(default=50, description="Max records to return", ge=1, le=1000)
    skip: int = Field(default=0, description="Records to skip (for pagination)", ge=0)
    filter: str = Field(default="", description=(
            "An OData system query expression to narrow down results. "
            "Common Examples:\n"
            "- DeviceIdentifier eq 'ABC1234'\n"
            "- DeviceId eq 12345\n"
            "- DeviceName eq 'Prod-Server-01'\n"
            "- Model eq 'PowerEdge R640'\n"
            "- Type eq 1000"
        )
    )

class DeviceInput(BaseModel):
    """Device pagination parameters."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    top: int = Field(default=50, description="Max records to return", ge=1, le=1000)
    skip: int = Field(default=0, description="Records to skip (for pagination)", ge=0)
    filter: str = Field(default="", description=(
            "An OData system query expression to narrow down devices. "
            "You can ONLY filter using these fields: "
            "DeviceServiceTag, DeviceName, Id, Model, Type. "
            "String values must be enclosed in single quotes. "
            "Examples:\n"
            "- DeviceServiceTag eq 'ABC1234'\n"
            "- DeviceIdentifier eq 'ABC1234'\n"
            "- DeviceId eq 12345\n"
            "- DeviceName eq 'Prod-Server-01'\n"
            "- Model eq 'PowerEdge R740'\n"
            "- Type eq 1000\n"
        )
    )


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
    filter: str = Field(default="", description=(
            "An OData system query string to filter alerts. "
            "Available fields: Id, AlertDeviceId, AlertDeviceIdentifier, AlertDeviceType, "
            "StatusType, SeverityType, CategoryName, SubcategoryId, SubcategoryName, "
            "AlertDeviceName, Message. "
            "CRITICAL: You MUST use the exact ID mappings for filters. "
            "Examples:\n"
            "- SeverityType eq 16 (for CRITICAL)\n"
            "- StatusType eq 3000 (for WARNING)\n"
            "- AlertDeviceType eq 1000 (for SERVER)\n"
            "- CategoryName eq 1 (for SYSTEM_HEALTH)\n"
            "- Message eq 'Drive Failure'"
        ))

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
