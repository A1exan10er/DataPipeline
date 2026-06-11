"""Phase 2 duration and count consistency checks."""

from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable

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


PHASE_NUMBER = 2


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run Phase 2 duration and count consistency checks for each episode."""
    if workers > 1:
        return _run_phase_parallel(states, db_path, progress_callback, workers)
    pending = _pending_states(states)
    total_count = len(pending)
    processed_count = 0
    findings_by_episode: dict[str, list[Finding]] = {}
    for state in pending:
        _ensure_metadata(state)
        findings = []
        findings.extend(_check_duration_absolute_minimum(state))
        findings.extend(_check_duration_positive(state))
        findings.extend(_check_total_frames_positive(state))
        findings.extend(_check_duration_frames_fps_consistency(state))
        findings.extend(_check_timestamps_row_count(state))
        findings.extend(_check_state_csv_row_count(state))
        findings.extend(_check_modality_frame_alignment(state))
        findings_by_episode[str(state.episode_path)] = findings
        _record_metrics(state, findings, 0.0)
        processed_count += 1
        if progress_callback:
            progress_callback(processed_count, total_count)
    if progress_callback:
        progress_callback(len(pending), len(pending))
    print()
    print("  Running group checks...", end="", flush=True)
    for state, finding, iqr_distance in _check_duration_task_outlier(pending):
        findings_by_episode[str(state.episode_path)].append(finding)
        state.metrics["p2_duration_iqr_distance"] = iqr_distance
    for state, finding in _check_duration_absolute(pending):
        findings_by_episode[str(state.episode_path)].append(finding)
    print(" done.")
    for state in pending:
        _finish_state(state, db_path, findings_by_episode[str(state.episode_path)])
    return states


def _process_episode_worker(
    args: tuple[str, dict],
) -> tuple[str, list[dict], dict]:
    """Worker function for multiprocessing. Must be module-level for pickling.

    Args:
        args: Tuple of (episode_path_str, metadata_dict)
              metadata is passed in to avoid re-reading the file in each worker.

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
        task="",
        date="",
        operator="",
        robot="",
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

    findings = []
    findings.extend(_check_duration_absolute_minimum(state))
    findings.extend(_check_duration_positive(state))
    findings.extend(_check_total_frames_positive(state))
    findings.extend(_check_duration_frames_fps_consistency(state))
    findings.extend(_check_timestamps_row_count(state))
    findings.extend(_check_state_csv_row_count(state))
    findings.extend(_check_modality_frame_alignment(state))

    _record_metrics(state, findings, 0.0)

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
    for state, finding, iqr_distance in _check_duration_task_outlier(pending):
        findings_by_episode[str(state.episode_path)].append(finding)
        state.metrics["p2_duration_iqr_distance"] = iqr_distance
    for state, finding in _check_duration_absolute(pending):
        findings_by_episode[str(state.episode_path)].append(finding)
    print(" done.")

    for state in pending:
        _finish_state(state, db_path, findings_by_episode.get(str(state.episode_path), []))
    return states


def _check_duration_positive(state: EpisodeState) -> list[Finding]:
    duration = state.metadata.get("duration_seconds")
    if is_positive_number(duration):
        return []
    return [
        _finding(
            state,
            "duration_not_positive",
            "critical",
            "fail",
            "duration_seconds must be a positive number.",
        )
    ]


def _check_duration_absolute_minimum(state: EpisodeState) -> list[Finding]:
    """Fail any episode shorter than 5 seconds regardless of task median.

    This is a hard floor independent of the relative threshold check.
    Episodes this short are almost certainly test recordings or
    capture failures with no training value.
    """
    duration = state.metadata.get("duration_seconds")
    if not is_positive_number(duration):
        return []
    if float(duration) >= 5.0:
        return []
    return [
        _finding(
            state,
            "duration_under_5s",
            "critical",
            "fail",
            "Episode duration is under 5 seconds and is not usable for training.",
            {"duration_seconds": float(duration), "threshold_seconds": 5.0},
        )
    ]


