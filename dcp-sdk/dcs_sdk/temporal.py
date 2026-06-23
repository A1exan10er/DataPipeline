"""Temporal connection and task queue helpers."""

from __future__ import annotations

import os
from typing import Any

from dcs_sdk.config import get_section


def get_temporal_target() -> str:
    return str(get_section("temporal").get("target_host", "127.0.0.1:7233"))


def get_task_queue_env() -> str:
    return str(get_section("temporal").get("task_queue_env", "TEMPORAL_TASK_QUEUE"))


def resolve_task_queue(
    *,
    payload_task_queue: str | None = None,
    identity: dict[str, Any] | None = None,
    default: str = "episode-queue",
) -> str:
    if payload_task_queue:
        return payload_task_queue

    env_value = os.getenv(get_task_queue_env(), "").strip()
    if env_value:
        return env_value

    if identity and identity.get("temporal_task_queue"):
        return str(identity["temporal_task_queue"])

    return default
