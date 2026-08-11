#!/usr/bin/env python3
"""Recover UMI gripper width from raw rotation and wrist-view video.

Subcommands:

* assess  - classify failed gripper sides as recoverable/unrecoverable;
* fit     - learn raw-rotation and visual gripper-width mappings from correct data;
* recover - use the learned model to repair recoverable gripper CSVs.

The visual signal is intentionally simple and inspectable: for sampled wrist-view
frames, it looks in the upper part of the frame for two dark gripper fingers and
uses their inner pixel gap. This gives an independent check against the magnetic
raw rotation stream and avoids relying on metadata calibration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import numpy as np


SIDES = ("left", "right")
DEFAULT_REPORT = Path("tools/data/gripper_outputs/all_samples/gripper_compact_report.csv")
DEFAULT_MODEL = Path("tools/data/gripper_recovery/gripper_recovery_model.json")
DEFAULT_OUT = Path("tools/data/gripper_recovery/reports")
DEFAULT_CURATED_SUCCESS_ROOT = Path("/home/ubuntu/data/curated_samples/success")
GOOD_GRIPPER_TYPES = {"DYNAMIC_OK", "STATIC_OK"}


@dataclass
class SideSeries:
    timestamps: list[str]
    raw: list[float]
    gripper: list[float] | None


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    common.add_argument("--out", type=Path, default=DEFAULT_OUT)

    assess = sub.add_parser("assess", parents=[common], help="Classify failed gripper sides.")
    assess.add_argument("--samples-per-video", type=int, default=48)
    assess.add_argument("--video-motion-threshold-px", type=float, default=4.0)
    assess.add_argument("--raw-static-deg", type=float, default=2.0)
    assess.add_argument("--raw-invalid-deg", type=float, default=30.0)
    assess.add_argument("--visual-motion-range", type=float, default=0.03)
    assess.add_argument("--include-missing-video-sides", action="store_true")
    assess.add_argument("--limit", type=int)

    fit = sub.add_parser("fit", parents=[common], help="Fit raw/vision gripper recovery model.")
    fit.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    fit.add_argument("--min-correct-sides", type=int, default=100)
    fit.add_argument("--samples-per-side", type=int, default=32)
    fit.add_argument("--max-sides", type=int, default=1200)
    fit.add_argument("--min-visual-confidence", type=float, default=0.35)
    fit.add_argument("--min-dynamic-raw-range-deg", type=float, default=5.0)
    fit.add_argument("--min-dynamic-gripper-range-m", type=float, default=0.003)
    fit.add_argument("--min-dynamic-visual-range", type=float, default=0.03)

    recover = sub.add_parser("recover", parents=[common], help="Repair recoverable gripper CSVs.")
    recover.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    recover.add_argument("--assessment", type=Path, default=DEFAULT_OUT / "gripper_recoverability.csv")
    recover.add_argument("--samples-per-side", type=int, default=64)
    recover.add_argument("--agreement-threshold-m", type=float, default=0.012)
    recover.add_argument("--raw-invalid-deg", type=float, default=30.0)
    recover.add_argument("--visual-motion-range", type=float, default=0.03)
    recover.add_argument("--apply", action="store_true")
    recover.add_argument("--include-missing-video-sides", action="store_true")
    recover.add_argument("--limit", type=int)
    recover.add_argument("--only-source", action="append", default=[])

    process = sub.add_parser(
        "process-root",
        help="Scan a dataset root, detect bad gripper sides, recover them, and mark unrecoverable metadata.",
    )
    process.add_argument("--data-root", type=Path, default=DEFAULT_CURATED_SUCCESS_ROOT)
    process.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    process.add_argument("--out", type=Path, default=DEFAULT_OUT / "curated_samples_success")
    process.add_argument("--samples-per-side", type=int, default=64)
    process.add_argument("--agreement-threshold-m", type=float, default=0.012)
    process.add_argument("--raw-invalid-deg", type=float, default=30.0)
    process.add_argument("--visual-motion-range", type=float, default=0.03)
    process.add_argument("--apply", action="store_true")
    process.add_argument("--include-missing-video-sides", action="store_true")
    process.add_argument("--limit", type=int)
    process.add_argument("--raw-static-deg", type=float, default=2.0)
    process.add_argument("--gripper-static-m", type=float, default=0.001)
    process.add_argument("--min-correlation", type=float, default=0.8)
    process.add_argument("--min-r2", type=float, default=0.8)
    process.add_argument("--min-gripper-m", type=float, default=-0.002)
    process.add_argument("--max-gripper-m", type=float, default=0.09)
    process.add_argument("--recompute-tolerance-m", type=float, default=0.003)
    process.add_argument("--disable-metadata-check", action="store_true")

    ensure = sub.add_parser("ensure-originals", help="Copy observation.state.gripper to gripper_original where missing.")
    ensure.add_argument("--data-root", type=Path, default=DEFAULT_CURATED_SUCCESS_ROOT)
    ensure.add_argument("--out", type=Path, default=DEFAULT_OUT / "curated_samples_success")
    ensure.add_argument("--refresh-checksums", action="store_true", default=True)

    return parser.parse_args()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        make_writable(path.parent)
        make_writable(path)
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)


def write_json(path: Path, data: Any):
    def default(obj):
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=default)
        f.write("\n")


def make_writable(path: Path):
    if path.exists():
        path.chmod(path.stat().st_mode | 0o200)


def load_report(path: Path) -> list[dict[str, str]]:
    return read_csv_rows(path)


def side_col(fieldnames: list[str], side: str, suffix: str, single_name: str | None = None):
    prefixed = f"{side}_{suffix}"
    if prefixed in fieldnames:
        return prefixed
    if single_name and single_name in fieldnames:
        return single_name
    return None


def load_side_series(episode: Path, side: str, want_gripper=True) -> SideSeries | None:
    raw_path = episode / "observation.state.raw_gripper_rotation" / "data.csv"
    gripper_path = episode / "observation.state.gripper" / "data.csv"
    if not raw_path.exists():
        return None
    raw_rows = read_csv_rows(raw_path)
    if not raw_rows:
        return None
    raw_fields = list(raw_rows[0].keys())
    raw_col = side_col(raw_fields, side, "raw_gripper_rotation", "raw_gripper_rotation")
    if raw_col is None:
        return None
    raw_by_ts = {
        row["timestamp_ms"]: float(row[raw_col])
        for row in raw_rows
        if row.get("timestamp_ms") and row.get(raw_col) not in {None, ""}
    }

    if not want_gripper:
        ts = sorted(raw_by_ts, key=lambda item: int(float(item)))
        return SideSeries(ts, [raw_by_ts[t] for t in ts], None)

    if not gripper_path.exists():
        return None
    gripper_rows = read_csv_rows(gripper_path)
    if not gripper_rows:
        return None
    grip_fields = list(gripper_rows[0].keys())
    grip_col = side_col(grip_fields, side, "gripper", "gripper")
    if grip_col is None:
        return None
    grip_by_ts = {
        row["timestamp_ms"]: float(row[grip_col])
        for row in gripper_rows
        if row.get("timestamp_ms") and row.get(grip_col) not in {None, ""}
    }
    ts = sorted(set(raw_by_ts) & set(grip_by_ts), key=lambda item: int(float(item)))
    if not ts:
        return None
    return SideSeries(ts, [raw_by_ts[t] for t in ts], [grip_by_ts[t] for t in ts])


def unwrap_degrees(values: list[float]) -> list[float]:
    out = []
    prev = None
    cur = 0.0
    for value in values:
        if prev is None:
            cur = value
        else:
            cur += (value - prev + 180.0) % 360.0 - 180.0
        out.append(cur)
        prev = value
    return out


def pearson(xs, ys):
    if len(xs) < 2:
        return None
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if np.std(x) <= 1e-12 or np.std(y) <= 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def rank_values(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2.0
        for k in range(i, j):
            ranks[order[k]] = rank
        i = j
    return ranks


def spearman(xs, ys):
    if len(xs) < 2:
        return None
    return pearson(rank_values(list(xs)), rank_values(list(ys)))


def fit_linear(xs, ys):
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if len(x) < 2 or np.var(x) <= 1e-12:
        return None
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    ss_res = float(np.sum((y - pred) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return {"slope": float(slope), "intercept": float(intercept), "r2": float(r2)}


def predict_linear(model, xs):
    return np.asarray(xs, dtype=float) * model["slope"] + model["intercept"]


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), q))


def wrist_video_path(episode: Path, side: str) -> Path:
    return episode / f"observation.image.{side}_wrist_view" / "video.mp4"


def timestamp_to_frame(timestamp_ms: str, fps: float, total_frames: int):
    idx = int(round(float(timestamp_ms) * fps / 1000.0))
    return max(0, min(total_frames - 1, idx))


def sample_indices(n: int, count: int) -> list[int]:
    if n <= 0:
        return []
    return sorted({int(round(v)) for v in np.linspace(0, n - 1, min(n, count))})


def extract_gripper_gap(frame) -> tuple[float | None, float]:
    """Return normalized inner gap between two dark gripper fingers and confidence."""
    h, w = frame.shape[:2]
    y0, y1 = 0, int(h * 0.52)
    x0, x1 = int(w * 0.16), int(w * 0.84)
    roi = frame[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # Black gripper fingers: low value. The top-central ROI avoids most objects
    # and the dark image borders. Measure row-wise inner edges instead of the
    # largest components because the gripper body is often connected to the top.
    mask = (hsv[:, :, 2] < 80).astype("uint8") * 255
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mid = mask.shape[1] // 2
    row_gaps = []
    row_start = int(mask.shape[0] * 0.08)
    row_end = int(mask.shape[0] * 0.94)
    for y in range(row_start, row_end):
        xs = np.flatnonzero(mask[y] > 0)
        if xs.size < 8:
            continue
        left = xs[xs < mid]
        right = xs[xs >= mid]
        if left.size < 4 or right.size < 4:
            continue
        gap = int(right.min()) - int(left.max())
        if gap < -0.04 * mask.shape[1] or gap > 0.72 * mask.shape[1]:
            continue
        row_gaps.append(max(0, gap))
    if len(row_gaps) >= max(8, int(0.05 * (row_end - row_start))):
        gap = float(np.median(row_gaps))
        confidence = min(1.0, len(row_gaps) / max(1.0, 0.45 * (row_end - row_start)))
        return gap / w, confidence

    # Fallback for frames where row scanning fails after glare/occlusion.
    num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    comps = []
    area_min = max(80, int(0.0015 * roi.shape[0] * roi.shape[1]))
    for i in range(1, num):
        x, y, bw, bh, area = stats[i]
        if area < area_min or bh < 8 or bw < 4:
            continue
        cx, cy = centroids[i]
        # Prefer components touching/near top camera gripper area.
        if cy > roi.shape[0] * 0.95:
            continue
        comps.append({"x": x, "y": y, "w": bw, "h": bh, "area": area, "cx": cx, "cy": cy})
    if len(comps) < 2:
        return None, 0.0
    comps = sorted(comps, key=lambda c: c["area"], reverse=True)[:6]
    best = None
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            a, b = sorted([comps[i], comps[j]], key=lambda c: c["cx"])
            center_dist = b["cx"] - a["cx"]
            if center_dist < w * 0.03:
                continue
            gap = b["x"] - (a["x"] + a["w"])
            if gap < -w * 0.1:
                continue
            # Score prefers sizable, horizontally separated dark components.
            area_score = min(1.0, (a["area"] + b["area"]) / (0.04 * roi.shape[0] * roi.shape[1]))
            sep_score = min(1.0, center_dist / (0.35 * roi.shape[1]))
            y_score = 1.0 - min(1.0, abs(a["cy"] - b["cy"]) / max(roi.shape[0], 1))
            score = 0.45 * area_score + 0.35 * sep_score + 0.20 * y_score
            if best is None or score > best[0]:
                best = (score, max(0.0, gap) / w)
    if best is None:
        return None, 0.0
    return best[1], best[0]


def sample_visual_series(episode: Path, side: str, timestamps: list[str], samples: int):
    path = wrist_video_path(episode, side)
    if not path.exists():
        return {}
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    out = {}
    for i in sample_indices(len(timestamps), samples):
        ts = timestamps[i]
        frame_idx = timestamp_to_frame(ts, fps, total)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gap, conf = extract_gripper_gap(frame)
        if gap is not None:
            out[ts] = {"gap": gap, "confidence": conf, "frame": frame_idx}
    cap.release()
    return out


def visual_motion_metrics(episode: Path, side: str, timestamps: list[str], samples: int):
    visuals = sample_visual_series(episode, side, timestamps, samples)
    vals = [v["gap"] for v in visuals.values() if v["confidence"] >= 0.2]
    if len(vals) < 2:
        return {"visual_samples": len(vals), "visual_range_px_norm": None, "visual_moving": None}
    return {
        "visual_samples": len(vals),
        "visual_range_px_norm": max(vals) - min(vals),
        "visual_moving": (max(vals) - min(vals)) >= 0.015,
    }


def report_side_value(row, side, key):
    return row.get(f"{side}_{key}", "")


def float_or_none(value):
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def assess(args):
    rows = load_report(args.report)
    out_rows = []
    processed = 0
    for row in rows:
        episode = Path(row["source_path"])
        for side in SIDES:
            correct = report_side_value(row, side, "correct") == "True"
            typ = report_side_value(row, side, "type")
            if correct or typ in {"", "DYNAMIC_OK", "STATIC_OK"}:
                continue
            if not args.include_missing_video_sides and not wrist_video_path(episode, side).exists():
                continue
            if args.limit and processed >= args.limit:
                break
            series = load_side_series(episode, side, want_gripper=True)
            raw_range = float_or_none(report_side_value(row, side, "raw_range_deg"))
            gripper_range = float_or_none(report_side_value(row, side, "gripper_range_m"))
            visual = {"visual_samples": 0, "visual_range_px_norm": None, "visual_moving": None}
            if series is not None:
                visual = visual_motion_metrics(episode, side, series.timestamps, args.samples_per_video)
                raw_unwrapped = unwrap_degrees(series.raw)
                raw_range = max(raw_unwrapped) - min(raw_unwrapped) if raw_unwrapped else raw_range
                if series.gripper is not None:
                    gripper_range = max(series.gripper) - min(series.gripper)

            raw_static = raw_range is None or raw_range <= args.raw_static_deg
            visual_range = visual.get("visual_range_px_norm")
            visual_moving = None if visual_range is None else visual_range >= args.visual_motion_range
            visual["visual_moving"] = visual_moving
            raw_invalid = raw_range is None or raw_range <= args.raw_invalid_deg
            if visual_moving is True and raw_invalid:
                recoverable = True
                category = "raw_invalid_visual_recoverable"
            elif visual_moving is False and raw_static:
                recoverable = True
                category = "static_visual_and_raw"
            else:
                recoverable = True
                category = "mapping_or_calibration_recoverable"
            out_rows.append({
                "source_path": str(episode),
                "episode": row.get("episode", episode.name),
                "side": side,
                "type": typ,
                "problem_type": report_side_value(row, side, "problem_type"),
                "reason": report_side_value(row, side, "reason"),
                "recoverable": recoverable,
                "category": category,
                "raw_range_deg": raw_range,
                "gripper_range_m": gripper_range,
                **visual,
            })
            processed += 1
    fields = [
        "source_path", "episode", "side", "type", "problem_type", "reason",
        "recoverable", "category", "raw_range_deg", "gripper_range_m",
        "visual_samples", "visual_range_px_norm", "visual_moving",
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv_rows(args.out / "gripper_recoverability.csv", out_rows, fields)
    write_json(args.out / "gripper_recoverability.json", {"rows": out_rows})
    print(f"assessed failed sides: {len(out_rows)}")
    print(f"wrote {args.out / 'gripper_recoverability.csv'}")


def fit(args):
    rows = load_report(args.report)
    visual_measurements = []
    raw_slopes = []
    visual_slopes = []
    visual_signed_slopes = []
    side_summaries = []
    side_count = 0
    for row in rows:
        episode = Path(row["source_path"])
        for side in SIDES:
            if report_side_value(row, side, "correct") != "True":
                continue
            typ = report_side_value(row, side, "type")
            if typ not in {"DYNAMIC_OK", "STATIC_OK"}:
                continue
            series = load_side_series(episode, side, want_gripper=True)
            if series is None or series.gripper is None:
                continue
            raw_unwrapped = unwrap_degrees(series.raw)
            raw_range = max(raw_unwrapped) - min(raw_unwrapped)
            gripper_range = max(series.gripper) - min(series.gripper)
            local_raw_model = None
            if (
                typ == "DYNAMIC_OK"
                and raw_range >= args.min_dynamic_raw_range_deg
                and gripper_range >= args.min_dynamic_gripper_range_m
            ):
                local_raw_model = fit_linear(raw_unwrapped, series.gripper)
                if local_raw_model and abs(local_raw_model["slope"]) > 1e-7:
                    raw_slopes.append(abs(local_raw_model["slope"]))
            visual = sample_visual_series(episode, side, series.timestamps, args.samples_per_side)
            by_ts = {
                ts: (raw_unwrapped[i], series.gripper[i])
                for i, ts in enumerate(series.timestamps)
            }
            local = 0
            local_visual_pairs = []
            for ts, info in visual.items():
                if info["confidence"] < args.min_visual_confidence or ts not in by_ts:
                    continue
                raw_val, grip_val = by_ts[ts]
                visual_measurements.append({
                    "raw_unwrapped": raw_val,
                    "visual_gap": info["gap"],
                    "gripper": grip_val,
                    "side": side,
                    "source_path": str(episode),
                })
                local_visual_pairs.append((info["gap"], grip_val))
                local += 1
            local_visual_model = None
            if len(local_visual_pairs) >= 8:
                visual_gaps = [p[0] for p in local_visual_pairs]
                visual_grippers = [p[1] for p in local_visual_pairs]
                visual_range = max(visual_gaps) - min(visual_gaps)
                if visual_range >= args.min_dynamic_visual_range and gripper_range >= args.min_dynamic_gripper_range_m:
                    local_visual_model = fit_linear(visual_gaps, visual_grippers)
                    local_visual_corr = pearson(visual_gaps, visual_grippers)
                    if local_visual_model and local_visual_corr is not None and abs(local_visual_corr) >= 0.25:
                        visual_signed_slopes.append(local_visual_model["slope"])
                        visual_slopes.append(abs(local_visual_model["slope"]))
            if local:
                side_count += 1
                side_summaries.append({
                    "source_path": str(episode),
                    "side": side,
                    "type": typ,
                    "visual_samples": local,
                    "raw_range_deg_unwrapped": raw_range,
                    "gripper_range_m": gripper_range,
                    "local_raw_slope_m_per_deg": None if local_raw_model is None else local_raw_model["slope"],
                    "local_raw_r2": None if local_raw_model is None else local_raw_model["r2"],
                    "local_visual_slope_m_per_gap_norm": None if local_visual_model is None else local_visual_model["slope"],
                    "local_visual_r2": None if local_visual_model is None else local_visual_model["r2"],
                })
            if args.max_sides and side_count >= args.max_sides:
                break
        if args.max_sides and side_count >= args.max_sides:
            break
    if side_count < args.min_correct_sides:
        raise SystemExit(f"Only collected {side_count} correct sides; need >= {args.min_correct_sides}")
    if len(visual_measurements) < 50:
        raise SystemExit(f"Only collected {len(visual_measurements)} visual measurements; not enough to fit")
    if len(raw_slopes) < max(10, args.min_correct_sides // 4):
        raise SystemExit(f"Only collected {len(raw_slopes)} dynamic raw slopes; not enough to fit")

    visual_model = fit_linear([m["visual_gap"] for m in visual_measurements], [m["gripper"] for m in visual_measurements])
    if visual_model is None:
        raise SystemExit("Could not fit visual model")

    visual_pred = predict_linear(visual_model, [m["visual_gap"] for m in visual_measurements])
    truth = np.asarray([m["gripper"] for m in visual_measurements])
    visual_abs_error = np.abs(visual_pred - truth)
    visual_median_error = float(np.median(visual_abs_error))
    visual_p90_error = float(np.percentile(visual_abs_error, 90))
    visual_absolute_usable = bool(visual_model["r2"] >= 0.20 and visual_p90_error <= 0.015)
    model = {
        "schema_version": 2,
        "created_from": str(args.report),
        "correct_sides": side_count,
        "visual_measurements": len(visual_measurements),
        "raw_delta_model": {
            "description": "Per-side unwrapped raw_rotation angle change to gripper width change. Recovery uses vision as an absolute anchor only when the visual fit is reliable; otherwise vision is a motion consistency check.",
            "m_per_deg_median_abs": percentile(raw_slopes, 50),
            "m_per_deg_p10_abs": percentile(raw_slopes, 10),
            "m_per_deg_p90_abs": percentile(raw_slopes, 90),
            "dynamic_side_count": len(raw_slopes),
            "raw_wrap_handling": "Angles are unwrapped before all fitting and recovery so 359->0 and 0->359 crossings are continuous.",
        },
        "visual_delta_model": {
            "description": "Per-side wrist-view gap change to gripper width change. Recovery uses this for visual-primary repair when the visual motion is reliable.",
            "m_per_gap_norm_median_abs": percentile(visual_slopes, 50),
            "m_per_gap_norm_p10_abs": percentile(visual_slopes, 10),
            "m_per_gap_norm_p90_abs": percentile(visual_slopes, 90),
            "preferred_sign": 1 if (median(visual_signed_slopes) if visual_signed_slopes else 1.0) >= 0 else -1,
            "dynamic_side_count": len(visual_slopes),
        },
        "visual_model": visual_model,
        "visual_absolute_usable": visual_absolute_usable,
        "visual_usage": (
            "absolute_width_anchor"
            if visual_absolute_usable
            else "motion_consistency_only"
        ),
        "visual_median_abs_error_m": visual_median_error,
        "visual_p90_abs_error_m": visual_p90_error,
        "gripper_min_m": float(np.percentile(truth, 0.5)),
        "gripper_max_m": float(np.percentile(truth, 99.5)),
        "side_summaries_sample": side_summaries[:200],
    }
    write_json(args.model_out, model)
    args.out.mkdir(parents=True, exist_ok=True)
    write_json(args.out / "fit_summary.json", model)
    print(f"fit visual_measurements={len(visual_measurements)} correct_sides={side_count}")
    print(f"raw dynamic slopes={len(raw_slopes)} median_abs={model['raw_delta_model']['m_per_deg_median_abs']:.9f} m/deg")
    print(f"visual mae={model['visual_median_abs_error_m']:.6f} p90={model['visual_p90_abs_error_m']:.6f}")
    print(f"wrote {args.model_out}")


def load_model(path: Path):
    return json.loads(path.read_text())


def direction_agreement(xs, ys):
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if len(x) < 3:
        return None
    dx = np.diff(x)
    dy = np.diff(y)
    x_eps = max(1e-9, float(np.percentile(np.abs(dx), 25)))
    y_eps = max(1e-9, float(np.percentile(np.abs(dy), 25)))
    keep = (np.abs(dx) >= x_eps) & (np.abs(dy) >= y_eps)
    if int(np.sum(keep)) < 2:
        return None
    return float(np.mean(np.sign(dx[keep]) == np.sign(dy[keep])))


def motion_consistency(raw_values, visual_values):
    best = {
        "score": 0.0,
        "sign": 1,
        "pearson": None,
        "spearman": None,
        "direction_agreement": None,
    }
    for sign in (-1, 1):
        oriented = [sign * float(v) for v in raw_values]
        corr = pearson(oriented, visual_values)
        rank_corr = spearman(oriented, visual_values)
        direction = direction_agreement(oriented, visual_values)
        score = max(
            0.0 if corr is None else max(0.0, corr),
            0.0 if rank_corr is None else max(0.0, rank_corr),
            0.0 if direction is None else direction,
        )
        if score > best["score"]:
            best = {
                "score": float(score),
                "sign": sign,
                "pearson": corr,
                "spearman": rank_corr,
                "direction_agreement": direction,
            }
    return best


def interpolate_prediction(all_timestamps: list[str], sample_timestamps: list[str], sample_values):
    all_ts = np.asarray([float(ts) for ts in all_timestamps], dtype=float)
    sample_ts = np.asarray([float(ts) for ts in sample_timestamps], dtype=float)
    sample_vals = np.asarray(sample_values, dtype=float)
    order = np.argsort(sample_ts)
    return np.interp(all_ts, sample_ts[order], sample_vals[order])


def visual_primary_prediction(all_timestamps, visual_pairs, model):
    visual_delta = model.get("visual_delta_model", {})
    visual_coeff = visual_delta.get("m_per_gap_norm_median_abs")
    if visual_coeff is None:
        return None
    sign = int(visual_delta.get("preferred_sign") or 1)
    sample_timestamps = [ts for _, ts, _, _, _ in visual_pairs]
    gaps = np.asarray([sign * p[2] for p in visual_pairs], dtype=float)
    sample_widths = model["gripper_min_m"] + (gaps - float(np.min(gaps))) * float(visual_coeff)
    sample_widths = np.clip(sample_widths, model["gripper_min_m"], model["gripper_max_m"])
    return interpolate_prediction(all_timestamps, sample_timestamps, sample_widths)


def recover_predictions(
    episode: Path,
    side: str,
    model: dict[str, Any],
    samples: int,
    agreement_threshold: float,
    raw_invalid_deg: float = 30.0,
    visual_motion_range: float = 0.03,
):
    series = load_side_series(episode, side, want_gripper=False)
    if series is None:
        return None, "missing raw series"
    visual = sample_visual_series(episode, side, series.timestamps, samples)
    raw_unwrapped = unwrap_degrees(series.raw)
    raw_range = max(raw_unwrapped) - min(raw_unwrapped) if raw_unwrapped else 0.0
    raw_delta_model = model.get("raw_delta_model", {})
    raw_m_per_deg = raw_delta_model.get("m_per_deg_median_abs")
    if raw_m_per_deg is None:
        return None, "model has no raw delta calibration"

    visual_pairs = []
    for i, ts in enumerate(series.timestamps):
        info = visual.get(ts)
        if not info or info["confidence"] < 0.25:
            continue
        pred_v = predict_linear(model["visual_model"], [info["gap"]])[0]
        pred_v = float(np.clip(pred_v, model["gripper_min_m"], model["gripper_max_m"]))
        visual_pairs.append((i, ts, info["gap"], pred_v, info["confidence"]))
    if len(visual_pairs) < 3:
        return None, f"not enough visual samples ({len(visual_pairs)})"

    visual_gaps = [p[2] for p in visual_pairs]
    visual_width_values = [p[3] for p in visual_pairs]
    visual_range = max(visual_gaps) - min(visual_gaps)
    raw_arr = np.asarray(raw_unwrapped, dtype=float)
    visual_absolute_usable = bool(model.get("visual_absolute_usable"))
    visual_reliable = (
        len(visual_pairs) >= 8
        and visual_range >= visual_motion_range
        and model.get("visual_delta_model", {}).get("m_per_gap_norm_median_abs") is not None
    )
    raw_invalid = raw_range <= raw_invalid_deg and visual_range >= visual_motion_range
    raw_valid = not raw_invalid

    # If the encoder is static and vision is also static, recover a constant
    # width from vision. If vision moves while raw is static, assessment should
    # have marked it unrecoverable; keep the guard here for direct calls.
    if raw_range <= 2.0:
        if visual_range >= visual_motion_range and visual_reliable:
            pred_all = visual_primary_prediction(series.timestamps, visual_pairs, model)
            if pred_all is None:
                return None, "visual model is unavailable"
            med_delta = None
            agree = True
            raw_sign = 0
            visual_consistency = None
            motion = {"score": None, "spearman": None, "direction_agreement": None}
            recovery_mode = "visual_primary_raw_invalid"
        else:
            constant_width = float(median(visual_width_values)) if visual_absolute_usable else float(model["gripper_min_m"])
            pred_all = np.full(len(raw_arr), constant_width, dtype=float)
            med_delta = 0.0
            agree = True
            raw_sign = 0
            visual_consistency = None
            motion = {"score": None, "spearman": None, "direction_agreement": None}
            recovery_mode = "static"
    else:
        sample_raw = [raw_arr[i] for i, _, _, _, _ in visual_pairs]
        motion = motion_consistency(sample_raw, visual_gaps)
        if raw_invalid and visual_reliable:
            pred_all = visual_primary_prediction(series.timestamps, visual_pairs, model)
            if pred_all is None:
                return None, "visual model is unavailable"
            med_delta = None
            sign = motion["sign"]
            visual_consistency = motion["pearson"]
            agree = True
            recovery_mode = "visual_primary_raw_invalid"
        elif visual_reliable and raw_range > raw_invalid_deg:
            sign = motion["sign"]
            med_delta = None
            visual_consistency = motion["pearson"]
            if motion["score"] >= 0.35:
                oriented_all = sign * raw_arr
                pred_all = model["gripper_min_m"] + (oriented_all - float(np.min(oriented_all))) * raw_m_per_deg
                agree = True
                recovery_mode = "raw_primary_high_raw_visual_correlation"
            else:
                pred_all = visual_primary_prediction(series.timestamps, visual_pairs, model)
                if pred_all is None:
                    return None, "visual model is unavailable"
                agree = True
                recovery_mode = "visual_primary_low_raw_visual_correlation"
        elif visual_absolute_usable:
            candidates = []
            for sign in (-1.0, 1.0):
                sample_terms = np.asarray([sign * raw_m_per_deg * raw_arr[i] for i, _, _, _, _ in visual_pairs], dtype=float)
                offset = float(median([v - t for t, (_, _, _, v, _) in zip(sample_terms, visual_pairs)]))
                sample_pred = offset + sample_terms
                sample_truth = np.asarray(visual_width_values, dtype=float)
                errors = np.abs(sample_pred - sample_truth)
                candidates.append((float(np.median(errors)), sign, offset))
            med_delta, sign, offset = min(candidates, key=lambda item: item[0])
            pred_all = offset + sign * raw_m_per_deg * raw_arr
            agree = med_delta <= agreement_threshold
            visual_consistency = None
            recovery_mode = "raw_primary_visual_absolute_anchor"
        else:
            sign = motion["sign"]
            oriented_all = sign * raw_arr
            pred_all = model["gripper_min_m"] + (oriented_all - float(np.min(oriented_all))) * raw_m_per_deg
            med_delta = None
            visual_consistency = motion["pearson"]
            # In this mode vision is used as an independent motion check, not
            # as an absolute metric measurement.
            agree = motion["score"] >= 0.35 or visual_range <= 0.015
            recovery_mode = "raw_primary_visual_gate" if agree else "quality_false_visual_unreliable"
        raw_sign = int(sign)

    pred_all = np.clip(pred_all, model["gripper_min_m"], model["gripper_max_m"])
    return {
        "timestamps": series.timestamps,
        "predicted": [float(v) for v in pred_all],
        "visual_samples": len(visual_pairs),
        "raw_range_deg_unwrapped": raw_range,
        "visual_gap_range_norm": visual_range,
        "raw_visual_median_abs_delta_m": med_delta,
        "visual_motion_correlation": visual_consistency,
        "visual_motion_spearman": motion["spearman"],
        "visual_direction_agreement": motion["direction_agreement"],
        "raw_visual_motion_score": motion["score"],
        "raw_valid": raw_valid,
        "visual_reliable": visual_reliable,
        "quality_ok": agree,
        "recovery_mode": recovery_mode,
        "agreement_ok": agree,
        "raw_direction_sign": raw_sign,
    }, ""


def backup_file(path: Path, backup_root: Path, episode: Path):
    rel = path.relative_to(episode)
    dst = backup_root / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)


def rewrite_gripper_file(
    path: Path,
    episode: Path,
    side: str,
    timestamps: list[str],
    predicted: list[float],
    suffix: str,
    single_name: str,
    backup_root: Path | None,
):
    rows = read_csv_rows(path)
    if not rows:
        return 0
    fields = list(rows[0].keys())
    col = side_col(fields, side, suffix, single_name)
    if col is None:
        return 0
    pred_by_ts = dict(zip(timestamps, predicted))
    changed = 0
    for row in rows:
        ts = row.get("timestamp_ms")
        if ts in pred_by_ts:
            row[col] = f"{pred_by_ts[ts]:.6f}"
            changed += 1
    if changed:
        if backup_root is not None:
            backup_file(path, backup_root, episode)
        write_csv_rows(path, rows, fields)
    return changed


def rewrite_gripper_csvs(episode: Path, side: str, timestamps: list[str], predicted: list[float], backup_root: Path | None):
    targets = [
        ("observation.state.gripper/data.csv", "gripper", "gripper"),
        ("observation.state.eef_pose/data.csv", "gripper", "gripper"),
        ("observation.state.eef_pose_raw/data.csv", "gripper", "gripper"),
        ("observation.state.eef_pose_smooth/data.csv", "gripper", "gripper"),
        ("actions.eef_pose/data.csv", "gripper.pos", "gripper.pos"),
        ("actions.eef_pose_raw/data.csv", "gripper.pos", "gripper.pos"),
        ("actions.eef_pose_smooth/data.csv", "gripper.pos", "gripper.pos"),
        ("episode.eef_pose_smooth/data.csv", "gripper.pos", "gripper.pos"),
    ]
    rewritten = {}
    for rel, suffix, single_name in targets:
        path = episode / rel
        if not path.exists():
            continue
        changed = rewrite_gripper_file(path, episode, side, timestamps, predicted, suffix, single_name, backup_root)
        if changed:
            rewritten[rel] = changed
    return rewritten


def copy_missing_gripper_original_files(src: Path, dst: Path) -> int:
    copied = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        target = dst / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def ensure_gripper_original(episode: Path, backup_root: Path | None = None) -> tuple[bool, str]:
    src = episode / "observation.state.gripper"
    dst = episode / "observation.state.gripper_original"
    if not src.exists():
        return False, "missing observation.state.gripper"
    make_writable(episode)
    if dst.exists():
        copied = copy_missing_gripper_original_files(src, dst)
        if copied:
            return True, f"copied_missing_files:{copied}"
        return False, "exists"
    if backup_root is not None:
        for path in src.rglob("*"):
            if path.is_file():
                backup_file(path, backup_root, episode)
    shutil.copytree(src, dst)
    return True, "copied"


def ensure_all_gripper_original(root: Path, refresh_checksums: bool = False) -> dict[str, Any]:
    episodes = find_episode_dirs(root)
    copied = []
    completed = []
    existing = 0
    missing_gripper = []
    errors = []
    for episode in episodes:
        if not (episode / "observation.state.gripper" / "data.csv").exists():
            missing_gripper.append(str(episode))
            continue
        try:
            backup = episode / ".gripper_recovery_backup"
            backup.mkdir(exist_ok=True)
            made, status = ensure_gripper_original(episode, backup)
            if made:
                if status.startswith("copied_missing_files:"):
                    completed.append(str(episode))
                else:
                    copied.append(str(episode))
                if refresh_checksums:
                    refresh_checksum_file(episode)
            elif status != "exists":
                errors.append({"source_path": str(episode), "error": status})
            else:
                existing += 1
        except Exception as exc:
            errors.append({"source_path": str(episode), "error": str(exc)})
    return {
        "episodes": len(episodes),
        "existing": existing,
        "copied": len(copied),
        "completed": len(completed),
        "missing_gripper": missing_gripper,
        "errors": errors,
        "copied_paths": copied,
        "completed_paths": completed,
    }


def refresh_checksum_file(episode: Path):
    checksum_path = episode / "checksums.sha256"
    manifest_path = episode / ".checksum_manifest"
    files = sorted(p for p in episode.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(episode).parts) and p.name != "checksums.sha256")
    manifest = {}
    for p in files:
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        manifest[str(p.relative_to(episode))] = h
    make_writable(episode)
    if checksum_path.exists():
        make_writable(checksum_path)
    with checksum_path.open("w", encoding="utf-8") as f:
        for rel, digest in manifest.items():
            f.write(f"{digest}  {rel}\n")
    if manifest_path.exists():
        make_writable(manifest_path)
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def recover(args):
    model = load_model(args.model)
    assessment = read_csv_rows(args.assessment)
    only = set(args.only_source)
    rows = []
    processed = 0
    for item in assessment:
        if str(item.get("recoverable")) not in {"True", "true", "1"}:
            continue
        source = item["source_path"]
        if only and source not in only:
            continue
        if args.limit and processed >= args.limit:
            break
        episode = Path(source)
        side = item["side"]
        if not args.include_missing_video_sides and not wrist_video_path(episode, side).exists():
            continue
        processed += 1
        try:
            pred, err = recover_predictions(
                episode,
                side,
                model,
                args.samples_per_side,
                args.agreement_threshold_m,
                args.raw_invalid_deg,
                args.visual_motion_range,
            )
        except Exception as exc:
            pred, err = None, f"prediction error: {exc}"
        row = {
            "source_path": source,
            "episode": item.get("episode", episode.name),
            "side": side,
            "category": item.get("category", ""),
            "recoverable": True,
            "original_copied": False,
            "original_copy_status": "",
            "agreement_ok": False,
            "visual_samples": 0,
            "raw_range_deg_unwrapped": "",
            "visual_gap_range_norm": "",
            "raw_visual_median_abs_delta_m": "",
            "visual_motion_correlation": "",
            "visual_motion_spearman": "",
            "visual_direction_agreement": "",
            "raw_visual_motion_score": "",
            "raw_valid": "",
            "visual_reliable": "",
            "quality_ok": False,
            "recovery_mode": "",
            "raw_direction_sign": "",
            "applied": False,
            "rows_rewritten": 0,
            "files_rewritten": "",
            "error": err,
        }
        backup = None
        touched_episode = False
        if args.apply:
            try:
                backup = episode / ".gripper_recovery_backup"
                make_writable(episode)
                backup.mkdir(exist_ok=True)
                copied, copy_status = ensure_gripper_original(episode, backup)
                row["original_copied"] = copied
                row["original_copy_status"] = copy_status
                touched_episode = copied
            except OSError as exc:
                row["original_copy_status"] = f"error: {exc}"
                row["error"] = f"{row['error']}; {exc}" if row["error"] else str(exc)
        if pred is not None:
            row["agreement_ok"] = pred["agreement_ok"]
            row["visual_samples"] = pred["visual_samples"]
            row["raw_range_deg_unwrapped"] = pred["raw_range_deg_unwrapped"]
            row["visual_gap_range_norm"] = pred["visual_gap_range_norm"]
            row["raw_visual_median_abs_delta_m"] = pred["raw_visual_median_abs_delta_m"]
            row["visual_motion_correlation"] = pred["visual_motion_correlation"]
            row["visual_motion_spearman"] = pred["visual_motion_spearman"]
            row["visual_direction_agreement"] = pred["visual_direction_agreement"]
            row["raw_visual_motion_score"] = pred["raw_visual_motion_score"]
            row["raw_valid"] = pred["raw_valid"]
            row["visual_reliable"] = pred["visual_reliable"]
            row["quality_ok"] = pred["quality_ok"]
            row["recovery_mode"] = pred["recovery_mode"]
            row["raw_direction_sign"] = pred["raw_direction_sign"]
            can_write_repair = args.apply and not str(row["original_copy_status"]).startswith("error:")
            if pred["agreement_ok"] and can_write_repair:
                try:
                    rewritten = rewrite_gripper_csvs(episode, side, pred["timestamps"], pred["predicted"], backup)
                    row["rows_rewritten"] = sum(rewritten.values())
                    row["files_rewritten"] = json.dumps(rewritten, sort_keys=True)
                    touched_episode = touched_episode or bool(rewritten)
                    row["applied"] = bool(rewritten)
                except Exception as exc:
                    row["error"] = f"{row['error']}; {exc}" if row["error"] else str(exc)
        if args.apply and touched_episode:
            try:
                refresh_checksum_file(episode)
            except Exception as exc:
                row["error"] = f"{row['error']}; checksum refresh: {exc}" if row["error"] else f"checksum refresh: {exc}"
        rows.append(row)

    args.out.mkdir(parents=True, exist_ok=True)
    fields = [
        "source_path", "episode", "side", "category", "recoverable",
        "original_copied", "original_copy_status",
        "agreement_ok", "visual_samples", "raw_range_deg_unwrapped",
        "visual_gap_range_norm", "raw_visual_median_abs_delta_m",
        "visual_motion_correlation", "visual_motion_spearman",
        "visual_direction_agreement", "raw_visual_motion_score",
        "raw_valid", "visual_reliable", "quality_ok", "recovery_mode",
        "raw_direction_sign",
        "applied", "rows_rewritten", "files_rewritten", "error",
    ]
    write_csv_rows(args.out / ("recovery_apply.csv" if args.apply else "recovery_dry_run.csv"), rows, fields)
    write_json(args.out / ("recovery_apply.json" if args.apply else "recovery_dry_run.json"), {"rows": rows})
    print(f"recover candidates: {len(rows)} apply={args.apply}")
    print(f"agreement ok: {sum(1 for r in rows if r['agreement_ok'])}")


def is_episode_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("episode_")


def find_episode_dirs(root: Path) -> list[Path]:
    root = root.expanduser().resolve()
    if is_episode_dir(root):
        return [root]
    return sorted(path for path in root.rglob("episode_*") if is_episode_dir(path))


def load_validate_gripper():
    tools_data_root = Path(__file__).resolve().parents[1]
    if str(tools_data_root) not in sys.path:
        sys.path.insert(0, str(tools_data_root))
    from assessment.validate_raw_data import validate_gripper

    return validate_gripper


def validation_args(args):
    return argparse.Namespace(
        raw_static_deg=args.raw_static_deg,
        gripper_static_m=args.gripper_static_m,
        min_correlation=args.min_correlation,
        min_r2=args.min_r2,
        min_gripper_m=args.min_gripper_m,
        max_gripper_m=args.max_gripper_m,
        recompute_tolerance_m=args.recompute_tolerance_m,
        disable_metadata_check=args.disable_metadata_check,
    )


def json_safe(value: Any):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def update_metadata_gripper_quality(episode: Path, quality_ok: bool, sides: dict[str, Any], apply: bool):
    if not apply:
        return False, "dry_run"
    metadata_path = episode / "metadata.json"
    if not metadata_path.exists():
        return False, "missing metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"invalid metadata.json: {exc}"
    quality = metadata.setdefault("quality", {})
    quality["gripper_quality"] = bool(quality_ok)
    quality["gripper_recovery"] = {
        "quality": bool(quality_ok),
        "status": "ok" if quality_ok else "false",
        "sides": json_safe(sides),
    }
    make_writable(metadata_path)
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, "updated"


def process_root(args):
    model = load_model(args.model)
    validate_gripper = load_validate_gripper()
    v_args = validation_args(args)
    episodes = find_episode_dirs(args.data_root)
    if args.limit:
        episodes = episodes[: args.limit]
    original_summary = None
    if args.apply:
        original_summary = ensure_all_gripper_original(args.data_root, refresh_checksums=True)

    rows = []
    processed_bad_sides = 0
    repaired_sides = 0
    unrecovered_sides = 0
    already_good_sides = 0

    for episode in episodes:
        episode_row_start = len(rows)
        try:
            before = validate_gripper(episode, v_args)
        except Exception as exc:
            before = {"correct": False, "error": str(exc), "sides": {}}

        episode_side_details: dict[str, Any] = {}
        episode_touched = False
        backup = None
        for side in SIDES:
            side_result = (before.get("sides") or {}).get(side) or {
                "type": "MISSING_DATA",
                "correct": False,
                "reason": before.get("error", "missing side result"),
            }
            typ = side_result.get("type", "")
            base_row = {
                "source_path": str(episode),
                "episode": episode.name,
                "side": side,
                "initial_type": typ,
                "initial_correct": bool(side_result.get("correct")),
                "initial_reason": side_result.get("reason", ""),
                "needs_recovery": not bool(side_result.get("correct")),
                "agreement_ok": False,
                "quality_ok": bool(side_result.get("correct")),
                "recovery_mode": "",
                "applied": False,
                "rows_rewritten": 0,
                "files_rewritten": "",
                "metadata_status": "",
                "error": "",
            }
            if side_result.get("correct") or typ in GOOD_GRIPPER_TYPES:
                already_good_sides += 1
                rows.append(base_row)
                episode_side_details[side] = {**base_row, "validation": side_result}
                continue

            processed_bad_sides += 1
            if not args.include_missing_video_sides and not wrist_video_path(episode, side).exists():
                base_row["error"] = "missing wrist-view video"
                base_row["quality_ok"] = False
                rows.append(base_row)
                episode_side_details[side] = {**base_row, "validation": side_result}
                unrecovered_sides += 1
                continue

            try:
                pred, err = recover_predictions(
                    episode,
                    side,
                    model,
                    args.samples_per_side,
                    args.agreement_threshold_m,
                    args.raw_invalid_deg,
                    args.visual_motion_range,
                )
            except Exception as exc:
                pred, err = None, f"prediction error: {exc}"
            if pred is None:
                base_row["error"] = err
                base_row["quality_ok"] = False
                rows.append(base_row)
                episode_side_details[side] = {**base_row, "validation": side_result}
                unrecovered_sides += 1
                continue

            base_row.update({
                "agreement_ok": bool(pred["agreement_ok"]),
                "quality_ok": bool(pred["quality_ok"]),
                "recovery_mode": pred["recovery_mode"],
                "visual_samples": pred["visual_samples"],
                "raw_range_deg_unwrapped": pred["raw_range_deg_unwrapped"],
                "visual_gap_range_norm": pred["visual_gap_range_norm"],
                "raw_visual_motion_score": pred["raw_visual_motion_score"],
                "error": "" if pred["agreement_ok"] else "recovery agreement failed",
            })
            if pred["agreement_ok"] and args.apply:
                try:
                    if backup is None:
                        backup = episode / ".gripper_recovery_backup"
                        make_writable(episode)
                        backup.mkdir(exist_ok=True)
                        ensure_gripper_original(episode, backup)
                    rewritten = rewrite_gripper_csvs(episode, side, pred["timestamps"], pred["predicted"], backup)
                    base_row["rows_rewritten"] = sum(rewritten.values())
                    base_row["files_rewritten"] = json.dumps(rewritten, sort_keys=True)
                    base_row["applied"] = bool(rewritten)
                    episode_touched = episode_touched or bool(rewritten)
                except Exception as exc:
                    base_row["quality_ok"] = False
                    base_row["error"] = f"{base_row['error']}; {exc}" if base_row["error"] else str(exc)

            if base_row["quality_ok"]:
                repaired_sides += 1
            else:
                unrecovered_sides += 1
            rows.append(base_row)
            episode_side_details[side] = {**base_row, "validation": side_result}

        episode_quality_ok = all(detail.get("quality_ok") for detail in episode_side_details.values()) if episode_side_details else False
        _metadata_updated, metadata_status = update_metadata_gripper_quality(
            episode,
            episode_quality_ok,
            episode_side_details,
            args.apply,
        )
        for row in rows[episode_row_start:]:
            row["metadata_status"] = metadata_status
        if args.apply and episode_touched:
            try:
                refresh_checksum_file(episode)
            except Exception as exc:
                rows.append({
                    "source_path": str(episode),
                    "episode": episode.name,
                    "side": "",
                    "initial_type": "",
                    "initial_correct": "",
                    "initial_reason": "",
                    "needs_recovery": "",
                    "agreement_ok": "",
                    "quality_ok": False,
                    "recovery_mode": "",
                    "applied": False,
                    "rows_rewritten": 0,
                    "files_rewritten": "",
                    "metadata_status": metadata_status,
                    "error": f"checksum refresh: {exc}",
                })

    fields = [
        "source_path", "episode", "side", "initial_type", "initial_correct",
        "initial_reason", "needs_recovery", "agreement_ok", "quality_ok",
        "recovery_mode", "visual_samples", "raw_range_deg_unwrapped",
        "visual_gap_range_norm", "raw_visual_motion_score", "applied",
        "rows_rewritten", "files_rewritten", "metadata_status", "error",
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv_rows(args.out / ("process_apply.csv" if args.apply else "process_dry_run.csv"), rows, fields)
    write_json(
        args.out / ("process_apply.json" if args.apply else "process_dry_run.json"),
        {
            "data_root": str(args.data_root),
            "apply": bool(args.apply),
            "episodes": len(episodes),
            "already_good_sides": already_good_sides,
            "bad_sides": processed_bad_sides,
            "repaired_sides": repaired_sides,
            "unrecovered_sides": unrecovered_sides,
            "gripper_original": original_summary,
            "rows": rows,
        },
    )
    print(f"episodes scanned: {len(episodes)}")
    print(f"already good sides: {already_good_sides}")
    print(f"bad sides: {processed_bad_sides}")
    print(f"repaired sides: {repaired_sides}")
    print(f"unrecovered sides: {unrecovered_sides}")
    if original_summary is not None:
        print(f"gripper_original existing: {original_summary['existing']}")
        print(f"gripper_original copied: {original_summary['copied']}")
        print(f"gripper_original errors: {len(original_summary['errors'])}")
    print(f"wrote {args.out}")


def ensure_originals_command(args):
    summary = ensure_all_gripper_original(args.data_root, refresh_checksums=args.refresh_checksums)
    args.out.mkdir(parents=True, exist_ok=True)
    write_json(args.out / "ensure_originals.json", summary)
    rows = [{"source_path": path, "status": "copied"} for path in summary["copied_paths"]]
    rows.extend({"source_path": path, "status": "completed"} for path in summary["completed_paths"])
    rows.extend({"source_path": path, "status": "missing_gripper"} for path in summary["missing_gripper"])
    rows.extend({"source_path": item["source_path"], "status": f"error: {item['error']}"} for item in summary["errors"])
    write_csv_rows(args.out / "ensure_originals.csv", rows, ["source_path", "status"])
    print(f"episodes scanned: {summary['episodes']}")
    print(f"gripper_original existing: {summary['existing']}")
    print(f"gripper_original copied: {summary['copied']}")
    print(f"gripper_original completed: {summary['completed']}")
    print(f"missing gripper: {len(summary['missing_gripper'])}")
    print(f"errors: {len(summary['errors'])}")
    print(f"wrote {args.out}")


def main():
    args = parse_args()
    if args.cmd == "assess":
        assess(args)
    elif args.cmd == "fit":
        fit(args)
    elif args.cmd == "recover":
        recover(args)
    elif args.cmd == "process-root":
        process_root(args)
    elif args.cmd == "ensure-originals":
        ensure_originals_command(args)
    else:
        raise SystemExit(f"unknown command {args.cmd}")


if __name__ == "__main__":
    main()
