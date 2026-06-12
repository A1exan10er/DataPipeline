#!/usr/bin/env python3
"""Trajectory smoothing assessment for ``actions.eef_pose`` data.

This is the *preprocess* counterpart to the ``assessment`` quality validator.
Where ``assessment`` only decides whether an episode's data is *valid*, this
tool decides whether (and how) the end-effector trajectory needs **smoothing**,
by detecting position *jumps* (突变) in ``actions.eef_pose/data.csv`` and
classifying every frame -- separately for the left and right device -- as one
of three states:

* ``smooth``        (平滑段)         -- normal motion.
* ``recoverable``   (可平滑突变段)   -- a fast jump (> ``jump_displacement_m`` within
                                       ``window_s``) that returns to near the
                                       pre-jump position within ``recover_window_s``.
* ``unrecoverable`` (不可平滑突变段) -- a fast jump that does *not* return within
                                       ``recover_window_s``; it lasts until the
                                       windowed speed drops back below threshold.

Detection model (per device, X/Y/Z only)
-----------------------------------------
* Windowed displacement ``rdisp[i] = ||P[i] - P[k]||`` where ``k`` is the
  earliest frame still within the trailing ``window_s`` (default 0.5 s). A frame
  is *fast* when ``rdisp[i] > jump_displacement_m`` (default 0.35 m).
* Maximal contiguous runs of fast frames are *mutation segments* (runs separated
  by a gap shorter than ``merge_gap_s`` are merged so a single out-and-back spike
  is not split at its apex).
* For each mutation segment ``[a, b]`` the *anchor* is the last smooth position
  before it (``P[a-1]``, or ``P[a]`` when the run starts at frame 0). The segment
  is **recoverable** iff, after departing to its peak, the position returns to
  within ``return_tolerance_m`` of the anchor within ``recover_window_s`` of the
  onset; otherwise it is **unrecoverable**.

Whole-trajectory label (combines left & right; "either device" escalates)
-------------------------------------------------------------------------
A boundary unrecoverable segment is one that lies in the first or last
``boundary_window_s`` (default 3 s) of the trajectory. "首尾".

1. 平滑轨迹           ``smooth``              -- both devices fully smooth.
2. 可恢复轨迹         ``recoverable``         -- no unrecoverable anywhere; >=1 recoverable.
3. 中部平滑轨迹       ``middle_smooth``       -- unrecoverable only at head/tail, each < 3 s,
                                                and the middle is fully smooth.
4. 中部可恢复轨迹     ``middle_recoverable``  -- unrecoverable only at head/tail, each < 3 s,
                                                and the middle contains recoverable (+ smooth).
5. 不可恢复轨迹       ``unrecoverable``       -- some device has an unrecoverable segment in the
                                                middle, or a head/tail one >= 3 s.

Path handling and output layout mirror ``assessment/validate_raw_data.py``
exactly (single ``episode_XXX`` dir / class dir / recursive discovery, with the
input layout reproduced under ``--output-root``). Per episode a
``<episode>.smoothing.json`` report is written, plus a ``summary.smoothing.json``
per group.
"""

import argparse
import json
import math
import sys
from pathlib import Path

# Reuse the assessment validator's path-discovery and IO helpers so the input
# forms and the mirrored output layout stay identical between the two tools.
_ASSESS_DIR = Path(__file__).resolve().parent.parent / "assessment"
if str(_ASSESS_DIR) not in sys.path:
    sys.path.insert(0, str(_ASSESS_DIR))
import validate_raw_data as vrd  # noqa: E402  (path set up above)

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional progress bar
    tqdm = None


SCHEMA_VERSION = 1
REPORT_SUFFIX = ".smoothing.json"
SUMMARY_NAME = "summary.smoothing.json"

DEFAULT_CONFIG = {
    "jump_displacement_m": 0.35,
    "window_s": 0.5,
    "recover_window_s": 1.0,
    "return_tolerance_m": 0.35,
    "merge_gap_s": 0.5,
    "boundary_window_s": 3.0,
    "boundary_max_unrecoverable_s": 3.0,
}

