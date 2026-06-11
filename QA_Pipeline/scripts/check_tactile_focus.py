#!/usr/bin/env python3
"""Check focus quality for RGB camera videos using Laplacian variance."""

import argparse
import csv
import sys
import time
from multiprocessing import Pool
from pathlib import Path


BLUR_THRESHOLD = 50.0  # RGB cameras: below this is considered blurry


def blur_score(frame_bgr) -> float:
    """Compute Laplacian variance as a focus quality score.

    Higher = sharper. Lower = more blurry / out of focus.
    Typical range for a focused tactile camera: > 50.
    Typical range for a blurry/unfocused frame: < 20.
    """
    import cv2

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _frame_result(frame_index: int, frame) -> dict:
    score = blur_score(frame)
    height, width = frame.shape[:2]
    return {
        "frame_index": frame_index,
        "blur_score": score,
        "is_blurry": score < BLUR_THRESHOLD,
        "width": int(width),
        "height": int(height),
    }


def _is_checkable_camera(modality_name: str) -> bool:
    """Return True for all image modalities except flow."""
    if not modality_name.startswith("observation.image."):
        return False
    if modality_name.startswith("observation.image.flow_"):
        return False
    return True


def _camera_type(modality_name: str) -> str:
    return "tactile" if "tactile" in modality_name else "rgb"


def check_first_n_frames(
    video_path: Path,
    n: int = 3,
) -> list[dict]:
    """Decode the first n frames of a video and compute blur scores.

    Returns a list of dicts, one per frame:
        frame_index: int (0-based)
        blur_score: float
        is_blurry: bool  (blur_score < BLUR_THRESHOLD)
        width: int
        height: int

    Returns empty list if video cannot be opened.
    """
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return []

        results = []
        for frame_index in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok:
                break
            result = _frame_result(frame_index, frame)
            result["position"] = "start"
            results.append(result)
        return results
    finally:
        cap.release()


def check_middle_n_frames(
    video_path: Path,
    n: int = 3,
) -> list[dict]:
    """Decode n frames from the middle of the video and compute blur scores.

    Samples frames at evenly spaced positions between 40% and 60% of
    total frame count. Middle frames are more representative of actual
    task execution than first or last frames.

    Returns a list of dicts, one per frame:
        frame_index: int
        position_ratio: float  (e.g. 0.4, 0.5, 0.6)
        blur_score: float
        is_blurry: bool
        width: int
        height: int

    Returns empty list if video cannot be opened.
    """
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return []

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count <= 0 or n <= 0:
            return []

        if n == 1:
            ratios = [0.5]
        else:
            step = 0.2 / (n - 1)
            ratios = [0.4 + step * i for i in range(n)]

        results = []
        for ratio in ratios:
            frame_index = min(frame_count - 1, int(frame_count * ratio))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok:
                continue

            result = _frame_result(frame_index, frame)
            result["position"] = "middle"
            result["position_ratio"] = ratio
            results.append(result)
        return results
    finally:
        cap.release()


def check_last_n_frames(
    video_path: Path,
    n: int = 3,
) -> list[dict]:
    """Decode the last n frames of a video and compute blur scores."""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return []

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            return []

        positions = [max(0, total - n + i) for i in range(n)]
        results = []
        for idx, pos in enumerate(positions):
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ret, frame = cap.read()
            if not ret:
                continue
            score = blur_score(frame)
            h, w = frame.shape[:2]
            results.append(
                {
                    "frame_index": pos,
                    "position": "end",
                    "blur_score": score,
                    "is_blurry": score < BLUR_THRESHOLD,
                    "width": w,
                    "height": h,
                }
            )
        return results
    finally:
        cap.release()


def check_episode_tactile_focus(
    episode_path: Path,
    n_frames: int = 3,
) -> list[dict]:
    """Check focus quality of all RGB camera videos in one episode.

    Returns a list of dicts, one per (modality, frame):
        episode_path: str
        modality: str
        frame_index: int
        blur_score: float
        is_blurry: bool
    """
    results = []
    for modality_path in sorted(episode_path.iterdir()):
        if not modality_path.is_dir() or not _is_checkable_camera(modality_path.name):
            continue

        video_path = modality_path / "video.mp4"
        if not video_path.is_file():
            continue

        camera_type = _camera_type(modality_path.name)
        frame_results = []
        frame_results.extend(check_middle_n_frames(video_path, n_frames))
        frame_results.extend(check_last_n_frames(video_path, n_frames))

        for frame_result in frame_results:
            results.append(
                {
                    "episode_path": str(episode_path),
                    "modality": modality_path.name,
                    "camera_type": camera_type,
                    "frame_index": frame_result["frame_index"],
                    "position": frame_result["position"],
                    "blur_score": frame_result["blur_score"],
                    "is_blurry": frame_result["is_blurry"],
                    "width": frame_result["width"],
                    "height": frame_result["height"],
                }
            )
    return results


def check_episode_rgb_focus(episode_path: Path) -> list[dict]:
    return check_episode_tactile_focus(episode_path)


