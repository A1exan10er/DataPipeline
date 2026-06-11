"""Phase 5 robot state reasonableness checks."""

from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable
import csv
import math

from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    decide_status,
    load_metadata,
    save_episode_state,
    save_findings,
)
from scripts.pipeline.qa_config import config_value


LIMIT_TOLERANCE = 0.003  # 1mm / ~0.001 rad tolerance for floating point noise at boundaries
STANDSTILL_BUFFER_MS = 5000
PHASE_NUMBER = 5
JOINT_MODALITIES = ("actions.joint_position", "observation.state.joint_position")
VELOCITY_MODALITY = "observation.state.joint_velocity"
EEF_MODALITIES = ("actions.eef_pose", "action.eef_pose", "observation.state.eef_pose")

ROBOT_CONFIGS: dict[str, dict] = {
    "arx5": {
        "joint_count_per_arm": 6,
        # Calibrated from 3 ARX5 tasks (classify_battery, shake_spray_paint,
        # classification_building_blocks), 20260602-20260603.
        # joint_limits derived from observed range x1.2, capped at ±2π.
        "joint_limits_rad": (-3.46, 4.25),
        # gripper: observed min is slightly negative due to float noise,
        # LIMIT_TOLERANCE=0.001 handles boundary cases.
        "gripper_limits_m": (0.0, 0.082),
        # velocity: p99.9 from calibration data
        "max_joint_velocity_rad_s": 4.5,
        "max_gripper_velocity_m_s": 0.45,
        # step: p99.9 from calibration data
        "max_joint_step_rad": 0.15,
        "max_gripper_step_m": 0.015,
        # EEF pose step (not yet calibrated, keeping conservative default)
        "max_eef_position_step_m": 0.05,
        "max_eef_rotation_step_rad": 0.3,
        # Jitter
        "jitter_smooth_window": 5,
        "jitter_score_warn": 0.01,
        "jitter_score_fail": 0.05,
        # Static motion
        "static_motion_threshold_rad": 0.001,
        "static_ratio_warn": 0.95,
        # Acceleration (not yet calibrated)
        "max_acceleration_rad_s2": 50.0,
    },
    "flexiv": {
        "joint_count_per_arm": 7,
        # Flexiv Rizon 4: using ±2π as conservative hard limit until
        # official spec sheet values are confirmed.
        "joint_limits_rad": (-6.2832, 6.2832),
        # Confirmed from data: gripper opens to ~0.10m
        "gripper_limits_m": (0.0, 0.10),
        "max_joint_velocity_rad_s": 2.5,
        "max_gripper_velocity_m_s": 0.3,
        "max_acceleration_rad_s2": 50.0,
        "max_joint_step_rad": 0.3,
        "max_gripper_step_m": 0.05,
        "max_eef_position_step_m": 0.05,
        "max_eef_rotation_step_rad": 0.3,
        "jitter_smooth_window": 5,
        "jitter_score_warn": 0.01,
        "jitter_score_fail": 0.05,
        "static_motion_threshold_rad": 0.001,
        "static_ratio_warn": 0.95,
    },
    "aloha": {
        "joint_count_per_arm": 7,
        "joint_limits_rad": (-6.2832, 6.2832),
        "gripper_limits_m": (0.0, 0.10),
        "max_joint_velocity_rad_s": 2.5,
        "max_gripper_velocity_m_s": 0.3,
        "max_acceleration_rad_s2": 50.0,
        "max_joint_step_rad": 0.3,
        "max_gripper_step_m": 0.05,
        "max_eef_position_step_m": 0.05,
        "max_eef_rotation_step_rad": 0.3,
        "jitter_smooth_window": 5,
        "jitter_score_warn": 0.01,
        "jitter_score_fail": 0.05,
        "static_motion_threshold_rad": 0.001,
        "static_ratio_warn": 0.95,
    },
}
DEFAULT_CONFIG = ROBOT_CONFIGS["arx5"]


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run Phase 5 robot state checks for each unfinished episode."""
    pending = [
        state
        for state in states
        if PHASE_NUMBER not in state.phases_completed
    ]
    if workers > 1:
        return _run_phase_parallel(pending, db_path, progress_callback, workers, states)
    processed = 0
    for state in pending:
        _ensure_metadata(state)
        metrics = _initial_metrics()
        findings = _episode_findings(state, metrics)
        _finish_state(state, db_path, findings, metrics)
        processed += 1
        if progress_callback:
            progress_callback(processed, len(pending))
    return states


def _process_episode_worker(
    args: tuple[str, str],
) -> tuple[str, list[dict], dict]:
    """Worker for multiprocessing. Must be module-level for pickling.

    Args:
        args: Tuple of (episode_path_str, robot_str)

    Returns:
        Tuple of (episode_path_str, findings_as_dicts, metrics_dict)
    """
    episode_path_str, robot = args
    episode_path = Path(episode_path_str)
    config, is_fallback = _robot_config(robot)

    findings = []
    metrics = {}

    new_findings, new_metrics = _check_episode(episode_path, robot, config, is_fallback)
    findings.extend(new_findings)
    metrics.update(new_metrics)

    findings_dicts = [
        {
            "episode_path": f.episode_path,
            "phase": f.phase,
            "check_name": f.check_name,
            "severity": f.severity,
            "status": f.status,
            "message": f.message,
            "details": f.details,
        }
        for f in findings
    ]
    return episode_path_str, findings_dicts, metrics


def _run_phase_parallel(
    pending: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None,
    workers: int,
    states: list[EpisodeState],
) -> list[EpisodeState]:
    completed = len(states) - len(pending)
    total = len(states)
    if progress_callback and completed:
        progress_callback(completed, total)
    if not pending:
        return states
    args = [(str(state.episode_path), state.robot) for state in pending]
    states_by_path = {str(state.episode_path): state for state in pending}
    with Pool(processes=workers) as pool:
        for index, (episode_path_str, findings_dicts, metrics) in enumerate(
            pool.imap_unordered(_process_episode_worker, args)
        ):
            state = states_by_path[episode_path_str]
            new_findings = [Finding(**item) for item in findings_dicts]
            _finish_state(state, db_path, new_findings, metrics)
            if progress_callback:
                progress_callback(completed + index + 1, total)
    return states


def _check_episode(
    episode_path: Path, robot: str, config: dict, is_fallback: bool = False
) -> tuple[list[Finding], dict]:
    """Run all Phase 5 checks for one episode. Returns (findings, metrics)."""
    state = EpisodeState(
        episode_path=episode_path,
        task="",
        date="",
        operator="",
        robot=robot,
        controller="",
    )
    metrics = _initial_metrics()
    findings = _episode_findings(state, metrics, config)
    if is_fallback:
        findings.insert(0, _robot_config_fallback_finding(state))
    return findings, metrics


def _robot_config(robot: str) -> tuple[dict, bool]:
    """Return (config, is_fallback). is_fallback=True when robot is unknown."""
    robot_key = (robot or "").lower()
    is_fallback = robot_key not in ROBOT_CONFIGS
    config = dict(ROBOT_CONFIGS.get(robot_key, DEFAULT_CONFIG))
    overrides = config_value(["phase5_robot_state", "robots", robot_key], {})
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key == "gripper_limits_m" and isinstance(value, list):
                config[key] = tuple(value)
            else:
                config[key] = value
    return config, is_fallback


def _ensure_metadata(state: EpisodeState) -> None:
    if state.metadata:
        return
    metadata, findings = load_metadata(state.episode_path)
    if not findings:
        state.metadata = metadata


def _episode_findings(
    state: EpisodeState, metrics: dict[str, Any], config: dict | None = None
) -> list[Finding]:
    findings = []
    if config is None:
        config, is_fallback = _robot_config(state.robot)
        if is_fallback:
            findings.append(_robot_config_fallback_finding(state))
    position_data = {}
    for modality in JOINT_MODALITIES:
        data = _load_modality_data(state, modality, findings)
        if data is None:
            continue
        position_data[modality] = data
        findings.extend(_joint_position_findings(state, modality, data, config, metrics))
    findings.extend(_standstill_findings(state, position_data, config, metrics))
    findings.extend(_velocity_findings(state, position_data, config, metrics))
    findings.extend(_eef_pose_findings(state, config))
    return findings


def _robot_config_fallback_finding(state: EpisodeState) -> Finding:
    return _finding(
        state,
        "robot_config_fallback",
        "info",
        "pass",
        f"Robot '{state.robot}' not in ROBOT_CONFIGS. Using arx5 defaults.",
        {"robot": state.robot, "fallback": "arx5"},
    )


def _load_modality_data(
    state: EpisodeState, modality: str, findings: list[Finding]
) -> tuple[list[str], list[list[float | None]]] | None:
    path = state.episode_path / modality / "data.csv"
    if not path.is_file():
        return None
    data = _load_csv_columns(path)
    if data is None:
        findings.extend(_check_csv_parseable(state, modality))
    return data


def _joint_position_findings(
    state: EpisodeState,
    modality: str,
    data: tuple[list[str], list[list[float | None]]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    headers, rows = data
    columns = _detect_columns(headers, state.robot)
    findings = _column_detection_findings(state, modality, columns)
    joint_cols = columns["joint_cols"]
    gripper_cols = columns["gripper_cols"]
    findings.extend(_check_nan_inf(state, modality, headers, rows, joint_cols + gripper_cols, metrics))
    findings.extend(_check_timestamps_monotonic(state, modality, headers, rows))
    findings.extend(_check_joint_limits(state, modality, headers, rows, joint_cols, config, metrics))
    findings.extend(_check_gripper_limits(state, modality, headers, rows, gripper_cols, config))
    findings.extend(_check_gripper_mean_remap_needed(state, modality, headers, rows, gripper_cols))
    findings.extend(_check_joint_steps(state, modality, headers, rows, joint_cols, config, metrics))
    findings.extend(_check_gripper_steps(state, modality, headers, rows, gripper_cols, config))
    findings.extend(_check_jitter(state, modality, headers, rows, joint_cols, config, metrics))
    return findings


def _standstill_findings(
    state: EpisodeState,
    position_data: dict[str, tuple[list[str], list[list[float | None]]]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    selected = _standstill_source(position_data)
    if selected is None:
        return []
    modality, data = selected
    headers, rows = data
    findings = _check_standstill(state, modality, _rows_as_dicts(headers, rows), config)
    metrics["p5_standstill_segment_count"] = state.metrics.get("p5_standstill_segment_count", 0)
    metrics["p5_standstill_total_excess_ms"] = state.metrics.get("p5_standstill_total_excess_ms", 0.0)
    metrics["p5_standstill_excess_ratio"] = state.metrics.get("p5_standstill_excess_ratio", 0.0)
    return findings


def _standstill_source(
    position_data: dict[str, tuple[list[str], list[list[float | None]]]]
) -> tuple[str, tuple[list[str], list[list[float | None]]]] | None:
    for modality in ("observation.state.joint_position", "actions.joint_position"):
        if modality in position_data:
            return modality, position_data[modality]
    return None


def _velocity_findings(
    state: EpisodeState,
    position_data: dict[str, tuple[list[str], list[list[float | None]]]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    velocity_path = state.episode_path / VELOCITY_MODALITY / "data.csv"
    data = _load_csv_columns(velocity_path)
    if data is not None:
        return _measured_velocity_findings(state, data, config, metrics)
    if velocity_path.is_file():
        return _check_csv_parseable(state, VELOCITY_MODALITY)
    return _estimated_velocity_findings(state, position_data, config, metrics)


def _measured_velocity_findings(
    state: EpisodeState,
    data: tuple[list[str], list[list[float | None]]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    headers, rows = data
    velocity_cols = _detect_velocity_columns(headers)
    findings = _check_nan_inf(state, VELOCITY_MODALITY, headers, rows, velocity_cols, metrics)
    findings.extend(_check_timestamps_monotonic(state, VELOCITY_MODALITY, headers, rows))
    findings.extend(_check_velocity_values(state, VELOCITY_MODALITY, headers, rows, velocity_cols, config, metrics))
    findings.extend(_check_acceleration(state, VELOCITY_MODALITY, headers, rows, velocity_cols, config, metrics))
    return findings


def _estimated_velocity_findings(
    state: EpisodeState,
    position_data: dict[str, tuple[list[str], list[list[float | None]]]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    for modality, data in position_data.items():
        headers, rows = data
        joint_cols = _detect_columns(headers, state.robot)["joint_cols"]
        velocities = _estimate_velocity(headers, rows, joint_cols)
        findings.extend(_check_estimated_velocity(state, modality, velocities, config, metrics))
        findings.extend(_check_estimated_acceleration(state, modality, data, config, metrics))
    return findings


def _eef_pose_findings(state: EpisodeState, config: dict) -> list[Finding]:
    findings = []
    for modality in EEF_MODALITIES:
        data = _load_csv_columns(state.episode_path / modality / "data.csv")
        if data is not None:
            findings.extend(_check_eef_pose_steps(state, modality, data, config))
    return findings


def _check_csv_parseable(state: EpisodeState, modality: str) -> list[Finding]:
    return [
        _finding(
            state,
            "csv_not_parseable",
            "critical",
            "fail",
            "data.csv could not be read or parsed.",
            {"modality": modality},
        )
    ]


def _check_nan_inf(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    for column in columns:
        values = _column_values(headers, rows, column)
        count = sum(1 for value in values if not _finite(value))
        metrics["p5_nan_inf_count"] += count
        if count:
            findings.append(_nan_inf_finding(state, modality, column, count))
    return findings


def _check_timestamps_monotonic(
    state: EpisodeState, modality: str, headers: list[str], rows: list[list[float | None]]
) -> list[Finding]:
    """Check that timestamp_ms values are strictly increasing."""
    timestamps = _finite_column_values(headers, rows, "timestamp_ms")
    if not timestamps:
        return [
            _finding(
                state,
                "timestamps_missing_or_unparseable",
                "major",
                "fail",
                "timestamp_ms column is missing or contains no parseable values.",
                {"modality": modality},
            )
        ]
    violations, first_violation = _monotonic_violations(timestamps)
    if violations == 0:
        return []
    ratio = violations / len(timestamps) if timestamps else 1.0
    severity, status = _ratio_severity(ratio)
    return [_finding(state, "timestamps_not_monotonic", severity, status, "Timestamps are not strictly increasing.", {"modality": modality, "violation_count": violations, "violation_ratio": ratio, "first_violation_ms": first_violation})]


def _check_joint_limits(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    low, high = config["joint_limits_rad"]
    for column in columns:
        values = _finite_column_values(headers, rows, column)
        metrics["p5_max_joint_abs"] = max(metrics["p5_max_joint_abs"], _max_abs(values))
        violations = [
            max(low - value, value - high)
            for value in values
            if value < low - LIMIT_TOLERANCE or value > high + LIMIT_TOLERANCE
        ]
        metrics["p5_joint_limit_violations"] += len(violations)
        if violations:
            findings.append(_limit_finding(state, modality, column, violations, [low, high]))
    return findings


def _check_gripper_limits(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
) -> list[Finding]:
    findings = []
    low, high = config["gripper_limits_m"]
    for column in columns:
        values = _finite_column_values(headers, rows, column)
        violations = [
            max(low - value, value - high)
            for value in values
            if value < low - LIMIT_TOLERANCE or value > high + LIMIT_TOLERANCE
        ]
        if violations:
            findings.append(_gripper_limit_finding(state, modality, column, violations, [low, high]))
    return findings


def _check_gripper_mean_remap_needed(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
) -> list[Finding]:
    findings = []
    threshold = float(
        config_value(["phase5_robot_state", "gripper", "mean_remap_threshold_m"], 0.005)
    )
    for column in columns:
        values = _finite_column_values(headers, rows, column)
        if not values:
            continue
        mean_value = sum(values) / len(values)
        if mean_value < threshold:
            findings.append(_gripper_remap_finding(state, modality, column, mean_value, threshold))
    return findings


def _check_joint_steps(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    return _step_findings(state, modality, headers, rows, columns, config["max_joint_step_rad"], "joint_step_too_large", metrics)


def _check_gripper_steps(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
) -> list[Finding]:
    metrics: dict[str, Any] = {}
    return _step_findings(state, modality, headers, rows, columns, config["max_gripper_step_m"], "gripper_step_too_large", metrics)


def _step_findings(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    threshold: float,
    check_name: str,
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    for column in columns:
        steps = _absolute_steps(_finite_column_values(headers, rows, column))
        max_step = max(steps, default=0.0)
        if check_name == "joint_step_too_large":
            metrics["p5_max_joint_step"] = max(metrics["p5_max_joint_step"], max_step)
        violations = [step for step in steps if step > threshold]
        if violations:
            findings.append(_step_finding(state, modality, column, check_name, max_step, threshold, len(violations)))
    return findings


def _check_velocity_values(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    for column in columns:
        values = [abs(value) for value in _finite_column_values(headers, rows, column)]
        metrics["p5_max_velocity"] = max(metrics["p5_max_velocity"], max(values, default=0.0))
        findings.extend(_velocity_exceeded_findings(state, modality, column, values, config, False))
    return findings


def _check_estimated_velocity(
    state: EpisodeState,
    modality: str,
    velocities: dict[str, list[float]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    for column, values in velocities.items():
        abs_values = [abs(value) for value in values]
        metrics["p5_max_velocity"] = max(metrics["p5_max_velocity"], max(abs_values, default=0.0))
        findings.extend(_velocity_exceeded_findings(state, modality, column, abs_values, config, True))
    return findings


def _velocity_exceeded_findings(
    state: EpisodeState, modality: str, column: str, values: list[float], config: dict, estimated: bool
) -> list[Finding]:
    threshold = config["max_joint_velocity_rad_s"]
    max_velocity = max(values, default=0.0)
    p99_velocity = _quantile(values, 0.99)
    if p99_velocity <= threshold:
        return []
    return [_finding(state, "joint_velocity_exceeded", "minor", "needs_review", "Joint velocity exceeds configured limit.", {"modality": modality, "column": column, "max_velocity": max_velocity, "p99_velocity": p99_velocity, "threshold": threshold, "note": "threshold applied to p99, not max"})]


def _check_acceleration(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    timestamps = _finite_column_values(headers, rows, "timestamp_ms")
    findings = []
    for column in columns:
        velocities = _finite_column_values(headers, rows, column)
        findings.extend(
            _acceleration_findings(
                state,
                modality,
                column,
                velocities,
                timestamps,
                config["max_acceleration_rad_s2"],
                metrics,
            )
        )
    return findings


def _check_estimated_acceleration(
    state: EpisodeState,
    modality: str,
    data: tuple[list[str], list[list[float | None]]],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    headers, rows = data
    timestamps = _finite_column_values(headers, rows, "timestamp_ms")
    joint_cols = _detect_columns(headers, state.robot)["joint_cols"]
    findings = []
    for column in joint_cols:
        values = _finite_column_values(headers, rows, column)
        accel = _position_acceleration(values, timestamps)
        metrics["p5_max_acceleration"] = max(metrics["p5_max_acceleration"], _max_abs(accel))
        threshold = config["max_acceleration_rad_s2"]
        if _quantile([abs(value) for value in accel], 0.99) > threshold:
            findings.append(_accel_finding(state, modality, column, _quantile([abs(value) for value in accel], 0.99)))
    return findings


def _acceleration_findings(
    state: EpisodeState,
    modality: str,
    column: str,
    velocities: list[float],
    timestamps: list[float],
    threshold: float,
    metrics: dict[str, Any],
) -> list[Finding]:
    accel = _velocity_acceleration(velocities, timestamps)
    p99 = _quantile([abs(value) for value in accel], 0.99)
    metrics["p5_max_acceleration"] = max(metrics["p5_max_acceleration"], _max_abs(accel))
    return [_accel_finding(state, modality, column, p99)] if p99 > threshold else []


def _check_jitter(
    state: EpisodeState,
    modality: str,
    headers: list[str],
    rows: list[list[float | None]],
    columns: list[str],
    config: dict,
    metrics: dict[str, Any],
) -> list[Finding]:
    findings = []
    for column in columns:
        values = _finite_column_values(headers, rows, column)
        score = _jitter_score(values, config["jitter_smooth_window"])
        metrics["p5_jitter_score"] = max(metrics["p5_jitter_score"], score)
        if score >= config["jitter_score_fail"]:
            findings.append(_jitter_finding(state, modality, column, "major", "fail", score, config["jitter_score_fail"]))
        elif score >= config["jitter_score_warn"]:
            findings.append(_jitter_finding(state, modality, column, "minor", "warning", score, config["jitter_score_warn"]))
    return findings


def _check_standstill(
    state: EpisodeState,
    modality: str,
    rows: list[dict],
    config: dict,
) -> list[Finding]:
    """Detect long operator idle segments in joint position data.

    Allows short pauses (up to STANDSTILL_BUFFER_MS). Only flags continuous
    stillness that exceeds the buffer, which indicates operator idling.
    Multiple stop-move-stop patterns are detected independently.
    """
    joint_cols = _standstill_joint_columns(rows)
    segments = _detect_standstill_segments(
        rows,
        joint_cols,
        config["static_motion_threshold_rad"],
    )
    _record_standstill_metrics(state, rows, segments)
    findings = _standstill_segment_findings(state, modality, segments)
    excessive = _excessive_standstill_finding(state, modality, rows, segments)
    if excessive is not None:
        findings.append(excessive)
    return findings


def _detect_standstill_segments(
    rows: list[dict],
    joint_cols: list[str],
    motion_threshold: float,
) -> list[dict]:
    """Find all continuous stillness segments exceeding STANDSTILL_BUFFER_MS."""
    if len(rows) < 2 or not joint_cols:
        return []
    segments = []
    segment_start = None
    segment_end = None
    for previous, current in zip(rows, rows[1:]):
        if _row_pair_still(previous, current, joint_cols, motion_threshold):
            segment_start = _row_time(previous) if segment_start is None else segment_start
            segment_end = _row_time(current)
        elif segment_start is not None and segment_end is not None:
            _append_standstill_segment(segments, segment_start, segment_end)
            segment_start = None
            segment_end = None
    if segment_start is not None and segment_end is not None:
        _append_standstill_segment(segments, segment_start, segment_end)
    return segments


def _standstill_joint_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return [
        column
        for column in rows[0]
        if column not in ("timestamp_ms", "is_standstill") and "gripper" not in column
    ]


def _row_pair_still(
    previous: dict, current: dict, joint_cols: list[str], motion_threshold: float
) -> bool:
    for column in joint_cols:
        previous_value = previous.get(column)
        current_value = current.get(column)
        if not _finite(previous_value) or not _finite(current_value):
            return False
        if abs(current_value - previous_value) >= motion_threshold:
            return False
    return _row_time(current) > _row_time(previous)


def _append_standstill_segment(
    segments: list[dict], stop_start_ms: float, stop_end_ms: float
) -> None:
    duration_ms = stop_end_ms - stop_start_ms
    if duration_ms <= STANDSTILL_BUFFER_MS:
        return
    segments.append(
        {
            "stop_start_ms": stop_start_ms,
            "stop_end_ms": stop_end_ms,
            "duration_ms": duration_ms,
            "annotate_start_ms": stop_start_ms + STANDSTILL_BUFFER_MS,
            "annotate_end_ms": stop_end_ms,
        }
    )


def _row_time(row: dict) -> float:
    value = row.get("timestamp_ms")
    return float(value) if _finite(value) else 0.0


def _episode_duration_ms(rows: list[dict]) -> float:
    timestamps = [_row_time(row) for row in rows if _finite(row.get("timestamp_ms"))]
    if len(timestamps) < 2:
        return 0.0
    return max(timestamps) - min(timestamps)


def _record_standstill_metrics(
    state: EpisodeState, rows: list[dict], segments: list[dict]
) -> None:
    total_excess = sum(segment["duration_ms"] - STANDSTILL_BUFFER_MS for segment in segments)
    episode_duration = _episode_duration_ms(rows)
    ratio = total_excess / episode_duration if episode_duration > 0 else 0.0
    state.metrics["p5_standstill_segment_count"] = len(segments)
    state.metrics["p5_standstill_total_excess_ms"] = total_excess
    state.metrics["p5_standstill_excess_ratio"] = ratio


def _standstill_segment_findings(
    state: EpisodeState, modality: str, segments: list[dict]
) -> list[Finding]:
    findings = []
    total_segments = len(segments)
    for index, segment in enumerate(segments, start=1):
        findings.append(
            _finding(
                state,
                "operator_standstill",
                "minor",
                "warning",
                "Operator idle segment detected beyond 5-second buffer.",
                {
                    "modality": modality,
                    "stop_start_ms": segment["stop_start_ms"],
                    "stop_end_ms": segment["stop_end_ms"],
                    "duration_ms": segment["duration_ms"],
                    "excess_ms": segment["duration_ms"] - STANDSTILL_BUFFER_MS,
                    "segment_index": index,
                    "total_segments": total_segments,
                },
            )
        )
    return findings


def _excessive_standstill_finding(
    state: EpisodeState, modality: str, rows: list[dict], segments: list[dict]
) -> Finding | None:
    total_excess = sum(segment["duration_ms"] - STANDSTILL_BUFFER_MS for segment in segments)
    episode_duration = _episode_duration_ms(rows)
    if episode_duration <= 0:
        return None
    ratio = total_excess / episode_duration
    if ratio <= 0.20:
        return None
    return _finding(
        state,
        "operator_standstill_excessive",
        "major",
        "needs_review",
        "Total operator idle time exceeds 20% of episode duration.",
        {
            "modality": modality,
            "total_excess_ms": total_excess,
            "episode_duration_ms": episode_duration,
            "excess_ratio": ratio,
        },
    )


def _check_eef_pose_steps(
    state: EpisodeState,
    modality: str,
    data: tuple[list[str], list[list[float | None]]],
    config: dict,
) -> list[Finding]:
    headers, rows = data
    findings = []
    for arm, columns in _eef_position_columns(headers).items():
        steps = _eef_steps(headers, rows, columns)
        max_step = max(steps, default=0.0)
        violations = [step for step in steps if step > config["max_eef_position_step_m"]]
        if violations:
            findings.append(_finding(state, "eef_position_step_too_large", "minor", "needs_review", "EEF position step exceeds configured limit.", {"modality": modality, "arm": arm, "max_step_m": max_step, "threshold": config["max_eef_position_step_m"]}))
    return findings


def _load_csv_columns(path: Path) -> tuple[list[str], list[list[float | None]]] | None:
    """Load a CSV file and return (headers, rows_as_float_or_none)."""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or [])
            rows = [[_float_or_none(row.get(header)) for header in headers] for row in reader]
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return (headers, rows) if headers else None


def _rows_as_dicts(
    headers: list[str], rows: list[list[float | None]]
) -> list[dict[str, float | None]]:
    return [
        {
            header: row[index] if index < len(row) else None
            for index, header in enumerate(headers)
        }
        for row in rows
    ]


def _detect_columns(headers: list[str], robot: str) -> dict:
    """Detect joint and gripper columns from CSV headers."""
    arx_joint_cols = [header for header in headers if _arx_joint_col(header)]
    flexiv_joint_cols = [header for header in headers if _flexiv_joint_col(header)]
    joint_cols = arx_joint_cols or flexiv_joint_cols
    return {
        "joint_cols": joint_cols,
        "gripper_cols": [header for header in headers if "gripper" in header.lower()],
        "is_bimanual": bool(arx_joint_cols),
    }


def _detect_velocity_columns(headers: list[str]) -> list[str]:
    return [header for header in headers if _arx_velocity_col(header) or _flexiv_velocity_col(header)]


def _arx_joint_col(header: str) -> bool:
    return (header.startswith("left_j") or header.startswith("right_j")) and header.split("_j")[-1].isdigit()


def _flexiv_joint_col(header: str) -> bool:
    return (header.startswith("j") and header[1:].isdigit()) or (header.startswith("joint_") and header.endswith(".pos"))


def _arx_velocity_col(header: str) -> bool:
    return (header.startswith("left_v") or header.startswith("right_v")) and header.split("_v")[-1].isdigit()


def _flexiv_velocity_col(header: str) -> bool:
    return (header.startswith("v") and header[1:].isdigit()) or (header.startswith("joint_") and header.endswith(".vel"))


def _column_detection_findings(state: EpisodeState, modality: str, columns: dict) -> list[Finding]:
    if columns["joint_cols"]:
        return []
    return [_finding(state, "joint_columns_not_detected", "info", "pass", "Joint columns could not be detected.", {"modality": modality})]


def _column_values(headers: list[str], rows: list[list[float | None]], column: str) -> list[float | None]:
    if column not in headers:
        return []
    index = headers.index(column)
    return [row[index] for row in rows if index < len(row)]


def _finite_column_values(headers: list[str], rows: list[list[float | None]], column: str) -> list[float]:
    return [value for value in _column_values(headers, rows, column) if _finite(value)]


def _finite(value: float | None) -> bool:
    return value is not None and not math.isnan(value) and not math.isinf(value)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _monotonic_violations(values: list[float]) -> tuple[int, float]:
    violations = 0
    first = 0.0
    for previous, current in zip(values, values[1:]):
        if current <= previous:
            violations += 1
            if violations == 1:
                first = current
    return violations, first


def _ratio_severity(ratio: float) -> tuple[str, str]:
    if ratio >= 0.05:
        return "major", "fail"
    if ratio >= 0.01:
        return "major", "needs_review"
    return "minor", "warning"


def _absolute_steps(values: list[float]) -> list[float]:
    return [abs(current - previous) for previous, current in zip(values, values[1:])]


def _estimate_velocity(
    headers: list[str], rows: list[list[float | None]], columns: list[str]
) -> dict[str, list[float]]:
    timestamps = _finite_column_values(headers, rows, "timestamp_ms")
    result = {}
    for column in columns:
        values = _finite_column_values(headers, rows, column)
        result[column] = _position_velocity(values, timestamps)
    return result


def _position_velocity(values: list[float], timestamps: list[float]) -> list[float]:
    velocities = []
    for previous, current, dt in zip(values, values[1:], _timestamp_dts(timestamps)):
        if dt > 0:
            velocities.append((current - previous) / dt)
    return velocities


def _position_acceleration(values: list[float], timestamps: list[float]) -> list[float]:
    velocities = _position_velocity(values, timestamps)
    return _adjacent_deltas(velocities, _timestamp_dts(timestamps)[1:])


def _timestamp_dts(timestamps: list[float]) -> list[float]:
    return [(current - previous) / 1000.0 for previous, current in zip(timestamps, timestamps[1:])]


def _velocity_acceleration(velocities: list[float], timestamps: list[float]) -> list[float]:
    dts = _timestamp_dts(timestamps)
    return _adjacent_deltas(velocities, dts)


def _adjacent_deltas(values: list[float], dts: list[float]) -> list[float]:
    return [(current - previous) / dt for previous, current, dt in zip(values, values[1:], dts) if dt > 0]


def _jitter_score(values: list[float], window: int) -> float:
    if not values:
        return 0.0
    smoothed = _moving_average(values, window)
    residuals = [abs(value - smooth) for value, smooth in zip(values, smoothed)]
    return sum(residuals) / len(residuals)


def _moving_average(values: list[float], window: int) -> list[float]:
    """Compute centered moving average. Edge values use smaller windows."""
    if not values:
        return []
    radius = max(0, window // 2)
    averages = []
    for index in range(len(values)):
        start = max(0, index - radius)
        end = min(len(values), index + radius + 1)
        averages.append(sum(values[start:end]) / (end - start))
    return averages


def _eef_position_columns(headers: list[str]) -> dict[str, tuple[str, str, str]]:
    columns = {}
    if all(column in headers for column in ("left_x", "left_y", "left_z")):
        columns["left"] = ("left_x", "left_y", "left_z")
    if all(column in headers for column in ("right_x", "right_y", "right_z")):
        columns["right"] = ("right_x", "right_y", "right_z")
    if all(column in headers for column in ("tcp.x", "tcp.y", "tcp.z")):
        columns["tcp"] = ("tcp.x", "tcp.y", "tcp.z")
    return columns


def _eef_steps(headers: list[str], rows: list[list[float | None]], columns: tuple[str, str, str]) -> list[float]:
    xs, ys, zs = (_finite_column_values(headers, rows, column) for column in columns)
    return [math.sqrt((cx - px) ** 2 + (cy - py) ** 2 + (cz - pz) ** 2) for px, cx, py, cy, pz, cz in zip(xs, xs[1:], ys, ys[1:], zs, zs[1:])]


def _quantile(values: list[float], q: float) -> float:
    """Compute quantile q (0.0 to 1.0) from a sorted or unsorted list."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = q * (len(sorted_vals) - 1)
    lower = int(idx)
    upper = min(lower + 1, len(sorted_vals) - 1)
    return sorted_vals[lower] + (idx - lower) * (sorted_vals[upper] - sorted_vals[lower])