# Per-frame state names.
SMOOTH = "smooth"
RECOVERABLE = "recoverable"
UNRECOVERABLE = "unrecoverable"

# Whole-trajectory labels, ordered by severity (index == severity).
LABELS = [
    ("smooth", "平滑轨迹"),
    ("recoverable", "可恢复轨迹"),
    ("middle_smooth", "中部平滑轨迹"),
    ("middle_recoverable", "中部可恢复轨迹"),
    ("unrecoverable", "不可恢复轨迹"),
]
LABEL_INDEX = {code: i for i, (code, _zh) in enumerate(LABELS)}
# Trajectories that can be salvaged by smoothing (everything except cat 5).
SMOOTHABLE_LABELS = {"recoverable", "middle_smooth", "middle_recoverable"}


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------


def load_config(path=None):
    """Load thresholds, falling back to :data:`DEFAULT_CONFIG` for any missing
    key. ``path`` defaults to ``smooth_assessment_config.json`` beside this file.
    """
    config = dict(DEFAULT_CONFIG)
    config_path = Path(path) if path else Path(__file__).resolve().parent / "smooth_assessment_config.json"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            user = json.load(f)
        for key in DEFAULT_CONFIG:
            if key in user and user[key] is not None:
                config[key] = float(user[key])
    return config


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Assess and report where an eef_pose trajectory needs smoothing.",
    )
    parser.add_argument("input_path_arg", nargs="?", type=Path,
                        help="Dataset class directory or a single episode_XXX directory.")
    parser.add_argument("-i", "--input-path", type=Path, help="Class or episode directory.")
    parser.add_argument("-o", "--output-root", type=Path, default=Path("outputs"),
                        help="Report output root (default: outputs). Mirrors the input layout.")
    parser.add_argument("--no-reports", action="store_true",
                        help="Only print the console summary; do not write JSON reports.")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="Additionally write the combined summary to this path.")
    parser.add_argument("--config", dest="config_path", type=Path,
                        help="Threshold config file (default: smooth_assessment_config.json).")
    parser.add_argument("--fps", type=float,
                        help="Override FPS. Defaults to metadata fps_config or 30.")

    overrides = parser.add_argument_group("threshold overrides")
    overrides.add_argument("--jump-displacement-m", type=float,
                           help="Jump threshold: displacement over window_s (default 0.35 m).")
    overrides.add_argument("--window-s", type=float, help="Jump detection window (default 0.5 s).")
    overrides.add_argument("--recover-window-s", type=float,
                           help="Time within which a jump must return to be recoverable (default 1.0 s).")
    overrides.add_argument("--return-tolerance-m", type=float,
                           help="Distance to the pre-jump anchor counted as 'returned' (default 0.35 m).")
    overrides.add_argument("--merge-gap-s", type=float,
                           help="Merge fast runs separated by a shorter gap (default 0.5 s).")
    overrides.add_argument("--boundary-window-s", type=float,
                           help="Head/tail span for 首尾 boundary segments (default 3.0 s).")
    overrides.add_argument("--boundary-max-unrecoverable-s", type=float,
                           help="A boundary unrecoverable segment must be shorter than this (default 3.0 s).")
    return parser.parse_args(argv)


def apply_overrides(config, args):
    mapping = {
        "jump_displacement_m": args.jump_displacement_m,
        "window_s": args.window_s,
        "recover_window_s": args.recover_window_s,
        "return_tolerance_m": args.return_tolerance_m,
        "merge_gap_s": args.merge_gap_s,
        "boundary_window_s": args.boundary_window_s,
        "boundary_max_unrecoverable_s": args.boundary_max_unrecoverable_s,
    }
    for key, value in mapping.items():
        if value is not None:
            config[key] = float(value)
    return config


# ---------------------------------------------------------------------------
# Trajectory loading
# ---------------------------------------------------------------------------


def _dist(p, q):
    return math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)