def _process_episode_worker(episode_path_str: str) -> tuple[str, list[dict]]:
    """Worker function for multiprocessing. Must be module-level for pickling."""
    episode_path = Path(episode_path_str)
    results = check_episode_rgb_focus(episode_path)
    return episode_path_str, results


def discover_episodes(roots: list[Path], max_episodes: int | None = None) -> list[Path]:
    """Find episode directories under roots."""
    episodes = []
    seen = set()

    for root in roots:
        if not root.exists():
            print(f"Warning: root does not exist: {root}", file=sys.stderr)
            continue

        candidates = [root] if root.is_dir() else []
        if root.is_dir():
            candidates.extend(root.rglob("episode_*"))

        for candidate in candidates:
            if not candidate.is_dir():
                continue
            if not candidate.name.startswith("episode_"):
                continue
            if not (candidate / "metadata.json").is_file():
                continue

            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            episodes.append(candidate)

            if max_episodes is not None and len(episodes) >= max_episodes:
                return sorted(episodes)

    return sorted(episodes)


def write_report(output_path: Path, rows: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "episode_path",
        "modality",
        "camera_type",
        "frame_index",
        "position",
        "blur_score",
        "is_blurry",
        "width",
        "height",
    ]

    with output_path.open("w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _print_progress(current: int, total: int, width: int = 40) -> None:
    filled = int(width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r  [{bar}] {current}/{total}", end="", flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check RGB camera focus quality using Laplacian variance."
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        required=True,
        type=Path,
        help="One or more task or episode directories to scan.",
    )
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=None,
        help="Optional episode limit for quick testing.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/camera_focus_report.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel worker processes. Default: 1.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    start_time = time.time()

    episodes = discover_episodes(args.roots, args.max_episodes)
    rows = []
    total_episodes = len(episodes)

    workers = args.workers
    if workers > 1:
        with Pool(processes=workers) as pool:
            for i, (ep_path_str, episode_rows) in enumerate(
                pool.imap_unordered(
                    _process_episode_worker,
                    [str(episode_path) for episode_path in episodes],
                )
            ):
                rows.extend(episode_rows)

                _print_progress(i + 1, total_episodes)
    else:
        for i, episode_path in enumerate(episodes):
            episode_rows = check_episode_tactile_focus(episode_path)
            rows.extend(episode_rows)

            _print_progress(i + 1, total_episodes)

    print()
    write_report(args.output, rows)

    elapsed = time.time() - start_time
    rgb_rows = [row for row in rows if row["camera_type"] == "rgb"]
    tactile_rows = [row for row in rows if row["camera_type"] == "tactile"]
    rgb_cameras_checked = len({(row["episode_path"], row["modality"]) for row in rgb_rows})
    tactile_cameras_checked = len(
        {(row["episode_path"], row["modality"]) for row in tactile_rows}
    )
    rgb_middle_frames = [row for row in rgb_rows if row["position"] == "middle"]
    rgb_end_frames = [row for row in rgb_rows if row["position"] == "end"]
    tactile_middle_frames = [
        row for row in tactile_rows if row["position"] == "middle"
    ]
    tactile_end_frames = [row for row in tactile_rows if row["position"] == "end"]
    blurry_rgb_middle_frames = sum(
        1 for row in rgb_middle_frames if row["is_blurry"]
    )
    blurry_rgb_end_frames = sum(1 for row in rgb_end_frames if row["is_blurry"])
    blurry_tactile_middle_frames = sum(
        1 for row in tactile_middle_frames if row["is_blurry"]
    )
    blurry_tactile_end_frames = sum(
        1 for row in tactile_end_frames if row["is_blurry"]
    )
    blurry_rgb_middle_percent = (
        blurry_rgb_middle_frames / len(rgb_middle_frames) * 100.0
        if rgb_middle_frames
        else 0.0
    )
    blurry_rgb_end_percent = (
        blurry_rgb_end_frames / len(rgb_end_frames) * 100.0 if rgb_end_frames else 0.0
    )
    blurry_rgb_episode_paths = {
        row["episode_path"] for row in rgb_rows if row["is_blurry"]
    }

    print(f"Episodes checked: {len(episodes)}")
    print(f"RGB cameras checked: {rgb_cameras_checked}")
    print(
        f"  Middle blurry: {blurry_rgb_middle_frames} "
        f"({blurry_rgb_middle_percent:.1f}%)"
    )
    print(f"  End blurry: {blurry_rgb_end_frames} ({blurry_rgb_end_percent:.1f}%)")
    print()
    print(f"Tactile cameras checked: {tactile_cameras_checked}")
    print(
        f"  Middle blurry: {blurry_tactile_middle_frames} "
        "(note: low scores may indicate no contact, not blur)"
    )
    print(f"  End blurry: {blurry_tactile_end_frames}")
    print()
    print(f"Blurry episodes (RGB only): {len(blurry_rgb_episode_paths)}")
    print(f"Time elapsed: {elapsed:.2f}s")
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
