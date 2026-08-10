"""Windowed EEF slow-motion analysis and action/observation attribution."""

from __future__ import annotations

import csv
import fnmatch
import math
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.pipeline.qa_config import config_value


@dataclass(frozen=True)
class SlowMotionSettings:
    enabled: bool
    observation_modality: str
    action_modalities: tuple[str, ...]
    window_seconds: float
    min_speed_cm_s: float
    max_speed_cm_s: float
    merge_gap_seconds: float
    min_duration_seconds: float
    min_segment_path_cm: float
    active_hand_min_path_cm: float
    active_hand_min_ratio: float
    action_slow_fraction: float
    action_normal_fraction: float
    alignment_tolerance_ms: float
    gripper_activity_gate: bool
    gripper_window_min_motion: float
    gripper_window_min_avg_speed: float
    gripper_slow_min_duration_seconds: float
    joint_rotation_min_window_motion: float
    joint_rotation_min_avg_speed: float
    raw_rotation_task_patterns: tuple[str, ...]
    raw_rotation_min_window_motion: float
    raw_rotation_min_avg_speed: float


@dataclass(frozen=True)
class EefSeries:
    modality: str
    timestamps_ms: list[float]
    positions: dict[str, list[tuple[float, float, float]]]


@dataclass(frozen=True)
class SpeedSeries:
    modality: str
    timestamps_ms: list[float]
    by_side_cm_s: dict[str, list[float]]
    step_paths_cm: dict[str, list[float]]


@dataclass(frozen=True)
class SlowSegment:
    start_index: int
    end_index: int
    start_ms: float
    end_ms: float
    duration_seconds: float
    path_cm: float
    avg_speed_cm_s: float


