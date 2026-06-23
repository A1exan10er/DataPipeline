"""Collector control event contracts shared by DCP and dc."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


COLLECTOR_UPDATE_REQUESTED = "collector.update.requested"
COLLECTOR_UPDATE_ACKED = "collector.update.acked"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CollectorUpdateRequestedPayload(BaseModel):
    collector_id: str
    device_id: str
    temporal_task_queue: str | None = None
    computer_ip: str = ""
    hostname: str | None = None
    is_running: bool = False
    current_user_id: str | None = None
    current_user_username: str | None = None
    current_task_id: str | None = None
    current_task_title: str | None = None
    update_version: str | None = None
    message: str = ""
    force: bool = False
    requested_by: str
    requested_at: str = Field(default_factory=utc_now_iso)


class CollectorUpdateAckedPayload(BaseModel):
    collector_id: str
    update_version: str | None = None
    status: str
    message: str = ""
    device_id: str = ""
    requested_by: str = ""
    current_state: str = ""
    git_branch: str = ""
    git_stdout: str = ""
    git_stderr: str = ""
    dirty_files: list[str] = Field(default_factory=list)
    acked_at: str = Field(default_factory=utc_now_iso)
