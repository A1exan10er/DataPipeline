"""Phase 7 operator standstill / motion-content checks."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable

from scripts.pipeline.qa_config import config_value
from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    decide_status,
    load_metadata,
    save_episode_state,
    save_findings,
)


PHASE_NUMBER = 7


@dataclass
class StandstillSettings:
    enabled: bool
    motion_delta_threshold: float
    stillness_buffer_ms: float
    warn_segment_ms: float
    review_segment_ms: float
    fail_segment_ms: float
    review_excess_ratio: float
    fail_excess_ratio: float
    edge_tolerance_ms: float
    min_useful_motion_ms: float
    source_modalities: list[str]


@dataclass
class MotionSeries:
    modality: str
    timestamps_ms: list[float]
    values: list[list[float]]
    columns: list[str]


@dataclass
class StandstillSegment:
    stop_start_ms: float
    stop_end_ms: float
    duration_ms: float
    excess_ms: float
    location: str


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run Phase 7 standstill checks for each unfinished episode."""
    pending = [state for state in states if PHASE_NUMBER not in state.phases_completed]
    settings = _settings()
    if workers > 1:
        return _run_phase_parallel(states, pending, db_path, progress_callback, workers, settings)
    processed = 0
    for state in pending:
        _ensure_metadata(state)
        findings, metrics = _episode_findings(state.episode_path, settings)
        state.metrics.update(metrics)
        _finish_state(state, db_path, findings)
        processed += 1
        if progress_callback:
            progress_callback(processed, len(pending))
    return states


def _run_phase_parallel(
    states: list[EpisodeState],
    pending: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None,
    workers: int,
    settings: StandstillSettings,
) -> list[EpisodeState]:
    if not pending:
        return states
    args = [(str(state.episode_path), settings) for state in pending]
    states_by_path = {str(state.episode_path): state for state in pending}
    with Pool(processes=workers) as pool:
        for index, (episode_path_str, finding_dicts, metrics) in enumerate(pool.imap_unordered(_worker, args), start=1):
            state = states_by_path[episode_path_str]
            findings = [Finding(**item) for item in finding_dicts]
            state.metrics.update(metrics)
            _finish_state(state, db_path, findings)
            if progress_callback:
                progress_callback(index, len(pending))
    return states


