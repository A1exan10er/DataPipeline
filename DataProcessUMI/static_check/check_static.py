#!/usr/bin/env python3
"""Check whether wrist-view videos have ego/self motion.

The check samples frames from left_wrist_view and right_wrist_view videos,
then estimates the dominant geometric transform between sampled frame pairs
with ORB features and RANSAC. Local scene changes, such as an object being
moved on the table, should not be treated as ego motion unless the estimated
motion is spatially well covered across the image.
"""

import argparse
import csv
import json
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "check_static.py requires OpenCV and NumPy, which are not installed "
        f"({exc}).\n"
        "Install them with:\n"
        "    pip install opencv-python numpy"
    )


VIEWS = ("left_wrist_view", "right_wrist_view")
VIEW_DIRS = {
    "left_wrist_view": "observation.image.left_wrist_view",
    "right_wrist_view": "observation.image.right_wrist_view",
}


@dataclass
class Thresholds:
    max_corner_px: float
    max_translation_px: float
    max_rotation_deg: float
    max_scale_change: float
    min_keypoints: int
    min_matches: int
    min_inliers: int
    min_inlier_ratio: float
    min_grid_cells: int
    min_bbox_area: float
    moved_pair_ratio: float
    min_moved_pairs: int
    strong_local_corner_px: float
    strong_local_translation_px: float
    strong_local_scale_change: float
    min_strong_local_pairs: int
    min_valid_pairs: int


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sample wrist-view video frames and decide whether the camera/device "
            "itself stayed static. The check uses optical-flow/feature motion "
            "with global RANSAC support, so local object manipulation is ignored "
            "when it lacks image-wide spatial support."
        )
    )
    parser.add_argument(
        "input",
        type=Path,
        help=(
            "Episode directory, dataset/class root containing episode_* dirs, "
            "or a single video file."
        ),
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Output directory for static_check_results.csv/json.",
    )
    parser.add_argument(
        "--num-frames",
        type=int,
        default=24,
        help="Uniformly sampled start frames per video.",
    )
    parser.add_argument(
        "--pair-gap",
        type=int,
        default=10,
        help=(
            "Compare each sampled start frame with the frame this many frames "
            "later. Use 0 to fall back to long-range sampled-frame pairs."
        ),
    )
    parser.add_argument(
        "--max-side",
        type=int,
        default=640,
        help="Resize sampled frames so their largest side is at most this.",
    )
    parser.add_argument(
        "--max-features",
        type=int,
        default=2000,
        help="Maximum tracked/keypoint features per sampled frame.",
    )
    parser.add_argument(
        "--max-pairs",
        type=int,
        default=64,
        help="Maximum sampled frame pairs to compare per video.",
    )

    thresholds = parser.add_argument_group("motion thresholds")
    thresholds.add_argument(
        "--max-corner-px",
        type=float,
        default=4.0,
        help="Max allowed transformed corner displacement for a static pair.",
    )
    thresholds.add_argument(
        "--max-translation-px",
        type=float,
        default=3.0,
        help="Max allowed affine translation for a static pair.",
    )
    thresholds.add_argument(
        "--max-rotation-deg",
        type=float,
        default=1.0,
        help="Max allowed affine rotation for a static pair.",
    )
    thresholds.add_argument(
        "--max-scale-change",
        type=float,
        default=0.015,
        help="Max allowed absolute scale change, e.g. 0.015 means 1.5%%.",
    )
    thresholds.add_argument(
        "--min-keypoints",
        type=int,
        default=80,
        help="Minimum keypoints in both frames before a pair can be assessed.",
    )
    thresholds.add_argument(
        "--min-matches",
        type=int,
        default=24,
        help="Minimum tracked points or descriptor matches before fitting RANSAC.",
    )
    thresholds.add_argument(
        "--min-inliers",
        type=int,
        default=18,
        help="Minimum RANSAC inliers for a reliable global motion model.",
    )
    thresholds.add_argument(
        "--min-inlier-ratio",
        type=float,
        default=0.35,
        help="Minimum RANSAC inlier ratio among matched features.",
    )
    thresholds.add_argument(
        "--min-grid-cells",
        type=int,
        default=5,
        help=(
            "Minimum occupied 4x4 source-image cells among inliers. This keeps "
            "local moving objects from being mistaken for camera motion."
        ),
    )
    thresholds.add_argument(
        "--min-bbox-area",
        type=float,
        default=0.08,
        help="Minimum inlier source bounding-box area fraction for global support.",
    )
    thresholds.add_argument(
        "--moved-pair-ratio",
        type=float,
        default=0.15,
        help="Video is self_moved when at least this fraction of valid pairs moved.",
    )
    thresholds.add_argument(
        "--min-moved-pairs",
        type=int,
        default=1,
        help="Video is self_moved after at least this many reliable moved pairs.",
    )
    thresholds.add_argument(
        "--strong-local-corner-px",
        type=float,
        default=20.0,
        help=(
            "Local/clustered motion above this transformed-corner displacement "
            "is counted as strong secondary evidence for non-static."
        ),
    )
    thresholds.add_argument(
        "--strong-local-translation-px",
        type=float,
        default=15.0,
        help="Local/clustered motion above this translation is strong secondary evidence.",
    )
    thresholds.add_argument(
        "--strong-local-scale-change",
        type=float,
        default=0.05,
        help="Local/clustered motion above this scale change is strong secondary evidence.",
    )
    thresholds.add_argument(
        "--min-strong-local-pairs",
        type=int,
        default=3,
        help=(
            "Video is self_moved when at least this many repeated strong local "
            "motion pairs are seen, even if global spatial coverage is low."
        ),
    )
    thresholds.add_argument(
        "--min-valid-pairs",
        type=int,
        default=3,
        help=(
            "Deprecated compatibility knob. The default classifier is binary: "
            "moved evidence => self_moved, otherwise static."
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="Parallel worker processes.",
    )
    parser.add_argument(
        "--dump-pairs",
        action="store_true",
        help="Include per-frame-pair diagnostics in the JSON output.",
    )
    parser.add_argument(
        "--fail-on-motion",
        action="store_true",
        help="Exit 1 if any video/episode is classified as self_moved.",
    )
    parser.add_argument(
        "--fail-on-inconclusive",
        action="store_true",
        help="Exit 1 if any video/episode is inconclusive or has an error.",
    )
    return parser.parse_args()


def thresholds_from_args(args):
    return Thresholds(
        max_corner_px=args.max_corner_px,
        max_translation_px=args.max_translation_px,
        max_rotation_deg=args.max_rotation_deg,
        max_scale_change=args.max_scale_change,
        min_keypoints=args.min_keypoints,
        min_matches=args.min_matches,
        min_inliers=args.min_inliers,
        min_inlier_ratio=args.min_inlier_ratio,
        min_grid_cells=args.min_grid_cells,
        min_bbox_area=args.min_bbox_area,
        moved_pair_ratio=args.moved_pair_ratio,
        min_moved_pairs=args.min_moved_pairs,
        strong_local_corner_px=args.strong_local_corner_px,
        strong_local_translation_px=args.strong_local_translation_px,
        strong_local_scale_change=args.strong_local_scale_change,
        min_strong_local_pairs=args.min_strong_local_pairs,
        min_valid_pairs=args.min_valid_pairs,
    )


def view_video_path(episode_dir, view):
    return episode_dir / VIEW_DIRS[view] / "video.mp4"


def is_hidden_or_backup_path(path):
    return any(part.startswith(".") for part in path.parts)


def is_episode_dir(path):
    return all(view_video_path(path, view).exists() for view in VIEWS)


def has_any_wrist_view(path):
    return any(view_video_path(path, view).exists() for view in VIEWS)


def scope_for_episode(episode_dir, root=None):
    if root is not None:
        try:
            return str(episode_dir.relative_to(root).parent)
        except ValueError:
            pass
    return episode_dir.parent.name


def find_tasks(input_path):
    input_path = input_path.expanduser()
    if input_path.is_file():
        return [{
            "scope": input_path.parent.parent.name,
            "episode": input_path.parent.parent.name,
            "view": input_path.parent.name,
            "path": str(input_path),
        }]

    if has_any_wrist_view(input_path):
        return [{
            "scope": scope_for_episode(input_path),
            "episode": input_path.name,
            "view": view,
            "path": str(view_video_path(input_path, view)),
        } for view in VIEWS if view_video_path(input_path, view).exists()]

    episode_dirs = []
    for child in sorted(input_path.iterdir()):
        if child.is_dir() and child.name.startswith("episode_") and has_any_wrist_view(child):
            episode_dirs.append(child)
    if not episode_dirs:
        for video in sorted(input_path.rglob("video.mp4")):
            if is_hidden_or_backup_path(video.relative_to(input_path)):
                continue
            if video.parent.name in VIEW_DIRS.values():
                episode_dirs.append(video.parent.parent)
        episode_dirs = sorted(set(episode_dirs))

    tasks = []
    for episode_dir in episode_dirs:
        for view in VIEWS:
            path = view_video_path(episode_dir, view)
            if path.exists():
                tasks.append({
                    "scope": scope_for_episode(episode_dir, input_path),
                    "episode": episode_dir.name,
                    "view": view,
                    "path": str(path),
                })
    return tasks


def sample_indices(total_frames, num_frames):
    n = min(max(2, num_frames), total_frames)
    return sorted({int(round(v)) for v in np.linspace(0, total_frames - 1, n)})


def resize_gray(frame, max_side):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = min(1.0, float(max_side) / max(h, w))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return gray


def read_sampled_frames(video_path, num_frames, max_side):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None, "could not open video"
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 1:
        cap.release()
        return None, "video has fewer than 2 frames"

    frames = []
    for idx in sample_indices(total, num_frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if ok and frame is not None:
            frames.append((idx, resize_gray(frame, max_side)))
    cap.release()

    if len(frames) < 2:
        return None, "fewer than 2 sampled frames could be read"
    return frames, ""


def read_temporal_frame_pairs(video_path, num_frames, pair_gap, max_side):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None, "could not open video"
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 1:
        cap.release()
        return None, "video has fewer than 2 frames"

    gap = min(max(1, pair_gap), total - 1)
    starts = sample_indices(total - gap, num_frames)
    pairs = []
    for start in starts:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(start))
        ok_a, frame_a = cap.read()
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(start + gap))
        ok_b, frame_b = cap.read()
        if ok_a and ok_b and frame_a is not None and frame_b is not None:
            pairs.append((
                (start, resize_gray(frame_a, max_side)),
                (start + gap, resize_gray(frame_b, max_side)),
            ))
    cap.release()

    if not pairs:
        return None, "no sampled frame pairs could be read"
    return pairs, ""


