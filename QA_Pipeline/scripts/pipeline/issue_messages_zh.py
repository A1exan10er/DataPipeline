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
LEGACY_TRANSLATIONS_PATH = REPO_ROOT / "scripts" / "issue_translations.json"

# This catalog is deliberately independent from detector implementation.  It is
# the last Chinese title fallback for historical databases and newly generated
# reports when a richer rule entry has not yet been configured.
BUILTIN_ISSUE_TITLES_ZH = {
    "action_modality_singular_name": "动作模态目录命名不规范",
    "both_wrist_views_still": "双腕相机画面同时静止",
    "checksum_hash_mismatch": "文件校验值不一致",
    "checksum_hash_unreadable": "文件校验值无法读取",
    "checksum_manifest_file_empty": "校验清单中的文件为空",
    "checksum_manifest_file_missing": "校验清单中的文件缺失",
    "checksum_manifest_invalid": "校验清单格式无效",
    "checksum_manifest_missing": "缺少文件校验清单",
    "checksum_manifest_path_not_file": "校验清单路径不是文件",
    "consecutive_drops_outlier": "连续丢帧异常偏高",
    "csv_not_parseable": "CSV 数据无法解析",
    "duplicate_timestamps": "时间戳重复",
    "duration_absolute_too_long": "episode 时长过长",
    "duration_absolute_too_short": "episode 时长过短",
    "duration_extreme_long": "episode 时长极端过长",
    "duration_frames_fps_inconsistent": "时长、帧数与帧率不一致",
    "duration_not_positive": "episode 时长不是正数",
    "duration_task_outlier": "episode 时长相对任务异常",
    "duration_training_limit_exceeded": "episode 时长超过训练上限",
    "duration_under_5s": "episode 时长低于 5 秒",
    "eef_position_step_too_large": "末端位置单帧跳变过大",
    "episode_folder_name": "episode 目录名称不规范",
    "equipment_motion_response_slow": "设备运动响应偏慢",
    "equipment_motion_response_stalled": "设备运动响应停滞",
    "frame_drop_consecutive": "连续丢帧过多",
    "frame_drop_ratio": "视频丢帧比例过高",
    "frequency_deviation": "采集频率偏离配置",
    "frequency_group_outlier": "采集频率与同组数据差异过大",
    "gripper_mean_too_low_remap_needed": "夹爪数值范围疑似需要重映射",
    "gripper_out_of_limits": "夹爪数据超出允许范围",
    "gripper_step_too_large": "夹爪数据单帧跳变过大",
    "hidden_directory_skipped": "发现并跳过隐藏目录",
    "jitter_high": "机器人状态抖动过大",
    "joint_acceleration_high": "关节加速度过高",
    "joint_columns_not_detected": "未识别到关节数据列",
    "joint_nan_inf": "关节数据包含无效数值",
    "joint_out_of_limits": "关节位置超出允许范围",
    "joint_step_too_large": "关节位置单帧跳变过大",
    "joint_velocity_exceeded": "关节速度超过允许范围",
    "metadata_exists": "metadata.json 缺失",
    "metadata_valid_json": "metadata.json 格式错误",
    "modality_alignment_end": "多模态结尾未对齐",
    "modality_alignment_start": "多模态开头未对齐",
    "modality_folder_missing": "模态数据目录缺失",
    "modality_frame_count_misaligned": "不同模态帧数不一致",
    "operator_standstill": "操作过程静止过长",
    "operator_standstill_excessive": "总静止占比过高",
    "operator_standstill_leading": "开头静止过长",
    "operator_standstill_middle": "中段静止过长",
    "operator_standstill_motion_too_short": "有效运动时长过短",
    "operator_standstill_trailing": "结尾静止过长",
    "operator_sustained_slow_motion": "操作员持续低速运动",
    "parent_path_structure": "episode 上级目录结构不规范",
    "quality_label_unexpected": "采集质量标签非正常",
    "quality_labels_missing": "缺少采集质量标签",
    "required_metadata_field": "metadata.json 必需字段无效",
    "required_modality_file_empty": "必需模态文件为空",
    "required_modality_file_missing": "必需模态文件缺失",
    "robot_config_fallback": "机器人使用了默认质检配置",
    "robot_device_category_unknown": "无法识别机器人设备类别",
    "slow_motion_cause_uncertain": "持续低速原因不确定",
    "standstill_cause_uncertain": "静止原因不确定",
    "standstill_motion_source_invalid": "静止检测运动源无效",
    "standstill_motion_source_missing": "静止检测运动源缺失",
    "state_csv_row_count_mismatch": "状态数据行数与预期不一致",
    "task_device_reference_missing": "缺少任务设备参考信息",
    "task_device_reference_unavailable": "任务设备参考目录不可用",
    "task_folder_metadata_mismatch": "任务目录与 metadata 记录不一致",
    "task_robot_mismatch": "任务与机器人类型不匹配",
    "timestamp_large_gap": "时间戳存在过大间隔",
    "timestamps_missing_or_unparseable": "时间戳缺失或无法解析",
    "timestamps_not_monotonic": "时间戳顺序异常",
    "timestamps_raw_inconsistency": "原始时间戳不一致",
    "timestamps_row_count_mismatch": "时间戳行数与预期不一致",
    "timestamps_unreadable": "时间戳文件无法读取",
    "total_frames_not_positive": "总帧数不是正数",
    "umi_not_applicable": "当前 episode 不适用 UMI 检查",
    "umi_phase6_deferred": "UMI Phase 6 处理已转交独立任务",
    "umi_processed_passed": "UMI 数据处理通过",
    "umi_processed_repaired": "UMI 数据修复后通过",
    "umi_processing_disabled": "UMI 数据处理未启用",
    "umi_processing_error": "UMI 数据处理失败",
    "umi_processing_rejected": "UMI 数据处理未通过",
    "umi_trajectory_gate_rejected": "UMI 轨迹质量未通过",
    "umi_video_orientation_passed": "UMI 视频方向检查通过",
    "unknown_modality_detected": "发现未识别的数据模态",
    "video_black_frames": "视频存在黑帧",
    "video_duration_mismatch": "视频时长与 episode 不一致",
    "video_frame_count_mismatch": "视频帧数与预期不一致",
    "video_frame_count_unreadable": "视频帧数无法读取",
    "video_frozen": "视频画面长时间冻结",
    "video_not_openable": "视频文件无法打开",
    "video_resolution_mismatch": "视频分辨率不符合要求",
    "video_white_frames": "视频存在白帧",
}