class IntervalAttributor:
    """Classify command motion inside an observed slow/stalled interval."""

    def __init__(
        self,
        action_speeds: SpeedSeries | None,
        observation_sides: list[str],
        settings: SlowMotionSettings,
    ) -> None:
        self.action_speeds = action_speeds
        self.observation_sides = observation_sides
        self.settings = settings

    def cause(
        self,
        start_ms: float,
        end_ms: float,
        require_moving_slow: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        speeds = self.action_speeds
        if speeds is None:
            return "uncertain", {"reason": "action_motion_source_missing"}
        mapped_sides = [side for side in self.observation_sides if side in speeds.by_side_cm_s]
        if not mapped_sides and len(speeds.by_side_cm_s) == 1:
            mapped_sides = list(speeds.by_side_cm_s)
        if not mapped_sides:
            return "uncertain", {"reason": "action_hand_schema_mismatch"}
        lower = start_ms - self.settings.alignment_tolerance_ms
        upper = end_ms + self.settings.alignment_tolerance_ms
        values = [
            max(speeds.by_side_cm_s[side][index] for side in mapped_sides)
            for index, timestamp in enumerate(speeds.timestamps_ms)
            if lower <= timestamp <= upper
        ]
        if not values:
            return "uncertain", {"reason": "no_aligned_action_samples"}
        slow_count = sum(
            (
                self.settings.min_speed_cm_s <= value <= self.settings.max_speed_cm_s
                if require_moving_slow
                else value <= self.settings.max_speed_cm_s
            )
            for value in values
        )
        normal_count = sum(value > self.settings.max_speed_cm_s for value in values)
        slow_fraction = slow_count / len(values)
        normal_fraction = normal_count / len(values)
        evidence = {
            "action_sample_count": len(values),
            "action_slow_fraction": slow_fraction,
            "action_normal_fraction": normal_fraction,
            "action_max_speed_cm_s": max(values),
            "action_median_speed_cm_s": _percentile(values, 50),
            "action_source_modality": speeds.modality,
            "action_must_be_moving_slow": require_moving_slow,
        }
        if slow_fraction >= self.settings.action_slow_fraction:
            return "operator_associated", evidence
        if normal_fraction >= self.settings.action_normal_fraction:
            return "equipment_associated", evidence
        evidence["reason"] = "mixed_action_motion"
        return "uncertain", evidence


def settings_for_task(task_key: str) -> SlowMotionSettings:
    configured = config_value(["phase7_slow_motion"], {})
    if not isinstance(configured, dict):
        raise RuntimeError("phase7_slow_motion must be a JSON object")
    values = dict(configured)
    overrides = values.pop("task_overrides", {})
    if not isinstance(overrides, dict):
        raise RuntimeError("phase7_slow_motion.task_overrides must be a JSON object")
    override = overrides.get(task_key, {})
    if not isinstance(override, dict):
        raise RuntimeError(f"Slow-motion override for {task_key!r} must be a JSON object")
    values.update(override)
    settings = SlowMotionSettings(
        enabled=bool(values.get("enabled", True)),
        observation_modality=str(values.get("observation_modality", "observation.state.eef_pose")),
        action_modalities=tuple(str(item) for item in values.get("action_modalities", ["actions.eef_pose", "action.eef_pose"])),
        window_seconds=float(values.get("window_seconds", 1.0)),
        min_speed_cm_s=float(values.get("min_speed_cm_s", 0.1)),
        max_speed_cm_s=float(values.get("max_speed_cm_s", 3.0)),
        merge_gap_seconds=float(values.get("merge_gap_seconds", 2.0)),
        min_duration_seconds=float(values.get("min_duration_seconds", 15.0)),
        min_segment_path_cm=float(values.get("min_segment_path_cm", 1.5)),
        active_hand_min_path_cm=float(values.get("active_hand_min_path_cm", 1.0)),
        active_hand_min_ratio=float(values.get("active_hand_min_ratio", 0.2)),
        action_slow_fraction=float(values.get("action_slow_fraction", 0.8)),
        action_normal_fraction=float(values.get("action_normal_fraction", 0.8)),
        alignment_tolerance_ms=float(values.get("alignment_tolerance_ms", 500.0)),
        gripper_activity_gate=bool(values.get("gripper_activity_gate", True)),
        gripper_window_min_motion=float(values.get("gripper_window_min_motion", 0.03)),
        gripper_window_min_avg_speed=float(values.get("gripper_window_min_avg_speed", 0.002)),
        gripper_slow_min_duration_seconds=float(values.get("gripper_slow_min_duration_seconds", 15.0)),
        joint_rotation_min_window_motion=float(values.get("joint_rotation_min_window_motion", 1.5)),
        joint_rotation_min_avg_speed=float(values.get("joint_rotation_min_avg_speed", 0.1)),
        raw_rotation_task_patterns=tuple(str(item) for item in values.get("raw_rotation_task_patterns", [])),
        raw_rotation_min_window_motion=float(values.get("raw_rotation_min_window_motion", 1.0)),
        raw_rotation_min_avg_speed=float(values.get("raw_rotation_min_avg_speed", 0.07)),
    )
    _validate_settings(settings)
    return settings


def analyze_episode(
    episode_path: Path,
    task_key: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], IntervalAttributor | None]:
    settings = settings_for_task(task_key)
    metrics = {
        "p7_slow_motion_segment_count": 0,
        "p7_slow_motion_longest_ms": 0.0,
        "p7_slow_motion_cause": "none",
        "p7_slow_motion_source": "",
        "p7_slow_motion_suppressed_segment_count": 0,
        "p7_slow_motion_suppression_reasons": [],
    }
    if not settings.enabled:
        return [], metrics, None
    observation = _load_eef_series(episode_path, (settings.observation_modality,))
    if observation is None:
        return [], metrics, None
    action = _load_eef_series(episode_path, settings.action_modalities)
    observation_speeds = _speed_series(observation, settings.window_seconds)
    action_speeds = _speed_series(action, settings.window_seconds) if action is not None else None
    active_sides = _active_sides(observation_speeds, settings)
    attributor = IntervalAttributor(action_speeds, active_sides, settings)
    segments = _slow_segments(observation_speeds, active_sides, settings)
    qualified = [
        segment
        for segment in segments
        if segment.duration_seconds >= settings.min_duration_seconds
        and segment.path_cm >= settings.min_segment_path_cm
    ]
    metrics["p7_slow_motion_segment_count"] = len(qualified)
    metrics["p7_slow_motion_longest_ms"] = max(
        (segment.duration_seconds * 1000.0 for segment in qualified), default=0.0
    )
    metrics["p7_slow_motion_source"] = observation.modality
    if not qualified:
        return [], metrics, attributor

    by_cause: dict[str, list[tuple[SlowSegment, dict[str, Any], list[str]]]] = {}
    suppression_reasons: list[str] = []
    suppressed_segment_count = 0
    for segment in qualified:
        cause, cause_evidence = attributor.cause(
            segment.start_ms, segment.end_ms, require_moving_slow=True
        )
        gates = _activity_gates(episode_path, task_key, segment, settings)
        if gates:
            suppressed_segment_count += 1
            suppression_reasons.extend(gates)
            continue
        by_cause.setdefault(cause, []).append((segment, cause_evidence, gates))

    findings = []
    metrics["p7_slow_motion_suppressed_segment_count"] = suppressed_segment_count
    metrics["p7_slow_motion_suppression_reasons"] = sorted(set(suppression_reasons))
    for cause, items in sorted(by_cause.items()):
        if not items:
            continue
        check_name, severity, status, message = _finding_identity(cause)
        longest = max(items, key=lambda item: item[0].duration_seconds)
        details = {
            "cause": cause,
            "observation_source_modality": observation.modality,
            "action_source_modality": action.modality if action is not None else "",
            "active_sides": active_sides,
            "hand_mode": "primary" if len(active_sides) == 1 else "all_active_hands",
            "longest_segment": _segment_dict(longest[0]),
            "segments": [
                {**_segment_dict(segment), "cause_evidence": evidence}
                for segment, evidence, _gates in items
            ],
            "thresholds": _threshold_details(settings),
        }
        findings.append(
            {
                "check_name": check_name,
                "severity": severity,
                "status": status,
                "message": message,
                "details": details,
            }
        )
    if by_cause:
        metrics["p7_slow_motion_cause"] = ",".join(sorted(by_cause))
    return findings, metrics, attributor