def load_trajectory(episode_dir):
    """Return ``(times_s, {'left': [...], 'right': [...]})`` or raise ``ValueError``.

    ``times_s`` is the per-frame timestamp in seconds (from ``timestamp_ms``),
    and each device maps to a list of ``(x, y, z)`` tuples. Rows with any
    non-finite / missing x,y,z on either device are dropped (and reported via the
    returned ``dropped`` count through the second element's ``_meta``).
    """
    data_csv = episode_dir / vrd.ACTION_DIR / "data.csv"
    if not data_csv.exists():
        raise ValueError(f"missing {vrd.ACTION_DIR}/data.csv")
    rows = vrd.read_csv_rows(data_csv)
    if not rows:
        raise ValueError("action data.csv is empty")

    needed = ["timestamp_ms",
              "left_x", "left_y", "left_z",
              "right_x", "right_y", "right_z"]
    missing_cols = [c for c in needed if c not in rows[0]]
    if missing_cols:
        raise ValueError(f"missing columns: {', '.join(missing_cols)}")

    times, left, right = [], [], []
    dropped = 0
    for index, row in enumerate(rows):
        vals = {c: vrd._parse_float(row.get(c)) for c in needed}
        if any(v is None or v != v or v in (float("inf"), float("-inf")) for v in vals.values()):
            dropped += 1
            continue
        times.append(vals["timestamp_ms"] / 1000.0)
        left.append((vals["left_x"], vals["left_y"], vals["left_z"]))
        right.append((vals["right_x"], vals["right_y"], vals["right_z"]))

    if len(times) < 2:
        raise ValueError("fewer than 2 finite frames in action data.csv")

    meta = {"total_rows": len(rows), "used_frames": len(times), "dropped_frames": dropped}
    return times, {"left": left, "right": right, "_meta": meta}


# ---------------------------------------------------------------------------
# Per-device jump detection and segmentation
# ---------------------------------------------------------------------------


def _windowed_displacement(times, positions, window_s):
    """``rdisp[i]`` = distance from ``P[i]`` to the earliest frame within the
    trailing ``window_s``. Early frames use the partial window from frame 0."""
    n = len(positions)
    rdisp = [0.0] * n
    k = 0
    for i in range(n):
        while times[i] - times[k] > window_s:
            k += 1
        rdisp[i] = _dist(positions[i], positions[k])
    return rdisp


