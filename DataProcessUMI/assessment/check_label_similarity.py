#!/usr/bin/env python3
"""Detect cross-class mislabeling between wrist-view and tactile videos.

Each episode has six image streams that fall into two visually distinct
appearance classes:

    view    : observation.image.left_wrist_view
              observation.image.right_wrist_view
    tactile : observation.image.left_wrist_left_tactile
              observation.image.left_wrist_right_tactile
              observation.image.right_wrist_left_tactile
              observation.image.right_wrist_right_tactile

The two view cameras show a wide-angle scene (table, objects, arms); the four
tactile cameras show a granular gel-contact texture. The two classes look
nothing alike, while the four tactile streams look very much alike.

Occasionally a stream is filed under the wrong directory (e.g. a tactile
video sitting in *_wrist_view). This script catches such CROSS-CLASS swaps
from the FIRST FRAME of each video.

Method
------
For each episode we take the first frame of every stream, describe it with a
normalized HSV hue-saturation histogram, and correlate every pair. The four
tactile streams form a tight, stable cluster (mutual correlation ~0.75-0.98),
so we score each stream by its *tactile affinity* = the median correlation to
the streams NAMED as tactile (excluding itself). A genuine tactile frame
scores high; a genuine view frame scores low (~0.1) even when the view is out
of focus (defocus lowers view-to-view similarity but never makes a view look
like tactile speckle). The appearance class is then:

    tactile  if tactile_affinity >= threshold (default 0.40)
    view     otherwise

and a stream whose appearance class disagrees with its directory name is
flagged as a cross-class mislabel.

Note: this only detects view<->tactile swaps. It cannot tell apart the four
tactile streams from each other (they are appearance-identical), so a swap
*within* the tactile group is out of scope.

Outputs a CSV (one row per stream) and a JSON summary.

Usage:
    python3 check_label_similarity.py [--root ~/data/samples] [--out .]
                                      [--threshold 0.40]
"""

import argparse
import csv
import json
import os
from statistics import median

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "check_label_similarity.py requires OpenCV and NumPy, which are not "
        f"installed ({exc}).\n"
        "Install them with:\n"
        "    pip install opencv-python numpy"
    )

VIEW_STREAMS = [
    "observation.image.left_wrist_view",
    "observation.image.right_wrist_view",
]
TACTILE_STREAMS = [
    "observation.image.left_wrist_left_tactile",
    "observation.image.left_wrist_right_tactile",
    "observation.image.right_wrist_left_tactile",
    "observation.image.right_wrist_right_tactile",
]
# All streams we inspect, in display order, with their expected class.
EXPECTED_CLASS = {s: "view" for s in VIEW_STREAMS}
EXPECTED_CLASS.update({s: "tactile" for s in TACTILE_STREAMS})
STREAMS = VIEW_STREAMS + TACTILE_STREAMS

# A stream whose tactile-affinity is >= this looks like tactile; below, view.
DEFAULT_THRESHOLD = 0.40


