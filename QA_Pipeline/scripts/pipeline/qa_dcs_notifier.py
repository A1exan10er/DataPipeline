"""Optional DCS event notifications for QA abnormalities."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.pipeline.qa_config import config_value


DEFAULT_EVENT_NAME = "qa.episode_abnormal.detected"
DEFAULT_NOTIFY_STATUSES = ["fail"]
DEFAULT_ACTIONABLE_CHECKS = [
    "abnormal_fps_loss",
    "frame_drop_ratio",
    "duration_under_5s",
    "modality_alignment_start",
    "modality_alignment_end",
    "standstill_segment",
    "excessive_standstill",
    "trajectory_not_smooth",
    "ik_executability_failed",
]


class DcsIssueNotifier:
    """Publish issue events to DCS while keeping QA pipeline best-effort."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.audit_path = self.run_dir / "dcs_notifications.jsonl"
        self.enabled = _bool_env("QA_DCS_NOTIFY_ENABLED", _config_bool("enabled", False))
        self.dry_run = _bool_env("QA_DCS_NOTIFY_DRY_RUN", _config_bool("dry_run", False))
        self.wait = _bool_env("QA_DCS_NOTIFY_WAIT", _config_bool("wait", False))
        self.event_name = os.environ.get(
            "QA_DCS_NOTIFY_EVENT",
            str(config_value(["dcs_notifications", "event_name"], DEFAULT_EVENT_NAME)),
        )
        self.notify_statuses = _string_list_env(
            "QA_DCS_NOTIFY_STATUSES",
            config_value(["dcs_notifications", "notify_statuses"], DEFAULT_NOTIFY_STATUSES),
        )
        self.actionable_statuses = _string_list_env(
            "QA_DCS_NOTIFY_ACTIONABLE_STATUSES",
            config_value(["dcs_notifications", "actionable_statuses"], ["needs_review"]),
        )
        self.actionable_checks = set(
            _string_list_env(
                "QA_DCS_NOTIFY_ACTIONABLE_CHECKS",
                config_value(["dcs_notifications", "actionable_check_names"], DEFAULT_ACTIONABLE_CHECKS),
            )
        )
        self.exclude_checks = set(
            _string_list_env(
                "QA_DCS_NOTIFY_EXCLUDE_CHECKS",
                config_value(["dcs_notifications", "exclude_check_names"], ["timestamps_raw_inconsistency"]),
            )
        )
        self._emit_event = None

    def notify_many(self, events: list[dict[str, Any]]) -> None:
        if not self.enabled:
            return
        for event in events:
            self.notify(event)

    def notify(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        decision = self._decision(event)
        payload = self._validated_payload(event)
        if decision != "publish":
            self._audit("skipped", payload, reason=decision)
            return
        if self.dry_run:
            self._audit("dry_run", payload)
            return
        try:
            emit_event = self._load_emit_event()
            future = emit_event(self.event_name, payload, wait=self.wait)
            self._audit("published", payload, future=str(future) if future is not None else "")
        except Exception as exc:
            self._audit("error", payload, error=str(exc), traceback=traceback.format_exc(limit=8))

    def _decision(self, event: dict[str, Any]) -> str:
        check_name = str(event.get("check_name") or "")
        status = str(event.get("status") or "").lower()
        if check_name in self.exclude_checks:
            return "excluded_check"
        if status in self.notify_statuses:
            return "publish"
        if status in self.actionable_statuses and check_name in self.actionable_checks:
            return "publish"
        return "filtered_status"

    def _payload(self, event: dict[str, Any]) -> dict[str, Any]:
        episode_path = str(event.get("episode_path") or "")
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        payload = {
            "event_schema": "qa_episode_abnormal_detected.v1",
            "event_key": self._event_key(event),
            "run_id": self.run_id,
            "finding_id": event.get("finding_id"),
            "episode_path": episode_path,
            "episode_name": Path(episode_path).name if episode_path else "",
            "task": event.get("task") or "",
            "date": event.get("date") or "",
            "operator": event.get("operator") or "",
            "robot": event.get("robot") or "",
            "controller": event.get("controller") or "",
            "collector_id": collector_id_from_episode_path(episode_path),
            "phase": event.get("phase"),
            "check_name": event.get("check_name") or "",
            "severity": event.get("severity") or "",
            "status": event.get("status") or "",
            "message": event.get("message") or "",
            "details": details,
            "detected_at": event.get("recorded_at") or _now(),
            "source": "qa_pipeline",
            "recommended_action": recommended_action(event),
        }
        return payload

    def _event_key(self, event: dict[str, Any]) -> str:
        raw = "|".join(
            [
                self.run_id,
                str(event.get("finding_id") or ""),
                str(event.get("episode_path") or ""),
                str(event.get("phase") or ""),
                str(event.get("check_name") or ""),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _load_emit_event(self):
        if self._emit_event is not None:
            return self._emit_event
        project_root = Path(__file__).resolve().parents[3]
        sdk_root = project_root / "dcp-sdk"
        if sdk_root.is_dir() and str(sdk_root) not in sys.path:
            sys.path.insert(0, str(sdk_root))
        from dcs_sdk import emit_event

        self._emit_event = emit_event
        return self._emit_event

    def _validated_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        model_cls = self._load_qa_payload_model()
        return model_cls.model_validate(self._payload(event)).model_dump(mode="json")

    def _load_qa_payload_model(self):
        project_root = Path(__file__).resolve().parents[3]
        sdk_root = project_root / "dcp-sdk"
        if sdk_root.is_dir() and str(sdk_root) not in sys.path:
            sys.path.insert(0, str(sdk_root))
        from dcs_sdk import QAEpisodeAbnormalPayload

        return QAEpisodeAbnormalPayload

    def _audit(self, status: str, payload: dict[str, Any], **extra: Any) -> None:
        row = {
            "recorded_at": _now(),
            "event_name": self.event_name,
            "status": status,
            "payload": payload,
            **extra,
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")


def collector_id_from_episode_path(path: str) -> str:
    for part in Path(path).parts:
        if part.startswith("collector-"):
            return part
    return ""


def recommended_action(event: dict[str, Any]) -> str:
    status = str(event.get("status") or "").lower()
    check_name = str(event.get("check_name") or "")
    if status == "fail":
        return "Please inspect this episode and consider recollecting it before continuing the same task."
    if check_name in {"standstill_segment", "excessive_standstill"}:
        return "Please remind the operator to avoid long pauses during collection."
    if check_name in {"abnormal_fps_loss", "frame_drop_ratio"}:
        return "Please check camera/network load and recollect if video quality is important."
    return "Please inspect this episode in the QA dashboard."


def _config_bool(key: str, default: bool) -> bool:
    return bool(config_value(["dcs_notifications", key], default))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _string_list_env(name: str, default: Any) -> list[str]:
    raw = os.environ.get(name)
    if raw is not None:
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(default, list):
        values = [str(item).strip() for item in default]
    else:
        values = [str(default).strip()]
    return [value for value in values if value]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
