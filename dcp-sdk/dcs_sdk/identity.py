"""Collector identity and device helpers shared across DCS processes."""

from __future__ import annotations

import json
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dcs_sdk.config import get_section, project_root


DEFAULT_IDENTITY_PATH = project_root() / "collector" / ".collector_identity.json"
COLLECTOR_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")

ROBOT_DEVICE_TYPES = {
    "arx5": "ARX X5",
    "aloha": "PiPER",
    "flexiv": "Flexiv Rizon 4",
    "franka": "Franka Research 3",
    "simulate": "virtual",
    "umi": "UMI Headless",
    "ur": "UR5e",
}
SERVER_DEVICE_TYPES = {
    "unknown",
    "Flexiv Rizon 4",
    "Franka Research 3",
    "Franka Panda 7-DOF",
    "PiPER",
    "Piper 6-DOF Bimanual",
    "ARX X5",
    "UR",
    "UR5e",
    "UR3e",
    "UR7e",
    "UMI Headless",
    "virtual",
}


def sanitize_collector_id(raw: str) -> str:
    value = COLLECTOR_ID_RE.sub("-", (raw or "").strip()).strip("-._")
    return value or "collector-unknown"


def _host_from_target(target: str | None) -> str | None:
    if not target:
        return None
    value = str(target).strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"//{value}")
    return parsed.hostname or value.split(":", 1)[0]


def get_local_ip(target: str | None = None) -> str:
    """Return the local address used to reach the target service.

    Multi-NIC collectors should identify themselves by the interface that can
    reach DCP, not by the default internet route.
    """
    target_host = _host_from_target(target)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target_host or "8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_local_ip_for_server() -> str:
    server_url = str(get_section("server").get("api_base_url") or "")
    return get_local_ip(server_url)


def temporal_task_queue_for(collector_id: str) -> str:
    return f"episode-queue-{sanitize_collector_id(collector_id)}"


def load_or_create_collector_identity(
    path: Path = DEFAULT_IDENTITY_PATH,
) -> dict[str, Any]:
    env_id = os.getenv("COLLECTOR_ID", "").strip()
    now = datetime.now(timezone.utc).isoformat()

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("collector_id"):
                if env_id and sanitize_collector_id(env_id) != data["collector_id"]:
                    data["collector_id"] = sanitize_collector_id(env_id)
                    data["updated_at"] = now
                    _write_identity(path, data)
                return _normalize_identity(data)
        except Exception:
            pass

    hostname = socket.gethostname()
    collector_id = sanitize_collector_id(
        env_id or f"collector-{hostname}-{uuid.uuid4().hex[:8]}"
    )
    data = {
        "collector_id": collector_id,
        "hostname": hostname,
        "created_at": now,
        "updated_at": now,
    }
    _write_identity(path, data)
    return _normalize_identity(data)


def load_or_create_identity(path: Path = DEFAULT_IDENTITY_PATH) -> dict[str, Any]:
    return load_or_create_collector_identity(path)


def _normalize_identity(data: dict[str, Any]) -> dict[str, Any]:
    collector_id = sanitize_collector_id(str(data["collector_id"]))
    hostname = str(data.get("hostname") or socket.gethostname())
    return {
        **data,
        "collector_id": collector_id,
        "hostname": hostname,
        "temporal_task_queue": temporal_task_queue_for(collector_id),
    }


def _write_identity(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def configured_collector_device_type(default: str = "unknown") -> str:
    return str(get_section("collector").get("collector_device_type") or default)


def device_type_for_robot(robot_name: str, fallback: str | None = None) -> str:
    robot_key = (robot_name or "").strip().lower()
    if robot_key:
        return ROBOT_DEVICE_TYPES.get(robot_key, "unknown")

    fallback_value = fallback or configured_collector_device_type()
    return fallback_value if fallback_value in SERVER_DEVICE_TYPES else "unknown"