def _max_abs(values: list[float]) -> float:
    return max((abs(value) for value in values), default=0.0)


def _initial_metrics() -> dict[str, Any]:
    return {
        "p5_max_joint_abs": 0.0,
        "p5_max_joint_step": 0.0,
        "p5_max_velocity": 0.0,
        "p5_max_acceleration": 0.0,
        "p5_jitter_score": 0.0,
        "p5_standstill_segment_count": 0,
        "p5_standstill_total_excess_ms": 0.0,
        "p5_standstill_excess_ratio": 0.0,
        "p5_nan_inf_count": 0,
        "p5_joint_limit_violations": 0,
    }


def _finish_state(
    state: EpisodeState, db_path: Path, new_findings: list[Finding], metrics: dict[str, Any]
) -> None:
    state.metrics.update(metrics)
    if PHASE_NUMBER not in state.phases_completed:
        state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(new_findings)
    state.findings.extend(new_findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, new_findings, phase=PHASE_NUMBER, episode_path=str(state.episode_path))


def _nan_inf_finding(state: EpisodeState, modality: str, column: str, count: int) -> Finding:
    return _finding(state, "joint_nan_inf", "critical", "fail", "NaN, Inf, or unparseable values were found.", {"modality": modality, "column": column, "count": count})


def _limit_finding(
    state: EpisodeState, modality: str, column: str, violations: list[float], limit: list[float]
) -> Finding:
    return _finding(state, "joint_out_of_limits", "minor", "needs_review", "Joint values exceed configured limits.", {"modality": modality, "column": column, "violation_count": len(violations), "max_violation": max(violations), "limit": limit})


