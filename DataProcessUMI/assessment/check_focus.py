#!/usr/bin/env python3
"""Detect out-of-focus (blurry) wrist-view videos in an episode dataset.

For each episode under the samples root, this checks the two wrist cameras:
    observation.image.left_wrist_view/video.mp4
    observation.image.right_wrist_view/video.mp4

A defocused video lacks high-frequency detail, so the variance of the
Laplacian (a second-derivative focus measure) collapses. We uniformly
sample frames, resize to a fixed size (so the threshold is resolution
independent), and take the median per-frame score to be robust against the
occasional motion-blurred frame. Tenengrad (Sobel gradient energy) is kept
as a supporting metric.

Calibrated on the sample set (256x256 gray frames):
    in-focus  : lapVar ~ 680-880,  tenengrad ~ 11000-14000
    defocused : lapVar ~  70-200,  tenengrad ~  3800-7800
A Laplacian-variance threshold of 350 cleanly separates the two with a
wide margin.

Outputs both a CSV (one row per video) and a JSON summary into the
assessment directory.

Usage:
    python3 check_focus.py [--root ~/data/samples] [--out .] [--threshold 350]
                           [--num-frames 20] [--workers N]
"""

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "check_focus.py requires OpenCV and NumPy, which are not installed "
        f"({exc}).\n"
        "Install them with:\n"
        "    pip install opencv-python numpy"
    )

# Camera views to inspect for each episode.
VIEWS = ["left_wrist_view", "right_wrist_view"]

# Frames are resized to this square size before scoring so that the
# absolute threshold is independent of the source resolution.
RESIZE = (256, 256)

# Laplacian-variance threshold: below this a video is flagged as defocused.
DEFAULT_THRESHOLD = 350.0

# Number of frames uniformly sampled from each video.
DEFAULT_NUM_FRAMES = 20


def score_video(path, num_frames=DEFAULT_NUM_FRAMES):
    """Return per-video focus metrics by sampling `num_frames` frames.

    Returns a dict with the median Laplacian variance, median Tenengrad
    energy, and the number of frames actually scored, or an ``error`` key
    if the video could not be read.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return {"error": "could not open video"}

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        cap.release()
        return {"error": "no frames"}

    idxs = np.linspace(0, total - 1, min(num_frames, total)).astype(int)
    lap_scores, ten_scores = [], []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, RESIZE)
        lap_scores.append(cv2.Laplacian(gray, cv2.CV_64F).var())
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        ten_scores.append(float(np.mean(gx ** 2 + gy ** 2)))
    cap.release()

    if not lap_scores:
        return {"error": "no readable frames"}

    return {
        "lap_var": float(np.median(lap_scores)),
        "tenengrad": float(np.median(ten_scores)),
        "frames_scored": len(lap_scores),
    }


def assess_one(task):
    """Worker: score one (episode, view, path) task and apply the label."""
    episode, view, path, threshold, num_frames = task
    result = {"episode": episode, "view": view, "path": path}
    metrics = score_video(path, num_frames)
    if "error" in metrics:
        result.update(
            lap_var=None, tenengrad=None, frames_scored=0,
            label="error", note=metrics["error"],
        )
        return result
    is_blurry = metrics["lap_var"] < threshold
    result.update(
        lap_var=round(metrics["lap_var"], 2),
        tenengrad=round(metrics["tenengrad"], 1),
        frames_scored=metrics["frames_scored"],
        label="defocused" if is_blurry else "in_focus",
        note="",
    )
    return result


def find_tasks(root, threshold, num_frames):
    """Build the list of videos to assess across all episodes under `root`."""
    tasks = []
    for episode in sorted(os.listdir(root)):
        ep_dir = os.path.join(root, episode)
        if not os.path.isdir(ep_dir):
            continue
        for view in VIEWS:
            path = os.path.join(ep_dir, f"observation.image.{view}", "video.mp4")
            if os.path.exists(path):
                tasks.append((episode, view, path, threshold, num_frames))
    return tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=os.path.expanduser("~/data/samples"),
                        help="dataset root containing episode_* directories")
    parser.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)),
                        help="output directory for CSV/JSON results")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="Laplacian-variance threshold; below = defocused")
    parser.add_argument("--num-frames", type=int, default=DEFAULT_NUM_FRAMES,
                        help="frames sampled per video")
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1),
                        help="parallel worker processes")
    args = parser.parse_args()

    root = os.path.expanduser(args.root)
    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=True)

    tasks = find_tasks(root, args.threshold, args.num_frames)
    if not tasks:
        print(f"No wrist-view videos found under {root}")
        return

    print(f"Assessing {len(tasks)} videos from {root} "
          f"(threshold={args.threshold}, frames={args.num_frames})\n")

    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(assess_one, t): t for t in tasks}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: (r["episode"], r["view"]))

    # Console report.
    for r in results:
        lv = f"{r['lap_var']:8.1f}" if r["lap_var"] is not None else "     n/a"
        flag = "  <-- DEFOCUSED" if r["label"] == "defocused" else ""
        if r["label"] == "error":
            flag = f"  <-- ERROR: {r['note']}"
        print(f"{r['episode']:14s} {r['view']:17s} lapVar={lv}  -> {r['label']}{flag}")

    n_blur = sum(r["label"] == "defocused" for r in results)
    n_err = sum(r["label"] == "error" for r in results)
    print(f"\nTotal: {len(results)}  in_focus: {len(results) - n_blur - n_err}  "
          f"defocused: {n_blur}  error: {n_err}")

    # CSV output.
    csv_path = os.path.join(out, "focus_results.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["episode", "view", "label", "lap_var",
                           "tenengrad", "frames_scored", "note", "path"])
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    # JSON summary (grouped by episode + flat defocused list).
    by_episode = {}
    for r in results:
        by_episode.setdefault(r["episode"], {})[r["view"]] = {
            "label": r["label"], "lap_var": r["lap_var"],
            "tenengrad": r["tenengrad"],
        }
    summary = {
        "root": root,
        "threshold": args.threshold,
        "num_frames": args.num_frames,
        "counts": {
            "total": len(results),
            "in_focus": len(results) - n_blur - n_err,
            "defocused": n_blur,
            "error": n_err,
        },
        "defocused_videos": [
            {"episode": r["episode"], "view": r["view"], "lap_var": r["lap_var"]}
            for r in results if r["label"] == "defocused"
        ],
        "by_episode": by_episode,
    }
    json_path = os.path.join(out, "focus_results.json")
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nWrote {csv_path}\n      {json_path}")


if __name__ == "__main__":
    main()