def _finding_identity(cause: str) -> tuple[str, str, str, str]:
    if cause == "operator_associated":
        return (
            "operator_sustained_slow_motion",
            "major",
            "fail",
            "Sustained slow movement is associated with slow operator commands.",
        )
    if cause == "equipment_associated":
        return (
            "equipment_motion_response_slow",
            "major",
            "fail",
            "Observed robot motion is slow while commanded motion is normal.",
        )
    return (
        "slow_motion_cause_uncertain",
        "major",
        "needs_review",
        "Sustained slow movement was detected but its cause is uncertain.",
    )


def _load_eef_series(episode_path: Path, modalities: tuple[str, ...]) -> EefSeries | None:
    for modality in modalities:
        path = episode_path / modality / "data.csv"
        if not path.is_file():
            continue
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                source_rows = list(csv.DictReader(handle))
            rows = []
            previous_timestamp: float | None = None
            for row in source_rows:
                timestamp = float(row["timestamp_ms"])
                if previous_timestamp is not None and timestamp <= previous_timestamp:
                    continue
                rows.append(row)
                previous_timestamp = timestamp
            if len(rows) < 2:
                continue
            timestamps = [float(row["timestamp_ms"]) for row in rows]
            schemas: dict[str, tuple[str, str, str]] = {}
            headers = set(rows[0])
            if {"left_x", "left_y", "left_z"} <= headers:
                schemas["left"] = ("left_x", "left_y", "left_z")
            if {"right_x", "right_y", "right_z"} <= headers:
                schemas["right"] = ("right_x", "right_y", "right_z")
            if {"x", "y", "z"} <= headers:
                schemas["arm"] = ("x", "y", "z")
            if {"tcp.x", "tcp.y", "tcp.z"} <= headers:
                schemas["arm"] = ("tcp.x", "tcp.y", "tcp.z")
            if not schemas:
                continue
            positions = {
                side: [tuple(float(row[column]) for column in columns) for row in rows]
                for side, columns in schemas.items()
            }
            return EefSeries(modality, timestamps, positions)
        except (OSError, csv.Error, KeyError, TypeError, ValueError):
            continue
    return None


def _speed_series(series: EefSeries, window_seconds: float) -> SpeedSeries:
    step_times = series.timestamps_ms[1:]
    by_side: dict[str, list[float]] = {}
    paths: dict[str, list[float]] = {}
    for side, positions in series.positions.items():
        distances_cm = [
            math.dist(current, previous) * 100.0
            for previous, current in zip(positions, positions[1:])
        ]
        paths[side] = distances_cm
        by_side[side] = _rolling_speed(distances_cm, step_times, window_seconds * 1000.0)
    return SpeedSeries(series.modality, step_times, by_side, paths)


