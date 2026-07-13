#!/usr/bin/env python3
"""Judge transformed ``actions.eef_pose`` trajectories for drift and jitter.

The detector is intentionally independent from ``assessment`` and
``preprocess``. It reads raw ``actions.eef_pose/data.csv``, transforms poses to
the world EEF frame with ``transform/ee_transform.py``, then emits one report per
episode named exactly ``episode_XXXX.json`` under ``<output-root>/report``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TRANSFORM_DIR = ROOT / "transform"
ASSESSMENT_DIR = ROOT / "assessment"
if str(TRANSFORM_DIR) not in sys.path:
    sys.path.insert(0, str(TRANSFORM_DIR))
if str(ASSESSMENT_DIR) not in sys.path:
    sys.path.insert(0, str(ASSESSMENT_DIR))

from ee_transform import load_config as load_transform_config  # noqa: E402
from ee_transform import transform_row  # noqa: E402

try:
    import validate_raw_data as vrd  # noqa: E402
except ImportError:  # pragma: no cover - this repo normally provides it.
    vrd = None


ACTION_DIR = "actions.eef_pose"
TRANSFORMED_ACTION_DIR = "actions.eef_pose_raw"
SIDES = ("left", "right")
WRIST_VIEW = {
    "left": "observation.image.left_wrist_view",
    "right": "observation.image.right_wrist_view",
}
XYZ = ("x", "y", "z")
ROTATION_COLUMNS = ("r1", "r2", "r3", "r4", "r5", "r6")
DEFAULT_CONFIG_PATH = Path(__file__).with_name("judgment_config.json")
DEFAULT_TRANSFORM_CONFIG = TRANSFORM_DIR / "ee_trajectory_config.json"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None) -> dict[str, Any]:
    with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if path is not None:
        with path.open("r", encoding="utf-8") as f:
            config = deep_merge(config, json.load(f))
    return config


def is_episode_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("episode_")


def find_episode_dirs(input_path: Path) -> list[Path]:
    if vrd is not None:
        return vrd.find_episode_dirs(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")
    if is_episode_dir(input_path):
        return [input_path]
    if not input_path.is_dir():
        raise ValueError(f"Input path is not a directory: {input_path}")
    episodes = []
    for path in sorted(input_path.rglob("episode_*")):
        if is_episode_dir(path):
            episodes.append(path)
    if not episodes:
        raise ValueError(f"No episode_* directories found under {input_path}")
    return episodes


def report_path_for(input_path: Path, episode_dir: Path, output_root: Path) -> Path:
    report_root = output_root / "report"
    if input_path.name.startswith("episode_"):
        rel_parent = Path(episode_dir.parent.name or "episodes")
    else:
        try:
            rel_parent = episode_dir.parent.relative_to(input_path)
        except ValueError:
            rel_parent = Path(episode_dir.parent.name or "episodes")
        if rel_parent == Path("."):
            rel_parent = Path(input_path.name or "episodes")
    return report_root / rel_parent / f"{episode_dir.name}.json"


def parse_float(value: Any, *, column: str, row_index: int, path: Path) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid float in {path}, row {row_index}, column {column}: {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"Non-finite float in {path}, row {row_index}, column {column}: {value!r}")
    return out


def required_columns() -> list[str]:
    cols = ["timestamp_ms"]
    for side in SIDES:
        cols.extend(f"{side}_{axis}" for axis in XYZ)
        cols.extend(f"{side}_{name}" for name in ROTATION_COLUMNS)
    return cols


def standard_pose_columns_present(row: dict[str, str]) -> bool:
    return all(col in row for col in required_columns())


def single_standard_pose_columns_present(row: dict[str, str]) -> bool:
    return all(col in row for col in ["timestamp_ms", *XYZ])


def tcp_pose_columns_present(row: dict[str, str]) -> bool:
    cols = ["timestamp_ms"]
    for side in SIDES:
        cols.extend(f"{side}_tcp.{axis}" for axis in XYZ)
    return all(col in row for col in cols)


def single_tcp_pose_columns_present(row: dict[str, str]) -> bool:
    return all(col in row for col in ["timestamp_ms", *(f"tcp.{axis}" for axis in XYZ)])


def transformed_tcp_csv_path(episode_dir: Path) -> Path | None:
    candidates = [
        episode_dir / TRANSFORMED_ACTION_DIR / "data.csv",
        episode_dir / ACTION_DIR / "data.csv",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            rows = read_csv_rows(path)
        except ValueError:
            continue
        if rows and (
            tcp_pose_columns_present(rows[0])
            or single_tcp_pose_columns_present(rows[0])
            or single_standard_pose_columns_present(rows[0])
        ):
            return path
    return None


def load_transformed_episode(
    episode_dir: Path,
    transform_config: dict[str, Any],
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    csv_path = episode_dir / ACTION_DIR / "data.csv"
    if not csv_path.exists():
        raise ValueError(f"missing {ACTION_DIR}/data.csv")
    rows = read_csv_rows(csv_path)
    if not rows:
        raise ValueError(f"empty {ACTION_DIR}/data.csv")

    use_standard_pose = standard_pose_columns_present(rows[0])
    use_tcp_pose = False
    use_single_tcp_pose = False
    use_single_standard_pose = False
    if not use_standard_pose:
        tcp_path = transformed_tcp_csv_path(episode_dir)
        if tcp_path is not None:
            csv_path = tcp_path
            rows = read_csv_rows(csv_path)
            use_tcp_pose = tcp_pose_columns_present(rows[0])
            use_single_tcp_pose = single_tcp_pose_columns_present(rows[0])
            use_single_standard_pose = single_standard_pose_columns_present(rows[0])
        else:
            missing = [col for col in required_columns() if col not in rows[0]]
            raise ValueError(f"missing columns in {csv_path}: {', '.join(missing)}")

    timestamps = []
    active_sides = ("single",) if (use_single_tcp_pose or use_single_standard_pose) else SIDES
    positions = {side: [] for side in active_sides}
    for row_index, row in enumerate(rows):
        timestamps.append(parse_float(row.get("timestamp_ms"), column="timestamp_ms", row_index=row_index, path=csv_path))
        if use_single_tcp_pose:
            positions["single"].append(
                [
                    parse_float(row[f"tcp.{axis}"], column=f"tcp.{axis}", row_index=row_index, path=csv_path)
                    for axis in XYZ
                ]
            )
        elif use_single_standard_pose:
            positions["single"].append(
                [
                    parse_float(row[axis], column=axis, row_index=row_index, path=csv_path)
                    for axis in XYZ
                ]
            )
        elif use_tcp_pose:
            for side in SIDES:
                positions[side].append(
                    [
                        parse_float(row[f"{side}_tcp.{axis}"], column=f"{side}_tcp.{axis}", row_index=row_index, path=csv_path)
                        for axis in XYZ
                    ]
                )
        else:
            # Validate raw numeric fields before calling the transform, so parse
            # errors point to the source file and row.
            for side in SIDES:
                for suffix in (*XYZ, *ROTATION_COLUMNS):
                    col = f"{side}_{suffix}"
                    parse_float(row.get(col), column=col, row_index=row_index, path=csv_path)

            transformed = transform_row(row, config=transform_config)
            for side in SIDES:
                positions[side].append(
                    [
                        parse_float(transformed[f"{side}_{axis}"], column=f"{side}_{axis}", row_index=row_index, path=csv_path)
                        for axis in XYZ
                    ]
                )

    return np.asarray(timestamps, dtype=np.float64), {
        side: np.asarray(values, dtype=np.float64) for side, values in positions.items()
    }


def workspace_violations(pos: np.ndarray, cfg: dict[str, Any]) -> list[list[dict[str, Any]]]:
    bounds = cfg["workspace_bounds"]
    per_frame: list[list[dict[str, Any]]] = [[] for _ in range(len(pos))]
    checks = (
        ("x", 0, "lt", float(bounds["x_min"])),
        ("x", 0, "gt", float(bounds["x_max"])),
        ("y", 1, "abs_gt", float(bounds["y_abs_max"])),
        ("z", 2, "lt", float(bounds["z_min"])),
        ("z", 2, "gt", float(bounds["z_max"])),
    )
    for axis, axis_index, op, threshold in checks:
        values = pos[:, axis_index]
        if op == "lt":
            mask = values < threshold
        elif op == "gt":
            mask = values > threshold
        else:
            mask = np.abs(values) > threshold
        for frame in np.flatnonzero(mask):
            per_frame[int(frame)].append(
                {
                    "type": "workspace_drift",
                    "axis": axis,
                    "operator": op,
                    "threshold": threshold,
                    "value": round(float(values[frame]), 9),
                }
            )
    return per_frame


def rolling_median_residual(pos: np.ndarray, window: int) -> np.ndarray:
    n = len(pos)
    window = max(3, int(window))
    if window % 2 == 0:
        window += 1
    half = window // 2
    residual = np.zeros(n, dtype=np.float64)
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        med = np.median(pos[a:b], axis=0)
        residual[i] = float(np.linalg.norm(pos[i] - med))
    return residual


def axis_reversal_count(delta: np.ndarray, small_motion_m: float) -> int:
    total = 0
    for axis_index in range(delta.shape[1]):
        axis_delta = delta[:, axis_index]
        signs = []
        for value in axis_delta:
            if abs(value) < small_motion_m:
                continue
            signs.append(1 if value > 0 else -1)
        total += sum(1 for prev, cur in zip(signs, signs[1:]) if prev != cur)
    return total


def sharp_turn_count(delta: np.ndarray, small_motion_m: float, sharp_turn_cos: float) -> int:
    total = 0
    for prev, cur in zip(delta[:-1], delta[1:]):
        prev_norm = float(np.linalg.norm(prev))
        cur_norm = float(np.linalg.norm(cur))
        if prev_norm < small_motion_m or cur_norm < small_motion_m:
            continue
        cos_value = float(np.dot(prev, cur) / max(prev_norm * cur_norm, 1e-12))
        if cos_value < sharp_turn_cos:
            total += 1
    return total


def cluster_jitter_metrics(
    pos: np.ndarray,
    small_motion_m: float,
    sharp_turn_cos: float,
) -> dict[str, float | int]:
    if len(pos) < 2:
        return {
            "path_m": 0.0,
            "chord_m": 0.0,
            "extent_m": 0.0,
            "path_extent_ratio": 0.0,
            "path_chord_ratio": 0.0,
            "axis_reversals": 0,
            "sharp_turns": 0,
        }
    delta = np.diff(pos, axis=0)
    step = np.linalg.norm(delta, axis=1)
    path = float(np.sum(step))
    chord = float(np.linalg.norm(pos[-1] - pos[0]))
    extent = float(np.linalg.norm(np.max(pos, axis=0) - np.min(pos, axis=0)))
    return {
        "path_m": path,
        "chord_m": chord,
        "extent_m": extent,
        "path_extent_ratio": path / max(extent, 1e-6),
        "path_chord_ratio": path / max(chord, 1e-6),
        "axis_reversals": axis_reversal_count(delta, small_motion_m),
        "sharp_turns": sharp_turn_count(delta, small_motion_m, sharp_turn_cos),
    }


def cluster_jitter_hit(metrics: dict[str, float | int], cfg: dict[str, Any]) -> bool:
    return (
        float(metrics["path_m"]) >= float(cfg["min_path_m"])
        and float(metrics["extent_m"]) <= float(cfg["max_extent_m"])
        and float(metrics["path_extent_ratio"]) >= float(cfg["min_path_extent_ratio"])
        and float(metrics["path_chord_ratio"]) >= float(cfg["min_path_chord_ratio"])
        and int(metrics["axis_reversals"]) >= int(cfg["min_axis_reversals"])
        and int(metrics["sharp_turns"]) >= int(cfg["min_sharp_turns"])
    )


def merge_ranges(ranges: list[tuple[int, int]], merge_gap: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start - merged[-1][1] - 1 > merge_gap:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def cluster_jitter_ranges(pos: np.ndarray, cfg: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    cluster_cfg = cfg.get("cluster_jitter", {})
    if not bool(cluster_cfg.get("enabled", False)):
        return []
    n = len(pos)
    window = max(4, int(cluster_cfg.get("window_frames", 301)))
    stride = max(1, int(cluster_cfg.get("stride_frames", 30)))
    min_len = max(1, int(cluster_cfg.get("min_segment_frames", 90)))
    if n < min(window, min_len):
        return []

    small_motion = float(cluster_cfg.get("small_motion_m", 0.0005))
    sharp_turn_cos = float(cluster_cfg.get("sharp_turn_cos", 0.0))
    starts = list(range(0, max(1, n - window + 1), stride))
    if starts[-1] != n - window:
        starts.append(max(0, n - window))

    hits: list[tuple[int, int]] = []
    for start in starts:
        end = min(n, start + window)
        metrics = cluster_jitter_metrics(pos[start:end], small_motion, sharp_turn_cos)
        if cluster_jitter_hit(metrics, cluster_cfg):
            hits.append((start, end - 1))

    out: list[tuple[int, int, dict[str, Any]]] = []
    for start, end in merge_ranges(hits, int(cluster_cfg.get("merge_gap_frames", 120))):
        if end - start + 1 < min_len:
            continue
        metrics = cluster_jitter_metrics(pos[start:end + 1], small_motion, sharp_turn_cos)
        if not cluster_jitter_hit(metrics, cluster_cfg):
            continue
        reason = {
            "type": "cluster_jitter",
            "window": [start, end],
            "path_m": round(float(metrics["path_m"]), 9),
            "chord_m": round(float(metrics["chord_m"]), 9),
            "extent_m": round(float(metrics["extent_m"]), 9),
            "path_extent_ratio": round(float(metrics["path_extent_ratio"]), 6),
            "path_chord_ratio": round(float(metrics["path_chord_ratio"]), 6),
            "axis_reversals": int(metrics["axis_reversals"]),
            "sharp_turns": int(metrics["sharp_turns"]),
            "thresholds": cluster_cfg,
        }
        out.append((start, end, reason))
    return out


def micro_jitter_metrics(
    pos: np.ndarray,
    small_motion_m: float,
    sharp_turn_cos: float,
) -> dict[str, float | int]:
    metrics = cluster_jitter_metrics(pos, small_motion_m, sharp_turn_cos)
    if len(pos) < 3:
        return {
            **metrics,
            "accel_p95_m": 0.0,
            "jerk_p95_m": 0.0,
            "step_mean_m": 0.0,
            "step_p95_m": 0.0,
            "step_cv": 0.0,
            "accel_step_ratio": 0.0,
            "jerk_step_ratio": 0.0,
        }
    delta = np.diff(pos, axis=0)
    step = np.linalg.norm(delta, axis=1)
    accel = np.diff(pos, n=2, axis=0)
    accel_norm = np.linalg.norm(accel, axis=1) if len(accel) else np.asarray([])
    jerk = np.diff(pos, n=3, axis=0)
    jerk_norm = np.linalg.norm(jerk, axis=1) if len(jerk) else np.asarray([])
    step_mean = float(np.mean(step)) if len(step) else 0.0
    step_p95 = float(np.percentile(step, 95)) if len(step) else 0.0
    accel_p95 = float(np.percentile(accel_norm, 95)) if len(accel_norm) else 0.0
    jerk_p95 = float(np.percentile(jerk_norm, 95)) if len(jerk_norm) else 0.0
    return {
        **metrics,
        "accel_p95_m": accel_p95,
        "jerk_p95_m": jerk_p95,
        "step_mean_m": step_mean,
        "step_p95_m": step_p95,
        "step_cv": float(np.std(step) / max(step_mean, 1e-9)) if len(step) else 0.0,
        "accel_step_ratio": accel_p95 / max(step_p95, 1e-9),
        "jerk_step_ratio": jerk_p95 / max(step_p95, 1e-9),
    }


def micro_jitter_hit(metrics: dict[str, float | int], cfg: dict[str, Any]) -> bool:
    rough_motion = (
        float(metrics["accel_step_ratio"]) >= float(cfg["min_accel_step_ratio"])
        or float(metrics["jerk_step_ratio"]) >= float(cfg["min_jerk_step_ratio"])
        or float(metrics["step_cv"]) >= float(cfg["min_step_cv"])
    )
    directional_jitter = (
        int(metrics["axis_reversals"]) >= int(cfg["min_axis_reversals"])
        and int(metrics["sharp_turns"]) >= int(cfg["min_sharp_turns"])
    )
    local_spike_jitter = (
        float(metrics["extent_m"]) <= float(cfg["max_spike_extent_m"])
        and int(metrics["axis_reversals"]) >= int(cfg["min_axis_reversals"])
        and float(metrics["step_cv"]) >= float(cfg["min_spike_step_cv"])
        and float(metrics["jerk_step_ratio"]) >= float(cfg["min_spike_jerk_step_ratio"])
    )
    return (
        float(metrics["path_m"]) >= float(cfg["min_path_m"])
        and float(metrics["extent_m"]) <= float(cfg["max_extent_m"])
        and float(metrics["path_extent_ratio"]) >= float(cfg["min_path_extent_ratio"])
        and float(metrics["path_chord_ratio"]) >= float(cfg["min_path_chord_ratio"])
        and (directional_jitter or local_spike_jitter)
        and float(metrics["accel_p95_m"]) >= float(cfg["min_accel_p95_m"])
        and rough_motion
    )


def micro_jitter_core_ranges(pos: np.ndarray, start: int, end: int, cfg: dict[str, Any]) -> list[tuple[int, int]]:
    region = pos[start:end + 1]
    if len(region) < 4:
        return []
    delta = np.diff(region, axis=0)
    accel = np.diff(region, n=2, axis=0)
    jerk = np.diff(region, n=3, axis=0)
    accel_norm = np.linalg.norm(accel, axis=1) if len(accel) else np.asarray([])
    jerk_norm = np.linalg.norm(jerk, axis=1) if len(jerk) else np.asarray([])
    point_accel = float(cfg.get("point_accel_m", 0.003))
    point_jerk = float(cfg.get("point_jerk_m", 0.004))
    small_motion = float(cfg.get("small_motion_m", 0.0003))
    sharp_turn_cos = float(cfg.get("sharp_turn_cos", 0.2))
    points: set[int] = set()
    for idx, value in enumerate(accel_norm):
        if float(value) >= point_accel:
            points.add(start + idx + 1)
    for idx, value in enumerate(jerk_norm):
        if float(value) >= point_jerk:
            points.add(start + idx + 1)
            points.add(start + idx + 2)
    for idx, (prev, cur) in enumerate(zip(delta[:-1], delta[1:])):
        prev_norm = float(np.linalg.norm(prev))
        cur_norm = float(np.linalg.norm(cur))
        if prev_norm < small_motion or cur_norm < small_motion:
            continue
        cos_value = float(np.dot(prev, cur) / max(prev_norm * cur_norm, 1e-12))
        if cos_value < sharp_turn_cos:
            points.add(start + idx + 1)

    if not points:
        return []
    padding = max(0, int(cfg.get("point_padding_frames", 12)))
    ranges = [(max(start, point - padding), min(end, point + padding)) for point in sorted(points)]
    return merge_ranges(ranges, int(cfg.get("point_merge_gap_frames", padding)))


def micro_jitter_ranges(pos: np.ndarray, cfg: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    micro_cfg = cfg.get("micro_jitter", {})
    if not bool(micro_cfg.get("enabled", False)):
        return []
    n = len(pos)
    windows = [int(win) for win in micro_cfg.get("window_frames", [])]
    windows = sorted({win for win in windows if win >= 4})
    if not windows or n < min(windows):
        return []

    stride = max(1, int(micro_cfg.get("stride_frames", 10)))
    small_motion = float(micro_cfg.get("small_motion_m", 0.0003))
    sharp_turn_cos = float(micro_cfg.get("sharp_turn_cos", 0.2))
    hits: list[tuple[int, int]] = []
    for window in windows:
        if n < window:
            continue
        starts = list(range(0, max(1, n - window + 1), stride))
        if starts[-1] != n - window:
            starts.append(max(0, n - window))
        for start in starts:
            end = min(n, start + window)
            metrics = micro_jitter_metrics(pos[start:end], small_motion, sharp_turn_cos)
            if micro_jitter_hit(metrics, micro_cfg):
                hits.extend(micro_jitter_core_ranges(pos, start, end - 1, micro_cfg))

    out: list[tuple[int, int, dict[str, Any]]] = []
    min_len = max(1, int(micro_cfg.get("min_segment_frames", min(windows))))
    for start, end in merge_ranges(hits, int(micro_cfg.get("merge_gap_frames", 25))):
        if end - start + 1 < min_len:
            continue
        metrics = micro_jitter_metrics(pos[start:end + 1], small_motion, sharp_turn_cos)
        reason = {
            "type": "micro_jitter",
            "window": [start, end],
            "path_m": round(float(metrics["path_m"]), 9),
            "chord_m": round(float(metrics["chord_m"]), 9),
            "extent_m": round(float(metrics["extent_m"]), 9),
            "path_extent_ratio": round(float(metrics["path_extent_ratio"]), 6),
            "path_chord_ratio": round(float(metrics["path_chord_ratio"]), 6),
            "axis_reversals": int(metrics["axis_reversals"]),
            "sharp_turns": int(metrics["sharp_turns"]),
            "accel_p95_m": round(float(metrics["accel_p95_m"]), 9),
            "jerk_p95_m": round(float(metrics["jerk_p95_m"]), 9),
            "step_mean_m": round(float(metrics["step_mean_m"]), 9),
            "step_p95_m": round(float(metrics["step_p95_m"]), 9),
            "step_cv": round(float(metrics["step_cv"]), 6),
            "accel_step_ratio": round(float(metrics["accel_step_ratio"]), 6),
            "jerk_step_ratio": round(float(metrics["jerk_step_ratio"]), 6),
            "thresholds": micro_cfg,
        }
        out.append((start, end, reason))
    return out


def mark_range(flags: np.ndarray, reasons: list[list[dict[str, Any]]], start: int, end: int, reason: dict[str, Any]) -> None:
    n = len(flags)
    a = max(0, start)
    b = min(n - 1, end)
    for index in range(a, b + 1):
        flags[index] = True
        reasons[index].append(reason)


def merge_and_filter(flags: np.ndarray, reasons: list[list[dict[str, Any]]], merge_gap: int, min_len: int) -> np.ndarray:
    n = len(flags)
    merged = flags.copy()
    runs = bool_runs(merged)
    for left, right in zip(runs, runs[1:]):
        gap = right[0] - left[1] - 1
        if 0 < gap <= merge_gap:
            merged[left[1] + 1:right[0]] = True
            for index in range(left[1] + 1, right[0]):
                reasons[index].append({"type": "merged_short_gap", "gap_frames": gap})

    filtered = merged.copy()
    for start, end in bool_runs(merged):
        if end - start + 1 < min_len:
            filtered[start:end + 1] = False
    return filtered


def bool_runs(flags: np.ndarray) -> list[tuple[int, int]]:
    runs = []
    n = len(flags)
    i = 0
    while i < n:
        if not bool(flags[i]):
            i += 1
            continue
        start = i
        while i + 1 < n and bool(flags[i + 1]):
            i += 1
        runs.append((start, i))
        i += 1
    return runs


def analyze_side(timestamps_ms: np.ndarray, pos: np.ndarray, cfg: dict[str, Any]) -> dict[str, Any]:
    n = len(pos)
    flags = np.zeros(n, dtype=bool)
    reasons: list[list[dict[str, Any]]] = [[] for _ in range(n)]

    for index, violations in enumerate(workspace_violations(pos, cfg)):
        if violations:
            flags[index] = True
            reasons[index].extend(violations)

    abrupt = cfg["abrupt_change"]
    expand = int(abrupt["expand_frames"])
    step_norm_thr = float(abrupt["step_norm_m"])
    axis_step_thr = float(abrupt["axis_step_m"])
    accel_thr = float(abrupt["accel_norm_m"])
    jerk_thr = float(abrupt["jerk_norm_m"])

    delta = np.diff(pos, axis=0)
    step_norm = np.linalg.norm(delta, axis=1) if n >= 2 else np.asarray([])
    axis_step = np.max(np.abs(delta), axis=1) if n >= 2 else np.asarray([])
    accel = np.diff(pos, n=2, axis=0)
    accel_norm = np.linalg.norm(accel, axis=1) if n >= 3 else np.asarray([])
    jerk = np.diff(pos, n=3, axis=0)
    jerk_norm = np.linalg.norm(jerk, axis=1) if n >= 4 else np.asarray([])

    for i, value in enumerate(step_norm, start=1):
        axis_value = float(axis_step[i - 1])
        if value > step_norm_thr or axis_value > axis_step_thr:
            reason = {
                "type": "abrupt_step",
                "step_norm_m": round(float(value), 9),
                "max_axis_step_m": round(axis_value, 9),
                "thresholds": {"step_norm_m": step_norm_thr, "axis_step_m": axis_step_thr},
            }
            mark_range(flags, reasons, i - expand, i + expand, reason)

    for i, value in enumerate(accel_norm, start=1):
        if value > accel_thr:
            reason = {
                "type": "abrupt_acceleration",
                "accel_norm_m": round(float(value), 9),
                "threshold": accel_thr,
            }
            mark_range(flags, reasons, i - expand, i + 1 + expand, reason)

    for i, value in enumerate(jerk_norm, start=1):
        if value > jerk_thr:
            reason = {
                "type": "abrupt_jerk",
                "jerk_norm_m": round(float(value), 9),
                "threshold": jerk_thr,
            }
            mark_range(flags, reasons, i - expand, i + 2 + expand, reason)

    jitter = cfg["jitter"]
    residual = rolling_median_residual(pos, int(jitter["median_window_frames"]))
    residual_thr = float(jitter["residual_norm_m"])
    accel_rms_thr = float(jitter["accel_rms_m"])
    step_rms_thr = float(jitter["step_rms_m"])
    path_ratio_thr = float(jitter["path_ratio"])
    min_reversals = int(jitter["min_axis_reversals"])
    small_motion = float(jitter["small_motion_m"])
    accel_step_ratio_thr = float(jitter.get("min_accel_step_ratio", 0.0))
    half = max(1, int(jitter["window_frames"]) // 2)

    for center in range(n):
        a = max(0, center - half)
        b = min(n, center + half + 1)
        if b - a < 4:
            continue
        local_delta = np.diff(pos[a:b], axis=0)
        local_step = np.linalg.norm(local_delta, axis=1)
        local_path = float(np.sum(local_step))
        chord = float(np.linalg.norm(pos[b - 1] - pos[a]))
        path_ratio = local_path / max(chord, 1e-6)
        local_accel = np.diff(pos[a:b], n=2, axis=0)
        accel_rms = float(np.sqrt(np.mean(np.linalg.norm(local_accel, axis=1) ** 2))) if len(local_accel) else 0.0
        step_rms = float(np.sqrt(np.mean(local_step ** 2))) if len(local_step) else 0.0
        accel_step_ratio = accel_rms / max(step_rms, 1e-9)
        reversals = axis_reversal_count(local_delta, small_motion)
        residual_peak = float(np.max(residual[a:b]))

        high_freq = (
            accel_rms > accel_rms_thr
            and accel_step_ratio >= accel_step_ratio_thr
            and reversals >= min_reversals
            and path_ratio > path_ratio_thr
        )
        residual_spike_train = (
            residual_peak > residual_thr
            and reversals >= max(2, min_reversals - 1)
            and accel_step_ratio >= accel_step_ratio_thr
            and (accel_rms > accel_rms_thr * 0.75 or step_rms > step_rms_thr)
        )
        rough_motion = (
            step_rms > step_rms_thr
            and accel_rms > accel_rms_thr
            and accel_step_ratio >= accel_step_ratio_thr
            and reversals >= max(2, min_reversals - 1)
        )
        if high_freq or residual_spike_train or rough_motion:
            reason = {
                "type": "jitter_window",
                "window": [a, b - 1],
                "accel_rms_m": round(accel_rms, 9),
                "step_rms_m": round(step_rms, 9),
                "path_ratio": round(path_ratio, 6),
                "axis_reversals": reversals,
                "residual_peak_m": round(residual_peak, 9),
                "accel_step_ratio": round(accel_step_ratio, 6),
            }
            mark_range(flags, reasons, a, b - 1, reason)

    for start, end, reason in cluster_jitter_ranges(pos, cfg):
        mark_range(flags, reasons, start, end, reason)

    for start, end, reason in micro_jitter_ranges(pos, cfg):
        mark_range(flags, reasons, start, end, reason)

    flags = merge_and_filter(
        flags,
        reasons,
        merge_gap=int(jitter["merge_gap_frames"]),
        min_len=int(jitter["min_segment_frames"]),
    )
    for index, is_bad in enumerate(flags):
        if not is_bad:
            reasons[index] = []

    segments = []
    for start, end in bool_runs(flags):
        reason_types = sorted({reason["type"] for frame in reasons[start:end + 1] for reason in frame})
        segments.append(
            {
                "label": 1,
                "smooth": False,
                "start_frame": start,
                "end_frame": end,
                "start_timestamp_ms": json_number(timestamps_ms[start]),
                "end_timestamp_ms": json_number(timestamps_ms[end]),
                "duration_ms": json_number(timestamps_ms[end] - timestamps_ms[start]),
                "reason_types": reason_types,
                "max_step_norm_m": round(float(np.max(step_norm[max(0, start - 1):end])) if len(step_norm) and end > 0 else 0.0, 9),
                "max_accel_norm_m": round(float(np.max(accel_norm[max(0, start - 1):max(0, end - 1)])) if len(accel_norm) and end > 1 else 0.0, 9),
            }
        )

    return {
        "smooth": ~flags,
        "bad": flags,
        "reasons": reasons,
        "segments": segments,
        "metrics": {
            "max_step_norm_m": round(float(np.max(step_norm)) if len(step_norm) else 0.0, 9),
            "max_axis_step_m": round(float(np.max(axis_step)) if len(axis_step) else 0.0, 9),
            "max_accel_norm_m": round(float(np.max(accel_norm)) if len(accel_norm) else 0.0, 9),
            "max_jerk_norm_m": round(float(np.max(jerk_norm)) if len(jerk_norm) else 0.0, 9),
            "bad_frame_count": int(np.sum(flags)),
            "bad_frame_ratio": round(float(np.mean(flags)) if n else 0.0, 9),
        },
    }


def json_number(value: Any) -> int | float:
    number = float(value)
    rounded = round(number)
    if abs(number - rounded) < 1e-6:
        return int(rounded)
    return float(f"{number:.6f}")


def frame_payloads(timestamps_ms: np.ndarray, side_results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    frames = []
    for index, ts in enumerate(timestamps_ms):
        per_side = {}
        any_bad = False
        for side, result in side_results.items():
            bad = bool(side_results[side]["bad"][index])
            any_bad = any_bad or bad
            per_side[side] = {
                "smooth": not bad,
                "label": 1 if bad else 0,
                "reasons": result["reasons"][index],
            }
        frames.append(
            {
                "frame": index,
                "timestamp_ms": json_number(ts),
                "smooth": not any_bad,
                "label": 1 if any_bad else 0,
                "sides": per_side,
            }
        )
    return frames


def side_has_workspace_drift(side_result: dict[str, Any]) -> bool:
    return any(
        "workspace_drift" in segment.get("reason_types", [])
        for segment in side_result.get("segments", [])
    )


def static_drift_sides(static_drift: dict[str, Any] | None) -> list[str]:
    if not static_drift or not static_drift.get("available"):
        return []
    sides = static_drift.get("drift_sides") or []
    return [side for side in sides if side in SIDES]


def wrist_view_video_motion(video_path: Path, num_frames: int) -> float | None:
    """Mean frame-to-frame absolute difference over sampled wrist-view frames."""
    try:
        import cv2
    except ImportError:
        return None

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 1:
        cap.release()
        return None

    idxs = np.linspace(0, total - 1, min(num_frames, total)).astype(int)
    prev = None
    diffs: list[float] = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(cv2.resize(frame, (128, 128)), cv2.COLOR_BGR2GRAY).astype("float32")
        if prev is not None:
            diffs.append(float(np.mean(np.abs(gray - prev))))
        prev = gray
    cap.release()
    return (sum(diffs) / len(diffs)) if diffs else None


def action_motion_from_positions(positions: dict[str, np.ndarray]) -> dict[str, dict[str, float]]:
    """Per-side trajectory path length and XYZ extent in the judged coordinate frame."""
    out: dict[str, dict[str, float]] = {}
    for side in SIDES:
        pos = positions.get(side)
        if pos is None or len(pos) == 0:
            out[side] = {"path": 0.0, "extent": 0.0}
            continue
        if len(pos) >= 2:
            path = float(np.sum(np.linalg.norm(np.diff(pos, axis=0), axis=1)))
        else:
            path = 0.0
        extent = float(np.sum(np.max(pos, axis=0) - np.min(pos, axis=0)))
        out[side] = {"path": path, "extent": extent}
    return out


def assess_static_drift(
    episode_dir: Path,
    positions: dict[str, np.ndarray],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Detect wrist-view-static but trajectory-moving drift.

    This ports the assessment motion-consistency drift evidence into judgment:
    the wrist-view video is used as physical evidence that a side is static,
    and the judged trajectory path/extent is used as evidence that the action
    still moves. Static video + moving trajectory is reported as static drift.
    """
    drift_cfg = cfg.get("static_drift", {})
    video_static_thr = float(drift_cfg.get("video_static_threshold", 4.0))
    path_active = float(drift_cfg.get("action_path_active", 5.0))
    extent_active = float(drift_cfg.get("action_extent_active", 0.3))
    num_frames = int(drift_cfg.get("num_sample_frames", 30))

    base: dict[str, Any] = {
        "available": False,
        "has_drift": False,
        "status": "unavailable",
        "drift_sides": [],
        "thresholds": {
            "video_static_threshold": video_static_thr,
            "action_path_active": path_active,
            "action_extent_active": extent_active,
        },
        "num_sample_frames": num_frames,
    }

    if not all(side in positions for side in SIDES):
        return {**base, "error": "static drift check requires left/right trajectories"}

    video_paths = {side: episode_dir / WRIST_VIEW[side] / "video.mp4" for side in SIDES}
    missing = [str(path.relative_to(episode_dir)) for path in video_paths.values() if not path.exists()]
    if missing:
        return {**base, "error": "missing wrist_view video(s)", "missing": missing}

    video_motion: dict[str, float] = {}
    unreadable: list[str] = []
    for side, path in video_paths.items():
        motion = wrist_view_video_motion(path, num_frames)
        if motion is None:
            unreadable.append(side)
        else:
            video_motion[side] = motion
    if unreadable:
        return {**base, "error": "unreadable wrist_view video(s)", "unreadable_sides": unreadable}

    action_motion = action_motion_from_positions(positions)
    video_static = {side: video_motion[side] < video_static_thr for side in SIDES}
    action_moving = {
        side: (
            action_motion[side]["path"] > path_active
            or action_motion[side]["extent"] > extent_active
        )
        for side in SIDES
    }
    drift_sides = [side for side in SIDES if video_static[side] and action_moving[side]]

    return {
        **base,
        "available": True,
        "has_drift": bool(drift_sides),
        "status": "static_drift" if drift_sides else "consistent",
        "drift_sides": drift_sides,
        "video_motion": {side: round(video_motion[side], 3) for side in SIDES},
        "video_static": video_static,
        "action_path": {side: round(action_motion[side]["path"], 4) for side in SIDES},
        "action_extent": {side: round(action_motion[side]["extent"], 4) for side in SIDES},
        "action_moving": action_moving,
    }


