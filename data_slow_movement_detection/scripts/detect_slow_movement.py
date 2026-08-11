#!/usr/bin/env python3
"""Detect intentionally slow, long-duration operator movement in UMI episodes.

The detector uses end-effector position only. It computes a rolling path speed
for each hand. By default it selects the primary hand, defined as the hand with
the larger total path, and flags an episode when that hand spends a continuous
period at low speed or near-stationary.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SIDES = ("left", "right")


@dataclass(frozen=True)
class Segment:
    start_idx: int
    end_idx: int
    start_s: float
    end_s: float
    duration_s: float
    path_m: float
    avg_speed_m_s: float


@dataclass(frozen=True)
class SideMetrics:
    side: str
    role: str
    total_path_m: float
    avg_speed_m_s: float
    median_speed_m_s: float
    p90_speed_m_s: float
    longest_slow: Segment | None
    slow_fraction: float
    flagged: bool


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def find_episode_dirs(paths: Iterable[Path]) -> list[Path]:
    episodes: set[Path] = set()
    for path in paths:
        if path.is_file():
            path = path.parent
        if (path / "metadata.json").is_file() and (
            path / "observation.state.eef_pose" / "data.csv"
        ).is_file():
            episodes.add(path)
            continue
        for metadata in path.rglob("metadata.json"):
            episode_dir = metadata.parent
            if (episode_dir / "observation.state.eef_pose" / "data.csv").is_file():
                episodes.add(episode_dir)
    return sorted(episodes)


def read_eef_pose(csv_path: Path) -> tuple[list[float], dict[str, list[tuple[float, float, float]]]]:
    with csv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    timestamps_s: list[float] = []
    positions = {side: [] for side in SIDES}
    for row in rows:
        timestamps_s.append(float(row["timestamp_ms"]) / 1000.0)
        for side in SIDES:
            positions[side].append(
                (
                    float(row[f"{side}_x"]),
                    float(row[f"{side}_y"]),
                    float(row[f"{side}_z"]),
                )
            )
    return timestamps_s, positions


def rolling_path_speed(
    step_distances_m: list[float],
    step_times_s: list[float],
    window_s: float,
) -> list[float]:
    queue: deque[tuple[float, float]] = deque()
    path_sum = 0.0
    speeds: list[float] = []

    for distance_m, time_s in zip(step_distances_m, step_times_s):
        queue.append((time_s, distance_m))
        path_sum += distance_m
        while queue and time_s - queue[0][0] > window_s:
            _, old_distance_m = queue.popleft()
            path_sum -= old_distance_m

        if len(queue) < 2:
            speeds.append(0.0)
            continue
        elapsed_s = min(window_s, max(1e-9, queue[-1][0] - queue[0][0]))
        speeds.append(path_sum / elapsed_s)

    return speeds


def slow_segments(
    timestamps_s: list[float],
    step_times_s: list[float],
    step_distances_m: list[float],
    speeds_m_s: list[float],
    min_speed_m_s: float,
    max_speed_m_s: float,
    include_static: bool,
) -> list[Segment]:
    segments: list[Segment] = []
    start_idx: int | None = None

    for idx, speed_m_s in enumerate(speeds_m_s):
        is_slow_moving = speed_m_s <= max_speed_m_s
        if not include_static:
            is_slow_moving = min_speed_m_s <= speed_m_s <= max_speed_m_s
        if is_slow_moving and start_idx is None:
            start_idx = idx
        if (not is_slow_moving or idx == len(speeds_m_s) - 1) and start_idx is not None:
            end_idx = idx - 1 if not is_slow_moving else idx
            start_s = step_times_s[start_idx]
            end_s = step_times_s[end_idx]
            duration_s = max(0.0, end_s - start_s)
            path_m = sum(step_distances_m[start_idx : end_idx + 1])
            avg_speed_m_s = path_m / duration_s if duration_s > 0 else 0.0
            segments.append(
                Segment(
                    start_idx,
                    end_idx,
                    start_s - timestamps_s[0],
                    end_s - timestamps_s[0],
                    duration_s,
                    path_m,
                    avg_speed_m_s,
                )
            )
            start_idx = None

    return segments


def merge_nearby_segments(
    segments: list[Segment],
    step_distances_m: list[float],
    max_gap_s: float,
) -> list[Segment]:
    if max_gap_s <= 0 or not segments:
        return segments

    merged: list[Segment] = []
    for segment in segments:
        if merged and segment.start_s - merged[-1].end_s <= max_gap_s:
            previous = merged[-1]
            path_m = sum(step_distances_m[previous.start_idx : segment.end_idx + 1])
            duration_s = segment.end_s - previous.start_s
            merged[-1] = Segment(
                previous.start_idx,
                segment.end_idx,
                previous.start_s,
                segment.end_s,
                duration_s,
                path_m,
                path_m / duration_s if duration_s > 0 else 0.0,
            )
        else:
            merged.append(segment)
    return merged


def analyze_side(
    side: str,
    role: str,
    timestamps_s: list[float],
    positions: list[tuple[float, float, float]],
    window_s: float,
    min_speed_m_s: float,
    max_speed_m_s: float,
    min_duration_s: float,
    min_segment_path_m: float,
    include_static: bool,
    eligible_for_flag: bool,
    merge_gap_s: float,
) -> SideMetrics:
    step_times_s: list[float] = []
    step_distances_m: list[float] = []
    for idx in range(1, len(timestamps_s)):
        if timestamps_s[idx] <= timestamps_s[idx - 1]:
            continue
        step_times_s.append(timestamps_s[idx])
        step_distances_m.append(math.dist(positions[idx], positions[idx - 1]))

    total_duration_s = max(1e-9, timestamps_s[-1] - timestamps_s[0])
    total_path_m = sum(step_distances_m)
    speeds_m_s = rolling_path_speed(step_distances_m, step_times_s, window_s)
    segments = slow_segments(
        timestamps_s,
        step_times_s,
        step_distances_m,
        speeds_m_s,
        min_speed_m_s,
        max_speed_m_s,
        include_static,
    )
    segments = merge_nearby_segments(segments, step_distances_m, merge_gap_s)
    qualified = [
        segment
        for segment in segments
        if eligible_for_flag
        and segment.duration_s >= min_duration_s
        and segment.path_m >= min_segment_path_m
    ]
    longest_slow = max(segments, key=lambda item: item.duration_s, default=None)
    slow_fraction = sum(segment.duration_s for segment in segments) / total_duration_s

    return SideMetrics(
        side=side,
        role=role,
        total_path_m=total_path_m,
        avg_speed_m_s=total_path_m / total_duration_s,
        median_speed_m_s=percentile(speeds_m_s, 50),
        p90_speed_m_s=percentile(speeds_m_s, 90),
        longest_slow=longest_slow,
        slow_fraction=slow_fraction,
        flagged=bool(qualified),
    )


def analyze_episode(args: argparse.Namespace, episode_dir: Path) -> dict:
    metadata_path = episode_dir / "metadata.json"
    csv_path = episode_dir / "observation.state.eef_pose" / "data.csv"
    metadata = json.loads(metadata_path.read_text())
    timestamps_s, positions_by_side = read_eef_pose(csv_path)
    if len(timestamps_s) < 2:
        raise ValueError(f"{episode_dir}: need at least two eef_pose samples")

    side_paths = {
        side: sum(
            math.dist(positions_by_side[side][idx], positions_by_side[side][idx - 1])
            for idx in range(1, len(timestamps_s))
            if timestamps_s[idx] > timestamps_s[idx - 1]
        )
        for side in SIDES
    }
    primary_side = max(SIDES, key=lambda side: side_paths[side])

    side_metrics = [
        analyze_side(
            side,
            "primary" if side == primary_side else "secondary",
            timestamps_s,
            positions_by_side[side],
            args.window_s,
            args.min_speed_cm_s / 100.0,
            args.max_speed_cm_s / 100.0,
            args.min_duration_s,
            args.min_segment_path_cm / 100.0,
            args.include_static,
            args.side_selection == "any" or side == primary_side,
            args.merge_gap_s,
        )
        for side in SIDES
    ]
    considered_metrics = [
        metric
        for metric in side_metrics
        if args.side_selection == "any" or metric.role == "primary"
    ]
    longest = max(
        (metric.longest_slow for metric in considered_metrics if metric.longest_slow),
        key=lambda item: item.duration_s,
        default=None,
    )

    return {
        "episode_dir": str(episode_dir),
        "episode_id": metadata.get("episode_id", episode_dir.name),
        "label": episode_dir.parent.name,
        "duration_s": float(metadata.get("duration_seconds", timestamps_s[-1] - timestamps_s[0])),
        "fps_actual": metadata.get("fps_actual"),
        "primary_side": primary_side,
        "flagged": any(metric.flagged for metric in side_metrics),
        "longest_slow_segment_s": round(longest.duration_s, 3) if longest else 0.0,
        "longest_slow_segment_path_cm": round(longest.path_m * 100.0, 3) if longest else 0.0,
        "sides": [side_metrics_to_dict(metric) for metric in side_metrics],
        "thresholds": {
            "window_s": args.window_s,
            "min_speed_cm_s": args.min_speed_cm_s,
            "max_speed_cm_s": args.max_speed_cm_s,
            "min_duration_s": args.min_duration_s,
            "min_segment_path_cm": args.min_segment_path_cm,
            "include_static": args.include_static,
            "side_selection": args.side_selection,
            "merge_gap_s": args.merge_gap_s,
        },
    }


def side_metrics_to_dict(metric: SideMetrics) -> dict:
    longest = metric.longest_slow
    return {
        "side": metric.side,
        "role": metric.role,
        "flagged": metric.flagged,
        "total_path_cm": round(metric.total_path_m * 100.0, 3),
        "avg_speed_cm_s": round(metric.avg_speed_m_s * 100.0, 3),
        "median_rolling_speed_cm_s": round(metric.median_speed_m_s * 100.0, 3),
        "p90_rolling_speed_cm_s": round(metric.p90_speed_m_s * 100.0, 3),
        "slow_fraction": round(metric.slow_fraction, 4),
        "longest_slow_segment": {
            "start_s": round(longest.start_s, 3) if longest else 0.0,
            "end_s": round(longest.end_s, 3) if longest else 0.0,
            "duration_s": round(longest.duration_s, 3) if longest else 0.0,
            "path_cm": round(longest.path_m * 100.0, 3) if longest else 0.0,
            "avg_speed_cm_s": round(longest.avg_speed_m_s * 100.0, 3) if longest else 0.0,
        },
    }


def print_table(results: list[dict]) -> None:
    header = (
        "flag,label,episode,duration_s,longest_slow_s,"
        "longest_slow_path_cm,primary_side,primary_avg_cm_s,"
        "primary_med_cm_s,primary_p90_cm_s"
    )
    print(header)
    for result in results:
        primary = next(side for side in result["sides"] if side["role"] == "primary")
        print(
            ",".join(
                [
                    "SLOW" if result["flagged"] else "OK",
                    result["label"],
                    result["episode_id"],
                    f"{result['duration_s']:.2f}",
                    f"{result['longest_slow_segment_s']:.2f}",
                    f"{result['longest_slow_segment_path_cm']:.2f}",
                    result["primary_side"],
                    f"{primary['avg_speed_cm_s']:.2f}",
                    f"{primary['median_rolling_speed_cm_s']:.2f}",
                    f"{primary['p90_rolling_speed_cm_s']:.2f}",
                ]
            )
        )


def write_results(args: argparse.Namespace, results: list[dict]) -> None:
    if args.format == "jsonl":
        lines = [json.dumps(result, ensure_ascii=False, sort_keys=True) for result in results]
        text = "\n".join(lines) + ("\n" if lines else "")
    elif args.format == "json":
        text = json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        from io import StringIO

        buffer = StringIO()
        old_stdout = sys.stdout
        sys.stdout = buffer
        try:
            print_table(results)
        finally:
            sys.stdout = old_stdout
        text = buffer.getvalue()

    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect long continuous slow movement from observation.state.eef_pose/data.csv."
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("docs")])
    parser.add_argument("--window-s", type=float, default=1.0, help="rolling path-speed window")
    parser.add_argument("--min-speed-cm-s", type=float, default=0.1, help="lower speed bound when --exclude-static is used")
    parser.add_argument("--max-speed-cm-s", type=float, default=3.0, help="upper bound for slow or near-stationary operation")
    parser.add_argument("--min-duration-s", type=float, default=15.0, help="minimum continuous slow segment duration")
    parser.add_argument("--min-segment-path-cm", type=float, default=1.5, help="minimum path inside the slow segment")
    parser.add_argument("--merge-gap-s", type=float, default=2.0, help="merge slow segments separated by gaps no longer than this")
    parser.add_argument("--side-selection", choices=("primary", "any"), default="primary", help="which hand can trigger a flag")
    static_group = parser.add_mutually_exclusive_group()
    static_group.add_argument("--include-static", dest="include_static", action="store_true", default=True, help="include near-stationary periods in slow segments")
    static_group.add_argument("--exclude-static", dest="include_static", action="store_false", help="require speed to be at least --min-speed-cm-s")
    parser.add_argument("--format", choices=("table", "jsonl", "json"), default="table")
    parser.add_argument("--output", help="write results to a file instead of stdout")
    parser.add_argument("--fail-on-detect", action="store_true", help="exit 1 when any episode is flagged")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    episode_dirs = find_episode_dirs(args.paths)
    if not episode_dirs:
        parser.error("no episode directories found")

    results = [analyze_episode(args, episode_dir) for episode_dir in episode_dirs]
    write_results(args, results)
    if args.fail_on_detect and any(result["flagged"] for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