def first_frame_descriptor(video_path):
    """Return a normalized HSV hue-saturation histogram of the first frame.

    Returns None if the video cannot be opened or has no decodable frame.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return None
    frame = cv2.resize(frame, (128, 128))
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().astype("float32")


def correlation(a, b):
    """HSV-histogram correlation in [-1, 1]; 1 == identical."""
    return float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))


def assess_episode(episode_dir, threshold):
    """Score one episode's streams and label cross-class mislabels.

    Returns a dict with per-stream results and a list of mislabeled streams.
    """
    descriptors = {}
    missing = []
    for stream in STREAMS:
        path = os.path.join(episode_dir, stream, "video.mp4")
        if not os.path.exists(path):
            missing.append(stream)
            continue
        desc = first_frame_descriptor(path)
        if desc is None:
            missing.append(stream)
            continue
        descriptors[stream] = desc

    present_tactile = [s for s in TACTILE_STREAMS if s in descriptors]
    present_view = [s for s in VIEW_STREAMS if s in descriptors]

    streams_out = []
    mislabeled = []
    for stream in STREAMS:
        if stream not in descriptors:
            streams_out.append({
                "stream": stream,
                "expected_class": EXPECTED_CLASS[stream],
                "appearance_class": "unknown",
                "tactile_affinity": None,
                "view_affinity": None,
                "mislabeled": False,
                "note": "missing or unreadable video",
            })
            continue

        desc = descriptors[stream]
        # Median correlation to the streams NAMED tactile / view (self excluded).
        tactile_refs = [correlation(desc, descriptors[s])
                        for s in present_tactile if s != stream]
        view_refs = [correlation(desc, descriptors[s])
                     for s in present_view if s != stream]
        tactile_affinity = median(tactile_refs) if tactile_refs else None
        view_affinity = median(view_refs) if view_refs else None

        # Classify by the stable tactile cluster: high affinity == tactile.
        if tactile_affinity is None:
            appearance = "unknown"
        else:
            appearance = "tactile" if tactile_affinity >= threshold else "view"

        expected = EXPECTED_CLASS[stream]
        is_mislabeled = appearance != "unknown" and appearance != expected
        if is_mislabeled:
            mislabeled.append(stream)

        streams_out.append({
            "stream": stream,
            "expected_class": expected,
            "appearance_class": appearance,
            "tactile_affinity": (round(tactile_affinity, 3)
                                 if tactile_affinity is not None else None),
            "view_affinity": (round(view_affinity, 3)
                              if view_affinity is not None else None),
            "mislabeled": is_mislabeled,
            "note": "",
        })

    return {
        "episode": os.path.basename(episode_dir.rstrip("/")),
        "path": episode_dir,
        "threshold": threshold,
        "streams": streams_out,
        "mislabeled_streams": mislabeled,
        "has_mislabel": bool(mislabeled),
        "missing_streams": missing,
    }


def find_episode_dirs(root):
    """Return episode_* dirs: `root` itself if it is one, else its children."""
    root = root.rstrip("/")
    if os.path.basename(root).startswith("episode_"):
        return [root]
    return [os.path.join(root, name) for name in sorted(os.listdir(root))
            if name.startswith("episode_") and os.path.isdir(os.path.join(root, name))]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=os.path.expanduser("~/data/samples"),
                        help="dataset root containing episode_* directories, "
                             "or a single episode_XXX directory")
    parser.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)),
                        help="output directory for CSV/JSON results")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="tactile-affinity threshold; >= is tactile, < is view")
    args = parser.parse_args()

    root = os.path.expanduser(args.root)
    out = os.path.expanduser(args.out)
    os.makedirs(out, exist_ok=True)

    episode_dirs = find_episode_dirs(root)
    if not episode_dirs:
        print(f"No episode_* directories found under {root}")
        return

    print(f"Checking first-frame label similarity for {len(episode_dirs)} "
          f"episode(s) under {root} (threshold={args.threshold})\n")

    results = [assess_episode(ep, args.threshold) for ep in episode_dirs]

    rows = []
    n_mislabel_eps = 0
    for r in results:
        flag = "  <-- MISLABEL" if r["has_mislabel"] else ""
        if r["has_mislabel"]:
            n_mislabel_eps += 1
        print(f"{r['episode']}: {'CROSS-CLASS MISLABEL' if r['has_mislabel'] else 'ok'}{flag}")
        for s in r["streams"]:
            ta = f"{s['tactile_affinity']:.3f}" if s["tactile_affinity"] is not None else "  n/a"
            va = f"{s['view_affinity']:.3f}" if s["view_affinity"] is not None else "  n/a"
            mark = "  <== mislabeled" if s["mislabeled"] else ""
            note = f"  ({s['note']})" if s["note"] else ""
            short = s["stream"].replace("observation.image.", "")
            print(f"    {short:26s} expected={s['expected_class']:7s} "
                  f"looks={s['appearance_class']:7s} "
                  f"tactile_aff={ta} view_aff={va}{mark}{note}")
            rows.append({
                "episode": r["episode"],
                "stream": s["stream"],
                "expected_class": s["expected_class"],
                "appearance_class": s["appearance_class"],
                "tactile_affinity": s["tactile_affinity"],
                "view_affinity": s["view_affinity"],
                "mislabeled": s["mislabeled"],
                "note": s["note"],
            })

    print(f"\nTotal episodes: {len(results)}  "
          f"with cross-class mislabel: {n_mislabel_eps}  "
          f"clean: {len(results) - n_mislabel_eps}")

    csv_path = os.path.join(out, "label_similarity_results.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "episode", "stream", "expected_class", "appearance_class",
            "tactile_affinity", "view_affinity", "mislabeled", "note"])
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "root": root,
        "threshold": args.threshold,
        "counts": {
            "episodes": len(results),
            "with_mislabel": n_mislabel_eps,
            "clean": len(results) - n_mislabel_eps,
        },
        "mislabeled": [
            {"episode": r["episode"], "streams": r["mislabeled_streams"]}
            for r in results if r["has_mislabel"]
        ],
        "episodes": results,
    }
    json_path = os.path.join(out, "label_similarity_results.json")
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nWrote {csv_path}\n      {json_path}")


if __name__ == "__main__":
    main()