def quality_for_side(
    side_result: dict[str, Any],
    cfg: dict[str, Any],
    *,
    static_drift: dict[str, Any] | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    if side is not None and side in static_drift_sides(static_drift):
        return {
            "trajectory_quality": False,
            "status": "static_drift",
            "reasons": ["wrist_view video is static while judged trajectory moves"],
            "thresholds": (static_drift or {}).get("thresholds", {}),
        }

    metrics = side_result["metrics"]
    segments = side_result["segments"]
    if not metrics["bad_frame_count"]:
        return {
            "trajectory_quality": True,
            "status": "clean",
            "reasons": ["no abnormal frames"],
        }

    quality_cfg = cfg["quality"]
    quality_bad = quality_relevant_bad_mask(side_result, cfg)
    quality_bad_ratio = float(np.mean(quality_bad)) if len(quality_bad) else 0.0
    allowed_reasons = set(quality_cfg["minor_jitter_allowed_reason_types"])
    reason_types = {
        reason
        for segment in segments
        for reason in segment.get("reason_types", [])
    }
    disallowed = sorted(reason_types - allowed_reasons)
    reasons = []
    if disallowed:
        reasons.append(f"contains non-minor reason types: {', '.join(disallowed)}")

    max_ratio = float(quality_cfg["minor_jitter_max_bad_frame_ratio"])
    if quality_bad_ratio > max_ratio:
        reasons.append(f"bad frame ratio exceeds minor jitter limit {max_ratio}")

    max_duration_ms = float(quality_cfg["minor_jitter_max_segment_duration_ms"])
    duration_exempt = set(quality_cfg.get("quality_duration_exempt_reason_types", []))
    duration_relevant_segments = [
        segment
        for segment in segments
        if not (segment_reason_types(segment) & duration_exempt)
    ]
    longest_segment_ms = max((float(segment["duration_ms"]) for segment in duration_relevant_segments), default=0.0)
    if longest_segment_ms > max_duration_ms:
        reasons.append(f"longest abnormal segment exceeds minor jitter limit {max_duration_ms:g} ms")

    minor_metric_limits = (
        ("max_step_norm_m", "minor_jitter_max_step_norm_m"),
        ("max_axis_step_m", "minor_jitter_max_axis_step_m"),
        ("max_accel_norm_m", "minor_jitter_max_accel_norm_m"),
        ("max_jerk_norm_m", "minor_jitter_max_jerk_norm_m"),
    )
    for metric_key, threshold_key in minor_metric_limits:
        threshold = float(quality_cfg[threshold_key])
        if float(metrics[metric_key]) > threshold:
            reasons.append(f"{metric_key} exceeds minor jitter limit {threshold:g}")

    trajectory_quality = not reasons
    return {
        "trajectory_quality": trajectory_quality,
        "status": "minor_jitter" if trajectory_quality else "bad",
        "reasons": reasons or ["only small local jitter below quality-failure thresholds"],
        "quality_bad_frame_count": int(np.sum(quality_bad)),
        "quality_bad_frame_ratio": round(quality_bad_ratio, 9),
        "longest_segment_duration_ms": json_number(longest_segment_ms),
        "thresholds": quality_cfg,
    }


def segment_reason_types(segment: dict[str, Any]) -> set[str]:
    return {str(reason) for reason in segment.get("reason_types", [])}


def frame_reason_types(frame_reasons: list[dict[str, Any]]) -> set[str]:
    return {str(reason.get("type")) for reason in frame_reasons if reason.get("type")}


def quality_relevant_bad_mask(side_result: dict[str, Any], cfg: dict[str, Any]) -> np.ndarray:
    bad = side_result["bad"]
    quality_cfg = cfg["quality"]
    ratio_exempt = set(quality_cfg.get("quality_ratio_exempt_reason_types", []))
    if not ratio_exempt:
        return bad.copy()
    relevant = bad.copy()
    for index, is_bad in enumerate(bad):
        if not is_bad:
            continue
        reason_types = frame_reason_types(side_result["reasons"][index])
        if reason_types and reason_types & ratio_exempt:
            relevant[index] = False
    return relevant


def quality_for_episode(
    side_results: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    static_drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sides = {
        side: quality_for_side(result, cfg, static_drift=static_drift, side=side)
        for side, result in side_results.items()
    }
    drift_sides = static_drift_sides(static_drift)
    reasons = [
        f"{side} side is bad"
        for side, result in sides.items()
        if result["trajectory_quality"] is not True and side not in drift_sides
    ]
    if drift_sides:
        reasons.insert(0, "static drift detected on side(s): " + ", ".join(drift_sides))
    quality_cfg = cfg["quality"]
    first_result = next(iter(side_results.values()))
    any_bad = np.zeros(len(first_result["bad"]), dtype=bool)
    for result in side_results.values():
        any_bad |= quality_relevant_bad_mask(result, cfg)
    episode_bad_ratio = float(np.mean(any_bad)) if len(any_bad) else 0.0
    max_episode_ratio = float(quality_cfg["minor_jitter_max_episode_bad_frame_ratio"])
    if episode_bad_ratio > max_episode_ratio:
        reasons.append(f"episode bad frame ratio exceeds minor jitter limit {max_episode_ratio}")

    moderate_ratio = float(quality_cfg.get("moderate_metric_min_episode_bad_frame_ratio", 1.0))
    if episode_bad_ratio > moderate_ratio:
        moderate_checks = (
            ("max_step_norm_m", "moderate_step_norm_m"),
            ("max_accel_norm_m", "moderate_accel_norm_m"),
            ("max_jerk_norm_m", "moderate_jerk_norm_m"),
        )
        for side, result in side_results.items():
            metrics = result["metrics"]
            for metric_key, threshold_key in moderate_checks:
                threshold = float(quality_cfg[threshold_key])
                if float(metrics[metric_key]) > threshold:
                    reasons.append(
                        f"{side} {metric_key} exceeds moderate limit {threshold:g} "
                        f"with episode bad ratio > {moderate_ratio}"
                    )
                    break
            if reasons and reasons[-1].startswith(f"{side} "):
                break

    trajectory_quality = not reasons
    return {
        "trajectory_quality": trajectory_quality,
        "status": "good" if trajectory_quality else ("static_drift" if drift_sides else "bad"),
        "reasons": reasons or ["all sides are clean or only have minor local jitter"],
        "episode_bad_frame_ratio": round(episode_bad_ratio, 9),
        "sides": sides,
    }


def recoverability_for_side(side_result: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    rec_cfg = cfg["recoverable"]
    metrics = side_result["metrics"]
    segments = side_result["segments"]
    bad = bool(metrics["bad_frame_count"])
    reasons = []

    if not bad:
        return {
            "recoverable": False,
            "status": "clean",
            "reasons": ["no abnormal frames"],
        }

    if side_has_workspace_drift(side_result) and bool(rec_cfg.get("workspace_drift_blocks_recovery", True)):
        reasons.append("workspace drift is not recoverable")

    max_ratio = float(rec_cfg["max_bad_frame_ratio"])
    if float(metrics["bad_frame_ratio"]) > max_ratio:
        reasons.append(f"bad frame ratio exceeds {max_ratio}")

    max_duration_ms = float(rec_cfg["max_segment_duration_ms"])
    longest_segment_ms = max((float(segment["duration_ms"]) for segment in segments), default=0.0)
    if longest_segment_ms > max_duration_ms:
        reasons.append(f"longest abnormal segment exceeds {max_duration_ms:g} ms")

    severity_checks = (
        ("max_step_norm_m", "severe_step_norm_m"),
        ("max_axis_step_m", "severe_axis_step_m"),
        ("max_accel_norm_m", "severe_accel_norm_m"),
        ("max_jerk_norm_m", "severe_jerk_norm_m"),
    )
    for metric_key, threshold_key in severity_checks:
        threshold = float(rec_cfg[threshold_key])
        if float(metrics[metric_key]) > threshold:
            reasons.append(f"{metric_key} exceeds severe threshold {threshold:g}")

    recoverable = not reasons
    return {
        "recoverable": recoverable,
        "status": "recoverable" if recoverable else "unrecoverable",
        "reasons": reasons or ["local jitter/jumps remain within recoverable limits"],
        "longest_segment_duration_ms": json_number(longest_segment_ms),
        "thresholds": rec_cfg,
    }


def static_drift_recoverability(static_drift: dict[str, Any], side_recoverability: dict[str, Any]) -> dict[str, Any]:
    drift_sides = static_drift_sides(static_drift)
    sides = dict(side_recoverability)
    for side in drift_sides:
        sides[side] = {
            "recoverable": True,
            "status": "recoverable",
            "reasons": ["static drift is treated as recoverable"],
            "thresholds": static_drift.get("thresholds", {}),
        }
    return {
        "recoverable": True,
        "status": "recoverable",
        "reasons": ["static drift is treated as recoverable"],
        "static_drift_sides": drift_sides,
        "sides": sides,
    }


def recoverability_for_episode(
    side_results: dict[str, dict[str, Any]],
    bad_frames: np.ndarray,
    cfg: dict[str, Any],
    quality: dict[str, Any],
    static_drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    side_recoverability = {
        side: recoverability_for_side(result, cfg)
        for side, result in side_results.items()
    }
    if static_drift_sides(static_drift):
        return static_drift_recoverability(static_drift or {}, side_recoverability)
    if quality["trajectory_quality"] is True:
        return {
            "recoverable": False,
            "status": "not_needed",
            "reasons": ["trajectory quality is true"],
            "sides": side_recoverability,
        }

    bad = bool(np.any(bad_frames))
    if not bad:
        return {
            "recoverable": False,
            "status": "clean",
            "reasons": ["no abnormal frames"],
            "sides": side_recoverability,
        }

    rec_cfg = cfg["recoverable"]
    reasons = []
    bad_ratio = float(np.mean(bad_frames)) if len(bad_frames) else 0.0
    max_ratio = float(rec_cfg["max_bad_frame_ratio"])
    if bad_ratio > max_ratio:
        reasons.append(f"episode bad frame ratio exceeds {max_ratio}")

    for side, result in side_recoverability.items():
        if side_results[side]["metrics"]["bad_frame_count"] and not result["recoverable"]:
            reasons.append(f"{side} side is unrecoverable")

    recoverable = not reasons
    return {
        "recoverable": recoverable,
        "status": "recoverable" if recoverable else "unrecoverable",
        "reasons": reasons or ["abnormal frames are local and remain within recoverable limits"],
        "sides": side_recoverability,
    }


def judge_episode(
    episode_dir: Path,
    config: dict[str, Any],
    transform_config: dict[str, Any],
    generated_at: str,
    *,
    skip_static_drift: bool = False,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "type": "trajectory_judgment",
        "generated_at": generated_at,
        "episode": episode_dir.name,
        "path": str(episode_dir),
        "action_source": f"{ACTION_DIR}/data.csv",
        "coordinate_frame": "world_eef",
        "config": config,
    }
    try:
        timestamps_ms, positions = load_transformed_episode(episode_dir, transform_config)
    except Exception as exc:
        base.update(
            {
                "ok": False,
                "trajectory_quality": False,
                "recoverable": False,
                "recoverability": {
                    "recoverable": False,
                    "status": "error",
                    "reasons": [str(exc)],
                    "sides": {},
                },
                "error": str(exc),
                "frame_count": 0,
                "frames": [],
                "sides": {},
            }
        )
        return base

    side_results = {side: analyze_side(timestamps_ms, pos, config) for side, pos in positions.items()}
    bad_frames = np.zeros(len(timestamps_ms), dtype=bool)
    for result in side_results.values():
        bad_frames |= result["bad"]
    if skip_static_drift:
        static_drift = {
            "available": False,
            "has_drift": False,
            "status": "skipped",
            "drift_sides": [],
            "reason": "disabled by --skip-static-drift",
        }
    else:
        static_drift = assess_static_drift(episode_dir, positions, config)
    quality = quality_for_episode(side_results, config, static_drift)
    recoverability = recoverability_for_episode(side_results, bad_frames, config, quality, static_drift)

    base.update(
        {
            "ok": True,
            "trajectory_quality": bool(quality["trajectory_quality"]),
            "quality": quality,
            "recoverable": bool(recoverability["recoverable"]),
            "recoverability": recoverability,
            "static_drift": static_drift,
            "frame_count": int(len(timestamps_ms)),
            "bad_frame_count": int(np.sum(bad_frames)),
            "bad_frame_ratio": round(float(np.mean(bad_frames)) if len(bad_frames) else 0.0, 9),
            "sides": {
                side: {
                    "trajectory_quality": bool(quality["sides"][side]["trajectory_quality"]),
                    "quality": quality["sides"][side],
                    "recoverable": (
                        bool(recoverability["sides"][side]["recoverable"])
                        if quality["sides"][side]["trajectory_quality"] is not True
                        else False
                    ),
                    "recoverability": recoverability["sides"][side],
                    "metrics": side_results[side]["metrics"],
                    "segments": side_results[side]["segments"],
                }
                for side in side_results
            },
            "frames": frame_payloads(timestamps_ms, side_results),
        }
    )
    return base


def build_summary(input_path: Path, reports: list[tuple[Path, dict[str, Any]]], generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "type": "trajectory_judgment_summary",
        "generated_at": generated_at,
        "source": str(input_path),
        "processed": len(reports),
        "ok": sum(1 for _path, report in reports if report.get("ok")),
        "normal": sum(1 for _path, report in reports if report.get("ok") and report.get("trajectory_quality") is True),
        "abnormal": sum(1 for _path, report in reports if report.get("trajectory_quality") is False),
        "static_drift": sum(
            1
            for _path, report in reports
            if (report.get("static_drift") or {}).get("has_drift") is True
        ),
        "recoverable": sum(1 for _path, report in reports if report.get("recoverable") is True),
        "unrecoverable": sum(
            1
            for _path, report in reports
            if report.get("trajectory_quality") is False and report.get("recoverable") is not True
        ),
        "episodes": [
            {
                "episode": report.get("episode"),
                "path": report.get("path"),
                "ok": report.get("ok"),
                "trajectory_quality": report.get("trajectory_quality"),
                "recoverable": report.get("recoverable"),
                "static_drift": (report.get("static_drift") or {}).get("has_drift"),
                "static_drift_sides": (report.get("static_drift") or {}).get("drift_sides"),
                "bad_frame_count": report.get("bad_frame_count"),
                "bad_frame_ratio": report.get("bad_frame_ratio"),
                "report": str(path),
                "error": report.get("error"),
            }
            for path, report in reports
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Judge transformed actions.eef_pose trajectories and write per-timestamp binary labels.",
    )
    parser.add_argument("input_path_arg", nargs="?", type=Path, help="An episode_* directory or a folder containing episodes.")
    parser.add_argument("-i", "--input-path", type=Path, help="An episode_* directory or a folder containing episodes.")
    parser.add_argument("-o", "--output-root", type=Path, default=Path("outputs"), help="Output root. Reports go under <root>/report.")
    parser.add_argument("--config", type=Path, help="Optional detector config JSON merged over judgment_config.json.")
    parser.add_argument(
        "--transform-config",
        type=Path,
        default=DEFAULT_TRANSFORM_CONFIG,
        help="Transform config JSON. Defaults to transform/ee_trajectory_config.json.",
    )
    parser.add_argument("--no-summary", action="store_true", help="Do not write report/summary.json.")
    parser.add_argument(
        "--skip-static-drift",
        action="store_true",
        help="Skip wrist-view video static-drift cross-check; trajectory jitter checks still run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = args.input_path or args.input_path_arg
    if input_path is None:
        raise ValueError("Input path is required.")
    input_path = input_path.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()

    config = load_config(args.config)
    transform_config = load_transform_config(args.transform_config)
    episodes = find_episode_dirs(input_path)
    generated_at = now_utc_iso()

    reports: list[tuple[Path, dict[str, Any]]] = []
    for episode_dir in episodes:
        report = judge_episode(
            episode_dir,
            config,
            transform_config,
            generated_at,
            skip_static_drift=args.skip_static_drift,
        )
        out_path = report_path_for(input_path, episode_dir, output_root)
        write_json(out_path, report)
        reports.append((out_path, report))
        status = "OK" if report.get("trajectory_quality") is True else "BAD"
        if report.get("trajectory_quality") is True:
            rec = "clean"
        else:
            rec = "recoverable" if report.get("recoverable") is True else "unrecoverable"
        print(f"{episode_dir.name}: {status} {rec} -> {out_path}")

    if not args.no_summary:
        summary_path = output_root / "report" / "summary.json"
        write_json(summary_path, build_summary(input_path, reports, generated_at))
        print(f"summary -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
