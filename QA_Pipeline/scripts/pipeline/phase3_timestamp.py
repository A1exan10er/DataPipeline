"""Phase 3 timestamp synchronization and frequency checks."""

from collections import defaultdict
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable
import csv
import math

from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    count_csv_rows,
    decide_status,
    first_present,
    is_positive_number,
    load_metadata,
    save_episode_state,
    save_findings,
)
from scripts.pipeline.qa_config import config_value


PHASE_NUMBER = 3
ALIGNMENT_THRESHOLD_MS = 500.0


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run Phase 3 timestamp checks for each unfinished episode."""
    if workers > 1:
        return _run_phase_parallel(states, db_path, progress_callback, workers)
    pending = _pending_states(states)
    total_count = len(pending)
    processed_count = 0
    findings_by_episode: dict[str, list[Finding]] = {}
    for state in pending:
        _ensure_metadata(state)
        readings = _read_episode_timestamps(state)
        findings = _per_episode_findings(state, readings)
        _record_metrics(state, readings)
        _finish_state(state, db_path, findings)
        findings_by_episode[str(state.episode_path)] = findings
        processed_count += 1
        if progress_callback:
            progress_callback(processed_count, total_count)
    if progress_callback:
        progress_callback(len(pending), len(pending))
    print()
    print("  Running group checks...", end="", flush=True)
    group_findings = _check_frequency_group_outlier(pending)
    group_findings += _check_consecutive_drops_outlier(pending)
    _apply_group_findings(group_findings, findings_by_episode, db_path)
    print(" done.")
    return states


def _process_episode_worker(
    args: tuple[str, dict],
) -> tuple[str, list[dict], dict]:
    """Worker function for multiprocessing. Must be module-level for pickling.

    Args:
        args: Tuple of (episode_path_str, metadata_dict)

    Returns:
        Tuple of (episode_path_str, findings_as_dicts, metrics_dict)
    """
    from pathlib import Path
    from scripts.pipeline.qa_core import EpisodeState
    import datetime

    episode_path_str, metadata = args
    episode_path = Path(episode_path_str)

    state = EpisodeState(
        episode_path=episode_path,
        task=metadata.get("task_key", ""),
        date="",
        operator=metadata.get("username", ""),
        robot=metadata.get("robot", ""),
        controller="",
        metadata=metadata,
        phases_completed=[],
        phase_status={},
        findings=[],
        metrics={},
        final_status="",
        training_ready=None,
        last_updated=datetime.datetime.now().isoformat(),
    )

    readings = _read_episode_timestamps(state)
    findings = _per_episode_findings(state, readings)
    _record_metrics(state, readings)

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
    return episode_path_str, findings_dicts, state.metrics


def _run_phase_parallel(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None,
    workers: int,
) -> list[EpisodeState]:
    pending = _pending_states(states)
    findings_by_episode: dict[str, list[Finding]] = {}

    for state in pending:
        _ensure_metadata(state)

    args = [(str(state.episode_path), state.metadata) for state in pending]
    states_by_path = {str(state.episode_path): state for state in pending}

    with Pool(processes=workers) as pool:
        for index, (episode_path_str, findings_dicts, metrics) in enumerate(
            pool.imap_unordered(_process_episode_worker, args)
        ):
            state = states_by_path[episode_path_str]
            new_findings = [Finding(**item) for item in findings_dicts]
            state.metrics.update(metrics)
            findings_by_episode[episode_path_str] = new_findings
            if progress_callback:
                progress_callback(index + 1, len(pending))

    print()
    print("  Running group checks...", end="", flush=True)
    group_findings = _check_frequency_group_outlier(pending)
    group_findings += _check_consecutive_drops_outlier(pending)
    _apply_group_findings(group_findings, findings_by_episode, db_path)
    print(" done.")

    for state in pending:
        episode_path_str = str(state.episode_path)
        new_findings = findings_by_episode.get(episode_path_str, [])
        _finish_state(state, db_path, new_findings)
    return states


def _pending_states(states: list[EpisodeState]) -> list[EpisodeState]:
    return [
        state
        for state in states
        if PHASE_NUMBER not in state.phases_completed
    ]


def _ensure_metadata(state: EpisodeState) -> None:
    if state.metadata:
        return
    metadata, findings = load_metadata(state.episode_path)
    if not findings:
        state.metadata = metadata


def _read_episode_timestamps(state: EpisodeState) -> dict[str, dict[str, Any]]:
    readings = {}
    for modality in _timestamp_modalities(state):
        source = _timestamp_source(state.episode_path, modality)
        if source is None:
            readings[modality] = {"timestamps": None, "source_path": None, "kind": _modality_kind(modality)}
            continue
        timestamps = _read_image_timestamps(source) if _is_image_modality(modality) else _read_data_timestamps(source)
        readings[modality] = {
            "timestamps": timestamps,
            "source_path": source,
            "kind": _modality_kind(modality),
        }
    return readings


def _per_episode_findings(state: EpisodeState, readings: dict[str, dict[str, Any]]) -> list[Finding]:
    findings = []
    for modality, reading in readings.items():
        timestamps = reading["timestamps"]
        if timestamps is None:
            findings.append(_unreadable_finding(state, modality, reading["source_path"]))
            continue
        findings.extend(_check_monotonic_increasing(state, modality, timestamps))
        findings.extend(_check_duplicate_timestamps(state, modality, timestamps))
        if _is_image_modality(modality):
            findings.extend(_check_frame_drops(state, modality))
        else:
            findings.extend(_check_large_gaps(state, modality, timestamps))
        findings.extend(_check_actual_frequency(state, modality, timestamps))
        findings.extend(_check_timestamps_raw_consistency(state, modality))
    findings.extend(_check_modality_alignment(state, readings))
    return findings


def _finish_state(state: EpisodeState, db_path: Path, new_findings: list[Finding]) -> None:
    state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(new_findings)
    state.findings.extend(new_findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, new_findings)


def _apply_group_findings(
    group_findings: list[tuple[EpisodeState, Finding]], findings_by_episode: dict[str, list[Finding]], db_path: Path
) -> None:
    for state, finding in group_findings:
        key = str(state.episode_path)
        findings_by_episode.setdefault(key, []).append(finding)
        state.findings.append(finding)
        state.phase_status[PHASE_NUMBER] = decide_status(findings_by_episode[key])
        state.last_updated = datetime.now().isoformat()
        save_episode_state(db_path, state)
        save_findings(db_path, findings_by_episode[key])


def _check_monotonic_increasing(
    state: EpisodeState, modality: str, timestamps: list[float]
) -> list[Finding]:
    """Check that timestamp_ms values are strictly increasing.

    Severity scales with the ratio of violations to total rows.
    """
    violations = 0
    first_violation = 0.0
    for previous, current in zip(timestamps, timestamps[1:]):
        if current <= previous:
            violations += 1
            if violations == 1:
                first_violation = current
    if violations == 0:
        return []
    violation_ratio = violations / len(timestamps)
    if violation_ratio >= 0.05:
        severity, status = "major", "fail"
    elif violation_ratio >= 0.01:
        severity, status = "major", "needs_review"
    else:
        severity, status = "minor", "warning"
    return [
        _finding(
            state,
            "timestamps_not_monotonic",
            severity,
            status,
            "Timestamps are not strictly increasing.",
            {
                "modality": modality,
                "violation_count": violations,
                "violation_ratio": violation_ratio,
                "first_violation_ms": first_violation,
            },
        )
    ]


def _check_duplicate_timestamps(
    state: EpisodeState, modality: str, timestamps: list[float]
) -> list[Finding]:
    """Check for duplicate timestamp_ms values.

    Severity scales with the ratio of duplicates to total rows.
    """
    duplicate_count = len(timestamps) - len(set(timestamps))
    if duplicate_count == 0:
        return []
    duplicate_ratio = duplicate_count / len(timestamps)
    if duplicate_ratio >= 0.05:
        severity, status = "major", "fail"
    elif duplicate_ratio >= 0.01:
        severity, status = "major", "needs_review"
    else:
        severity, status = "minor", "warning"
    return [
        _finding(
            state,
            "duplicate_timestamps",
            severity,
            status,
            "Duplicate timestamp_ms values were found.",
            {
                "modality": modality,
                "duplicate_count": duplicate_count,
                "duplicate_ratio": duplicate_ratio,
            },
        )
    ]


def _check_large_gaps(state: EpisodeState, modality: str, timestamps: list[float]) -> list[Finding]:
    intervals = _intervals(timestamps)
    median_interval = _median(intervals) if intervals else 0.0
    if median_interval <= 0:
        return []
    max_gap = max(intervals)
    if max_gap > 20.0 * median_interval:
        severity = "major"
        status = "fail"
    elif max_gap > 5.0 * median_interval:
        severity = "minor"
        status = "warning"
    else:
        return []
    index = intervals.index(max_gap)
    return [_large_gap_finding(state, modality, max_gap, median_interval, timestamps[index], severity, status)]


def _check_frame_drops(state: EpisodeState, modality: str) -> list[Finding]:
    """Check frame drop ratio from metadata frame_integrity field.

    Uses hard configured thresholds:
    - normal image streams: drop ratio threshold;
    - tactile image streams: tactile-specific drop ratio threshold;
    - all image streams: max consecutive drops threshold.
    """
    frame_integrity = state.metadata.get("frame_integrity", {})
    info = frame_integrity.get(modality)
    if not isinstance(info, dict):
        return []

    frame_count = info.get("frame_count", 0)
    total_drops = info.get("total_drops", 0)
    max_consecutive = info.get("max_consecutive_drops", 0)

    if frame_count <= 0:
        return []

    findings = []
    drop_ratio = total_drops / frame_count
    thresholds = _drop_thresholds(modality)
    drop_ratio_threshold = thresholds["drop_ratio_fail"]
    if drop_ratio > drop_ratio_threshold:
        findings.append(
            _frame_drop_ratio_finding(
                state,
                modality,
                "major",
                "fail",
                drop_ratio,
                drop_ratio_threshold,
                total_drops,
                frame_count,
                "Frame drop ratio exceeds configured hard threshold.",
            )
        )
    if max_consecutive >= thresholds["max_consecutive_fail"]:
        findings.append(
            _frame_drop_consecutive_finding(
                state,
                modality,
                "major",
                "fail",
                int(max_consecutive),
                thresholds["max_consecutive_fail"],
                total_drops,
                frame_count,
                drop_ratio,
                "Consecutive frame drops exceed configured hard threshold.",
            )
        )
    return findings


def _drop_thresholds(modality: str) -> dict:
    """Return frame drop thresholds for the given image modality.

    drop_ratio_fail: total_drops / frame_count at or above this -> major/fail
    max_consecutive_fail: max_consecutive_drops at or above this -> major/fail
    max_consecutive_warn: max_consecutive_drops at or above this -> minor/warning
        (used only as a fallback when group size is too small for statistics)
    """
    ratio_key = (
        "tactile_video_drop_ratio_fail"
        if _is_tactile_modality(modality)
        else "normal_video_drop_ratio_fail"
    )
    ratio_default = 0.20 if _is_tactile_modality(modality) else 0.15
    return {
        "drop_ratio_fail": float(
            config_value(["phase3_timestamp", "frame_drops", ratio_key], ratio_default)
        ),
        "max_consecutive_fail": int(
            config_value(["phase3_timestamp", "frame_drops", "max_consecutive_fail"], 25)
        ),
        "max_consecutive_warn": int(
            config_value(["phase3_timestamp", "frame_drops", "max_consecutive_warn"], 10)
        ),
    }


def _check_actual_frequency(state: EpisodeState, modality: str, timestamps: list[float]) -> list[Finding]:
    actual_fps = _actual_fps(timestamps)
    expected_fps = _expected_fps(state, modality)
    if actual_fps is None or expected_fps is None:
        return []
    loss_ratio = max(0.0, (expected_fps - actual_fps) / expected_fps)
    gain_ratio = max(0.0, (actual_fps - expected_fps) / expected_fps)
    loss_fail_ratio = float(
        config_value(["phase3_timestamp", "abnormal_fps", "loss_fail_ratio"], 0.10)
    )
    gain_warning_ratio = float(
        config_value(["phase3_timestamp", "abnormal_fps", "gain_warning_ratio"], 0.10)
    )
    if loss_ratio > loss_fail_ratio:
        return [
            _frequency_deviation_finding(
                state,
                modality,
                actual_fps,
                expected_fps,
                loss_ratio,
                loss_fail_ratio,
                "abnormal_fps_loss",
                "major",
                "fail",
                "Actual FPS is lower than expected beyond configured threshold.",
            )
        ]
    if gain_ratio > gain_warning_ratio:
        return [
            _frequency_deviation_finding(
                state,
                modality,
                actual_fps,
                expected_fps,
                gain_ratio,
                gain_warning_ratio,
                "abnormal_fps_gain",
                "minor",
                "warning",
                "Actual FPS is higher than expected beyond configured threshold.",
            )
        ]
    return []


def _check_timestamps_raw_consistency(state: EpisodeState, modality: str) -> list[Finding]:
    if not _is_image_modality(modality):
        return []
    processed_path = state.episode_path / modality / "timestamps.csv"
    raw_path = state.episode_path / modality / "timestamps_raw.csv"
    if not processed_path.exists() or not raw_path.exists():
        return []
    processed_rows = count_csv_rows(processed_path)
    raw_rows = count_csv_rows(raw_path)
    if processed_rows is None or raw_rows is None or processed_rows <= 0:
        return []
    difference = abs(raw_rows - processed_rows)
    if difference <= 2:
        return []
    return [_raw_inconsistency_finding(state, modality, processed_rows, raw_rows, difference)]


def _check_modality_alignment(state: EpisodeState, readings: dict[str, dict[str, Any]]) -> list[Finding]:
    spans = _readable_spans(readings)
    if len(spans) < 2:
        return []
    findings = []
    start_finding = _alignment_finding(state, spans, "start")
    end_finding = _alignment_finding(state, spans, "end")
    if start_finding is not None:
        findings.append(start_finding)
    if end_finding is not None:
        findings.append(end_finding)
    return findings


def _check_frequency_group_outlier(states: list[EpisodeState]) -> list[tuple[EpisodeState, Finding]]:
    results = []
    grouped_values = _group_frequency_values(states)
    for group_key, modalities in grouped_values.items():
        for modality, values in modalities.items():
            if len(values) < 5:
                continue
            results.extend(_frequency_outliers_for_values(group_key, modality, values))
    return results


def _group_frequency_values(
    states: list[EpisodeState],
) -> dict[str, dict[str, list[tuple[EpisodeState, float]]]]:
    grouped: dict[str, dict[str, list[tuple[EpisodeState, float]]]] = defaultdict(lambda: defaultdict(list))
    for state in states:
        group_key = str(first_present(state.task, state.metadata.get("task_key"))) + "_" + str(state.robot)
        for modality in _timestamp_modalities(state):
            value = state.metrics.get(f"p3_{_modality_key(modality)}_actual_fps")
            if _is_finite_number(value):
                grouped[group_key][modality].append((state, float(value)))
    return grouped


def _frequency_outliers_for_values(
    group_key: str, modality: str, values: list[tuple[EpisodeState, float]]
) -> list[tuple[EpisodeState, Finding]]:
    fps_values = sorted(value for _, value in values)
    median = _median(fps_values)
    iqr = _iqr(fps_values)
    if iqr <= 0:
        return []
    return [
        (state, _frequency_group_outlier_finding(state, modality, actual_fps, median, iqr, group_key))
        for state, actual_fps in values
        if abs(actual_fps - median) / iqr > 3.0
    ]


def _check_consecutive_drops_outlier(
    states: list[EpisodeState],
) -> list[tuple[EpisodeState, Finding]]:
    """Detect episodes with abnormally high max_consecutive_drops using IQR.

    Groups episodes by task + robot. Within each group and each modality,
    computes median and IQR of max_consecutive_drops values. Episodes whose
    value exceeds median + 3.0 * IQR are flagged as needs_review.

    Falls back to a fixed warning threshold when group size is less than 5.
    """
    results = []
    grouped = _group_consecutive_drop_values(states)

    for group_key, modalities in grouped.items():
        for modality, members in modalities.items():
            if len(members) < 5:
                results.extend(_consecutive_drops_fallback(members, modality))
                continue
            values = sorted(v for _, v in members)
            median = _median(values)
            iqr = _iqr(values)
            if iqr <= 0:
                continue
            threshold = median + 3.0 * iqr
            for state, value in members:
                if value > threshold:
                    results.append((
                        state,
                        _finding(
                            state,
                            "consecutive_drops_outlier",
                            "major",
                            "needs_review",
                            "Max consecutive frame drops is a statistical outlier within task and robot group.",
                            {
                                "modality": modality,
                                "max_consecutive_drops": value,
                                "group_median": median,
                                "group_iqr": iqr,
                                "threshold": threshold,
                                "group": group_key,
                            },
                        ),
                    ))
    return results


def _group_consecutive_drop_values(
    states: list[EpisodeState],
) -> dict[str, dict[str, list[tuple[EpisodeState, float]]]]:
    """Group max_consecutive_drops values by task+robot and modality."""
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for state in states:
        group_key = (
            str(first_present(state.task, state.metadata.get("task_key")))
            + "_"
            + str(state.robot)
        )
        frame_integrity = state.metadata.get("frame_integrity", {})
        for modality, info in frame_integrity.items():
            if not isinstance(info, dict):
                continue
            value = info.get("max_consecutive_drops")
            if isinstance(value, (int, float)) and value >= 0:
                grouped[group_key][modality].append((state, float(value)))
    return grouped


def _consecutive_drops_fallback(
    members: list[tuple[EpisodeState, float]], modality: str
) -> list[tuple[EpisodeState, Finding]]:
    """Fallback check when group size is too small for IQR statistics.

    Uses a fixed threshold from _drop_thresholds instead.
    """
    results = []
    for state, value in members:
        warn_threshold = _drop_thresholds(modality)["max_consecutive_warn"]
        if value >= warn_threshold:
            results.append((
                state,
                _finding(
                    state,
                    "consecutive_drops_outlier",
                    "minor",
                    "warning",
                    "Max consecutive frame drops exceeds fallback threshold (group too small for statistics).",
                    {
                        "modality": modality,
                        "max_consecutive_drops": value,
                        "threshold": warn_threshold,
                        "group_size": len(members),
                    },
                ),
            ))
    return results


def _timestamp_modalities(state: EpisodeState) -> list[str]:
    """Return image modalities that have timestamps to check.

    Only checks observation.image.* modalities (excluding flow).
    State and action modality timestamps are checked in Phase 5.
    """
    names = set()
    for modality in _metadata_modalities(state.metadata):
        if _is_image_modality(modality):
            names.add(modality)
    try:
        for child in state.episode_path.iterdir():
            if child.is_dir() and _is_image_modality(child.name):
                names.add(child.name)
    except OSError:
        pass
    return sorted(names)


def _metadata_modalities(metadata: dict) -> dict:
    modalities = metadata.get("modalities")
    return modalities if isinstance(modalities, dict) else {}


def _timestamp_source(episode_path: Path, modality: str) -> Path | None:
    modality_path = episode_path / modality
    if _is_image_modality(modality):
        return modality_path / "timestamps.csv"
    if _is_state_or_action_modality(modality):
        return modality_path / "data.csv"
    return None


def _read_image_timestamps(path: Path) -> list[float] | None:
    rows = _read_timestamp_rows(path)
    if rows is None:
        return None
    return [timestamp for timestamp, is_new in rows if is_new == "1"]


def _read_data_timestamps(path: Path) -> list[float] | None:
    rows = _read_timestamp_rows(path)
    if rows is None:
        return None
    return [timestamp for timestamp, _ in rows]


def _read_timestamp_rows(path: Path) -> list[tuple[float, str | None]] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as file_obj:
            return _rows_from_reader(csv.DictReader(file_obj))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def _rows_from_reader(reader: csv.DictReader) -> list[tuple[float, str | None]]:
    rows = []
    for row in reader:
        timestamp = _float_or_none(row.get("timestamp_ms"))
        if timestamp is None:
            continue
        rows.append((timestamp, row.get("is_new")))
    return rows


def _record_metrics(state: EpisodeState, readings: dict[str, dict[str, Any]]) -> None:
    for modality, reading in readings.items():
        timestamps = reading["timestamps"] or []
        key = _modality_key(modality)
        intervals = _intervals(timestamps)
        state.metrics.update(
            {
                f"p3_{key}_actual_fps": _actual_fps(timestamps) or 0.0,
                f"p3_{key}_row_count": len(timestamps),
                f"p3_{key}_max_gap_ms": max(intervals) if intervals else 0.0,
                f"p3_{key}_duplicate_count": len(timestamps) - len(set(timestamps)),
                f"p3_{key}_monotonic_ok": _monotonic_ok(timestamps),
            }
        )
    frame_integrity = state.metadata.get("frame_integrity", {})
    for modality, info in frame_integrity.items():
        if not isinstance(info, dict):
            continue
        key = _modality_key(modality)
        frame_count = info.get("frame_count", 0)
        total_drops = info.get("total_drops", 0)
        state.metrics[f"p3_{key}_total_drops"] = total_drops
        state.metrics[f"p3_{key}_max_consecutive_drops"] = info.get("max_consecutive_drops", 0)
        state.metrics[f"p3_{key}_drop_ratio"] = (
            total_drops / frame_count if frame_count > 0 else 0.0
        )


def _readable_spans(readings: dict[str, dict[str, Any]]) -> list[tuple[str, float, float]]:
    spans = []
    for modality, reading in readings.items():
        timestamps = reading["timestamps"]
        if timestamps:
            spans.append((modality, timestamps[0], timestamps[-1]))
    return spans


def _alignment_finding(state: EpisodeState, spans: list[tuple[str, float, float]], side: str) -> Finding | None:
    value_index = 1 if side == "start" else 2
    earliest = min(spans, key=lambda item: item[value_index])
    latest = max(spans, key=lambda item: item[value_index])
    spread = latest[value_index] - earliest[value_index]
    if spread <= ALIGNMENT_THRESHOLD_MS:
        return None
    return _alignment_spread_finding(state, side, spread, earliest[0], latest[0])


def _expected_fps(state: EpisodeState, modality: str) -> float | None:
    metadata = _metadata_modalities(state.metadata).get(modality)
    if metadata is None and _is_state_or_action_modality(modality):
        metadata = _metadata_modalities(state.metadata).get("actions")
    if isinstance(metadata, dict):
        expected = first_present(metadata.get("hz"), metadata.get("frequency"), metadata.get("hz_nominal"))
        if is_positive_number(expected):
            return float(expected)
    fallback = first_present(state.metadata.get("fps_actual"), state.metadata.get("fps_config"))
    return float(fallback) if is_positive_number(fallback) else None


def _actual_fps(timestamps: list[float]) -> float | None:
    if len(timestamps) < 2:
        return None
    duration_seconds = (timestamps[-1] - timestamps[0]) / 1000.0
    if duration_seconds <= 0:
        return None
    return (len(timestamps) - 1) / duration_seconds


def _intervals(timestamps: list[float]) -> list[float]:
    return [current - previous for previous, current in zip(timestamps, timestamps[1:])]


def _monotonic_ok(timestamps: list[float]) -> bool:
    return all(current > previous for previous, current in zip(timestamps, timestamps[1:]))


def _modality_key(modality: str) -> str:
    return modality.replace(".", "_")


def _modality_kind(modality: str) -> str:
    return "image" if _is_image_modality(modality) else "data"


def _has_timestamps(name: str) -> bool:
    return _is_image_modality(name) or _is_state_or_action_modality(name)


def _is_image_modality(modality: str) -> bool:
    return modality.startswith("observation.image.") and not modality.startswith("observation.image.flow_")


def _is_tactile_modality(modality: str) -> bool:
    return "tactile" in modality.lower()


def _is_state_or_action_modality(modality: str) -> bool:
    return (
        modality.startswith("observation.state.")
        or modality.startswith("action.")
        or modality.startswith("actions.")
    )


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _median(values: list[float]) -> float:
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / 2.0


def _iqr(values: list[float]) -> float:
    midpoint = len(values) // 2
    lower = values[:midpoint]
    upper = values[midpoint:] if len(values) % 2 == 0 else values[midpoint + 1 :]
    if not lower or not upper:
        return 0.0
    return _median(upper) - _median(lower)


def _timestamp_order_finding(
    state: EpisodeState,
    modality: str,
    violation_count: int,
    first_violation_ms: float,
    severity: str,
    status: str,
) -> Finding:
    return _finding(
        state,
        "timestamps_not_monotonic",
        severity,
        status,
        "Timestamps are not strictly increasing.",
        {
            "modality": modality,
            "violation_count": violation_count,
            "first_violation_ms": first_violation_ms,
        },
    )


def _large_gap_finding(
    state: EpisodeState,
    modality: str,
    gap_ms: float,
    median_interval_ms: float,
    at_timestamp_ms: float,
    severity: str,
    status: str,
) -> Finding:
    return _finding(
        state,
        "timestamp_large_gap",
        severity,
        status,
        "A timestamp gap exceeds five times the median interval.",
        {
            "modality": modality,
            "gap_ms": gap_ms,
            "median_interval_ms": median_interval_ms,
            "gap_ratio": gap_ms / median_interval_ms,
            "at_timestamp_ms": at_timestamp_ms,
        },
    )


def _frame_drop_consecutive_finding(
    state: EpisodeState,
    modality: str,
    severity: str,
    status: str,
    max_consecutive: int,
    threshold: int,
    total_drops: int,
    frame_count: int,
    drop_ratio: float,
    message: str,
) -> Finding:
    return _finding(
        state,
        "frame_drop_consecutive",
        severity,
        status,
        message,
        {
            "modality": modality,
            "max_consecutive_drops": max_consecutive,
            "threshold": threshold,
            "total_drops": total_drops,
            "frame_count": frame_count,
            "drop_ratio": drop_ratio,
        },
    )


def _frame_drop_ratio_finding(
    state: EpisodeState,
    modality: str,
    severity: str,
    status: str,
    drop_ratio: float,
    threshold: float,
    total_drops: int,
    frame_count: int,
    message: str,
) -> Finding:
    return _finding(
        state,
        "frame_drop_ratio",
        severity,
        status,
        message,
        {
            "modality": modality,
            "drop_ratio": drop_ratio,
            "threshold": threshold,
            "total_drops": total_drops,
            "frame_count": frame_count,
        },
    )


def _frequency_deviation_finding(
    state: EpisodeState,
    modality: str,
    actual_fps: float,
    expected_fps: float,
    deviation_ratio: float,
    threshold: float,
    check_name: str,
    severity: str,
    status: str,
    message: str,
) -> Finding:
    return _finding(
        state,
        check_name,
        severity,
        status,
        message,
        {
            "modality": modality,
            "actual_fps": actual_fps,
            "expected_fps": expected_fps,
            "deviation_ratio": deviation_ratio,
            "threshold": threshold,
        },
    )


def _raw_inconsistency_finding(
    state: EpisodeState, modality: str, processed_rows: int, raw_rows: int, difference: int
) -> Finding:
    return _finding(
        state,
        "timestamps_raw_inconsistency",
        "minor",
        "warning",
        "Raw and processed timestamp row counts differ substantially.",
        {
            "modality": modality,
            "processed_rows": processed_rows,
            "raw_rows": raw_rows,
            "difference": difference,
        },
    )


def _alignment_spread_finding(
    state: EpisodeState, side: str, spread_ms: float, earliest_modality: str, latest_modality: str
) -> Finding:
    return _finding(
        state,
        "modality_alignment_" + side,
        "major",
        "fail",
        "Timestamp alignment spread exceeds threshold.",
        {
            "spread_ms": spread_ms,
            "earliest_modality": earliest_modality,
            "latest_modality": latest_modality,
            "threshold_ms": 500,
        },
    )


def _frequency_group_outlier_finding(
    state: EpisodeState, modality: str, actual_fps: float, median: float, iqr: float, group: str
) -> Finding:
    return _finding(
        state,
        "frequency_group_outlier",
        "minor",
        "needs_review",
        "Actual frequency is an outlier within task and robot group.",
        {
            "modality": modality,
            "actual_fps": actual_fps,
            "median_fps": median,
            "iqr": iqr,
            "iqr_distance": abs(actual_fps - median) / iqr,
            "group": group,
        },
    )


def _unreadable_finding(state: EpisodeState, modality: str, path: Path | None) -> Finding:
    return _finding(
        state,
        "timestamps_unreadable",
        "major",
        "fail",
        "Timestamp source file is missing or unreadable.",
        {"modality": modality, "file": str(path) if path is not None else ""},
    )


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
