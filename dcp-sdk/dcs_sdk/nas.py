"""NAS configuration helpers."""

from __future__ import annotations

from typing import Any

from dcs_sdk.config import get_section


def get_nas_config() -> dict[str, Any]:
    return get_section("nas")


def get_nas_upload_dict() -> dict[str, Any]:
    nas = get_nas_config()
    return {
        "host": nas.get("host", "127.0.0.1"),
        "user": nas.get("user", ""),
        "password": nas.get("password", ""),
        "port": int(nas.get("port", 22)),
        "staging_path": nas.get("staging_path", "/volume1/database/staging"),
    }
