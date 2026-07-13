#!/usr/bin/env python3
"""Smooth local jitter windows detected by UMI trajectory judgment reports.

The tool scans trajectory judgment JSON reports, selects reports whose overall
trajectory quality is true, then smooths side-specific local jitter/abrupt
segments from ``actions.eef_pose_raw/data.csv`` or
``observation.state.eef_pose_raw/data.csv``. Smoothed trajectories are written
next to each raw source as ``actions.eef_pose_smooth/data.csv`` and
``observation.state.eef_pose_smooth/data.csv``. The source CSVs are never
overwritten. Episodes without selected windows are copied through so every
processed episode has the same output layout.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    from scipy.signal import savgol_filter
except ImportError:  # pragma: no cover - scipy is listed in tools/data requirements.
    savgol_filter = None


SOURCE_LAYOUTS = (
    ("actions", Path("actions.eef_pose_raw/data.csv"), Path("actions.eef_pose_smooth/data.csv")),
    (
        "observation",
        Path("observation.state.eef_pose_raw/data.csv"),
        Path("observation.state.eef_pose_smooth/data.csv"),
    ),
)
SIDES = ("left", "right")
ALLOWED_REASON_TYPES = {
    "abrupt_acceleration",
    "abrupt_jerk",
    "abrupt_step",
    "cluster_jitter",
    "jitter_window",
    "merged_short_gap",
    "micro_jitter",
}


@dataclass(frozen=True)
class ColumnSpec:
    xyz: tuple[str, str, str]
    rot6d: tuple[str, str, str, str, str, str]


@dataclass
class Segment:
    side: str
    start: int
    end: int
    reasons: tuple[str, ...]


@dataclass
class SmoothStats:
    report_path: Path
    episode_dir: Path | None
    output_path: Path | None
    selected_segments: int = 0
    smoothed_ranges: int = 0
    status: str = "skipped"
    error: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smooth eef_pose_raw trajectories using UMI trajectory judgment reports.",
    )
    parser.add_argument("data_root", type=Path, help="Curated data root or a single episode directory.")
    parser.add_argument("report_root", type=Path, help="Report directory or a single report JSON file.")
    parser.add_argument(
        "--output-dir-name",
        default=None,
        help="Deprecated; smooth output now mirrors raw source directories.",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "actions", "observation"),
        default="auto",
        help="Which eef_pose_raw source to smooth (default: auto: all present sources).",
    )
    parser.add_argument("--padding-frames", type=int, default=10, help="Frames to smooth before/after each segment.")
    parser.add_argument("--merge-gap-frames", type=int, default=8, help="Merge selected segments separated by <= this gap.")
    parser.add_argument("--savgol-window", type=int, default=31, help="Odd Savitzky-Golay window length.")
    parser.add_argument("--savgol-polyorder", type=int, default=3, help="Savitzky-Golay polynomial order.")
    parser.add_argument("--cluster-savgol-window", type=int, default=151, help="Odd smoothing window for cluster_jitter segments.")
    parser.add_argument("--cluster-median-window", type=int, default=31, help="Odd rolling-median window for cluster_jitter despiking.")
    parser.add_argument("--cluster-despike-threshold-m", type=float, default=0.004, help="Replace cluster_jitter points farther than this from rolling median before smoothing.")
    parser.add_argument("--micro-savgol-window", type=int, default=101, help="Odd smoothing window for micro_jitter segments.")
    parser.add_argument("--micro-median-window", type=int, default=21, help="Odd rolling-median window for micro_jitter despiking.")
    parser.add_argument("--micro-despike-threshold-m", type=float, default=0.0025, help="Replace micro_jitter points farther than this from rolling median before smoothing.")
    parser.add_argument(
        "--include-bad-quality",
        action="store_true",
        help="Also smooth reports with trajectory_quality=false. Default only smooths quality=true reports.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output data.csv. Default skips episodes that already have output.",
    )
    parser.add_argument(
        "--skip-clean-output",
        action="store_true",
        help="Do not write passthrough smooth CSVs for reports without selected windows.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be smoothed without writing output.")
    parser.add_argument("--limit", type=int, help="Process at most this many selected reports.")
    parser.add_argument("--summary-json", type=Path, help="Optional path for a JSON run summary.")
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"report is not a JSON object: {path}")
    return data


def iter_report_paths(report_root: Path) -> list[Path]:
    if report_root.is_file():
        return [report_root]
    if not report_root.exists():
        raise FileNotFoundError(f"report root does not exist: {report_root}")
    return sorted(p for p in report_root.rglob("*.json") if p.is_file())


def is_episode_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("episode_")


def existing_episode_from_report_path(data_root: Path, report_path_text: str | None) -> Path | None:
    if not report_path_text:
        return None
    report_episode_path = Path(report_path_text)
    candidates: list[Path] = []
    if report_episode_path.is_absolute():
        candidates.append(report_episode_path)
        parts = report_episode_path.parts
        for i, part in enumerate(parts):
            if part.startswith("episode_"):
                suffix = Path(*parts[max(1, i - 4):])
                candidates.append(data_root / suffix)
        for i in range(1, len(parts)):
            if parts[i].startswith("episode_"):
                candidates.append(data_root / Path(*parts[i:]))
                break
        for i in range(1, len(parts)):
            candidate = data_root / Path(*parts[i:])
            if candidate.name.startswith("episode_"):
                candidates.append(candidate)
    else:
        candidates.append(data_root / report_episode_path)
    for candidate in candidates:
        if is_episode_dir(candidate):
            return candidate
    return None


def episode_from_report_location(data_root: Path, report_root: Path, report_path: Path) -> Path | None:
    roots = [report_root]
    if report_root.name != "report":
        roots.append(report_root / "report")
    for root in roots:
        try:
            rel = report_path.relative_to(root).with_suffix("")
        except ValueError:
            continue
        candidate = data_root / rel
        if is_episode_dir(candidate):
            return candidate
    return None


def resolve_episode_dir(data_root: Path, report_root: Path, report_path: Path, report: dict[str, Any]) -> Path | None:
    if is_episode_dir(data_root):
        return data_root
    path_text = report.get("path")
    if isinstance(path_text, str):
        found = existing_episode_from_report_path(data_root, path_text)
        if found is not None:
            return found
    return episode_from_report_location(data_root, report_root, report_path)


def selected_segments(report: dict[str, Any], include_bad_quality: bool) -> list[Segment]:
    if not report.get("ok", False):
        return []
    if not include_bad_quality and not bool(report.get("trajectory_quality", False)):
        return []

    out: list[Segment] = []
    sides = report.get("sides") or {}
    quality_sides = (report.get("quality") or {}).get("sides") or {}
    for side in SIDES:
        side_report = sides.get(side) or {}
        side_quality = side_report.get("quality") or quality_sides.get(side) or {}
        if not include_bad_quality:
            if not bool(side_report.get("trajectory_quality", side_quality.get("trajectory_quality", False))):
                continue
            if side_quality.get("status") not in {"minor_jitter", "clean"}:
                continue
        for segment in side_report.get("segments") or []:
            reasons = tuple(str(r) for r in segment.get("reason_types") or ())
            if reasons and not (set(reasons) & ALLOWED_REASON_TYPES):
                continue
            try:
                start = int(segment["start_frame"])
                end = int(segment["end_frame"])
            except (KeyError, TypeError, ValueError):
                continue
            if end < start:
                continue
            out.append(Segment(side=side, start=start, end=end, reasons=reasons))
    return out


def source_jobs(episode_dir: Path, source: str) -> list[tuple[str, Path, Path]]:
    layouts = [layout for layout in SOURCE_LAYOUTS if source == "auto" or layout[0] == source]
    jobs: list[tuple[str, Path, Path]] = []
    missing: list[Path] = []
    for name, raw_rel, smooth_rel in layouts:
        raw_path = episode_dir / raw_rel
        if raw_path.exists():
            jobs.append((name, raw_path, episode_dir / smooth_rel))
        else:
            missing.append(raw_path)
    if jobs:
        return jobs
    tried = ", ".join(str(path) for path in missing)
    raise FileNotFoundError(f"no eef_pose_raw data.csv found; tried {tried}")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def side_column_spec(fieldnames: list[str], side: str) -> ColumnSpec | None:
    tcp_prefix = f"{side}_tcp."
    std_prefix = f"{side}_"
    if all(f"{tcp_prefix}{axis}" in fieldnames for axis in ("x", "y", "z")):
        xyz = tuple(f"{tcp_prefix}{axis}" for axis in ("x", "y", "z"))
        rot = tuple(f"{tcp_prefix}r{i}" for i in range(1, 7))
    elif all(f"{std_prefix}{axis}" in fieldnames for axis in ("x", "y", "z")):
        xyz = tuple(f"{std_prefix}{axis}" for axis in ("x", "y", "z"))
        rot = tuple(f"{std_prefix}r{i}" for i in range(1, 7))
    else:
        return None
    rot = rot if all(col in fieldnames for col in rot) else ()
    return ColumnSpec(xyz=xyz, rot6d=rot)  # type: ignore[arg-type]


def numeric_matrix(rows: list[dict[str, str]], cols: tuple[str, ...], csv_path: Path) -> np.ndarray:
    values = np.zeros((len(rows), len(cols)), dtype=float)
    for i, row in enumerate(rows):
        for j, col in enumerate(cols):
            try:
                value = float(row[col])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid float in {csv_path}, row {i}, column {col}: {row.get(col)!r}") from exc
            if not math.isfinite(value):
                raise ValueError(f"non-finite float in {csv_path}, row {i}, column {col}: {row.get(col)!r}")
            values[i, j] = value
    return values


def normalize_rot6d(rot: np.ndarray) -> np.ndarray:
    if rot.size == 0:
        return rot
    out = rot.copy()
    a = out[:, 0:3]
    b = out[:, 3:6]
    a_norm = np.linalg.norm(a, axis=1, keepdims=True)
    a_norm[a_norm < 1e-9] = 1.0
    a = a / a_norm
    b = b - np.sum(a * b, axis=1, keepdims=True) * a
    b_norm = np.linalg.norm(b, axis=1, keepdims=True)
    b_norm[b_norm < 1e-9] = 1.0
    b = b / b_norm
    out[:, 0:3] = a
    out[:, 3:6] = b
    return out


def odd_window(length: int, preferred: int, polyorder: int) -> int:
    max_odd = length if length % 2 == 1 else length - 1
    win = min(preferred if preferred % 2 == 1 else preferred + 1, max_odd)
    min_win = polyorder + 2
    if min_win % 2 == 0:
        min_win += 1
    return win if win >= min_win else 0


def rolling_median_matrix(values: np.ndarray, window: int) -> np.ndarray:
    n = len(values)
    if n == 0:
        return values.copy()
    window = max(3, int(window))
    if window % 2 == 0:
        window += 1
    half = window // 2
    out = np.zeros_like(values)
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        out[i] = np.median(values[a:b], axis=0)
    return out


def cosine_blend_weights(length: int, left_len: int, right_len: int) -> np.ndarray:
    weights = np.ones(length, dtype=float)
    if left_len:
        t = np.linspace(0.0, 1.0, left_len, endpoint=False)
        weights[:left_len] = 0.5 - 0.5 * np.cos(np.pi * t)
    if right_len:
        t = np.linspace(1.0, 0.0, right_len, endpoint=False)
        weights[-right_len:] = 0.5 - 0.5 * np.cos(np.pi * t)
    return weights.reshape(-1, 1)


def lowpass_region(region: np.ndarray, window: int, polyorder: int) -> np.ndarray:
    win = odd_window(len(region), window, polyorder)
    if savgol_filter is not None and win:
        return savgol_filter(region, window_length=win, polyorder=polyorder, axis=0, mode="interp")
    kernel_len = max(3, min(len(region), window))
    kernel = np.ones(kernel_len, dtype=float) / float(kernel_len)
    return np.vstack([
        np.convolve(region[:, col], kernel, mode="same") for col in range(region.shape[1])
    ]).T


def smooth_array(
    values: np.ndarray,
    segments: list[Segment],
    padding: int,
    merge_gap: int,
    savgol_window: int,
    savgol_polyorder: int,
    cluster_savgol_window: int,
    cluster_median_window: int,
    cluster_despike_threshold_m: float,
    micro_savgol_window: int,
    micro_median_window: int,
    micro_despike_threshold_m: float,
) -> tuple[np.ndarray, int]:
    n = len(values)
    if n == 0 or not segments:
        return values.copy(), 0

    merged: list[tuple[int, int, set[str]]] = []
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        start = segment.start
        end = segment.end
        start = max(0, min(n - 1, start))
        end = max(0, min(n - 1, end))
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append((start, end, set(segment.reasons)))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), merged[-1][2] | set(segment.reasons))

    out = values.copy()
    x_all = np.arange(n)
    for start, end, reasons in merged:
        edit_start = max(0, start - padding)
        edit_end = min(n - 1, end + padding)
        if "cluster_jitter" in reasons or "micro_jitter" in reasons:
            if "cluster_jitter" in reasons:
                median_window = cluster_median_window
                smooth_window = cluster_savgol_window
                despike_threshold = cluster_despike_threshold_m
            else:
                median_window = micro_median_window
                smooth_window = micro_savgol_window
                despike_threshold = micro_despike_threshold_m
            region = out[edit_start : edit_end + 1].copy()
            median = rolling_median_matrix(region, median_window)
            residual = np.linalg.norm(region - median, axis=1)
            candidate = region.copy()
            if despike_threshold > 0:
                candidate[residual > despike_threshold] = median[residual > despike_threshold]
            filtered = lowpass_region(candidate, smooth_window, savgol_polyorder)
        else:
            candidate = out.copy()
            # Replace the detected core with a bridge between the nearest clean
            # boundary frames, then low-pass the expanded window. This removes local
            # spikes while leaving normal motion outside the window almost intact.
            left_anchor = max(0, start - 1)
            right_anchor = min(n - 1, end + 1)
            if left_anchor < start and right_anchor > end:
                bridge_x = np.array([left_anchor, right_anchor], dtype=float)
                for col in range(values.shape[1]):
                    candidate[start : end + 1, col] = np.interp(
                        x_all[start : end + 1],
                        bridge_x,
                        np.array([out[left_anchor, col], out[right_anchor, col]], dtype=float),
                    )
            region = candidate[edit_start : edit_end + 1]
            filtered = lowpass_region(region, savgol_window, savgol_polyorder)

        left_len = max(0, start - edit_start) if padding > 0 else 0
        right_len = max(0, edit_end - end) if padding > 0 else 0
        weights = cosine_blend_weights(len(region), left_len, right_len)
        out[edit_start : edit_end + 1] = (1.0 - weights) * out[edit_start : edit_end + 1] + weights * filtered

    return out, len(merged)


def format_float(value: float) -> str:
    return f"{value:.9f}"


def smooth_rows(
    source_csv: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
    segments: list[Segment],
    args: argparse.Namespace,
) -> tuple[list[dict[str, str]], int, bool]:
    by_side: dict[str, list[Segment]] = {side: [] for side in SIDES}
    for segment in segments:
        by_side[segment.side].append(segment)

    changed_ranges = 0
    updated_columns: dict[str, np.ndarray] = {}
    for side in SIDES:
        if not by_side[side]:
            continue
        spec = side_column_spec(fieldnames, side)
        if spec is None:
            continue

        xyz = numeric_matrix(rows, spec.xyz, source_csv)
        smoothed_xyz, range_count = smooth_array(
            xyz,
            by_side[side],
            args.padding_frames,
            args.merge_gap_frames,
            args.savgol_window,
            args.savgol_polyorder,
            args.cluster_savgol_window,
            args.cluster_median_window,
            args.cluster_despike_threshold_m,
            args.micro_savgol_window,
            args.micro_median_window,
            args.micro_despike_threshold_m,
        )
        changed_ranges += range_count
        for idx, col in enumerate(spec.xyz):
            updated_columns[col] = smoothed_xyz[:, idx]

        if spec.rot6d:
            rot = numeric_matrix(rows, spec.rot6d, source_csv)
            smoothed_rot, _ = smooth_array(
                rot,
                by_side[side],
                args.padding_frames,
                args.merge_gap_frames,
                args.savgol_window,
                args.savgol_polyorder,
                args.cluster_savgol_window,
                args.cluster_median_window,
                args.cluster_despike_threshold_m,
                args.micro_savgol_window,
                args.micro_median_window,
                args.micro_despike_threshold_m,
            )
            smoothed_rot = normalize_rot6d(smoothed_rot)
            for idx, col in enumerate(spec.rot6d):
                updated_columns[col] = smoothed_rot[:, idx]

    if not updated_columns:
        return rows, 0, False

    out_rows = [dict(row) for row in rows]
    for i, row in enumerate(out_rows):
        for col, values in updated_columns.items():
            row[col] = format_float(float(values[i]))
    return out_rows, changed_ranges, True


def smooth_episode(
    episode_dir: Path,
    report_path: Path,
    segments: list[Segment],
    args: argparse.Namespace,
) -> SmoothStats:
    stats = SmoothStats(report_path=report_path, episode_dir=episode_dir, output_path=None)
    stats.selected_segments = len(segments)
    jobs = source_jobs(episode_dir, args.source)
    stats.output_path = jobs[0][2]

    statuses: list[str] = []
    for _, source_csv, output_path in jobs:
        if output_path.exists() and not args.overwrite:
            statuses.append("exists")
            continue

        fieldnames, rows = read_csv(source_csv)
        if not rows:
            raise ValueError(f"empty CSV: {source_csv}")
        if not segments:
            if args.skip_clean_output:
                statuses.append("no_segments")
                continue
            if args.dry_run:
                statuses.append("dry_run_clean")
                continue
            write_csv(output_path, fieldnames, rows)
            statuses.append("copied_clean")
            continue

        out_rows, changed_ranges, changed = smooth_rows(source_csv, fieldnames, rows, segments, args)
        if not changed:
            statuses.append("no_matching_columns")
            continue
        stats.smoothed_ranges += changed_ranges
        if args.dry_run:
            statuses.append("dry_run")
            continue
        write_csv(output_path, fieldnames, out_rows)
        statuses.append("smoothed")

    if "smoothed" in statuses:
        stats.status = "smoothed"
    elif "copied_clean" in statuses:
        stats.status = "copied_clean"
    elif "dry_run" in statuses:
        stats.status = "dry_run"
    elif "dry_run_clean" in statuses:
        stats.status = "dry_run_clean"
    elif statuses and all(status == "exists" for status in statuses):
        stats.status = "exists"
    elif statuses and all(status == "no_segments" for status in statuses):
        stats.status = "no_segments"
    elif statuses and all(status == "no_matching_columns" for status in statuses):
        stats.status = "no_matching_columns"
    else:
        stats.status = "skipped"
    return stats


def process_report(report_path: Path, report_root: Path, data_root: Path, args: argparse.Namespace) -> SmoothStats:
    report = load_json(report_path)
    episode_dir = resolve_episode_dir(data_root, report_root, report_path, report)
    stats = SmoothStats(report_path=report_path, episode_dir=episode_dir, output_path=None)
    if episode_dir is None:
        stats.status = "missing_episode"
        return stats
    segments = selected_segments(report, args.include_bad_quality)
    return smooth_episode(episode_dir, report_path, segments, args)


def stats_to_dict(stats: SmoothStats) -> dict[str, Any]:
    return {
        "report_path": str(stats.report_path),
        "episode_dir": str(stats.episode_dir) if stats.episode_dir else None,
        "output_path": str(stats.output_path) if stats.output_path else None,
        "selected_segments": stats.selected_segments,
        "smoothed_ranges": stats.smoothed_ranges,
        "status": stats.status,
        "error": stats.error,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_root = args.data_root.resolve()
    report_root = args.report_root.resolve()
    report_paths = iter_report_paths(report_root)

    results: list[SmoothStats] = []
    processed = 0
    for report_path in report_paths:
        if args.limit is not None and processed >= args.limit:
            break
        try:
            stats = process_report(report_path, report_root, data_root, args)
        except Exception as exc:  # Keep batch runs moving but record failures.
            stats = SmoothStats(report_path=report_path, episode_dir=None, output_path=None, status="error", error=str(exc))
        if stats.status not in {"no_segments", "missing_episode"}:
            processed += 1
        results.append(stats)
        if stats.status in {"smoothed", "copied_clean", "dry_run", "dry_run_clean", "exists", "error", "no_matching_columns"}:
            target = stats.output_path or stats.episode_dir or ""
            extra = f" error={stats.error}" if stats.error else ""
            print(
                f"{stats.status}: segments={stats.selected_segments} "
                f"ranges={stats.smoothed_ranges} report={stats.report_path} target={target}{extra}"
            )

    counts: dict[str, int] = {}
    for stats in results:
        counts[stats.status] = counts.get(stats.status, 0) + 1
    summary = {
        "data_root": str(data_root),
        "report_root": str(report_root),
        "report_count": len(report_paths),
        "status_counts": counts,
        "results": [stats_to_dict(stats) for stats in results if stats.status not in {"no_segments", "missing_episode"}],
    }
    print(json.dumps({"status_counts": counts, "report_count": len(report_paths)}, ensure_ascii=False, indent=2))
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_json.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return 0 if counts.get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