def _check_total_frames_positive(state: EpisodeState) -> list[Finding]:
    total_frames = _positive_int(state.metadata.get("total_frames"))
    if total_frames is not None:
        return []
    return [
        _finding(
            state,
            "total_frames_not_positive",
            "critical",
            "fail",
            "total_frames must be a positive integer.",
        )
    ]


def _check_duration_frames_fps_consistency(state: EpisodeState) -> list[Finding]:
    values = _duration_values(state)
    if values is None:
        return []
    duration, total_frames, fps = values
    expected_frames = duration * fps
    error_ratio = _error_ratio(total_frames, expected_frames)
    if error_ratio <= 0.10:
        return []
    return [
        _finding(
            state,
            "duration_frames_fps_inconsistent",
            "major",
            "fail",
            "duration_seconds, total_frames, and FPS are inconsistent.",
            {
                "expected_frames": expected_frames,
                "actual_frames": total_frames,
                "error_ratio": error_ratio,
            },
        )
    ]


def _check_timestamps_row_count(state: EpisodeState) -> list[Finding]:
    total_frames = _positive_int(state.metadata.get("total_frames"))
    if total_frames is None:
        return []
    findings = []
    for modality, modality_path in _image_modality_paths(state):
        row_count = count_csv_rows(modality_path / "timestamps.csv")
        if row_count is None:
            findings.append(_timestamps_unreadable(state, modality))
            continue
        error_ratio = _error_ratio(row_count, total_frames)
        if error_ratio > 0.10:
            findings.append(_timestamps_mismatch(state, modality, row_count, total_frames, error_ratio))
    return findings


def _check_state_csv_row_count(state: EpisodeState) -> list[Finding]:
    values = _duration_values(state)
    if values is None:
        return []
    duration, _, fps = values
    expected_rows = duration * fps
    findings = []
    for modality, modality_path in _state_modality_paths(state):
        row_count = count_csv_rows(modality_path / "data.csv")
        if row_count is None:
            continue
        error_ratio = _error_ratio(row_count, expected_rows)
        if error_ratio > 0.15:
            findings.append(_state_row_mismatch(state, modality, row_count, expected_rows, error_ratio))
    return findings


def _check_modality_frame_alignment(state: EpisodeState) -> list[Finding]:
    """Check that all modality frame/row counts are within 3 frames of each other.

    Reads counts from metadata modalities dict:
    - video modalities: 'frames' field
    - csv modalities: 'rows' field

    If max - min <= 3: pass (can be aligned to min during training)
    If max - min > 3: minor/warning
    """
    modalities = state.metadata.get("modalities", {})
    if not isinstance(modalities, dict) or len(modalities) < 2:
        return []

    counts = {}
    for name in modalities:
        count = _get_modality_count(state.metadata, name)
        if count is not None:
            counts[name] = count

    if len(counts) < 2:
        return []

    min_count = min(counts.values())
    max_count = max(counts.values())
    spread = max_count - min_count
    state.metrics["p2_modality_frame_spread"] = spread
    state.metrics["p2_modality_min_frames"] = min_count

    if spread <= 3:
        return []
    elif spread <= 10:
        severity, status = "minor", "warning"
    else:
        severity, status = "major", "needs_review"

    min_modality = min(counts, key=counts.get)
    max_modality = max(counts, key=counts.get)

    return [
        _finding(
            state,
            "modality_frame_count_misaligned",
            severity,
            status,
            f"Modality frame counts differ by {spread} frames (threshold: 3).",
            {
                "spread_frames": spread,
                "min_count": min_count,
                "max_count": max_count,
                "min_modality": min_modality,
                "max_modality": max_modality,
                "all_counts": counts,
            },
        )
    ]