def _rolling_speed(distances_cm: list[float], times_ms: list[float], window_ms: float) -> list[float]:
    queue: deque[tuple[float, float]] = deque()
    path_sum = 0.0
    values = []
    for distance, timestamp in zip(distances_cm, times_ms):
        queue.append((timestamp, distance))
        path_sum += distance
        while queue and timestamp - queue[0][0] > window_ms:
            _old_time, old_distance = queue.popleft()
            path_sum -= old_distance
        if len(queue) < 2:
            values.append(0.0)
            continue
        elapsed_seconds = max(1e-9, (queue[-1][0] - queue[0][0]) / 1000.0)
        values.append(path_sum / elapsed_seconds)
    return values


def _active_sides(speeds: SpeedSeries, settings: SlowMotionSettings) -> list[str]:
    paths = {side: sum(values) for side, values in speeds.step_paths_cm.items()}
    if not paths:
        return []
    primary = max(paths, key=paths.get)
    maximum = max(paths.values())
    active = [
        side
        for side, path in sorted(paths.items())
        if path >= settings.active_hand_min_path_cm
        and (maximum <= 0 or path / maximum >= settings.active_hand_min_ratio)
    ]
    return active or [primary]


def _slow_segments(
    speeds: SpeedSeries,
    active_sides: list[str],
    settings: SlowMotionSettings,
) -> list[SlowSegment]:
    if not active_sides:
        return []
    combined_speeds = [
        max(speeds.by_side_cm_s[side][index] for side in active_sides)
        for index in range(len(speeds.timestamps_ms))
    ]
    combined_paths = [
        max(speeds.step_paths_cm[side][index] for side in active_sides)
        for index in range(len(speeds.timestamps_ms))
    ]
    raw: list[SlowSegment] = []
    start: int | None = None
    for index, speed in enumerate(combined_speeds):
        is_slow = settings.min_speed_cm_s <= speed <= settings.max_speed_cm_s
        if is_slow and start is None:
            start = index
        if start is not None and (not is_slow or index == len(combined_speeds) - 1):
            end = index - 1 if not is_slow else index
            raw.append(_make_segment(start, end, speeds.timestamps_ms, combined_paths))
            start = None
    return _merge_segments(raw, speeds.timestamps_ms, combined_paths, settings.merge_gap_seconds)


def _make_segment(
    start: int,
    end: int,
    times_ms: list[float],
    paths_cm: list[float],
) -> SlowSegment:
    start_ms = times_ms[start]
    end_ms = times_ms[end]
    duration = max(0.0, (end_ms - start_ms) / 1000.0)
    path = sum(paths_cm[start : end + 1])
    return SlowSegment(start, end, start_ms, end_ms, duration, path, path / duration if duration else 0.0)


def _merge_segments(
    segments: list[SlowSegment],
    times_ms: list[float],
    paths_cm: list[float],
    max_gap_seconds: float,
) -> list[SlowSegment]:
    merged: list[SlowSegment] = []
    for segment in segments:
        if merged and (segment.start_ms - merged[-1].end_ms) / 1000.0 <= max_gap_seconds:
            previous = merged[-1]
            merged[-1] = _make_segment(previous.start_index, segment.end_index, times_ms, paths_cm)
        else:
            merged.append(segment)
    return merged


def _activity_gates(
    episode_path: Path,
    task_key: str,
    segment: SlowSegment,
    settings: SlowMotionSettings,
) -> list[str]:
    reasons = []
    if settings.gripper_activity_gate:
        gripper = _window_motion(
            episode_path / "observation.state.gripper" / "data.csv",
            segment.start_ms,
            segment.end_ms,
        )
        if (
            gripper["available"]
            and gripper["total_motion"] >= settings.gripper_window_min_motion
            and gripper["max_field_avg_speed"] >= settings.gripper_window_min_avg_speed
            and gripper["longest_directional_seconds"] < settings.gripper_slow_min_duration_seconds
        ):
            reasons.append("gripper_activity")
    joint = _window_motion(
        episode_path / "observation.state.joint_position" / "data.csv",
        segment.start_ms,
        segment.end_ms,
        exclude_gripper=True,
    )
    if (
        joint["available"]
        and joint["max_field_motion"] >= settings.joint_rotation_min_window_motion
        and joint["max_field_avg_speed"] >= settings.joint_rotation_min_avg_speed
    ):
        reasons.append("joint_rotation_activity")
    if any(fnmatch.fnmatchcase(task_key.lower(), pattern.lower()) for pattern in settings.raw_rotation_task_patterns):
        raw = _window_motion(
            episode_path / "observation.state.raw_gripper_rotation" / "data.csv",
            segment.start_ms,
            segment.end_ms,
        )
        if (
            raw["available"]
            and raw["max_field_motion"] >= settings.raw_rotation_min_window_motion
            and raw["max_field_avg_speed"] >= settings.raw_rotation_min_avg_speed
        ):
            reasons.append("raw_rotation_activity")
    return reasons