def _fast_runs(times, fast, merge_gap_s):
    """Maximal contiguous runs of fast frames, merging runs separated by a gap
    of non-fast frames shorter than ``merge_gap_s``. Returns ``[(a, b), ...]``
    inclusive frame indices."""
    n = len(fast)
    runs = []
    i = 0
    while i < n:
        if not fast[i]:
            i += 1
            continue
        a = i
        while i + 1 < n and fast[i + 1]:
            i += 1
        runs.append([a, i])
        i += 1
    if not runs:
        return runs
    merged = [runs[0]]
    for a, b in runs[1:]:
        prev = merged[-1]
        gap = times[a] - times[prev[1]]
        if gap < merge_gap_s:
            prev[1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _classify_run(times, positions, run, config):
    """Classify a mutation segment as recoverable or unrecoverable.

    Recoverable: after departing to its peak distance from the anchor (the last
    smooth position before the run), the position returns within
    ``return_tolerance_m`` of that anchor within ``recover_window_s`` of onset.
    """
    a, b = run
    n = len(positions)
    anchor = positions[a - 1] if a > 0 else positions[a]
    onset_t = times[a]
    recover_window_s = config["recover_window_s"]
    return_tol = config["return_tolerance_m"]

    # Peak departure within the run.
    peak_idx, peak_d = a, _dist(positions[a], anchor)
    for j in range(a, b + 1):
        d = _dist(positions[j], anchor)
        if d > peak_d:
            peak_idx, peak_d = j, d

    # Did it come back near the anchor after the peak, within the recover window?
    recovered = False
    return_frame = None
    for j in range(peak_idx + 1, n):
        if times[j] - onset_t > recover_window_s:
            break
        if _dist(positions[j], anchor) <= return_tol:
            recovered = True
            return_frame = j
            break

    return {
        "start_frame": a,
        "end_frame": b,
        "start_time_s": round(times[a], 4),
        "end_time_s": round(times[b], 4),
        "duration_s": round(times[b] - times[a], 4),
        "peak_frame": peak_idx,
        "peak_displacement_m": round(peak_d, 4),
        "anchor_xyz": [round(c, 5) for c in anchor],
        "returned": recovered,
        "return_frame": return_frame,
        "state": RECOVERABLE if recovered else UNRECOVERABLE,
    }


def _segments_from_labels(times, labels):
    """Run-length encode per-frame state labels into contiguous segments."""
    segments = []
    n = len(labels)
    i = 0
    while i < n:
        state = labels[i]
        a = i
        while i + 1 < n and labels[i + 1] == state:
            i += 1
        segments.append({
            "state": state,
            "start_frame": a,
            "end_frame": i,
            "start_time_s": round(times[a], 4),
            "end_time_s": round(times[i], 4),
            "duration_s": round(times[i] - times[a], 4),
        })
        i += 1
    return segments


def analyze_device(times, positions, config):
    """Run jump detection on one device's trajectory. Returns per-frame states,
    run-length-encoded segments (the 分点), and the list of mutation events."""
    n = len(positions)
    rdisp = _windowed_displacement(times, positions, config["window_s"])
    threshold = config["jump_displacement_m"]
    fast = [d > threshold for d in rdisp]
    runs = _fast_runs(times, fast, config["merge_gap_s"])

    labels = [SMOOTH] * n
    events = []
    for run in runs:
        info = _classify_run(times, positions, run, config)
        events.append(info)
        for j in range(run[0], run[1] + 1):
            labels[j] = info["state"]

    segments = _segments_from_labels(times, labels)
    counts = {SMOOTH: 0, RECOVERABLE: 0, UNRECOVERABLE: 0}
    for s in labels:
        counts[s] += 1
    return {
        "frame_count": n,
        "max_windowed_displacement_m": round(max(rdisp), 4) if rdisp else 0.0,
        "frame_state_counts": counts,
        "events": events,
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# Whole-trajectory classification
# ---------------------------------------------------------------------------


def _segment_boundary_flags(seg, duration, config):
    """Whether a segment touches the head / tail ``boundary_window_s`` region."""
    bw = config["boundary_window_s"]
    in_head = seg["start_time_s"] <= bw
    in_tail = seg["end_time_s"] >= duration - bw
    return in_head, in_tail


def classify_trajectory(times, device_analyses, config):
    """Combine the per-device segmentations into one of the five labels.

    ``device_analyses`` is a dict ``{"left": analysis, "right": analysis}``.
    "Either device" conditions escalate the whole-trajectory label.
    """
    duration = times[-1] - times[0]
    max_boundary_unrec_s = config["boundary_max_unrecoverable_s"]

    any_mutation = False
    any_recoverable = False
    any_unrecoverable = False
    any_disqualifying_unrec = False   # middle, or boundary but >= 3 s -> cat 5
    any_recoverable_in_middle = False
    per_device_notes = {}

    for side, analysis in device_analyses.items():
        notes = {"boundary_unrecoverable": [], "middle_unrecoverable": [],
                 "oversize_boundary_unrecoverable": [], "recoverable_in_middle": False}
        for seg in analysis["segments"]:
            if seg["state"] == SMOOTH:
                continue
            any_mutation = True
            in_head, in_tail = _segment_boundary_flags(seg, duration, config)
            is_boundary = in_head or in_tail
            if seg["state"] == RECOVERABLE:
                any_recoverable = True
                if not is_boundary:
                    any_recoverable_in_middle = True
                    notes["recoverable_in_middle"] = True
            elif seg["state"] == UNRECOVERABLE:
                any_unrecoverable = True
                tag = {"start_frame": seg["start_frame"], "end_frame": seg["end_frame"],
                       "duration_s": seg["duration_s"], "in_head": in_head, "in_tail": in_tail}
                if not is_boundary:
                    any_disqualifying_unrec = True
                    notes["middle_unrecoverable"].append(tag)
                elif seg["duration_s"] >= max_boundary_unrec_s:
                    any_disqualifying_unrec = True
                    notes["oversize_boundary_unrecoverable"].append(tag)
                else:
                    notes["boundary_unrecoverable"].append(tag)
        per_device_notes[side] = notes

    # Decide the label by severity.
    if not any_mutation:
        code, reason = "smooth", "左右设备均为平滑段，无突变。"
    elif any_disqualifying_unrec:
        code = "unrecoverable"
        reason = "存在不可恢复段位于中部，或首尾不可恢复段不短于 {:.0f}s。".format(max_boundary_unrec_s)
    elif not any_unrecoverable:
        code = "recoverable"
        reason = "无不可恢复段；存在可平滑突变段，可整体平滑。"
    elif any_recoverable_in_middle:
        code = "middle_recoverable"
        reason = "不可恢复段仅位于首尾且均 < {:.0f}s，中部存在可平滑突变段。".format(max_boundary_unrec_s)
    else:
        code = "middle_smooth"
        reason = "不可恢复段仅位于首尾且均 < {:.0f}s，中部全部为平滑段。".format(max_boundary_unrec_s)

    name_zh = LABELS[LABEL_INDEX[code]][1]
    return {
        "code": code,
        "name_zh": name_zh,
        "severity": LABEL_INDEX[code],
        "smoothable": code in SMOOTHABLE_LABELS,
        "reason": reason,
        "duration_s": round(duration, 4),
        "boundary_window_s": config["boundary_window_s"],
        "per_device": per_device_notes,
    }


# ---------------------------------------------------------------------------
# Per-episode assessment + report payload
# ---------------------------------------------------------------------------


def assess_episode(episode_dir, config, fps_override=None):
    fps = fps_override if fps_override is not None else vrd.load_episode_fps(episode_dir)
    result = {"episode": episode_dir.name, "path": str(episode_dir), "fps": fps}
    try:
        times, traj = load_trajectory(episode_dir)
    except ValueError as exc:
        result.update({"ok": False, "error": str(exc), "trajectory_label": None})
        return result

    device_analyses = {
        "left": analyze_device(times, traj["left"], config),
        "right": analyze_device(times, traj["right"], config),
    }
    label = classify_trajectory(times, device_analyses, config)
    result.update({
        "ok": True,
        "frame_count": traj["_meta"]["used_frames"],
        "dropped_frames": traj["_meta"]["dropped_frames"],
        "duration_s": round(times[-1] - times[0], 4),
        "trajectory_label": label,
        "devices": device_analyses,
    })
    return result


def episode_report_payload(class_name, result, config, generated_at):
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "smoothing_assessment",
        "generated_at": generated_at,
        "class_name": class_name,
        "episode": result["episode"],
        "path": result["path"],
        "fps": result.get("fps"),
        "config": config,
        "ok": result["ok"],
        "error": result.get("error"),
        "frame_count": result.get("frame_count"),
        "dropped_frames": result.get("dropped_frames"),
        "duration_s": result.get("duration_s"),
        "trajectory_label": result.get("trajectory_label"),
        "devices": result.get("devices"),
    }


def summary_payload(class_name, input_path, config, results, report_paths, generated_at):
    by_label = {code: 0 for code, _zh in LABELS}
    errors = 0
    episodes = []
    for result, report_path in zip(results, report_paths):
        label = result.get("trajectory_label")
        if result["ok"] and label is not None:
            by_label[label["code"]] += 1
            label_code = label["code"]
            smoothable = label["smoothable"]
        else:
            errors += 1
            label_code = None
            smoothable = None
        episodes.append({
            "episode": result["episode"],
            "ok": result["ok"],
            "label": label_code,
            "smoothable": smoothable,
            "error": result.get("error"),
            "report": str(report_path) if report_path is not None else None,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "smoothing_assessment_summary",
        "generated_at": generated_at,
        "class_name": class_name,
        "source_dir": str(input_path),
        "config": config,
        "processed": len(results),
        "errors": errors,
        "label_counts": by_label,
        "episodes": episodes,
    }


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def print_episode(result):
    ep = result["episode"]
    if not result["ok"]:
        print(f"  [ERROR] {ep}: {result['error']}")
        return
    label = result["trajectory_label"]
    flag = "smoothable" if label["smoothable"] else ("clean" if label["code"] == "smooth" else "UNRECOVERABLE")
    counts = {side: result["devices"][side]["frame_state_counts"] for side in ("left", "right")}

    def fmt(side):
        c = counts[side]
        return f"{side}: smooth={c[SMOOTH]} rec={c[RECOVERABLE]} unrec={c[UNRECOVERABLE]}"

    print(f"  {ep}: {label['code']} ({label['name_zh']}) [{flag}] "
          f"| {label['duration_s']}s | {fmt('left')} | {fmt('right')}")
    print(f"        {label['reason']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None):
    args = parse_args(argv)
    input_path = args.input_path or args.input_path_arg
    if input_path is None:
        raise ValueError(
            "Input path is required. Expected an episode_XXX directory "
            "(e.g. .../class_name/episode_0001) or a class directory containing "
            "episode_XXX subdirectories.")

    config = apply_overrides(load_config(args.config_path), args)
    episode_dirs = vrd.find_episode_dirs(input_path)
    generated_at = vrd.now_utc_iso()

    use_bar = tqdm is not None and len(episode_dirs) > 1
    iterator = (tqdm(episode_dirs, unit="episode", desc=f"Smoothing {input_path.name}")
                if use_bar else episode_dirs)

    results = []
    groups = {}
    for path in iterator:
        if use_bar:
            iterator.set_postfix_str(path.name, refresh=False)
        rel_subdir, class_name = vrd.episode_output_layout(input_path, path)
        result = assess_episode(path, config, args.fps)
        results.append(result)

        out_dir = ((args.output_root / rel_subdir).resolve() if not args.no_reports else None)
        report_path = None
        if out_dir is not None:
            report_path = out_dir / f"{result['episode']}{REPORT_SUFFIX}"
            vrd.write_json_report(report_path, episode_report_payload(class_name, result, config, generated_at))

        group = groups.setdefault(str(rel_subdir), {
            "class_name": class_name, "source_dir": path.parent, "out_dir": out_dir,
            "results": [], "report_paths": [],
        })
        group["results"].append(result)
        group["report_paths"].append(report_path)

    summaries = []
    summary_paths = []
    for group in groups.values():
        summary = summary_payload(group["class_name"], group["source_dir"], config,
                                  group["results"], group["report_paths"], generated_at)
        summaries.append(summary)
        if group["out_dir"] is not None:
            summary_path = group["out_dir"] / SUMMARY_NAME
            vrd.write_json_report(summary_path, summary)
            summary_paths.append(summary_path)

    # Console report.
    print(f"Smoothing assessment for: {input_path}")
    for group in groups.values():
        print(f"[{group['class_name']}]")
        for result in group["results"]:
            print_episode(result)

    totals = {code: 0 for code, _zh in LABELS}
    errors = 0
    for result in results:
        label = result.get("trajectory_label")
        if result["ok"] and label is not None:
            totals[label["code"]] += 1
        else:
            errors += 1
    smoothable = sum(totals[c] for c in SMOOTHABLE_LABELS)
    breakdown = " ".join(f"{code}={totals[code]}" for code, _zh in LABELS)
    print(f"Summary: processed={len(results)} smoothable={smoothable} "
          f"errors={errors} | {breakdown}")
    if summary_paths:
        written = sum(1 for g in groups.values() for p in g["report_paths"] if p is not None)
        print(f"Wrote {written} per-episode report(s) across {len(summary_paths)} "
              f"group(s) under: {args.output_root.resolve()}")
        for summary_path in summary_paths:
            print(f"  summary: {summary_path}")

    if args.json_path:
        if len(summaries) == 1:
            payload = summaries[0]
        else:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "type": "smoothing_assessment_summary_multi",
                "generated_at": generated_at,
                "input_path": str(input_path),
                "config": config,
                "processed": len(results),
                "groups": summaries,
            }
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        with args.json_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Wrote JSON report: {args.json_path}")


if __name__ == "__main__":
    main()
