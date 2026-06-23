#!/usr/bin/env python3
"""Label + preprocess episodes according to the smoothing assessment.

This is the *acting* counterpart to ``smooth_assessment.py``. Where the
assessment only *classifies* an ``actions.eef_pose`` trajectory into one of five
labels, this tool **acts** on that classification and emits a cleaned episode
(or refuses to, when the data is unsalvageable). The five assessment labels map
onto four handling categories:

==========================  ===================  ================================
assessment label            handling category    what we do
==========================  ===================  ================================
``smooth``                  1. passthrough       copy the episode verbatim.
``recoverable``             2. interpolate       repair every recoverable jump by
                                                 interpolation, keep full length.
``middle_smooth`` /         3. interpolate+crop  repair any recoverable jump in
``middle_recoverable``                           the kept middle, then crop the
                                                 head/tail unrecoverable spans.
``unrecoverable``           4. reject            mark unusable; emit nothing.
==========================  ===================  ================================

Interpolation (category 2 & 3)
------------------------------
A *recoverable* jump is a short tracker glitch that departs and returns. Because
motion is continuous, the glitch frames carry no usable information, so we
**drop** the pose samples inside every recoverable segment (per device) and
**re-derive** them by shape-preserving monotone cubic interpolation (PCHIP)
through the surrounding good samples -- "interpolate from the points before and
after the jump". Both the end-effector position (x,y,z) and the 6D rotation are
repaired (rotation columns are re-orthonormalised afterwards so they stay a
valid rotation). The gripper / tactile streams are untouched -- the glitch is a
tracker artefact, not a gripper one.

Cropping (category 3)
---------------------
The head/tail unrecoverable spans (each < ``boundary_window_s``) are sliced off.
Cropping is by *frame index*, so **every** modality is cut to the same retained
frame window -- the action / state / gripper CSVs, every video and its
``timestamps.csv``. Each retained stream's ``timestamp_ms`` is then re-zeroed so
the cleaned episode starts at t=0, and all streams keep the same frame count and
stay aligned.

Output
------
Path handling and the mirrored ``--output-root`` layout match
``smooth_assessment.py`` / ``assessment/validate_raw_data.py`` exactly. For each
kept episode the *entire* episode directory is reproduced under the output root
with the repaired / cropped data, an updated ``metadata.json`` (frame counts,
duration, and a ``preprocessing`` provenance block recording the judged type,
quality and operations), an updated ``meta/episode.json``, and freshly
recomputed ``checksums.sha256`` / ``.checksum_manifest``. A per-episode
``<episode>.preprocess.json`` report and a per-group ``summary.preprocess.json``
record what was decided and done.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator

# Reuse the assessment IO/path helpers and the smoothing detector so input
# forms, the mirrored output layout, and the labels stay identical across tools.
_PRE_DIR = Path(__file__).resolve().parent
_ASSESS_DIR = _PRE_DIR.parent / "assessment"
for _p in (str(_PRE_DIR), str(_ASSESS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import validate_raw_data as vrd  # noqa: E402
import smooth_assessment as sa  # noqa: E402

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - optional progress bar
    tqdm = None


SCHEMA_VERSION = 1
REPORT_SUFFIX = ".preprocess.json"
SUMMARY_NAME = "summary.preprocess.json"

# eef_pose CSV directories that carry the repairable tracker pose. Both the
# action target and the observed state are smoothed identically so they stay
# consistent after interpolation.
EEF_POSE_DIRS = (sa.vrd.ACTION_DIR, "observation.state.eef_pose")

# Per-side pose columns repaired during interpolation: 3D position + 6D rotation
# (the first two rotation-matrix columns; see metadata coordinate_reference).
POS_SUFFIXES = ("x", "y", "z")
ROT_SUFFIXES = ("r1", "r2", "r3", "r4", "r5", "r6")

# Handling category per assessment label code.
CATEGORY = {
    "smooth": "passthrough",
    "recoverable": "interpolate",
    "middle_smooth": "interpolate_crop",
    "middle_recoverable": "interpolate_crop",
    "unrecoverable": "reject",
}

DEFAULT_CONFIG = {
    # Minimum retained frames after cropping; below this the episode is rejected
    # as too short to be useful.
    "min_kept_frames": 30,
    # Safety margin (frames) trimmed beyond each cropped unrecoverable span so a
    # jump's settling tail is not left at the new boundary.
    "crop_margin_frames": 2,
    # Video re-encode settings used when cropping (category 3 only).
    "video_codec": "libx264",
    "video_crf": 18,
    "video_preset": "medium",
    "video_threads": None,
}


# ---------------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------------


def load_preprocess_config(path=None):
    config = dict(DEFAULT_CONFIG)
    config_path = Path(path) if path else _PRE_DIR / "preprocess_config.json"
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            user = json.load(f)
        for key in DEFAULT_CONFIG:
            if key in user and user[key] is not None:
                config[key] = user[key]
    return config


def _ffmpeg_thread_args(config):
    value = config.get("video_threads") or os.environ.get("UMI_FFMPEG_THREADS")
    if value in (None, ""):
        return []
    try:
        threads = int(value)
    except (TypeError, ValueError):
        return []
    return ["-threads", str(threads)] if threads > 0 else []


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Label and preprocess episodes per the smoothing assessment "
                    "(passthrough / interpolate / interpolate+crop / reject).",
    )
    parser.add_argument("input_path_arg", nargs="?", type=Path,
                        help="Dataset class directory or a single episode_XXX directory.")
    parser.add_argument("-i", "--input-path", type=Path, help="Class or episode directory.")
    parser.add_argument("-o", "--output-root", type=Path, default=Path("preprocessed"),
                        help="Output root (default: preprocessed). Mirrors the input layout.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an existing output episode directory if present.")
    parser.add_argument("--no-video", action="store_true",
                        help="Skip video cropping/copying (CSV/metadata only; faster for testing). "
                             "Cropped episodes then carry only their non-video streams.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Decide and report per episode, but write nothing.")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="Additionally write the combined summary to this path.")
    parser.add_argument("--config", dest="config_path", type=Path,
                        help="Preprocess config file (default: preprocess_config.json).")
    parser.add_argument("--smooth-config", dest="smooth_config_path", type=Path,
                        help="Smoothing-detector threshold config "
                             "(default: smooth_assessment_config.json).")
    parser.add_argument("--fps", type=float,
                        help="Override FPS. Defaults to metadata fps_config or 30.")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Trajectory loading (keeps the original row index so segments map back to CSV
# rows / video frames even when non-finite rows were dropped during detection).
# ---------------------------------------------------------------------------


def load_trajectory_indexed(episode_dir):
    """Like ``smooth_assessment.load_trajectory`` but also return, for every
    retained frame, its original row index in ``actions.eef_pose/data.csv``.

    Returns ``(times_s, positions, row_indices, total_rows)`` where ``positions``
    is ``{"left": [...], "right": [...]}`` of ``(x, y, z)`` tuples and
    ``row_indices[k]`` is the CSV/video frame index of retained frame ``k``.
    """
    data_csv = episode_dir / vrd.ACTION_DIR / "data.csv"
    if not data_csv.exists():
        raise ValueError(f"missing {vrd.ACTION_DIR}/data.csv")
    rows = vrd.read_csv_rows(data_csv)
    if not rows:
        raise ValueError("action data.csv is empty")
    needed = ["timestamp_ms", "left_x", "left_y", "left_z",
              "right_x", "right_y", "right_z"]
    missing_cols = [c for c in needed if c not in rows[0]]
    if missing_cols:
        raise ValueError(f"missing columns: {', '.join(missing_cols)}")

    times, left, right, row_indices = [], [], [], []
    for index, row in enumerate(rows):
        vals = {c: vrd._parse_float(row.get(c)) for c in needed}
        if any(v is None or v != v or v in (float("inf"), float("-inf")) for v in vals.values()):
            continue
        times.append(vals["timestamp_ms"] / 1000.0)
        left.append((vals["left_x"], vals["left_y"], vals["left_z"]))
        right.append((vals["right_x"], vals["right_y"], vals["right_z"]))
        row_indices.append(index)

    if len(times) < 2:
        raise ValueError("fewer than 2 finite frames in action data.csv")
    return times, {"left": left, "right": right}, row_indices, len(rows)


# ---------------------------------------------------------------------------
# Crop range + segment collection
# ---------------------------------------------------------------------------


def collect_segments(device_analyses, state):
    """All segments of ``state`` across both devices as ``(side, a, b)`` (frame
    indices into the *retained* trajectory)."""
    out = []
    for side, analysis in device_analyses.items():
        for seg in analysis["segments"]:
            if seg["state"] == state:
                out.append((side, seg["start_frame"], seg["end_frame"]))
    return out


def compute_crop_frames(times, device_analyses, config, smooth_config):
    """Compute the retained frame window ``[keep_start, keep_end)`` (inclusive /
    exclusive, in retained-frame indices) for a category-3 episode by slicing off
    the head/tail unrecoverable spans, plus a margin.

    Returns ``(keep_start, keep_end, head_cut, tail_cut)`` where ``head_cut`` /
    ``tail_cut`` are the lists of unrecoverable segments removed at each end.
    """
    n = len(times)
    duration = times[-1] - times[0]
    bw = smooth_config["boundary_window_s"]
    margin = int(config["crop_margin_frames"])

    keep_start, keep_end = 0, n
    head_cut, tail_cut = [], []
    for side, a, b in collect_segments(device_analyses, sa.UNRECOVERABLE):
        in_head = times[a] <= bw
        in_tail = times[b] >= duration - bw
        if in_head:
            keep_start = max(keep_start, b + 1 + margin)
            head_cut.append({"side": side, "start_frame": a, "end_frame": b})
        elif in_tail:
            keep_end = min(keep_end, a - margin)
            tail_cut.append({"side": side, "start_frame": a, "end_frame": b})
    return keep_start, keep_end, head_cut, tail_cut


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def _orthonormalize_6d(rot):
    """Re-orthonormalise an (N, 6) array of 6D rotations (two 3-vectors that are
    the first two columns of a rotation matrix) via Gram-Schmidt, in place-safe.
    """
    a = rot[:, 0:3].astype(float)
    b = rot[:, 3:6].astype(float)
    a_norm = np.linalg.norm(a, axis=1, keepdims=True)
    a_norm[a_norm == 0] = 1.0
    e0 = a / a_norm
    b = b - np.sum(e0 * b, axis=1, keepdims=True) * e0
    b_norm = np.linalg.norm(b, axis=1, keepdims=True)
    b_norm[b_norm == 0] = 1.0
    e1 = b / b_norm
    return np.hstack([e0, e1])


def interpolate_columns(values, bad_mask):
    """PCHIP-interpolate the ``True`` positions of ``bad_mask`` in a 1-D float
    array ``values`` from the remaining (good) samples. Good samples are kept
    verbatim; ends beyond the good support are held at the nearest good value."""
    n = len(values)
    idx = np.arange(n)
    good = ~bad_mask
    if good.sum() < 2:
        return values  # not enough support to interpolate
    interp = PchipInterpolator(idx[good], values[good], extrapolate=True)
    out = values.copy()
    out[bad_mask] = interp(idx[bad_mask])
    # Hold (rather than extrapolate) past the good support to avoid drift.
    first_good, last_good = idx[good][0], idx[good][-1]
    out[idx < first_good] = values[good][0]
    out[idx > last_good] = values[good][-1]
    return out


def interpolate_eef_rows(rows, recoverable_by_side, row_indices):
    """Repair the eef-pose ``rows`` (list of dict) in place for each recoverable
    segment. ``recoverable_by_side`` maps ``side -> list of (a, b)`` retained-
    frame index ranges; ``row_indices`` maps retained frame -> CSV row index.

    Returns the count of CSV rows modified per side.
    """
    total_rows = len(rows)
    modified = {"left": 0, "right": 0}
    for side, segments in recoverable_by_side.items():
        if not segments:
            continue
        # Map retained-frame segments to a bad-row mask over the full CSV.
        bad_mask = np.zeros(total_rows, dtype=bool)
        for a, b in segments:
            for k in range(a, b + 1):
                bad_mask[row_indices[k]] = True
        if not bad_mask.any():
            continue
        modified[side] = int(bad_mask.sum())

        # Position columns: independent PCHIP per axis.
        for suf in POS_SUFFIXES:
            col = f"{side}_{suf}"
            if col not in rows[0]:
                continue
            values = np.array([vrd._parse_float(r.get(col)) or 0.0 for r in rows], dtype=float)
            fixed = interpolate_columns(values, bad_mask)
            for i in range(total_rows):
                if bad_mask[i]:
                    rows[i][col] = f"{fixed[i]:.6f}"

        # Rotation columns: PCHIP per component, then re-orthonormalise the
        # repaired frames so the 6D stays a valid rotation.
        rot_cols = [f"{side}_{suf}" for suf in ROT_SUFFIXES]
        if all(c in rows[0] for c in rot_cols):
            rot = np.array([[vrd._parse_float(r.get(c)) or 0.0 for c in rot_cols]
                            for r in rows], dtype=float)
            for j in range(6):
                rot[:, j] = interpolate_columns(rot[:, j], bad_mask)
            rot_fixed = _orthonormalize_6d(rot)
            for i in range(total_rows):
                if bad_mask[i]:
                    for j, c in enumerate(rot_cols):
                        rows[i][c] = f"{rot_fixed[i, j]:.6f}"
    return modified


# ---------------------------------------------------------------------------
# CSV / video / timestamp cropping + rewriting
# ---------------------------------------------------------------------------


def _ts_column(header):
    for name in ("timestamp_ms", "timestamp"):
        if name in header:
            return name
    return header[0] if header else None


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def crop_and_rezero_csv(path, keep_start, keep_end):
    """Crop a per-frame CSV to rows ``[keep_start, keep_end)`` and re-zero its
    timestamp column so the kept data starts at 0. Returns the kept row count."""
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    kept = rows[keep_start:keep_end]
    ts_col = _ts_column(header)
    if kept and ts_col is not None:
        base = vrd._parse_float(kept[0].get(ts_col))
        if base is not None:
            for r in kept:
                v = vrd._parse_float(r.get(ts_col))
                if v is not None:
                    iv = v - base
                    r[ts_col] = str(int(iv)) if float(iv).is_integer() else f"{iv:.6f}"
    write_csv(path, header, kept)
    return len(kept)


def crop_video(src, dst, keep_start, keep_end, fps, config):
    """Crop ``src`` video to frames ``[keep_start, keep_end)`` into ``dst`` by
    re-encoding with a frame-select filter and resetting PTS to start at 0."""
    last = keep_end - 1
    vf = (f"select='between(n\\,{keep_start}\\,{last})',"
          f"setpts=N/FRAME_RATE/TB")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-vf", vf, "-vsync", "cfr", "-r", f"{fps}",
        "-c:v", str(config["video_codec"]), "-crf", str(config["video_crf"]),
        "-preset", str(config["video_preset"]),
        *_ffmpeg_thread_args(config),
        "-an", str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Checksums + metadata
# ---------------------------------------------------------------------------


CHECKSUM_FILES = ("checksums.sha256", ".checksum_manifest")


def _sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def recompute_checksums(episode_dir):
    """Rewrite ``checksums.sha256`` and ``.checksum_manifest`` so they match the
    bytes actually emitted (every file under the episode dir except the two
    checksum files themselves; the manifest additionally records checksums.sha256)."""
    files = []
    for p in sorted(episode_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(episode_dir).as_posix()
        if rel in CHECKSUM_FILES:
            continue
        files.append((rel, _sha256(p)))

    checksum_path = episode_dir / "checksums.sha256"
    with checksum_path.open("w", encoding="utf-8") as f:
        for rel, digest in files:
            f.write(f"{digest}  {rel}\n")

    manifest = {rel: digest for rel, digest in files}
    manifest["checksums.sha256"] = _sha256(checksum_path)
    manifest = dict(sorted(manifest.items()))
    with (episode_dir / ".checksum_manifest").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


def update_metadata(episode_dir, kept_frames, fps, preprocessing_block):
    """Update ``metadata.json`` and ``meta/episode.json`` frame counts/duration
    and attach the ``preprocessing`` provenance block to ``metadata.json``."""
    meta_path = episode_dir / "metadata.json"
    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["total_frames"] = kept_frames
        if fps:
            meta["duration_seconds"] = round(kept_frames / float(fps), 3)
        for mod in (meta.get("modalities") or {}).values():
            if "frames" in mod:
                mod["frames"] = kept_frames
            if "rows" in mod:
                mod["rows"] = kept_frames
        meta["preprocessing"] = preprocessing_block
        vrd.write_json_report(meta_path, meta)

    ep_meta_path = episode_dir / "meta" / "episode.json"
    if ep_meta_path.exists():
        with ep_meta_path.open("r", encoding="utf-8") as f:
            ep_meta = json.load(f)
        for mod in (ep_meta.get("modalities") or {}).values():
            if "frames" in mod:
                mod["frames"] = kept_frames
            if "rows" in mod:
                mod["rows"] = kept_frames
        vrd.write_json_report(ep_meta_path, ep_meta)


# ---------------------------------------------------------------------------
# Per-episode processing
# ---------------------------------------------------------------------------


def _make_writable(root):
    """Add owner write permission to every copied file/dir (the source dataset
    is often read-only, which would block rewriting CSVs/videos in place)."""
    import stat
    for p in root.rglob("*"):
        try:
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
    root.chmod(root.stat().st_mode | stat.S_IWUSR)


def _discover_modalities(episode_dir):
    """Return ``(csv_dirs, video_dirs)`` -- subdirectories holding a per-frame
    ``data.csv`` and those holding a ``video.mp4`` + ``timestamps.csv``."""
    csv_dirs, video_dirs = [], []
    for sub in sorted(episode_dir.iterdir()):
        if not sub.is_dir():
            continue
        if (sub / "data.csv").exists():
            csv_dirs.append(sub)
        elif (sub / "video.mp4").exists():
            video_dirs.append(sub)
    return csv_dirs, video_dirs


def assess(episode_dir, smooth_config, fps_override):
    """Run the smoothing detector and return
    ``(label, device_analyses, times, positions, row_indices, total_rows, fps)``."""
    fps = fps_override if fps_override is not None else vrd.load_episode_fps(episode_dir)
    times, positions, row_indices, total_rows = load_trajectory_indexed(episode_dir)
    device_analyses = {
        "left": sa.analyze_device(times, positions["left"], smooth_config),
        "right": sa.analyze_device(times, positions["right"], smooth_config),
    }
    label = sa.classify_trajectory(times, device_analyses, smooth_config)
    return label, device_analyses, times, row_indices, total_rows, fps


def process_episode(episode_dir, out_dir, config, smooth_config, fps_override,
                    write=True, do_video=True, overwrite=False):
    """Process one episode. Returns a record dict describing the decision and the
    operations performed (also the payload for the per-episode report)."""
    record = {
        "episode": episode_dir.name,
        "path": str(episode_dir),
        "ok": True,
        "error": None,
        "label": None,
        "label_zh": None,
        "category": None,
        "quality": None,
        "operations": [],
        "interpolated": None,
        "crop": None,
        "kept_frames": None,
        "original_frames": None,
        "output_path": None,
    }
    try:
        label, device_analyses, times, row_indices, total_rows, fps = assess(
            episode_dir, smooth_config, fps_override)
    except ValueError as exc:
        record.update({"ok": False, "error": str(exc)})
        return record

    code = label["code"]
    category = CATEGORY[code]
    record.update({
        "label": code,
        "label_zh": label["name_zh"],
        "category": category,
        "original_frames": total_rows,
        "fps": fps,
    })

    # Recoverable segments per side (used by categories 2 & 3).
    recoverable_by_side = {"left": [], "right": []}
    for side, a, b in collect_segments(device_analyses, sa.RECOVERABLE):
        recoverable_by_side[side].append((a, b))

    # Category 4: reject -- nothing is written.
    if category == "reject":
        record.update({
            "quality": "unusable",
            "operations": ["rejected"],
            "kept_frames": 0,
        })
        return record

    # Decide crop window (category 3) up front so recoverable segments outside
    # the kept window are not needlessly interpolated.
    keep_start, keep_end = 0, total_rows
    crop_info = None
    if category == "interpolate_crop":
        ks, ke, head_cut, tail_cut = compute_crop_frames(
            times, device_analyses, config, smooth_config)
        # Map retained-frame crop bounds to original CSV/video row indices.
        keep_start = row_indices[ks] if ks < len(row_indices) else total_rows
        keep_end = (row_indices[ke - 1] + 1) if 0 < ke <= len(row_indices) else total_rows
        kept = keep_end - keep_start
        if kept < int(config["min_kept_frames"]):
            record.update({
                "ok": False,
                "quality": "unusable",
                "operations": ["rejected_too_short_after_crop"],
                "kept_frames": kept,
                "error": f"only {kept} frames remain after cropping "
                         f"(< min_kept_frames={config['min_kept_frames']})",
            })
            return record
        crop_info = {
            "keep_start_frame": keep_start,
            "keep_end_frame": keep_end,
            "kept_frames": kept,
            "head_cut": head_cut,
            "tail_cut": tail_cut,
        }
        record["crop"] = crop_info
        # Drop recoverable segments that fall outside the retained window.
        for side in recoverable_by_side:
            recoverable_by_side[side] = [
                (a, b) for (a, b) in recoverable_by_side[side]
                if keep_start <= row_indices[a] and row_indices[b] < keep_end
            ]

    will_interpolate = any(recoverable_by_side[s] for s in recoverable_by_side)
    kept_frames = keep_end - keep_start

    operations = []
    if category == "passthrough":
        operations.append("passthrough")
        record["quality"] = "good"
    else:
        record["quality"] = "repaired"
        if will_interpolate:
            operations.append("interpolate_recoverable")
        if category == "interpolate_crop":
            operations.append("crop_head_tail")
    record["operations"] = operations
    record["kept_frames"] = kept_frames

    if not write:
        record["output_path"] = str(out_dir)
        return record

    # --- Materialise the cleaned episode ---------------------------------
    if out_dir.exists():
        if overwrite:
            shutil.rmtree(out_dir)
        else:
            raise FileExistsError(
                f"output already exists (use --overwrite): {out_dir}")
    shutil.copytree(episode_dir, out_dir)
    _make_writable(out_dir)
    record["output_path"] = str(out_dir)

    csv_dirs, video_dirs = _discover_modalities(out_dir)

    # 1) Interpolate the eef-pose CSVs (action + observed state).
    interp_summary = {"left": 0, "right": 0}
    if will_interpolate:
        for eef_name in EEF_POSE_DIRS:
            eef_csv = out_dir / eef_name / "data.csv"
            if not eef_csv.exists():
                continue
            with eef_csv.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                header = list(reader.fieldnames or [])
                rows = list(reader)
            mod = interpolate_eef_rows(rows, recoverable_by_side, row_indices)
            write_csv(eef_csv, header, rows)
            interp_summary = mod  # same per-side count for both files
    record["interpolated"] = interp_summary if will_interpolate else None

    # 2) Crop every modality to the retained frame window (category 3 only).
    if category == "interpolate_crop":
        for sub in csv_dirs:
            crop_and_rezero_csv(sub / "data.csv", keep_start, keep_end)
        for sub in video_dirs:
            crop_and_rezero_csv(sub / "timestamps.csv", keep_start, keep_end)
            if do_video:
                src = sub / "video.mp4"
                tmp = sub / "_cropped.mp4"
                crop_video(src, tmp, keep_start, keep_end, fps, config)
                tmp.replace(src)
            else:
                (sub / "video.mp4").unlink()
                record.setdefault("warnings", []).append(
                    f"--no-video: dropped {sub.name}/video.mp4")
    # No crop (passthrough / interpolate): timestamps are left exactly as
    # recorded -- only cropping re-zeros them ("裁剪后从头计算 timestamp").

    # 3) Update metadata + checksums to reflect the emitted bytes.
    preprocessing_block = {
        "schema_version": SCHEMA_VERSION,
        "tool": "preprocess_trajectory.py",
        "data_type": code,
        "data_type_zh": label["name_zh"],
        "category": category,
        "quality": record["quality"],
        "operations": operations,
        "interpolated_frames": record["interpolated"],
        "crop": crop_info,
        "original_frames": total_rows,
        "kept_frames": kept_frames,
        "smooth_config": smooth_config,
        "label_reason": label["reason"],
    }
    update_metadata(out_dir, kept_frames, fps, preprocessing_block)
    recompute_checksums(out_dir)
    return record


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def episode_report_payload(class_name, record, config, smooth_config, generated_at):
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "trajectory_preprocess",
        "generated_at": generated_at,
        "class_name": class_name,
        "config": config,
        "smooth_config": smooth_config,
        **record,
    }


def summary_payload(class_name, input_path, config, smooth_config, records,
                    report_paths, generated_at):
    by_category = {}
    by_label = {}
    written = 0
    rejected = 0
    errors = 0
    episodes = []
    for record, report_path in zip(records, report_paths):
        if not record["ok"] and record["error"] and record["label"] is None:
            errors += 1
        cat = record.get("category")
        if cat:
            by_category[cat] = by_category.get(cat, 0) + 1
        if record.get("label"):
            by_label[record["label"]] = by_label.get(record["label"], 0) + 1
        if record.get("output_path") and record["ok"] and cat != "reject":
            written += 1
        if cat == "reject" or (not record["ok"] and record["label"] is not None):
            rejected += 1
        episodes.append({
            "episode": record["episode"],
            "ok": record["ok"],
            "label": record.get("label"),
            "category": cat,
            "quality": record.get("quality"),
            "operations": record.get("operations"),
            "original_frames": record.get("original_frames"),
            "kept_frames": record.get("kept_frames"),
            "output_path": record.get("output_path"),
            "error": record.get("error"),
            "report": str(report_path) if report_path is not None else None,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "trajectory_preprocess_summary",
        "generated_at": generated_at,
        "class_name": class_name,
        "source_dir": str(input_path),
        "config": config,
        "smooth_config": smooth_config,
        "processed": len(records),
        "written": written,
        "rejected": rejected,
        "errors": errors,
        "category_counts": by_category,
        "label_counts": by_label,
        "episodes": episodes,
    }


def print_episode(record):
    ep = record["episode"]
    if not record["ok"] and record["label"] is None:
        print(f"  [ERROR] {ep}: {record['error']}")
        return
    ops = ",".join(record.get("operations") or [])
    extra = ""
    if record.get("category") == "interpolate_crop" and record.get("crop"):
        c = record["crop"]
        extra = f" | crop[{c['keep_start_frame']}:{c['keep_end_frame']}] kept={c['kept_frames']}"
    if record.get("interpolated"):
        m = record["interpolated"]
        extra += f" | interp L={m.get('left', 0)} R={m.get('right', 0)}"
    if not record["ok"]:
        print(f"  {ep}: {record['label']} ({record['label_zh']}) -> {record.get('quality')} "
              f"[{ops}] {record.get('error', '')}")
        return
    print(f"  {ep}: {record['label']} ({record['label_zh']}) -> {record['category']} "
          f"({record['quality']}) [{ops}]{extra}")


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

    config = load_preprocess_config(args.config_path)
    smooth_config = sa.load_config(args.smooth_config_path)
    episode_dirs = vrd.find_episode_dirs(input_path)
    generated_at = vrd.now_utc_iso()
    write = not args.dry_run

    use_bar = tqdm is not None and len(episode_dirs) > 1
    iterator = (tqdm(episode_dirs, unit="episode", desc=f"Preprocess {input_path.name}")
                if use_bar else episode_dirs)

    records = []
    groups = {}
    for path in iterator:
        if use_bar:
            iterator.set_postfix_str(path.name, refresh=False)
        rel_subdir, class_name = vrd.episode_output_layout(input_path, path)
        out_dir = (args.output_root / rel_subdir / path.name).resolve()
        try:
            record = process_episode(
                path, out_dir, config, smooth_config, args.fps,
                write=write, do_video=not args.no_video, overwrite=args.overwrite)
        except (FileExistsError, subprocess.CalledProcessError) as exc:
            record = {
                "episode": path.name, "path": str(path), "ok": False,
                "error": str(exc), "label": None, "category": None,
                "quality": None, "operations": ["failed"], "output_path": None,
                "original_frames": None, "kept_frames": None, "interpolated": None,
                "crop": None,
            }
        records.append(record)

        report_path = None
        if write:
            report_dir = (args.output_root / rel_subdir).resolve()
            report_path = report_dir / f"{path.name}{REPORT_SUFFIX}"
            vrd.write_json_report(
                report_path,
                episode_report_payload(class_name, record, config, smooth_config, generated_at))

        group = groups.setdefault(str(rel_subdir), {
            "class_name": class_name, "source_dir": path.parent,
            "out_dir": (args.output_root / rel_subdir).resolve(),
            "records": [], "report_paths": [],
        })
        group["records"].append(record)
        group["report_paths"].append(report_path)

    summaries = []
    summary_paths = []
    for group in groups.values():
        summary = summary_payload(group["class_name"], group["source_dir"], config,
                                  smooth_config, group["records"], group["report_paths"],
                                  generated_at)
        summaries.append(summary)
        if write:
            summary_path = group["out_dir"] / SUMMARY_NAME
            vrd.write_json_report(summary_path, summary)
            summary_paths.append(summary_path)

    # Console report.
    print(f"Preprocess for: {input_path}")
    for group in groups.values():
        print(f"[{group['class_name']}]")
        for record in group["records"]:
            print_episode(record)

    cat_totals = {}
    written = rejected = errors = 0
    for record in records:
        cat = record.get("category")
        if cat:
            cat_totals[cat] = cat_totals.get(cat, 0) + 1
        if record.get("output_path") and record["ok"] and cat != "reject":
            written += 1
        if cat == "reject" or (not record["ok"] and record.get("label") is not None):
            rejected += 1
        if not record["ok"] and record.get("label") is None:
            errors += 1
    breakdown = " ".join(f"{k}={v}" for k, v in sorted(cat_totals.items()))
    print(f"Summary: processed={len(records)} written={written} rejected={rejected} "
          f"errors={errors} | {breakdown}")
    if summary_paths:
        print(f"Wrote outputs + reports under: {args.output_root.resolve()}")
        for summary_path in summary_paths:
            print(f"  summary: {summary_path}")
    elif args.dry_run:
        print("(dry-run: nothing written)")

    if args.json_path:
        payload = summaries[0] if len(summaries) == 1 else {
            "schema_version": SCHEMA_VERSION,
            "type": "trajectory_preprocess_summary_multi",
            "generated_at": generated_at,
            "input_path": str(input_path),
            "config": config,
            "smooth_config": smooth_config,
            "processed": len(records),
            "groups": summaries,
        }
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        with args.json_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Wrote JSON report: {args.json_path}")


if __name__ == "__main__":
    main()