@lru_cache(maxsize=1)
def load_issue_messages() -> dict[str, Any]:
    return _load_json(ISSUE_MESSAGES_PATH)


@lru_cache(maxsize=1)
def load_work_session_config() -> dict[str, Any]:
    return _load_json(WORK_SESSION_CONFIG_PATH)


@lru_cache(maxsize=1)
def load_rule_explanations_zh() -> dict[str, Any]:
    return _load_json(RULE_EXPLANATIONS_PATH)


@lru_cache(maxsize=1)
def load_legacy_translations_zh() -> dict[str, Any]:
    return _load_json(LEGACY_TRANSLATIONS_PATH)


def issue_title_zh(check_name: str) -> str:
    check_name = str(check_name or "unknown")
    message_rule = _specific_message_rule(check_name)
    if message_rule.get("title_zh"):
        return str(message_rule["title_zh"])
    action = _issue_action(check_name)
    if action.get("label"):
        return str(action["label"])
    rule = _specific_rule_explanation(check_name)
    if rule.get("title_zh"):
        return str(rule["title_zh"])
    if check_name in BUILTIN_ISSUE_TITLES_ZH:
        return BUILTIN_ISSUE_TITLES_ZH[check_name]
    return f"未配置的问题类型（{check_name}）"


def issue_summary_zh(event_or_finding: dict[str, Any]) -> str:
    check_name = str(event_or_finding.get("check_name") or "unknown")
    message_rule = _specific_message_rule(check_name)
    if message_rule.get("summary_zh"):
        return str(message_rule["summary_zh"])
    rule = _specific_rule_explanation(check_name)
    if rule.get("description_zh"):
        return str(rule["description_zh"])
    legacy = load_legacy_translations_zh().get(check_name)
    if legacy:
        return str(legacy)
    default = (load_issue_messages().get("default") or {}).get("summary_zh")
    template = str(default or "检测到“{issue_title}”，该 episode 的结果不符合此项质检要求。")
    return template.format(check_name=check_name, issue_title=issue_title_zh(check_name))


