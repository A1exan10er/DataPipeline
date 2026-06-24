"""Central configuration loading for QA pipeline checks."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "quality_rules.json"


DEFAULT_CONFIG: dict[str, Any] = {
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
    },
    "phase2_duration": {
        "length_alignment": {
            "max_video_action_difference": 3,
        },
    },
    "phase3_timestamp": {
        "abnormal_fps": {
            "loss_fail_ratio": 0.10,
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
        "output_root": "outputs/umi_processed",
        "suffix": "_w_world_base",
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
