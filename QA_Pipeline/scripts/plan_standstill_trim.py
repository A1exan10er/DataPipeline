"""Plan report-only trimming for beginning/end standstill segments."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from multiprocessing import Pool
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline.qa_config import config_value
from scripts.pipeline.qa_core import discover_episodes, infer_context, load_metadata


CSV_FIELDNAMES = [
    "episode_path",
    "task",
    "date",
    "operator",
    "robot",
    "controller",
    "source_modality",
    "first_timestamp_ms",
    "last_timestamp_ms",
    "leading_standstill_start_ms",
    "leading_standstill_end_ms",
    "trailing_standstill_start_ms",
    "trailing_standstill_end_ms",
    "first_kept_timestamp_ms",
    "last_kept_timestamp_ms",
    "removed_leading_ms",
    "removed_trailing_ms",
    "removed_total_ms",
    "remaining_duration_ms",
    "removed_ratio",
    "decision",
    "reason",
    "affected_csv_modalities",
    "affected_video_modalities",
]


@dataclass
class StandstillConfig:
    enabled: bool
    motion_delta_threshold_rad: float
    standstill_min_duration_ms: float
    edge_tolerance_ms: float
    keep_context_ms: float
    min_remaining_duration_ms: float
    max_trim_ratio: float
    source_modalities: list[str]


@dataclass
class Segment:
    start_ms: float
    end_ms: float
    duration_ms: float


@dataclass
class TrimPlanRow:
    episode_path: str
    task: str
    date: str
    operator: str
    robot: str
    controller: str
    source_modality: str
    first_timestamp_ms: float | None
    last_timestamp_ms: float | None
    leading_standstill_start_ms: float | None
    leading_standstill_end_ms: float | None
    trailing_standstill_start_ms: float | None
    trailing_standstill_end_ms: float | None
    first_kept_timestamp_ms: float | None
    last_kept_timestamp_ms: float | None
    removed_leading_ms: float
    removed_trailing_ms: float
    removed_total_ms: float
    remaining_duration_ms: float
    removed_ratio: float
    decision: str
    reason: str
    affected_csv_modalities: str
    affected_video_modalities: str


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    roots = [Path(root) for root in args.roots]
    missing_roots = [root for root in roots if not root.exists()]
    if missing_roots:
        _print_error("Missing root path(s): " + ", ".join(str(root) for root in missing_roots))
        return 1

    config = _standstill_config()
    if not config.enabled:
        _print_error("standstill_trim.enabled is false in quality config.")
        return 1

    started = time.perf_counter()
    episodes = discover_episodes(roots)
    if args.max_episodes is not None:
        episodes = episodes[: args.max_episodes]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    if args.workers > 1:
        rows = _plan_episodes_parallel(roots, episodes, config, args.workers, args.progress, args.progress_interval)
    else:
        rows = _plan_episodes_serial(roots, episodes, config, args.progress, args.progress_interval)

    _write_csv(output_dir / "standstill_trim_plan.csv", rows)
    _write_jsonl(output_dir / "standstill_trim_plan.jsonl", rows)
    elapsed = time.perf_counter() - started
    _write_summary(output_dir / "standstill_trim_summary.md", rows, elapsed, config)

    print(f"Episodes scanned : {len(rows)}")
    print(f"Elapsed seconds  : {elapsed:.3f}")
    print(f"Episodes/second  : {_episodes_per_second(len(rows), elapsed):.2f}")
    print(f"Reports written  : {output_dir}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan report-only edge standstill trimming.")
    parser.add_argument("--roots", nargs="+", required=True, help="One or more data roots to scan.")
    parser.add_argument("--output-dir", default="outputs/standstill_trim", help="Directory for trim plan reports.")
    parser.add_argument("--max-episodes", type=int, default=None, help="Maximum number of episodes to process.")
    parser.add_argument("--progress", action="store_true", help="Print progress while scanning.")
    parser.add_argument("--progress-interval", type=int, default=100, help="Progress print interval in episodes.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker processes. Default: 1.")
    return parser.parse_args(argv)


def _plan_episodes_serial(
    roots: list[Path],
    episodes: list[Path],
    config: StandstillConfig,
    progress: bool,
    progress_interval: int,
) -> list[TrimPlanRow]:
    rows = []
    for index, episode_path in enumerate(episodes, start=1):
        _print_progress(index, len(episodes), progress, progress_interval)
        rows.append(_plan_episode(roots, episode_path, config))
    return rows


def _plan_episodes_parallel(
    roots: list[Path],
    episodes: list[Path],
    config: StandstillConfig,
    workers: int,
    progress: bool,
    progress_interval: int,
) -> list[TrimPlanRow]:
    worker_count = max(1, workers)
    root_strings = [str(root) for root in roots]
    args = [(root_strings, str(episode_path), config) for episode_path in episodes]
    chunksize = max(1, min(100, len(args) // (worker_count * 4) if worker_count else 1))
    rows = []
    with Pool(processes=worker_count) as pool:
        for index, row in enumerate(pool.imap(_plan_episode_worker, args, chunksize=chunksize), start=1):
            _print_progress(index, len(episodes), progress, progress_interval)
            rows.append(row)
    return rows


def _plan_episode_worker(args: tuple[list[str], str, StandstillConfig]) -> TrimPlanRow:
    root_strings, episode_path_string, config = args
    return _plan_episode([Path(root) for root in root_strings], Path(episode_path_string), config)


def _print_progress(index: int, total: int, progress: bool, progress_interval: int) -> None:
    if progress and (index == 1 or index % progress_interval == 0 or index == total):
        print(f"Processed {index}/{total} episodes...", flush=True)


def _standstill_config() -> StandstillConfig:
    return StandstillConfig(
        enabled=bool(config_value(["standstill_trim", "enabled"], True)),
        motion_delta_threshold_rad=_float_config("motion_delta_threshold_rad", 0.001),
        standstill_min_duration_ms=_float_config("standstill_min_duration_ms", 5000),
        edge_tolerance_ms=_float_config("edge_tolerance_ms", 1000),
        keep_context_ms=_float_config("keep_context_ms", 500),
        min_remaining_duration_ms=_float_config("min_remaining_duration_ms", 5000),
        max_trim_ratio=_float_config("max_trim_ratio", 0.40),
        source_modalities=_source_modalities_config(),
    )


def _float_config(key: str, default: float) -> float:
    value = config_value(["standstill_trim", key], default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _source_modalities_config() -> list[str]:
    value = config_value(
        ["standstill_trim", "source_modalities"],
        [
            "observation.state.joint_position",
            "actions.joint_position",
            "observation.state.eef_pose",
            "actions.eef_pose",
            "action.eef_pose",
        ],
    )
    if not isinstance(value, list):
        return ["observation.state.joint_position", "actions.joint_position"]
    return [str(item) for item in value if str(item).strip()]


def _plan_episode(roots: list[Path], episode_path: Path, config: StandstillConfig) -> TrimPlanRow:
    metadata, _ = load_metadata(episode_path)
    context = infer_context(roots, episode_path, metadata)
    csv_modalities = _csv_modalities(episode_path)
    video_modalities = _video_modalities(episode_path)
    source_modality = _source_modality(episode_path, config)
    base = {
        "episode_path": str(episode_path),
        "task": context["task"],
        "date": context["date"],
        "operator": context["operator"],
        "robot": context["robot"],
        "controller": context["controller"],
        "source_modality": source_modality,
        "affected_csv_modalities": ";".join(csv_modalities),
        "affected_video_modalities": ";".join(video_modalities),
    }
    source_path = episode_path / source_modality / "data.csv"
    if not source_path.is_file():
        return _row(base, None, None, None, None, None, None, "missing_motion_source", "joint position data.csv is missing")

    scan = _scan_standstill(source_path, config.motion_delta_threshold_rad)
    if scan["error"]:
        return _row(base, scan["first_timestamp_ms"], scan["last_timestamp_ms"], None, None, None, None, "invalid_timestamps", scan["error"])

    first_ts = scan["first_timestamp_ms"]
    last_ts = scan["last_timestamp_ms"]
    if first_ts is None or last_ts is None or last_ts <= first_ts:
        return _row(base, first_ts, last_ts, None, None, None, None, "invalid_timestamps", "not enough monotonic timestamp rows")

    leading = _leading_segment(scan["segments"], first_ts, config)
    trailing = _trailing_segment(scan["segments"], last_ts, config)
    return _planned_row(base, first_ts, last_ts, leading, trailing, config)


def _scan_standstill(csv_path: Path, motion_threshold: float) -> dict:
    first_timestamp = None
    last_timestamp = None
    previous_timestamp = None
    previous_values = None
    columns = None
    active_start = None
    active_end = None
    segments: list[Segment] = []
    error = ""

    try:
        with csv_path.open("r", encoding="utf-8", newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            if not reader.fieldnames or "timestamp_ms" not in reader.fieldnames:
                return _scan_result(None, None, [], "timestamp_ms column is missing")
            columns = [
                column
                for column in reader.fieldnames
                if column not in ("timestamp_ms", "is_standstill") and "gripper" not in column
            ]
            if not columns:
                return _scan_result(None, None, [], "no non-gripper motion columns found")
            for row in reader:
                timestamp = _float_or_none(row.get("timestamp_ms"))
                values = [_float_or_none(row.get(column)) for column in columns]
                if timestamp is None or any(value is None for value in values):
                    _append_active_segment(segments, active_start, active_end)
                    active_start = None
                    active_end = None
                    previous_timestamp = None
                    previous_values = None
                    continue
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    error = "timestamps move backward"
                    break
                if previous_timestamp is not None and timestamp == previous_timestamp:
                    previous_values = values
                    continue
                if previous_values is not None:
                    still = max(abs(value - previous) for value, previous in zip(values, previous_values)) < motion_threshold
                    if still:
                        active_start = previous_timestamp if active_start is None else active_start
                        active_end = timestamp
                    else:
                        _append_active_segment(segments, active_start, active_end)
                        active_start = None
                        active_end = None
                previous_timestamp = timestamp
                previous_values = values
    except OSError as exc:
        return _scan_result(first_timestamp, last_timestamp, segments, str(exc))

    _append_active_segment(segments, active_start, active_end)
    return _scan_result(first_timestamp, last_timestamp, segments, error)


def _scan_result(
    first_timestamp_ms: float | None, last_timestamp_ms: float | None, segments: list[Segment], error: str
) -> dict:
    return {
        "first_timestamp_ms": first_timestamp_ms,
        "last_timestamp_ms": last_timestamp_ms,
        "segments": segments,
        "error": error,
    }


def _append_active_segment(segments: list[Segment], start_ms: float | None, end_ms: float | None) -> None:
    if start_ms is None or end_ms is None:
        return
    duration = end_ms - start_ms
    if duration > 0:
        segments.append(Segment(start_ms, end_ms, duration))


def _leading_segment(segments: list[Segment], first_ts: float, config: StandstillConfig) -> Segment | None:
    for segment in segments:
        if (
            segment.duration_ms >= config.standstill_min_duration_ms
            and segment.start_ms <= first_ts + config.edge_tolerance_ms
        ):
            return segment
    return None


def _trailing_segment(segments: list[Segment], last_ts: float, config: StandstillConfig) -> Segment | None:
    for segment in reversed(segments):
        if (
            segment.duration_ms >= config.standstill_min_duration_ms
            and segment.end_ms >= last_ts - config.edge_tolerance_ms
        ):
            return segment
    return None


def _planned_row(
    base: dict,
    first_ts: float,
    last_ts: float,
    leading: Segment | None,
    trailing: Segment | None,
    config: StandstillConfig,
) -> TrimPlanRow:
    first_kept = first_ts
    last_kept = last_ts
    if leading is not None:
        first_kept = min(last_ts, max(first_ts, leading.end_ms - config.keep_context_ms))
    if trailing is not None:
        last_kept = max(first_ts, min(last_ts, trailing.start_ms + config.keep_context_ms))
    if first_kept > last_kept:
        first_kept = last_kept

    removed_leading = max(0.0, first_kept - first_ts)
    removed_trailing = max(0.0, last_ts - last_kept)
    duration = max(0.0, last_ts - first_ts)
    remaining = max(0.0, last_kept - first_kept)
    removed_total = removed_leading + removed_trailing
    removed_ratio = removed_total / duration if duration > 0 else 0.0

    if leading is None and trailing is None:
        decision = "no_trim"
        reason = "no eligible beginning/end standstill segment"
    elif remaining < config.min_remaining_duration_ms:
        decision = "reject_too_short_after_trim"
        reason = "remaining duration is below configured minimum"
    elif removed_ratio > config.max_trim_ratio:
        decision = "needs_review"
        reason = "removed duration ratio exceeds configured review threshold"
    else:
        decision = "trim_candidate"
        reason = "eligible beginning/end standstill segment found"

    return _row(
        base,
        first_ts,
        last_ts,
        leading,
        trailing,
        first_kept,
        last_kept,
        decision,
        reason,
        removed_leading,
        removed_trailing,
        removed_total,
        remaining,
        removed_ratio,
    )


def _row(
    base: dict,
    first_ts: float | None,
    last_ts: float | None,
    leading: Segment | None,
    trailing: Segment | None,
    first_kept: float | None,
    last_kept: float | None,
    decision: str,
    reason: str,
    removed_leading: float = 0.0,
    removed_trailing: float = 0.0,
    removed_total: float = 0.0,
    remaining: float = 0.0,
    removed_ratio: float = 0.0,
) -> TrimPlanRow:
    return TrimPlanRow(
        episode_path=base["episode_path"],
        task=base["task"],
        date=base["date"],
        operator=base["operator"],
        robot=base["robot"],
        controller=base["controller"],
        source_modality=base["source_modality"],
        first_timestamp_ms=first_ts,
        last_timestamp_ms=last_ts,
        leading_standstill_start_ms=leading.start_ms if leading else None,
        leading_standstill_end_ms=leading.end_ms if leading else None,
        trailing_standstill_start_ms=trailing.start_ms if trailing else None,
        trailing_standstill_end_ms=trailing.end_ms if trailing else None,
        first_kept_timestamp_ms=first_kept,
        last_kept_timestamp_ms=last_kept,
        removed_leading_ms=removed_leading,
        removed_trailing_ms=removed_trailing,
        removed_total_ms=removed_total,
        remaining_duration_ms=remaining,
        removed_ratio=removed_ratio,
        decision=decision,
        reason=reason,
        affected_csv_modalities=base["affected_csv_modalities"],
        affected_video_modalities=base["affected_video_modalities"],
    )


def _source_modality(episode_path: Path, config: StandstillConfig) -> str:
    for modality in config.source_modalities:
        if (episode_path / modality / "data.csv").is_file():
            return modality
    return config.source_modalities[0] if config.source_modalities else "observation.state.joint_position"


def _csv_modalities(episode_path: Path) -> list[str]:
    return [
        path.name
        for path in sorted(episode_path.iterdir())
        if path.is_dir() and (path / "data.csv").is_file()
    ]


def _video_modalities(episode_path: Path) -> list[str]:
    return [
        path.name
        for path in sorted(episode_path.iterdir())
        if path.is_dir() and (path / "video.mp4").is_file() and (path / "timestamps.csv").is_file()
    ]


def _write_csv(path: Path, rows: list[TrimPlanRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def _write_jsonl(path: Path, rows: list[TrimPlanRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def _write_summary(path: Path, rows: list[TrimPlanRow], elapsed_seconds: float, config: StandstillConfig) -> None:
    counts = Counter(row.decision for row in rows)
    lines = [
        "# Standstill Trim Summary",
        "",
        f"Episodes scanned: {len(rows)}",
        f"Elapsed seconds: {elapsed_seconds:.3f}",
        f"Episodes per second: {_episodes_per_second(len(rows), elapsed_seconds):.2f}",
        "",
        "## Config",
        "",
    ]
    for key, value in asdict(config).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Decisions", "", "| Decision | Episodes |", "|---|---:|"])
    for decision, count in sorted(counts.items()):
        lines.append(f"| {decision} | {count} |")
    lines.extend(["", "## Top Removed Candidates", "", "| Episode | Decision | Removed ms | Ratio | Reason |", "|---|---|---:|---:|---|"])
    candidates = sorted(rows, key=lambda item: item.removed_total_ms, reverse=True)[:20]
    for row in candidates:
        lines.append(
            f"| {row.episode_path} | {row.decision} | {row.removed_total_ms:.0f} | {row.removed_ratio:.3f} | {row.reason} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _csv_row(row: TrimPlanRow) -> dict:
    values = asdict(row)
    for key, value in list(values.items()):
        if isinstance(value, float):
            values[key] = f"{value:.6f}"
        elif value is None:
            values[key] = ""
    return values


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _episodes_per_second(count: int, elapsed_seconds: float) -> float:
    return count / elapsed_seconds if elapsed_seconds > 0 else 0.0


def _print_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