def make_frame_pairs(frame_count, max_pairs):
    pairs = set()
    for i in range(frame_count - 1):
        pairs.add((i, i + 1))
    for i in range(1, frame_count):
        pairs.add((0, i))

    pairs = sorted(pairs, key=lambda pair: (pair[1] - pair[0], pair[0], pair[1]))
    if len(pairs) <= max_pairs:
        return pairs

    idxs = np.linspace(0, len(pairs) - 1, max_pairs).astype(int)
    return [pairs[int(i)] for i in sorted(set(idxs))]


def keypoints_and_descriptors(gray, max_features):
    orb = cv2.ORB_create(nfeatures=max_features, fastThreshold=7)
    return orb.detectAndCompute(gray, None)


def match_descriptors(desc1, desc2):
    if desc1 is None or desc2 is None:
        return []
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw = matcher.knnMatch(desc1, desc2, k=2)
    good = []
    for pair in raw:
        if len(pair) != 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)
    return sorted(good, key=lambda match: match.distance)


def orb_match_points(gray_a, gray_b, max_features):
    kp_a, desc_a = keypoints_and_descriptors(gray_a, max_features)
    kp_b, desc_b = keypoints_and_descriptors(gray_b, max_features)
    matches = match_descriptors(desc_a, desc_b)
    if not matches:
        return None, None, len(kp_a), len(kp_b), 0
    src = np.float32([kp_a[m.queryIdx].pt for m in matches]).reshape(-1, 2)
    dst = np.float32([kp_b[m.trainIdx].pt for m in matches]).reshape(-1, 2)
    return src, dst, len(kp_a), len(kp_b), len(matches)