def recommended_action_zh(event_or_finding: dict[str, Any]) -> str:
    check_name = str(event_or_finding.get("check_name") or "unknown")
    message_rule = _specific_message_rule(check_name)
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
        "observation_source_modality",
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


def issue_time_range_zh(details: dict[str, Any]) -> str:
    """Return the most useful finding time range without assuming one details schema."""
    candidates = [details]
    for key in ("longest_segment", "segment", "cause_evidence"):
        value = details.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    segments = details.get("segments")
    if isinstance(segments, list):
        candidates.extend(item for item in segments if isinstance(item, dict))
    for item in candidates:
        start_ms = _first_number(item, "start_ms", "stop_start_ms", "start_time_ms")
        end_ms = _first_number(item, "end_ms", "stop_end_ms", "end_time_ms")
        if start_ms is not None and end_ms is not None:
            return f"{start_ms / 1000:.2f}–{end_ms / 1000:.2f} 秒"
        start_s = _first_number(item, "start_seconds", "start_time_seconds")
        end_s = _first_number(item, "end_seconds", "end_time_seconds")
        if start_s is not None and end_s is not None:
            return f"{start_s:.2f}–{end_s:.2f} 秒"
    location = str(details.get("location") or "")
    return {"leading": "开头", "middle": "中段", "trailing": "结尾"}.get(location, "")


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
    time_range = issue_time_range_zh(details)
    if time_range:
        parts.append(f"发生区间：{time_range}")
    detail_sources = [details]
    longest_segment = details.get("longest_segment")
    if isinstance(longest_segment, dict):
        detail_sources.append(longest_segment)
    for key, label in (
        ("actual_fps", "实际帧率"),
        ("expected_fps", "期望帧率"),
        ("deviation_ratio", "帧率偏离比例"),
        ("drop_ratio", "丢帧比例"),
        ("violation_ratio", "异常占比"),
        ("duplicate_ratio", "重复占比"),
        ("threshold", "阈值"),
        ("warning_threshold", "警告阈值"),
        ("review_threshold", "复查阈值"),
        ("fail_threshold", "失败阈值"),
        ("loss_fail_ratio", "失败阈值"),
        ("normal_video_loss_fail_ratio", "普通视频失败阈值"),
        ("tactile_video_loss_fail_ratio", "触觉视频失败阈值"),
        ("max_consecutive_drops", "最大连续丢帧数"),
        ("duration_ms", "持续时间"),
        ("duration_seconds", "持续时间"),
        ("duration", "实际时长"),
        ("expected_duration_seconds", "期望时长"),
        ("group_median_seconds", "同组中位时长"),
        ("task_median", "同任务中位时长"),
        ("ratio_to_median", "相对中位数倍数"),
        ("ratio", "实际值与参考值比例"),
        ("threshold_ratio", "报告阈值比例"),
        ("excess_ms", "超出缓冲时间"),
        ("episode_duration_ms", "episode 时长"),
        ("useful_motion_ms", "有效运动时长"),
        ("excess_ratio", "静止超出占比"),
        ("segment_count", "静止段数量"),
        ("row_count", "数据行数"),
        ("expected_rows", "期望行数"),
        ("actual_frames", "实际帧数"),
        ("expected_frames", "期望帧数"),
        ("frame_count", "帧数"),
        ("actual_count", "实际数量"),
        ("expected_count", "期望数量"),
        ("spread_ms", "模态时间差"),
        ("max_gap_ms", "最大时间间隔"),
        ("violation_count", "异常点数量"),
        ("max_step", "最大单帧变化"),
        ("max_velocity", "最大速度"),
        ("max_acceleration", "最大加速度"),
        ("missing_count", "缺失数量"),
        ("black_ratio", "黑帧占比"),
        ("white_ratio", "白帧占比"),
        ("frozen_ratio", "冻结帧占比"),
        ("path_cm", "区间运动距离"),
        ("avg_speed_cm_s", "平均速度"),
    ):
        for source in detail_sources:
            if key in source:
                parts.append(f"{label}：{format_detail_value(key, source.get(key))}")
                break
    return "；".join(parts[:10])