def _gripper_limit_finding(
    state: EpisodeState, modality: str, column: str, violations: list[float], limit: list[float]
) -> Finding:
    return _finding(state, "gripper_out_of_limits", "minor", "needs_review", "Gripper values exceed configured limits.", {"modality": modality, "column": column, "violation_count": len(violations), "max_violation": max(violations), "limit": limit})


def _gripper_remap_finding(
    state: EpisodeState, modality: str, column: str, mean_value: float, threshold: float
) -> Finding:
    return _finding(
        state,
        "gripper_mean_too_low_remap_needed",
        "minor",
        "needs_review",
        "Mean gripper distance is below configured threshold; remapping is needed.",
        {
            "modality": modality,
            "column": column,
            "mean_value_m": mean_value,
            "threshold_m": threshold,
            "action": "remap_gripper_distance",
        },
    )


def _step_finding(
    state: EpisodeState, modality: str, column: str, check_name: str, max_step: float, threshold: float, count: int
) -> Finding:
    return _finding(state, check_name, "minor", "needs_review", "Per-frame step exceeds configured limit.", {"modality": modality, "column": column, "max_step": max_step, "threshold": threshold, "violation_count": count})


def _accel_finding(state: EpisodeState, modality: str, column: str, p99: float) -> Finding:
    return _finding(state, "joint_acceleration_high", "minor", "warning", "Joint acceleration p99 is high.", {"modality": modality, "column": column, "p99_accel": p99})


def _jitter_finding(
    state: EpisodeState, modality: str, column: str, severity: str, status: str, score: float, threshold: float
) -> Finding:
    return _finding(state, "jitter_high", severity, status, "Joint jitter score is high.", {"modality": modality, "column": column, "jitter_score": score, "threshold": threshold})


def _finding(
    state: EpisodeState,
    check_name: str,
    severity: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        episode_path=str(state.episode_path),
        phase=PHASE_NUMBER,
        check_name=check_name,
        severity=severity,
        status=status,
        message=message,
        details=details or {},
    )