def track_points(gray_a, gray_b, max_features):
    points = cv2.goodFeaturesToTrack(
        gray_a,
        maxCorners=max_features,
        qualityLevel=0.01,
        minDistance=5,
        blockSize=7,
    )
    if points is None:
        return None, None, 0

    next_points, status, _err = cv2.calcOpticalFlowPyrLK(
        gray_a,
        gray_b,
        points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if next_points is None or status is None:
        return None, None, len(points)

    # Forward/backward filtering removes many bad tracks on manipulated objects
    # and repeated textures before RANSAC sees them.
    back_points, back_status, _back_err = cv2.calcOpticalFlowPyrLK(
        gray_b,
        gray_a,
        next_points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if back_points is None or back_status is None:
        valid = status.reshape(-1).astype(bool)
    else:
        fb_error = np.linalg.norm(back_points.reshape(-1, 2) - points.reshape(-1, 2), axis=1)
        valid = (
            status.reshape(-1).astype(bool) &
            back_status.reshape(-1).astype(bool) &
            (fb_error < 1.5)
        )

    src = points.reshape(-1, 2)[valid]
    dst = next_points.reshape(-1, 2)[valid]
    return src, dst, len(points)


def inlier_spatial_support(points, width, height):
    if len(points) == 0:
        return {"grid_cells": 0, "bbox_area": 0.0}

    cells = set()
    for x, y in points:
        gx = min(3, max(0, int(4 * x / max(width, 1))))
        gy = min(3, max(0, int(4 * y / max(height, 1))))
        cells.add((gx, gy))

    xs = points[:, 0]
    ys = points[:, 1]
    bbox_area = (
        (float(xs.max()) - float(xs.min())) *
        (float(ys.max()) - float(ys.min())) /
        max(float(width * height), 1.0)
    )
    return {"grid_cells": len(cells), "bbox_area": bbox_area}


def affine_metrics(matrix, width, height):
    corners = np.array(
        [[[0.0, 0.0]], [[width - 1.0, 0.0]], [[width - 1.0, height - 1.0]], [[0.0, height - 1.0]]],
        dtype=np.float32,
    )
    moved = cv2.transform(corners, matrix)
    displacements = np.linalg.norm(moved.reshape(-1, 2) - corners.reshape(-1, 2), axis=1)

    a = float(matrix[0, 0])
    b = float(matrix[1, 0])
    scale = math.sqrt(a * a + b * b)
    rotation = math.degrees(math.atan2(float(matrix[1, 0]), float(matrix[0, 0])))
    translation = math.hypot(float(matrix[0, 2]), float(matrix[1, 2]))

    return {
        "corner_max_px": float(displacements.max()),
        "corner_median_px": float(np.median(displacements)),
        "translation_px": translation,
        "rotation_deg": rotation,
        "scale": scale,
        "scale_change": abs(scale - 1.0),
    }


def estimate_pair_motion(src, dst, width, height, result, thresholds):
    if src is None or dst is None or len(src) < thresholds.min_matches:
        result["reason"] = "too_few_tracks"
        return result

    matrix, inlier_mask = cv2.estimateAffinePartial2D(
        src,
        dst,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        maxIters=2000,
        confidence=0.995,
        refineIters=10,
    )
    if matrix is None or inlier_mask is None:
        result["reason"] = "ransac_failed"
        return result

    inlier_mask = inlier_mask.reshape(-1).astype(bool)
    inliers = src[inlier_mask]
    inlier_count = int(inlier_mask.sum())
    inlier_ratio = inlier_count / max(len(src), 1)
    support = inlier_spatial_support(inliers, width, height)
    metrics = affine_metrics(matrix, width, height)

    result.update({
        "inliers": inlier_count,
        "inlier_ratio": inlier_ratio,
        "grid_cells": support["grid_cells"],
        "bbox_area": support["bbox_area"],
        "corner_max_px": metrics["corner_max_px"],
        "corner_median_px": metrics["corner_median_px"],
        "translation_px": metrics["translation_px"],
        "rotation_deg": metrics["rotation_deg"],
        "scale_change": metrics["scale_change"],
    })

    has_global_support = (
        inlier_count >= thresholds.min_inliers and
        inlier_ratio >= thresholds.min_inlier_ratio and
        support["grid_cells"] >= thresholds.min_grid_cells and
        support["bbox_area"] >= thresholds.min_bbox_area
    )
    motion_exceeds = (
        metrics["corner_max_px"] > thresholds.max_corner_px or
        metrics["translation_px"] > thresholds.max_translation_px or
        abs(metrics["rotation_deg"]) > thresholds.max_rotation_deg or
        metrics["scale_change"] > thresholds.max_scale_change
    )

    if has_global_support:
        result["status"] = "moved" if motion_exceeds else "static"
    elif motion_exceeds:
        result["status"] = "local_motion_ignored"
        result["reason"] = "motion_lacks_global_spatial_support"
    else:
        result["reason"] = "insufficient_global_spatial_support"
    return result


def classify_pair(pair, frame_a, frame_b, max_features, thresholds):
    idx_a, gray_a = frame_a
    idx_b, gray_b = frame_b
    src, dst, detected = track_points(gray_a, gray_b, max_features)

    result = {
        "frame_a": int(idx_a),
        "frame_b": int(idx_b),
        "status": "inconclusive",
        "reason": "",
        "keypoints_a": detected,
        "keypoints_b": detected,
        "matches": 0,
        "inliers": 0,
        "inlier_ratio": 0.0,
        "grid_cells": 0,
        "bbox_area": 0.0,
        "corner_max_px": None,
        "corner_median_px": None,
        "translation_px": None,
        "rotation_deg": None,
        "scale_change": None,
    }

    if detected >= thresholds.min_keypoints and src is not None and dst is not None:
        result["matches"] = len(src)
        if len(src) >= thresholds.min_matches:
            h, w = gray_a.shape[:2]
            result = estimate_pair_motion(src, dst, w, h, result, thresholds)
            if result["status"] != "inconclusive" or result["reason"] != "ransac_failed":
                return result

    orb_src, orb_dst, kp_a_count, kp_b_count, match_count = orb_match_points(
        gray_a, gray_b, max_features
    )
    result["keypoints_a"] = max(result["keypoints_a"], kp_a_count)
    result["keypoints_b"] = max(result["keypoints_b"], kp_b_count)
    result["matches"] = match_count
    if result["keypoints_a"] < thresholds.min_keypoints or result["keypoints_b"] < thresholds.min_keypoints:
        result["reason"] = "too_few_keypoints"
        return result
    if orb_src is None or orb_dst is None or len(orb_src) < thresholds.min_matches:
        result["reason"] = "too_few_tracks_or_matches"
        return result

    h, w = gray_a.shape[:2]
    return estimate_pair_motion(orb_src, orb_dst, w, h, result, thresholds)


def summarize_video(pair_results, thresholds):
    counts = {
        "static": sum(r["status"] == "static" for r in pair_results),
        "moved": sum(r["status"] == "moved" for r in pair_results),
        "local_motion_ignored": sum(r["status"] == "local_motion_ignored" for r in pair_results),
        "inconclusive": sum(r["status"] == "inconclusive" for r in pair_results),
    }
    strong_local_motion = 0
    for result in pair_results:
        if result["status"] != "local_motion_ignored":
            continue
        if (
            (result["corner_max_px"] is not None and
             result["corner_max_px"] >= thresholds.strong_local_corner_px) or
            (result["translation_px"] is not None and
             result["translation_px"] >= thresholds.strong_local_translation_px) or
            (result["scale_change"] is not None and
             result["scale_change"] >= thresholds.strong_local_scale_change)
        ):
            strong_local_motion += 1

    valid_pairs = counts["static"] + counts["moved"]
    moved_ratio = counts["moved"] / valid_pairs if valid_pairs else 0.0

    moved_metrics = [
        r for r in pair_results
        if r["status"] in ("static", "moved") and r["corner_max_px"] is not None
    ]
    if moved_metrics:
        corner_values = [r["corner_max_px"] for r in moved_metrics]
        translation_values = [r["translation_px"] for r in moved_metrics]
        rotation_values = [abs(r["rotation_deg"]) for r in moved_metrics]
        scale_values = [r["scale_change"] for r in moved_metrics]
        max_corner_px = max(corner_values)
        median_corner_px = float(np.median(corner_values))
        max_translation_px = max(translation_values)
        max_rotation_deg = max(rotation_values)
        max_scale_change = max(scale_values)
    else:
        max_corner_px = None
        median_corner_px = None
        max_translation_px = None
        max_rotation_deg = None
        max_scale_change = None

    if (
        counts["moved"] >= thresholds.min_moved_pairs and
        moved_ratio >= thresholds.moved_pair_ratio
    ) or strong_local_motion >= thresholds.min_strong_local_pairs:
        verdict = "self_moved"
        self_moved = True
    else:
        verdict = "static"
        self_moved = False

    return {
        "verdict": verdict,
        "self_moved": self_moved,
        "valid_pairs": valid_pairs,
        "moved_ratio": moved_ratio,
        "counts": counts,
        "strong_local_motion_pairs": strong_local_motion,
        "max_corner_px": max_corner_px,
        "median_corner_px": median_corner_px,
        "max_translation_px": max_translation_px,
        "max_rotation_deg": max_rotation_deg,
        "max_scale_change": max_scale_change,
    }


def assess_video(task, num_frames, pair_gap, max_side, max_features, max_pairs, thresholds):
    video_path = Path(task["path"])
    error = ""
    base = {
        **task,
        "available": False,
        "verdict": "error",
        "self_moved": None,
        "error": error,
        "num_sampled_frames": 0,
        "num_pairs": 0,
        "pair_results": [],
    }
    if pair_gap > 0:
        sampled_pairs, error = read_temporal_frame_pairs(video_path, num_frames, pair_gap, max_side)
        if sampled_pairs is None:
            base["error"] = error
            return base
        pair_results = [
            classify_pair((i, i), frame_a, frame_b, max_features, thresholds)
            for i, (frame_a, frame_b) in enumerate(sampled_pairs)
        ]
        sampled_frame_count = len(sampled_pairs) * 2
    else:
        frames, error = read_sampled_frames(video_path, num_frames, max_side)
        if frames is None:
            base["error"] = error
            return base
        pairs = make_frame_pairs(len(frames), max_pairs)
        pair_results = [
            classify_pair(pair, frames[pair[0]], frames[pair[1]], max_features, thresholds)
            for pair in pairs
        ]
        sampled_frame_count = len(frames)

    summary = summarize_video(pair_results, thresholds)
    base.update({
        "available": True,
        "verdict": summary["verdict"],
        "self_moved": summary["self_moved"],
        "error": "",
        "num_sampled_frames": sampled_frame_count,
        "num_pairs": len(pair_results),
        "valid_pairs": summary["valid_pairs"],
        "moved_ratio": summary["moved_ratio"],
        "pair_counts": summary["counts"],
        "strong_local_motion_pairs": summary["strong_local_motion_pairs"],
        "max_corner_px": summary["max_corner_px"],
        "median_corner_px": summary["median_corner_px"],
        "max_translation_px": summary["max_translation_px"],
        "max_rotation_deg": summary["max_rotation_deg"],
        "max_scale_change": summary["max_scale_change"],
        "pair_results": pair_results,
    })
    return base


def episode_verdict(video_results):
    if any(r["verdict"] == "self_moved" for r in video_results):
        return "self_moved"
    if any(r["verdict"] == "error" for r in video_results):
        return "inconclusive"
    return "static"


def compact_video_result(result):
    counts = result.get("pair_counts", {})
    return {
        "scope": result["scope"],
        "episode": result["episode"],
        "view": result["view"],
        "path": result["path"],
        "available": result["available"],
        "verdict": result["verdict"],
        "self_moved": result["self_moved"],
        "error": result.get("error", ""),
        "num_sampled_frames": result.get("num_sampled_frames", 0),
        "num_pairs": result.get("num_pairs", 0),
        "valid_pairs": result.get("valid_pairs", 0),
        "static_pairs": counts.get("static", 0),
        "moved_pairs": counts.get("moved", 0),
        "local_motion_ignored_pairs": counts.get("local_motion_ignored", 0),
        "strong_local_motion_pairs": result.get("strong_local_motion_pairs", 0),
        "inconclusive_pairs": counts.get("inconclusive", 0),
        "moved_ratio": result.get("moved_ratio"),
        "max_corner_px": result.get("max_corner_px"),
        "median_corner_px": result.get("median_corner_px"),
        "max_translation_px": result.get("max_translation_px"),
        "max_rotation_deg": result.get("max_rotation_deg"),
        "max_scale_change": result.get("max_scale_change"),
    }


def write_csv(path, rows):
    fieldnames = [
        "scope", "episode", "view", "path", "available", "verdict", "self_moved",
        "error", "num_sampled_frames", "num_pairs", "valid_pairs", "static_pairs",
        "moved_pairs", "local_motion_ignored_pairs", "inconclusive_pairs",
        "strong_local_motion_pairs", "moved_ratio", "max_corner_px",
        "median_corner_px", "max_translation_px", "max_rotation_deg",
        "max_scale_change",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    args = parse_args()
    thresholds = thresholds_from_args(args)
    tasks = find_tasks(args.input)
    if not tasks:
        raise SystemExit(f"No wrist-view videos found under {args.input}")

    args.out.mkdir(parents=True, exist_ok=True)
    print(
        f"Assessing {len(tasks)} wrist-view videos under {args.input} "
        f"(samples={args.num_frames}, pair_gap={args.pair_gap}, max_side={args.max_side})"
    )

    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                assess_video,
                task,
                args.num_frames,
                args.pair_gap,
                args.max_side,
                args.max_features,
                args.max_pairs,
                thresholds,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: (r["scope"], r["episode"], r["view"]))

    compact_results = [compact_video_result(result) for result in results]
    by_episode = {}
    for result in results:
        key = (result["scope"], result["episode"])
        by_episode.setdefault(key, []).append(result)

    episode_results = []
    for (scope, episode), videos in sorted(by_episode.items()):
        verdict = episode_verdict(videos)
        episode_results.append({
            "scope": scope,
            "episode": episode,
            "verdict": verdict,
            "self_moved": True if verdict == "self_moved" else (
                False if verdict == "static" else None
            ),
            "views": {video["view"]: video["verdict"] for video in videos},
        })

    for row in compact_results:
        note = row["error"] if row["error"] else (
            f"valid={row['valid_pairs']} moved={row['moved_pairs']} "
            f"local_ignored={row['local_motion_ignored_pairs']} "
            f"max_corner={row['max_corner_px']}"
        )
        print(f"{row['episode']:14s} {row['view']:17s} -> {row['verdict']:12s} {note}")

    counts = {
        "static": sum(r["verdict"] == "static" for r in compact_results),
        "self_moved": sum(r["verdict"] == "self_moved" for r in compact_results),
        "inconclusive": sum(r["verdict"] == "inconclusive" for r in compact_results),
        "error": sum(r["verdict"] == "error" for r in compact_results),
    }
    print(
        "\nVideos: "
        f"static={counts['static']} self_moved={counts['self_moved']} "
        f"inconclusive={counts['inconclusive']} error={counts['error']}"
    )

    csv_path = args.out / "static_check_results.csv"
    json_path = args.out / "static_check_results.json"
    write_csv(csv_path, compact_results)

    json_results = results if args.dump_pairs else [
        {key: value for key, value in result.items() if key != "pair_results"}
        for result in results
    ]
    summary = {
        "input": str(args.input),
        "thresholds": asdict(thresholds),
        "num_frames": args.num_frames,
        "pair_gap": args.pair_gap,
        "max_side": args.max_side,
        "counts": counts,
        "episodes": episode_results,
        "videos": json_results,
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Wrote {csv_path}\n      {json_path}")

    if args.fail_on_motion and counts["self_moved"]:
        raise SystemExit(1)
    if args.fail_on_inconclusive and (counts["inconclusive"] or counts["error"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