def _get_modality_count(metadata: dict, modality: str) -> int | None:
    """Get frame or row count for a modality from metadata.

    For state tactile modalities with rows=0, falls back to
    frame_count from frame_integrity of the corresponding image modality.

    Returns None if count cannot be determined.
    """
    modalities = metadata.get("modalities", {})
    info = modalities.get(modality)
    if not isinstance(info, dict):
        return None

    count = info.get("frames") or info.get("rows")

    # Fallback: tactile state rows=0 is common, use frame_integrity instead.
    if (
        count == 0
        and modality.startswith("observation.state.")
        and "tactile" in modality
    ):
        image_modality = modality.replace(
            "observation.state.", "observation.image."
        )
        frame_integrity = metadata.get("frame_integrity", {})
        fi = frame_integrity.get(image_modality)
        if isinstance(fi, dict) and fi.get("frame_count", 0) > 0:
            return fi["frame_count"]
        return None

    if isinstance(count, (int, float)) and count > 0:
        return int(count)
    return None


def _check_duration_task_outlier(states: list[EpisodeState]) -> list[tuple[EpisodeState, Finding, float]]:
    grouped = _duration_groups(states)
    results = []
    for task, members in grouped.items():
        if len(members) < 5:
            continue
        durations = sorted(duration for _, duration in members)
        median = _median(durations)
        iqr = _iqr(durations)
        if iqr <= 0:
            continue
        for state, duration in members:
            distance = abs(duration - median) / iqr
            if distance > 3.0:
                results.append((state, _duration_outlier(state, task, duration, median, iqr, distance), distance))
    return results


def _check_duration_absolute(
    states: list[EpisodeState],
) -> list[tuple[EpisodeState, Finding]]:
    """Flag episodes whose duration is extremely short or long relative to task median.

    Thresholds are relative to the task median to adapt automatically to
    different tasks. Runs after per-episode checks as a group-level check.

    Rules:
        duration < median * 0.20 -> major/fail   (likely interrupted recording)
        duration < median * 0.40 -> minor/needs_review
        duration > median * 2.50 -> minor/needs_review  (likely forgot to stop)
    """
    results = []
    grouped = _duration_groups(states)

    for task, members in grouped.items():
        if len(members) < 3:
            continue
        durations = sorted(d for _, d in members)
        median = _median(durations)
        if median <= 0:
            continue

        for state, duration in members:
            ratio = duration / median
            if ratio < 0.20:
                results.append((
                    state,
                    _finding(
                        state,
                        "duration_absolute_too_short",
                        "major",
                        "fail",
                        "Episode duration is less than 20% of task median. Likely an interrupted recording.",
                        {
                            "duration": duration,
                            "task_median": median,
                            "ratio": ratio,
                            "threshold_ratio": 0.20,
                        },
                    ),
                ))
            elif ratio < 0.40:
                results.append((
                    state,
                    _finding(
                        state,
                        "duration_absolute_too_short",
                        "minor",
                        "needs_review",
                        "Episode duration is less than 40% of task median.",
                        {
                            "duration": duration,
                            "task_median": median,
                            "ratio": ratio,
                            "threshold_ratio": 0.40,
                        },
                    ),
                ))
            elif ratio > 2.50:
                results.append((
                    state,
                    _finding(
                        state,
                        "duration_absolute_too_long",
                        "minor",
                        "needs_review",
                        "Episode duration exceeds 250% of task median. Recording may not have been stopped.",
                        {
                            "duration": duration,
                            "task_median": median,
                            "ratio": ratio,
                            "threshold_ratio": 2.50,
                        },
                    ),
                ))

    return results


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


def _finish_state(state: EpisodeState, db_path: Path, new_findings: list[Finding]) -> None:
    state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(new_findings)
    state.findings.extend(new_findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, new_findings)


def _record_metrics(state: EpisodeState, findings: list[Finding], iqr_distance: float) -> None:
    values = _duration_values(state)
    duration, total_frames, fps = values if values is not None else (0.0, 0, 0.0)
    expected_frames = duration * fps
    state.metrics.update(
        {
            "p2_duration_seconds": duration,
            "p2_total_frames": total_frames,
            "p2_fps": fps,
            "p2_expected_frames": expected_frames,
            "p2_frame_count_error_ratio": _error_ratio(total_frames, expected_frames),
            "p2_timestamps_checked": len(_image_modality_paths(state)),
            "p2_timestamps_mismatch_count": _timestamps_mismatch_count(findings),
            "p2_duration_iqr_distance": iqr_distance,
        }
    )


