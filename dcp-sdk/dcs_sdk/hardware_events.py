"""Shared hardware repair event contracts."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


CONFIG_ERROR_DETECTED = "hardware.config_error.detected"
REPAIR_STARTED = "hardware.repair.started"
REPAIR_COMPLETED = "hardware.repair.completed"
REPAIR_FAILED = "hardware.repair.failed"

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "api_key",
    "private_key",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_sensitive(value: Any) -> Any:
    """Return a copy with common credential fields redacted."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                clean[key] = "***REDACTED***"
            else:
                clean[key] = redact_sensitive(item)
        return clean
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def fingerprint_errors(errors: list[str]) -> str:
    normalized = "\n".join(sorted(str(item).strip() for item in errors if str(item).strip()))
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


class HardwareConfigErrorPayload(BaseModel):
    collector_id: str
    hostname: str = ""
    computer_ip: str = ""
    robot: str = ""
    device_type: str = "unknown"
    errors: list[str] = Field(default_factory=list)
    hardware_status: dict[str, Any] = Field(default_factory=dict)
    config_summary: dict[str, Any] = Field(default_factory=dict)
    log_excerpt: str = ""
    detected_at: str = Field(default_factory=utc_now_iso)
    error_fingerprint: str = ""


class RepairEventPayload(BaseModel):
    repair_id: str
    collector_id: str
    host: str = ""
    status: str
    category: str = ""
    action_plan: list[str] = Field(default_factory=list)
    audit: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""
    occurred_at: str = Field(default_factory=utc_now_iso)