def trigger_reason_zh(event_or_finding: dict[str, Any]) -> str:
    """Explain why the finding was emitted, favoring concrete evidence over jargon."""
    details = event_or_finding.get("details")
    details = details if isinstance(details, dict) else {}
    evidence = details_summary_zh(details)
    if evidence:
        return f"检测结果达到或超过该规则的报告条件。{evidence}"
    rule = _specific_rule_explanation(str(event_or_finding.get("check_name") or "unknown"))
    standard = str(rule.get("standard_zh") or "").strip()
    if standard:
        return standard
    return "该检测项返回了非通过状态，当前记录未包含可量化阈值，请由 QA 结合原始 finding 复查。"


def finding_presentation_zh(event_or_finding: dict[str, Any]) -> dict[str, str]:
    """Build one stable, human-facing Chinese representation for every consumer."""
    details = event_or_finding.get("details")
    details = details if isinstance(details, dict) else {}
    return {
        "issue_title_zh": issue_title_zh(str(event_or_finding.get("check_name") or "")),
        "occurrence_zh": issue_summary_zh(event_or_finding),
        "trigger_reason_zh": trigger_reason_zh(event_or_finding),
        "location_zh": issue_location_zh(details),
        "time_range_zh": issue_time_range_zh(details),
        "evidence_summary_zh": details_summary_zh(details),
        "recommended_action_zh": recommended_action_zh(event_or_finding),
    }


def notification_message_zh(event: dict[str, Any]) -> str:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    presentation = finding_presentation_zh({**event, "details": details})
    lines = [
        f"质检发现：{presentation['issue_title_zh']}",
        f"任务：{event.get('task') or '未填写'}",
        f"Episode：{Path(str(event.get('episode_path') or '')).name or '未填写'}",
        f"设备：{collector_id_from_episode_path(str(event.get('episode_path') or '')) or '未填写'}",
    ]
    location = presentation["location_zh"]
    if location:
        lines.append(f"位置：{location}")
    if presentation["time_range_zh"]:
        lines.append(f"发生区间：{presentation['time_range_zh']}")
    lines.append(f"发生情况：{presentation['occurrence_zh']}")
    lines.append(f"报告原因：{presentation['trigger_reason_zh']}")
    evidence = presentation["evidence_summary_zh"]
    if evidence:
        lines.append(f"证据：{evidence}")
    lines.append(f"处理建议：{presentation['recommended_action_zh']}")
    return "\n".join(lines)


def enrich_details_for_notification(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    enriched = dict(details)
    original_message = str(event.get("message") or "")
    if original_message:
        enriched.setdefault("original_message", original_message)
    presentation = finding_presentation_zh(event)
    enriched.update(presentation)
    enriched["message_zh"] = notification_message_zh(event)
    summary = details_summary_zh(enriched)
    if summary:
        enriched["details_summary_zh"] = summary
    return enriched


def format_detail_value(key: str, value: Any) -> str:
    number = _float_or_none(value)
    if number is not None:
        if key.endswith("_ratio") or key in {
            "ratio",
            "threshold",
            "warning_threshold",
            "review_threshold",
            "fail_threshold",
            "loss_fail_ratio",
            "normal_video_loss_fail_ratio",
            "tactile_video_loss_fail_ratio",
        }:
            return f"{number * 100:.1f}%"
        if key.endswith("_ms"):
            return f"{number:.0f} ms"
        if key.endswith("_fps"):
            return f"{number:.2f} fps"
        if key.endswith("_seconds"):
            return f"{number:.2f} 秒"
        if key in {"duration", "task_median"}:
            return f"{number:.2f} 秒"
        if key.endswith("_cm_s"):
            return f"{number:.2f} cm/s"
        if key.endswith("_cm"):
            return f"{number:.2f} cm"
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


def _specific_message_rule(check_name: str) -> dict[str, Any]:
    data = load_issue_messages()
    specific = (data.get("rules") or {}).get(check_name)
    return dict(specific) if isinstance(specific, dict) else {}


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


def _specific_rule_explanation(check_name: str) -> dict[str, Any]:
    specific = (load_rule_explanations_zh().get("rules") or {}).get(check_name)
    return dict(specific) if isinstance(specific, dict) else {}


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


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(data.get(key))
        if value is not None:
            return value
    return None
