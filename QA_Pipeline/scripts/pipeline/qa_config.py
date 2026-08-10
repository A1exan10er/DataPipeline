"""Central configuration loading for QA pipeline checks."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "quality_rules.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "task_catalog": {
        "enabled": True,
        "url": "http://192.168.50.209:7777/api/v1/admin/tasks/get_all_task_time_range_public",
        "timeout_seconds": 5.0,
        "fallback_path": "configs/task_time_ranges_fallback.json",
    },
    "phase1_metadata": {
        "checksum": {
            "required": True,
            "verify_hashes": False,
            "accepted_algorithms": ["sha256", "md5"],
            "max_missing_paths_in_details": 50,
        },
        "modalities": {
            "ignore_flow_modalities": True,
            "unknown_modality_status": "pass",
            "singular_action_status": "pass",
        },
        "task_robot_tokens": {
            "umi": ["umi"],
            "ur": ["ur", "ur5", "ur5e"],
            "arx": ["arx", "arx5"],
            "arx5": ["arx", "arx5"],
            "flexiv": ["flexiv"],
            "aloha": ["aloha"],
        },
        "robot_device_categories": {
            "single_arm": ["ur", "ur5", "ur5e", "ur7e", "franka", "fr3", "fr3v2", "flexiv", "flexiv_rizon4"],
            "dual_arm": ["arx", "arx5", "aloha", "piper", "aloha_piper"],
            "umi": ["umi"],
        },
        "quality_labels": {
            "normal_labels": ["完全正常"],
            "allowed_non_error_labels": ["测试任务"],
            "missing_status": "fail",
            "unexpected_status": "needs_review",
        },
    },
    "phase2_duration": {
        "length_alignment": {
            "max_video_action_difference": 3,
        },
        "extreme_long": {
            "enabled": True,
            "group_by": ["task", "robot"],
            "min_group_size": 5,
            "ratio_fail": 4.0,
            "iqr_distance_fail": 5.0,
        },
        "training_duration_limit": {
            "enabled": True,
            "default_max_duration_seconds": 120.0,
            "fail_offset_seconds": 30.0,
            "demo_exempt": True,
            "task_overrides": {},
        },
    },
    "phase3_timestamp": {
        "abnormal_fps": {
            "loss_fail_ratio": 0.10,
            "normal_video_loss_fail_ratio": 0.10,
            "tactile_video_loss_fail_ratio": 0.15,
            "gain_warning_ratio": 0.10,
        },
        "frame_drops": {
            "normal_video_drop_ratio_fail": 0.10,
            "tactile_video_drop_ratio_fail": 0.15,
            "max_consecutive_fail": 25,
            "max_consecutive_warn": 10,
        },
    },
    "phase5_robot_state": {
        "gripper": {
            "mean_remap_threshold_m": 0.005,
        },
        "robots": {
            "aloha": {
                "gripper_limits_m": [0.0, 0.1],
            },
            "arx5": {
                "gripper_limits_m": [0.0, 0.082],
            },
        },
    },
    "phase6_umi_processing": {
        "enabled": True,
        "profile": "full",
        "output_root": "outputs/umi_processed",
        "suffix": "_w_world_base",
        "video_only_suffix": "_video_aligned",
        "overwrite": True,
        "keep_intermediate": False,
        "skip_assessment": False,
        "assessment_args": "",
        "fps": None,
        "status_for_repaired": "warning",
        "trajectory_first_gate": True,
        "trajectory_pass_labels": ["smooth"],
        "trajectory_nonpass_status": "fail",
        "run_executability": False,
        "video": {
            "ffmpeg_threads": 2,
            "preprocess_preset": "veryfast",
            "output_mode": "derived_copy",
            "sidecar_filename": "video_rotated_180.mp4",
            "orientation_correction": {
                "observation.image.left_wrist_view": 180,
                "observation.image.right_wrist_view": 180,
            },
        },
        "ik": {
            "robots": None,
            "arm": "both",
            "source": "action",
            "max_points": 200,
            "min_segment": 5,
            "jobs": 1,
            "samples": 80000,
            "extra_args": "",
        },
        "umi_tokens": ["umi"],
        "required_modalities": [],
        "preprocess_config": None,
        "smooth_config": None,
        "transform_config": None
    },
    "phase7_standstill": {
        "enabled": True,
        "motion_delta_threshold": 0.001,
        "stillness_buffer_ms": 5000,
        "warn_segment_ms": 5000,
        "review_segment_ms": 10000,
        "fail_segment_ms": 30000,
        "review_excess_ratio": 0.20,
        "fail_excess_ratio": 0.40,
        "edge_tolerance_ms": 1000,
        "min_useful_motion_ms": 5000,
        "source_modalities": [
            "observation.state.joint_position",
            "actions.joint_position",
            "observation.state.eef_pose",
            "actions.eef_pose",
            "action.eef_pose",
        ],
    },
    "phase7_slow_motion": {
        "enabled": True,
        "observation_modality": "observation.state.eef_pose",
        "action_modalities": ["actions.eef_pose", "action.eef_pose"],
        "window_seconds": 1.0,
        "min_speed_cm_s": 0.1,
        "max_speed_cm_s": 3.0,
        "merge_gap_seconds": 2.0,
        "min_duration_seconds": 15.0,
        "min_segment_path_cm": 1.5,
        "active_hand_min_path_cm": 1.0,
        "active_hand_min_ratio": 0.2,
        "action_slow_fraction": 0.8,
        "action_normal_fraction": 0.8,
        "alignment_tolerance_ms": 500.0,
        "gripper_activity_gate": True,
        "gripper_window_min_motion": 0.03,
        "gripper_window_min_avg_speed": 0.002,
        "gripper_slow_min_duration_seconds": 15.0,
        "joint_rotation_min_window_motion": 1.5,
        "joint_rotation_min_avg_speed": 0.1,
        "raw_rotation_task_patterns": ["*rice*", "*pour*", "*tea*", "*cup*", "*water*"],
        "raw_rotation_min_window_motion": 1.0,
        "raw_rotation_min_avg_speed": 0.07,
        "task_overrides": {},
    },
    "standstill_trim": {
        "enabled": True,
        "motion_delta_threshold_rad": 0.001,
        "standstill_min_duration_ms": 5000,
        "edge_tolerance_ms": 1000,
        "keep_context_ms": 500,
        "min_remaining_duration_ms": 5000,
        "max_trim_ratio": 0.40,
        "source_modalities": [
            "observation.state.joint_position",
            "actions.joint_position",
            "observation.state.eef_pose",
            "actions.eef_pose",
            "action.eef_pose",
        ],
    },
    "dcs_notifications": {
        "enabled": False,
        "dry_run": False,
        "wait": False,
        "event_name": "qa.episode_abnormal.detected",
        "notify_statuses": ["fail"],
        "actionable_statuses": ["needs_review"],
        "actionable_check_names": [
            "abnormal_fps_loss",
            "frame_drop_ratio",
            "duration_under_5s",
            "modality_alignment_start",
            "modality_alignment_end",
            "standstill_segment",
            "excessive_standstill",
            "trajectory_not_smooth",
            "ik_executability_failed",
        ],
        "exclude_check_names": ["timestamps_raw_inconsistency", "quality_label_unexpected"],
    },
}


@lru_cache(maxsize=1)
def load_quality_config() -> dict[str, Any]:
    """Load QA config from JSON, falling back to built-in defaults.

    Set QA_PIPELINE_CONFIG to use a different config file for experiments or
    server deployment.
    """
    config_path = Path(os.environ.get("QA_PIPELINE_CONFIG", DEFAULT_CONFIG_PATH))
    config = _deep_copy(DEFAULT_CONFIG)
    try:
        with config_path.open("r", encoding="utf-8") as file_obj:
            user_config = json.load(file_obj)
    except FileNotFoundError:
        return config
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load QA config: {config_path}: {exc}") from exc
    if not isinstance(user_config, dict):
        raise RuntimeError(f"QA config must contain a JSON object: {config_path}")
    _deep_update(config, user_config)
    return config


def config_value(path: list[str], default: Any) -> Any:
    """Return a nested config value, or default when missing."""
    current: Any = load_quality_config()
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _deep_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))