def _duration_values(state: EpisodeState) -> tuple[float, int, float] | None:
    duration = state.metadata.get("duration_seconds")
    total_frames = _positive_int(state.metadata.get("total_frames"))
    fps = _fps_value(state.metadata)
    if not is_positive_number(duration) or total_frames is None or fps is None:
        return None
    return float(duration), total_frames, fps


def _fps_value(metadata: dict) -> float | None:
    fps_actual = metadata.get("fps_actual")
    if is_positive_number(fps_actual):
        return float(fps_actual)
    fps_config = metadata.get("fps_config")
    if is_positive_number(fps_config):
        return float(fps_config)
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _image_modality_paths(state: EpisodeState) -> list[tuple[str, Path]]:
    return [
        (modality, state.episode_path / modality)
        for modality in _modalities(state.metadata)
        if _is_image_modality(modality) and (state.episode_path / modality).is_dir()
    ]


def _state_modality_paths(state: EpisodeState) -> list[tuple[str, Path]]:
    return [
        (modality, state.episode_path / modality)
        for modality in _modalities(state.metadata)
        if modality.startswith("observation.state.") and (state.episode_path / modality).is_dir()
    ]


def _modalities(metadata: dict) -> dict:
    modalities = metadata.get("modalities")
    return modalities if isinstance(modalities, dict) else {}


def _is_image_modality(modality: str) -> bool:
    return modality.startswith("observation.image.") and not modality.startswith("observation.image.flow_")


def _error_ratio(actual: float, expected: float) -> float:
    if expected <= 0:
        return 0.0
    return abs(actual - expected) / expected


def _timestamps_mismatch_count(findings: list[Finding]) -> int:
    return sum(1 for finding in findings if finding.check_name == "timestamps_row_count_mismatch")


def _duration_groups(states: list[EpisodeState]) -> dict[str, list[tuple[EpisodeState, float]]]:
    grouped: dict[str, list[tuple[EpisodeState, float]]] = {}
    for state in states:
        if not is_positive_number(state.metadata.get("duration_seconds")):
            continue
        task = str(first_present(state.task, state.metadata.get("task_key")))
        grouped.setdefault(task, []).append((state, float(state.metadata["duration_seconds"])))
    return grouped


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


def _timestamps_unreadable(state: EpisodeState, modality: str) -> Finding:
    return _finding(
        state,
        "timestamps_unreadable",
        "major",
        "fail",
        "timestamps.csv cannot be read.",
        {"modality": modality},
    )


def _timestamps_mismatch(
    state: EpisodeState, modality: str, row_count: int, total_frames: int, error_ratio: float
) -> Finding:
    return _finding(
        state,
        "timestamps_row_count_mismatch",
        "major",
        "fail",
        "timestamps.csv row count differs from total_frames.",
        {
            "modality": modality,
            "timestamps_rows": row_count,
            "expected_frames": total_frames,
            "error_ratio": error_ratio,
        },
    )


def _state_row_mismatch(
    state: EpisodeState, modality: str, row_count: int, expected_rows: float, error_ratio: float
) -> Finding:
    return _finding(
        state,
        "state_csv_row_count_mismatch",
        "minor",
        "warning",
        "State CSV row count differs from expected duration and FPS.",
        {
            "modality": modality,
            "csv_rows": row_count,
            "expected_rows": expected_rows,
            "error_ratio": error_ratio,
        },
    )


def _duration_outlier(
    state: EpisodeState, task: str, duration: float, median: float, iqr: float, distance: float
) -> Finding:
    return _finding(
        state,
        "duration_task_outlier",
        "minor",
        "needs_review",
        "Episode duration is an outlier for its task.",
        {
            "task": task,
            "duration": duration,
            "median": median,
            "iqr": iqr,
            "iqr_distance": distance,
        },
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