def _worker(args: tuple[str, StandstillSettings]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    episode_path_str, settings = args
    findings, metrics = _episode_findings(Path(episode_path_str), settings)
    return (
        episode_path_str,
        [
            {
                "episode_path": item.episode_path,
                "phase": item.phase,
                "check_name": item.check_name,
                "severity": item.severity,
                "status": item.status,
                "message": item.message,
                "details": item.details,
            }
            for item in findings
        ],
        metrics,
    )


def _episode_findings(episode_path: Path, settings: StandstillSettings) -> tuple[list[Finding], dict[str, Any]]:
    metrics = _initial_metrics()
    if not settings.enabled:
        return [], metrics
    series, source_error = _load_motion_series(episode_path, settings)
    if series is None:
        return [_finding(episode_path, "standstill_motion_source_missing", "minor", "warning", source_error)], metrics

    duration_ms = series.timestamps_ms[-1] - series.timestamps_ms[0] if len(series.timestamps_ms) >= 2 else 0.0
    if duration_ms <= 0:
        return [
            _finding(
                episode_path,
                "standstill_motion_source_invalid",
                "major",
                "needs_review",
                "Motion source has insufficient monotonic timestamp data for standstill detection.",
                {"source_modality": series.modality, "row_count": len(series.timestamps_ms)},
            )
        ], metrics

    segments = _detect_segments(series, settings)
    useful_motion_ms = max(0.0, duration_ms - sum(segment.duration_ms for segment in segments))
    total_excess_ms = sum(segment.excess_ms for segment in segments)
    excess_ratio = total_excess_ms / duration_ms if duration_ms > 0 else 0.0
    metrics.update(
        {
            "p7_source_modality": series.modality,
            "p7_duration_ms": duration_ms,
            "p7_standstill_segment_count": len(segments),
            "p7_standstill_total_ms": sum(segment.duration_ms for segment in segments),
            "p7_standstill_excess_ms": total_excess_ms,
            "p7_standstill_excess_ratio": excess_ratio,
            "p7_useful_motion_ms": useful_motion_ms,
            "p7_leading_standstill_ms": sum(s.duration_ms for s in segments if s.location == "leading"),
            "p7_middle_standstill_ms": sum(s.duration_ms for s in segments if s.location == "middle"),
            "p7_trailing_standstill_ms": sum(s.duration_ms for s in segments if s.location == "trailing"),
        }
    )

    findings = _segment_findings(episode_path, series, segments, settings)
    summary = _summary_finding(episode_path, series, segments, duration_ms, total_excess_ms, excess_ratio, useful_motion_ms, settings)
    if summary is not None:
        findings.append(summary)
    return findings, metrics


def _load_motion_series(episode_path: Path, settings: StandstillSettings) -> tuple[MotionSeries | None, str]:
    errors = []
    for modality in settings.source_modalities:
        csv_path = episode_path / modality / "data.csv"
        if not csv_path.is_file():
            continue
        try:
            series = _read_motion_csv(csv_path, modality)
        except (OSError, csv.Error, ValueError) as exc:
            errors.append(f"{modality}: {exc}")
            continue
        if series is not None:
            return series, ""
        errors.append(f"{modality}: no usable motion columns")
    if errors:
        return None, "No usable motion source found for standstill detection. " + "; ".join(errors[:5])
    return None, "No configured motion source data.csv exists for standstill detection."


def _read_motion_csv(csv_path: Path, modality: str) -> MotionSeries | None:
    with csv_path.open("r", newline="", encoding="utf-8") as file_obj:
        reader = csv.reader(file_obj)
        headers = next(reader, None)
        if not headers or "timestamp_ms" not in headers:
            return None
        ts_idx = headers.index("timestamp_ms")
        value_indices = [
            index
            for index, header in enumerate(headers)
            if index != ts_idx and header != "is_standstill" and "gripper" not in header.lower()
        ]
        if not value_indices:
            return None
        timestamps: list[float] = []
        values: list[list[float]] = []
        for row in reader:
            if len(row) <= ts_idx or len(row) <= max(value_indices):
                continue
            try:
                timestamp = float(row[ts_idx])
                row_values = [float(row[index]) for index in value_indices]
            except ValueError:
                continue
            if timestamps and timestamp <= timestamps[-1]:
                continue
            timestamps.append(timestamp)
            values.append(row_values)
    if len(timestamps) < 2:
        return None
    return MotionSeries(
        modality=modality,
        timestamps_ms=timestamps,
        values=values,
        columns=[headers[index] for index in value_indices],
    )


def _detect_segments(series: MotionSeries, settings: StandstillSettings) -> list[StandstillSegment]:
    raw_segments: list[tuple[float, float]] = []
    start_ms: float | None = None
    end_ms: float | None = None
    for index in range(1, len(series.timestamps_ms)):
        if _pair_still(series.values[index - 1], series.values[index], settings.motion_delta_threshold):
            if start_ms is None:
                start_ms = series.timestamps_ms[index - 1]
            end_ms = series.timestamps_ms[index]
        elif start_ms is not None and end_ms is not None:
            raw_segments.append((start_ms, end_ms))
            start_ms = None
            end_ms = None
    if start_ms is not None and end_ms is not None:
        raw_segments.append((start_ms, end_ms))

    first_ts = series.timestamps_ms[0]
    last_ts = series.timestamps_ms[-1]
    segments = []
    for start, end in raw_segments:
        duration = end - start
        if duration <= settings.stillness_buffer_ms:
            continue
        segments.append(
            StandstillSegment(
                stop_start_ms=start,
                stop_end_ms=end,
                duration_ms=duration,
                excess_ms=duration - settings.stillness_buffer_ms,
                location=_segment_location(start, end, first_ts, last_ts, settings.edge_tolerance_ms),
            )
        )
    return segments


def _pair_still(previous: list[float], current: list[float], threshold: float) -> bool:
    return all(abs(curr - prev) < threshold for prev, curr in zip(previous, current))


def _segment_location(start_ms: float, end_ms: float, first_ts: float, last_ts: float, edge_tolerance_ms: float) -> str:
    if start_ms <= first_ts + edge_tolerance_ms:
        return "leading"
    if end_ms >= last_ts - edge_tolerance_ms:
        return "trailing"
    return "middle"


def _segment_findings(
    episode_path: Path,
    series: MotionSeries,
    segments: list[StandstillSegment],
    settings: StandstillSettings,
) -> list[Finding]:
    findings = []
    for index, segment in enumerate(segments, start=1):
        check_name = f"operator_standstill_{segment.location}"
        severity, status = _segment_status(segment.duration_ms, settings)
        findings.append(
            _finding(
                episode_path,
                check_name,
                severity,
                status,
                f"Operator standstill detected in {segment.location} part of episode.",
                {
                    "source_modality": series.modality,
                    "stop_start_ms": segment.stop_start_ms,
                    "stop_end_ms": segment.stop_end_ms,
                    "duration_ms": segment.duration_ms,
                    "excess_ms": segment.excess_ms,
                    "location": segment.location,
                    "segment_index": index,
                    "total_segments": len(segments),
                    "motion_delta_threshold": settings.motion_delta_threshold,
                    "stillness_buffer_ms": settings.stillness_buffer_ms,
                },
            )
        )
    return findings


def _summary_finding(
    episode_path: Path,
    series: MotionSeries,
    segments: list[StandstillSegment],
    duration_ms: float,
    total_excess_ms: float,
    excess_ratio: float,
    useful_motion_ms: float,
    settings: StandstillSettings,
) -> Finding | None:
    if useful_motion_ms < settings.min_useful_motion_ms and segments:
        return _finding(
            episode_path,
            "operator_standstill_motion_too_short",
            "critical",
            "fail",
            "Useful non-standstill motion duration is below the configured minimum.",
            _summary_details(series, segments, duration_ms, total_excess_ms, excess_ratio, useful_motion_ms, settings),
        )
    if excess_ratio >= settings.fail_excess_ratio:
        return _finding(
            episode_path,
            "operator_standstill_excessive",
            "critical",
            "fail",
            "Operator standstill excess occupies too much of the episode.",
            _summary_details(series, segments, duration_ms, total_excess_ms, excess_ratio, useful_motion_ms, settings),
        )
    if excess_ratio >= settings.review_excess_ratio:
        return _finding(
            episode_path,
            "operator_standstill_excessive",
            "major",
            "needs_review",
            "Operator standstill excess occupies a large portion of the episode.",
            _summary_details(series, segments, duration_ms, total_excess_ms, excess_ratio, useful_motion_ms, settings),
        )
    return None


def _summary_details(
    series: MotionSeries,
    segments: list[StandstillSegment],
    duration_ms: float,
    total_excess_ms: float,
    excess_ratio: float,
    useful_motion_ms: float,
    settings: StandstillSettings,
) -> dict[str, Any]:
    return {
        "source_modality": series.modality,
        "episode_duration_ms": duration_ms,
        "segment_count": len(segments),
        "total_excess_ms": total_excess_ms,
        "excess_ratio": excess_ratio,
        "useful_motion_ms": useful_motion_ms,
        "review_excess_ratio": settings.review_excess_ratio,
        "fail_excess_ratio": settings.fail_excess_ratio,
        "min_useful_motion_ms": settings.min_useful_motion_ms,
        "segments": [
            {
                "location": segment.location,
                "stop_start_ms": segment.stop_start_ms,
                "stop_end_ms": segment.stop_end_ms,
                "duration_ms": segment.duration_ms,
                "excess_ms": segment.excess_ms,
            }
            for segment in segments
        ],
    }


def _segment_status(duration_ms: float, settings: StandstillSettings) -> tuple[str, str]:
    if duration_ms >= settings.fail_segment_ms:
        return "major", "fail"
    if duration_ms >= settings.review_segment_ms:
        return "major", "needs_review"
    if duration_ms >= settings.warn_segment_ms:
        return "minor", "warning"
    return "info", "pass"


def _settings() -> StandstillSettings:
    source_modalities = config_value(
        ["phase7_standstill", "source_modalities"],
        config_value(["standstill_trim", "source_modalities"], ["observation.state.joint_position", "actions.joint_position"]),
    )
    if not isinstance(source_modalities, list):
        source_modalities = ["observation.state.joint_position", "actions.joint_position"]
    return StandstillSettings(
        enabled=bool(config_value(["phase7_standstill", "enabled"], True)),
        motion_delta_threshold=float(config_value(["phase7_standstill", "motion_delta_threshold"], 0.001)),
        stillness_buffer_ms=float(config_value(["phase7_standstill", "stillness_buffer_ms"], 5000)),
        warn_segment_ms=float(config_value(["phase7_standstill", "warn_segment_ms"], 5000)),
        review_segment_ms=float(config_value(["phase7_standstill", "review_segment_ms"], 10000)),
        fail_segment_ms=float(config_value(["phase7_standstill", "fail_segment_ms"], 30000)),
        review_excess_ratio=float(config_value(["phase7_standstill", "review_excess_ratio"], 0.20)),
        fail_excess_ratio=float(config_value(["phase7_standstill", "fail_excess_ratio"], 0.40)),
        edge_tolerance_ms=float(config_value(["phase7_standstill", "edge_tolerance_ms"], 1000)),
        min_useful_motion_ms=float(config_value(["phase7_standstill", "min_useful_motion_ms"], 5000)),
        source_modalities=[str(item) for item in source_modalities if str(item).strip()],
    )


def _ensure_metadata(state: EpisodeState) -> None:
    if state.metadata:
        return
    metadata, findings = load_metadata(state.episode_path)
    if not findings:
        state.metadata = metadata


def _finish_state(state: EpisodeState, db_path: Path, findings: list[Finding]) -> None:
    if PHASE_NUMBER not in state.phases_completed:
        state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(findings)
    state.findings.extend(findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, findings, phase=PHASE_NUMBER, episode_path=str(state.episode_path))


def _initial_metrics() -> dict[str, Any]:
    return {
        "p7_duration_ms": 0.0,
        "p7_standstill_segment_count": 0,
        "p7_standstill_total_ms": 0.0,
        "p7_standstill_excess_ms": 0.0,
        "p7_standstill_excess_ratio": 0.0,
        "p7_useful_motion_ms": 0.0,
    }


def _finding(
    episode_path: Path,
    check_name: str,
    severity: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        episode_path=str(episode_path),
        phase=PHASE_NUMBER,
        check_name=check_name,
        severity=severity,
        status=status,
        message=message,
        details=details or {},
    )