def _window_motion(
    path: Path,
    start_ms: float,
    end_ms: float,
    exclude_gripper: bool = False,
) -> dict[str, Any]:
    empty = {
        "available": False,
        "total_motion": 0.0,
        "max_field_motion": 0.0,
        "max_field_avg_speed": 0.0,
        "longest_directional_seconds": 0.0,
    }
    if not path.is_file() or end_ms <= start_ms:
        return empty
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = [
                row
                for row in csv.DictReader(handle)
                if start_ms <= float(row["timestamp_ms"]) <= end_ms
            ]
        if len(rows) < 2:
            return empty
        columns = [
            key
            for key in rows[0]
            if key != "timestamp_ms"
            and key != "is_standstill"
            and (not exclude_gripper or "gripper" not in key.lower())
        ]
        duration = (float(rows[-1]["timestamp_ms"]) - float(rows[0]["timestamp_ms"])) / 1000.0
        motions = []
        longest_directional = 0.0
        for column in columns:
            values = [float(row[column]) for row in rows]
            motions.append(sum(abs(current - previous) for previous, current in zip(values, values[1:])))
            longest_directional = max(longest_directional, _longest_directional(rows, column))
        total = sum(motions)
        maximum = max(motions, default=0.0)
        return {
            "available": bool(columns),
            "total_motion": total,
            "max_field_motion": maximum,
            "max_field_avg_speed": maximum / duration if duration > 0 else 0.0,
            "longest_directional_seconds": longest_directional,
        }
    except (OSError, csv.Error, KeyError, TypeError, ValueError):
        return empty


def _longest_directional(rows: list[dict[str, str]], column: str) -> float:
    longest = 0.0
    start_time: float | None = None
    direction = 0
    previous = float(rows[0][column])
    previous_time = float(rows[0]["timestamp_ms"])
    for row in rows[1:]:
        current = float(row[column])
        current_time = float(row["timestamp_ms"])
        delta = current - previous
        new_direction = 1 if delta > 0 else -1 if delta < 0 else direction
        if direction and new_direction != direction:
            if start_time is not None:
                longest = max(longest, (previous_time - start_time) / 1000.0)
            start_time = previous_time
        elif start_time is None and new_direction:
            start_time = previous_time
        direction = new_direction
        previous = current
        previous_time = current_time
    if start_time is not None:
        longest = max(longest, (previous_time - start_time) / 1000.0)
    return longest


def _segment_dict(segment: SlowSegment) -> dict[str, Any]:
    return {
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "duration_seconds": segment.duration_seconds,
        "path_cm": segment.path_cm,
        "avg_speed_cm_s": segment.avg_speed_cm_s,
    }


def _threshold_details(settings: SlowMotionSettings) -> dict[str, Any]:
    values = asdict(settings)
    values.pop("enabled", None)
    values.pop("task_overrides", None)
    return values


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _validate_settings(settings: SlowMotionSettings) -> None:
    if settings.window_seconds <= 0 or settings.min_duration_seconds <= 0:
        raise RuntimeError("Slow-motion window and duration thresholds must be positive")
    if settings.min_speed_cm_s < 0 or settings.max_speed_cm_s <= settings.min_speed_cm_s:
        raise RuntimeError("Slow-motion speed thresholds are invalid")
    for value in (
        settings.active_hand_min_ratio,
        settings.action_slow_fraction,
        settings.action_normal_fraction,
    ):
        if not 0 <= value <= 1:
            raise RuntimeError("Slow-motion ratio thresholds must be between zero and one")
