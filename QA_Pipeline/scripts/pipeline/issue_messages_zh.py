"""Chinese issue messages shared by notifications and reports."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_MESSAGES_PATH = REPO_ROOT / "configs" / "issue_messages_zh.json"
WORK_SESSION_CONFIG_PATH = REPO_ROOT / "configs" / "work_session_report.json"
RULE_EXPLANATIONS_PATH = REPO_ROOT / "configs" / "report_rule_explanations_zh.json"


@lru_cache(maxsize=1)
def load_issue_messages() -> dict[str, Any]:
    return _load_json(ISSUE_MESSAGES_PATH)


@lru_cache(maxsize=1)
def load_work_session_config() -> dict[str, Any]:
    return _load_json(WORK_SESSION_CONFIG_PATH)


@lru_cache(maxsize=1)
def load_rule_explanations_zh() -> dict[str, Any]:
    return _load_json(RULE_EXPLANATIONS_PATH)


def issue_title_zh(check_name: str) -> str:
    check_name = str(check_name or "unknown")
    message_rule = _message_rule(check_name)
    if message_rule.get("title_zh"):
        return str(message_rule["title_zh"])
    action = _issue_action(check_name)
    if action.get("label"):
        return str(action["label"])
    rule = _rule_explanation(check_name)
    if rule.get("title_zh"):
        return str(rule["title_zh"])
    return check_name


def issue_summary_zh(event_or_finding: dict[str, Any]) -> str:
    check_name = str(event_or_finding.get("check_name") or "unknown")
    message_rule = _message_rule(check_name)
    if message_rule.get("summary_zh"):
        return str(message_rule["summary_zh"])
    rule = _rule_explanation(check_name)
    if rule.get("description_zh"):
        return str(rule["description_zh"])
    default = (load_issue_messages().get("default") or {}).get("summary_zh")
    return str(default or "该 episode 触发了质检异常，请查看质检详情确认具体原因。")


def recommended_action_zh(event_or_finding: dict[str, Any]) -> str:
    check_name = str(event_or_finding.get("check_name") or "unknown")
    message_rule = _message_rule(check_name)
    if message_rule.get("operator_action_zh"):
        return str(message_rule["operator_action_zh"])
    action = _issue_action(check_name)
    if action.get("action"):
        return str(action["action"])
    default = (load_issue_messages().get("default") or {}).get("operator_action_zh")
    return str(default or "请联系现场负责人或 QA 人员确认处理方式。")


def issue_location_zh(details: dict[str, Any]) -> str:
    raw_location = issue_location_raw(details)
    if not raw_location:
        return ""
    readable = readable_location_zh(raw_location)
    if readable and readable != raw_location:
        return f"{readable}（{raw_location}）"
    return raw_location


def issue_location_raw(details: dict[str, Any]) -> str:
    for key in (
        "modality",
        "source_modality",
        "column",
        "earliest_modality",
        "latest_modality",
        "file",
        "missing_file",
    ):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in details.values():
        if isinstance(value, dict):
            nested = issue_location_raw(value)
            if nested:
                return nested
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested = issue_location_raw(item)
                    if nested:
                        return nested
    return ""


def readable_location_zh(location: str) -> str:
    value = str(location or "").strip()
    if not value:
        return ""
    mapping = {
        "observation.state.joint_position": "关节位置数据",
        "actions.joint_position": "动作关节位置数据",
        "actions": "动作数据",
        "observation.image.third_view": "第三视角相机",
        "observation.image.left_wrist_view": "左腕相机",
        "observation.image.right_wrist_view": "右腕相机",
        "observation.image.left_wrist_left_tactile": "左腕左侧触觉视频",
        "observation.image.left_wrist_right_tactile": "左腕右侧触觉视频",
        "observation.image.right_wrist_left_tactile": "右腕左侧触觉视频",
        "observation.image.right_wrist_right_tactile": "右腕右侧触觉视频",
    }
    if value in mapping:
        return mapping[value]
    if value.startswith("observation.image."):
        name = value.removeprefix("observation.image.")
        tokens = name.split("_")
        parts = []
        if "left" in tokens and "wrist" in tokens:
            parts.append("左腕")
        elif "right" in tokens and "wrist" in tokens:
            parts.append("右腕")
        elif "third" in tokens:
            parts.append("第三视角")
        if "tactile" in tokens:
            parts.append("触觉视频")
        elif "view" in tokens:
            parts.append("相机")
        return "".join(parts) if parts else value
    if value.startswith("observation.state."):
        return f"状态数据 {value.removeprefix('observation.state.')}"
    if value.startswith("action.") or value.startswith("actions."):
        return f"动作数据 {value.split('.', 1)[1] if '.' in value else value}"
    return value


def details_summary_zh(details: dict[str, Any]) -> str:
    if not isinstance(details, dict) or not details:
        return ""
    parts: list[str] = []
    location = issue_location_zh(details)
    if location:
        parts.append(f"位置：{location}")
    for key, label in (
        ("actual_fps", "实际帧率"),
        ("expected_fps", "期望帧率"),
        ("deviation_ratio", "帧率偏离比例"),
        ("drop_ratio", "丢帧比例"),
        ("threshold", "阈值"),
        ("loss_fail_ratio", "失败阈值"),
        ("normal_video_loss_fail_ratio", "普通视频失败阈值"),
        ("tactile_video_loss_fail_ratio", "触觉视频失败阈值"),
        ("max_consecutive_drops", "最大连续丢帧数"),
        ("duration_ms", "持续时间"),
        ("excess_ms", "超出缓冲时间"),
        ("episode_duration_ms", "episode 时长"),
        ("useful_motion_ms", "有效运动时长"),
        ("excess_ratio", "静止超出占比"),
        ("segment_count", "静止段数量"),
        ("row_count", "数据行数"),
    ):
        if key in details:
            parts.append(f"{label}：{format_detail_value(key, details.get(key))}")
    return "；".join(parts[:8])


def notification_message_zh(event: dict[str, Any]) -> str:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    lines = [
        f"质检发现：{issue_title_zh(str(event.get('check_name') or ''))}",
        f"任务：{event.get('task') or '未填写'}",
        f"Episode：{Path(str(event.get('episode_path') or '')).name or '未填写'}",
        f"设备：{collector_id_from_episode_path(str(event.get('episode_path') or '')) or '未填写'}",
    ]
    location = issue_location_zh(details)
    if location:
        lines.append(f"位置：{location}")
    lines.append(f"问题：{issue_summary_zh(event)}")
    evidence = details_summary_zh(details)
    if evidence:
        lines.append(f"证据：{evidence}")
    return "\n".join(lines)


def enrich_details_for_notification(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    enriched = dict(details)
    original_message = str(event.get("message") or "")
    if original_message:
        enriched.setdefault("original_message", original_message)
    enriched["issue_title_zh"] = issue_title_zh(str(event.get("check_name") or ""))
    enriched["message_zh"] = notification_message_zh(event)
    enriched["recommended_action_zh"] = recommended_action_zh(event)
    summary = details_summary_zh(enriched)
    if summary:
        enriched["details_summary_zh"] = summary
    return enriched


def format_detail_value(key: str, value: Any) -> str:
    number = _float_or_none(value)
    if number is not None:
        if key.endswith("_ratio") or key in {"threshold", "loss_fail_ratio", "normal_video_loss_fail_ratio", "tactile_video_loss_fail_ratio"}:
            return f"{number * 100:.1f}%"
        if key.endswith("_ms"):
            return f"{number:.0f} ms"
        if key.endswith("_fps"):
            return f"{number:.2f} fps"
        if number.is_integer():
            return str(int(number))
        return f"{number:.3f}"
    if isinstance(value, list):
        return "，".join(str(item) for item in value[:6])
    return str(value)


def collector_id_from_episode_path(path: str) -> str:
    for part in Path(path).parts:
        if part.startswith("collector-"):
            return part
    return ""


def _message_rule(check_name: str) -> dict[str, Any]:
    data = load_issue_messages()
    default = dict(data.get("default") or {})
    specific = (data.get("rules") or {}).get(check_name)
    if isinstance(specific, dict):
        default.update(specific)
    return default


def _issue_action(check_name: str) -> dict[str, Any]:
    config = load_work_session_config()
    default = dict(config.get("default_action") or {})
    specific = (config.get("issue_actions") or {}).get(check_name)
    if isinstance(specific, dict):
        default.update(specific)
    return default


def _rule_explanation(check_name: str) -> dict[str, Any]:
    config = load_rule_explanations_zh()
    default = dict(config.get("default") or {})
    specific = (config.get("rules") or {}).get(check_name)
    if isinstance(specific, dict):
        default.update(specific)
    return default


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
