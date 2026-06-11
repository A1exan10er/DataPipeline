#!/usr/bin/env python3
"""Check and optionally trim frame count alignment across episode modalities."""

import argparse
import csv
import time
from multiprocessing import Pool
from pathlib import Path


STILLNESS_SAMPLE_INTERVAL = 30
WRIST_DIFF_MIN_THRESHOLD = 3.0
STILLNESS_MIN_SECONDS = 5.0
STILLNESS_WINDOW = 5
STILLNESS_MIN_RATIO = 0.8
ADAPTIVE_THRESHOLD_MAX_SAMPLES = 10


def _is_image_modality(modality: str) -> bool:
    return modality.startswith("observation.image.") and not modality.startswith(
        "observation.image.flow_"
    )


def _is_table_modality(modality: str) -> bool:
    return (
        modality.startswith("observation.state.")
        or modality.startswith("action.")
        or modality.startswith("actions.")
    )


def _iter_modalities(episode_path: Path) -> list[str]:
    modalities = []
    for path in sorted(episode_path.iterdir()):
        if not path.is_dir():
            continue
        if path.name.startswith("observation.image.flow_"):
            continue
        if _is_image_modality(path.name) or _is_table_modality(path.name):
            modalities.append(path.name)
    return modalities


def _should_check_modality(modality: str) -> bool:
    """Return True if this modality should be included in frame alignment check.

    Excludes:
    - observation.state.*_tactile: tactile sensor state streams behave
      differently from other modalities (no raw files, often 1 row longer)
    - observation.image.flow_*: derived streams, not raw captures
    """
    if modality.startswith("observation.state.") and "tactile" in modality:
        return False
    if modality.startswith("observation.image.flow_"):
        return False
    return True


def _count_csv_data_rows(csv_path: Path) -> int | None:
    try:
        with csv_path.open("r", newline="") as csvfile:
            reader = csv.reader(csvfile)
            try:
                next(reader)
            except StopIteration:
                return 0
            return sum(1 for _ in reader)
    except OSError:
        return None


def _modality_file_paths(episode_path: Path, modality: str) -> tuple[Path | None, Path | None]:
    modality_path = episode_path / modality
    if _is_image_modality(modality):
        return modality_path / "timestamps_raw.csv", modality_path / "timestamps.csv"
    if _is_table_modality(modality):
        return modality_path / "data_raw.csv", modality_path / "data.csv"
    return None, None


def _raw_file_mode(episode_path: Path, modalities: list[str]) -> tuple[bool, str]:
    raw_count = 0
    eligible_count = 0

    for modality in modalities:
        raw_path, _ = _modality_file_paths(episode_path, modality)
        if raw_path is None:
            continue
        eligible_count += 1
        if raw_path.exists():
            raw_count += 1

    if eligible_count > 0 and raw_count == eligible_count:
        return True, ""
    if raw_count == 0:
        return False, ""
    return (
        False,
        "Warning: mixed raw and processed modality files; using processed files for all counts.",
    )


def _choose_file_strategy(episode_path: Path, modalities: list[str]) -> str:
    """Determine whether to use raw or processed files for this episode.

    Returns 'raw' if ALL modalities have raw files.
    Returns 'processed' if NO modalities have raw files.
    Returns 'processed' if MIXED (some have raw, some don't), to ensure
    consistent comparison. Also records a warning in the result.
    """
    has_raw = []
    for modality in modalities:
        raw_path, _ = _modality_file_paths(episode_path, modality)
        if raw_path is None:
            continue
        has_raw.append(raw_path.exists())

    if has_raw and all(has_raw):
        return "raw"
    return "processed"


def count_modality_frames(
    episode_path: Path, modality: str, prefer_raw: bool = True
) -> int | None:
    """Count frames/rows for a modality.

    For image modalities: count rows in timestamps.csv.
    Uses all rows (not filtering by is_new) for alignment purposes,
    because the video frame count includes padding frames.

    For state/action modalities: count rows in data.csv (excluding header).

    Returns None if the file cannot be read.
    """
    if modality.startswith("observation.image.flow_"):
        return None

    raw_path, processed_path = _modality_file_paths(episode_path, modality)
    if raw_path is None or processed_path is None:
        return None

    use_raw = False
    if prefer_raw:
        use_raw, _ = _raw_file_mode(episode_path, _iter_modalities(episode_path))

    path_to_read = raw_path if use_raw else processed_path
    if not path_to_read.is_file():
        return None
    return _count_csv_data_rows(path_to_read)


def _format_mismatched_modalities(modality_counts: dict[str, int]) -> str:
    if not modality_counts:
        return ""

    min_count = min(modality_counts.values())
    max_count = max(modality_counts.values())
    mismatched = [
        f"{modality}={count}"
        for modality, count in sorted(modality_counts.items())
        if count != min_count and count != max_count
    ]
    extremes = [
        f"{modality}={count}"
        for modality, count in sorted(modality_counts.items())
        if count == min_count or count == max_count
    ]
    return ", ".join(extremes + mismatched)


def analyze_episode(episode_path: Path) -> dict:
    """Analyze frame count alignment for one episode.

    Returns:
        episode_path: str
        status: 'pass' | 'needs_trim' | 'fail'
        min_count: int
        max_count: int
        spread: int  (max - min)
        target_count: int  (min_count, used for trimming)
        modality_counts: dict mapping modality name to frame count
        missing_modalities: list of modalities whose files could not be read
        reason: str  (human-readable explanation)
    """
    modality_counts = {}
    missing_modalities = []
    all_modalities = _iter_modalities(episode_path)
    modalities_to_check = [
        modality for modality in all_modalities if _should_check_modality(modality)
    ]
    file_strategy = _choose_file_strategy(episode_path, modalities_to_check)
    _, warning_note = _raw_file_mode(episode_path, modalities_to_check)

    for modality in modalities_to_check:
        count = count_modality_frames(
            episode_path, modality, prefer_raw=file_strategy == "raw"
        )
        if count is None:
            missing_modalities.append(modality)
        else:
            modality_counts[modality] = count

    if not modality_counts:
        reason = "No readable modality frame counts."
        if warning_note:
            reason = f"{reason} {warning_note}"
        return {
            "episode_path": str(episode_path),
            "status": "fail",
            "min_count": 0,
            "max_count": 0,
            "spread": 0,
            "target_count": 0,
            "file_strategy": file_strategy,
            "modality_counts": modality_counts,
            "missing_modalities": missing_modalities,
            "reason": reason,
        }

    min_count = min(modality_counts.values())
    max_count = max(modality_counts.values())
    spread = max_count - min_count
    target_count = min_count

    if missing_modalities:
        status = "fail"
        reason = "Missing or unreadable modality files: " + ", ".join(
            sorted(missing_modalities)
        )
    elif spread > 3:
        status = "fail"
        reason = "Frame count spread > 3: " + _format_mismatched_modalities(
            modality_counts
        )
    elif spread > 0:
        status = "needs_trim"
        reason = f"Frame count spread {spread}; safe to trim to {target_count}."
    else:
        status = "pass"
        reason = "All modality counts match."

    if warning_note:
        reason = f"{reason} {warning_note}"

    return {
        "episode_path": str(episode_path),
        "status": status,
        "min_count": min_count,
        "max_count": max_count,
        "spread": spread,
        "target_count": target_count,
        "file_strategy": file_strategy,
        "modality_counts": modality_counts,
        "missing_modalities": missing_modalities,
        "reason": reason,
    }


def _frame_diff(frame1, frame2) -> float:
    """Mean absolute pixel difference between two grayscale frames."""
    import cv2

    gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
    return float(cv2.absdiff(gray1, gray2).mean())


def _read_frame(cap, frame_index: int):
    import cv2

    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = cap.read()
    if not ok:
        return None
    return frame


def _get_timestamp_ms(timestamps_path: Path, frame_index: int) -> float | None:
    """Read timestamp_ms for a specific frame index from timestamps.csv."""
    if frame_index < 0 or not timestamps_path.is_file():
        return None

    try:
        with timestamps_path.open("r", newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            for index, row in enumerate(reader):
                if index == frame_index:
                    return float(row["timestamp_ms"])
    except (OSError, KeyError, ValueError):
        return None

    return None


def _compute_adaptive_threshold(
    left_video: Path,
    right_video: Path,
    sample_interval: int,
) -> float:
    """Compute adaptive motion threshold from video content.

    Collects max(left_diff, right_diff) at each sample point across
    the full video, then sets threshold = max(p50 * 0.15, 3.0).

    This adapts to both fast tasks (high diffs) and slow tasks (low diffs).
    """
    import cv2

    left_cap = cv2.VideoCapture(str(left_video))
    right_cap = cv2.VideoCapture(str(right_video))
    try:
        if not left_cap.isOpened() or not right_cap.isOpened():
            return WRIST_DIFF_MIN_THRESHOLD

        left_total = int(left_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        right_total = int(right_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        total_frames = min(left_total, right_total)
        if total_frames <= 0:
            return WRIST_DIFF_MIN_THRESHOLD

        sample_positions = list(range(0, total_frames, sample_interval))
        if len(sample_positions) > ADAPTIVE_THRESHOLD_MAX_SAMPLES:
            step = (len(sample_positions) - 1) / (ADAPTIVE_THRESHOLD_MAX_SAMPLES - 1)
            sample_positions = sorted(
                {sample_positions[int(round(index * step))] for index in range(ADAPTIVE_THRESHOLD_MAX_SAMPLES)}
            )

        prev_left = None
        prev_right = None
        diffs = []
        for frame_index in sample_positions:
            left_frame = _read_frame(left_cap, frame_index)
            right_frame = _read_frame(right_cap, frame_index)
            if left_frame is None or right_frame is None:
                continue

            if prev_left is not None and prev_right is not None:
                left_diff = _frame_diff(prev_left, left_frame)
                right_diff = _frame_diff(prev_right, right_frame)
                diffs.append(max(left_diff, right_diff))

            prev_left = left_frame
            prev_right = right_frame

        if not diffs:
            return WRIST_DIFF_MIN_THRESHOLD

        sorted_diffs = sorted(diffs)
        p25_idx = len(sorted_diffs) // 4
        p25 = sorted_diffs[p25_idx]
        return max(p25 * 0.5, WRIST_DIFF_MIN_THRESHOLD)
    finally:
        left_cap.release()
        right_cap.release()


def _is_idle_window(still_window: list[bool]) -> bool:
    if len(still_window) < STILLNESS_WINDOW:
        return False
    return sum(1 for is_still in still_window if is_still) / len(still_window) >= (
        STILLNESS_MIN_RATIO
    )


def _find_head_trim_point(
    left_video: Path,
    right_video: Path,
    sample_interval: int,
    diff_threshold: float,
    min_idle_frames: int,
) -> tuple[int | None, float | None]:
    """Scan forward from frame 0 to find where motion starts.

    Returns (trim_frame, trim_ms) or (None, None) if no idle period found.
    trim_frame is the first frame where both cameras are moving.
    """
    import cv2

    left_cap = cv2.VideoCapture(str(left_video))
    right_cap = cv2.VideoCapture(str(right_video))
    try:
        if not left_cap.isOpened() or not right_cap.isOpened():
            return None, None

        left_total = int(left_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        right_total = int(right_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        total_frames = min(left_total, right_total)
        if total_frames <= 0:
            return None, None

        max_frame = max(0, int(total_frames * 0.2))
        prev_left = None
        prev_right = None
        idle_start = None
        still_window = []

        for frame_index in range(0, max_frame + 1, sample_interval):
            left_frame = _read_frame(left_cap, frame_index)
            right_frame = _read_frame(right_cap, frame_index)
            if left_frame is None or right_frame is None:
                continue

            if prev_left is None or prev_right is None:
                prev_left = left_frame
                prev_right = right_frame
                continue

            left_diff = _frame_diff(prev_left, left_frame)
            right_diff = _frame_diff(prev_right, right_frame)
            combined_diff = max(left_diff, right_diff)
            is_still = combined_diff < diff_threshold
            still_window.append(is_still)
            if len(still_window) > STILLNESS_WINDOW:
                still_window.pop(0)

            if _is_idle_window(still_window):
                if idle_start is None:
                    idle_start = frame_index - (sample_interval * len(still_window))
            elif idle_start is not None:
                idle_frames = frame_index - idle_start
                if idle_frames >= min_idle_frames:
                    trim_ms = _get_timestamp_ms(
                        left_video.parent / "timestamps.csv", frame_index
                    )
                    return frame_index, trim_ms
                return None, None
            elif len(still_window) == STILLNESS_WINDOW:
                return None, None

            prev_left = left_frame
            prev_right = right_frame

        return None, None
    finally:
        left_cap.release()
        right_cap.release()


def _find_tail_trim_point(
    left_video: Path,
    right_video: Path,
    total_frames: int,
    sample_interval: int,
    diff_threshold: float,
    min_idle_frames: int,
) -> tuple[int | None, float | None]:
    """Scan backward from last frame to find where motion ends.

    Returns (trim_frame, trim_ms) or (None, None) if no idle period found.
    trim_frame is the last frame where both cameras are still moving.
    """
    import cv2

    left_cap = cv2.VideoCapture(str(left_video))
    right_cap = cv2.VideoCapture(str(right_video))
    try:
        if not left_cap.isOpened() or not right_cap.isOpened():
            return None, None
        if total_frames <= 0:
            return None, None

        last_sample = ((total_frames - 1) // sample_interval) * sample_interval
        min_frame = min(
            int(total_frames * 0.8),
            max(0, last_sample - min_idle_frames - (2 * sample_interval)),
        )
        prev_left = None
        prev_right = None
        idle_start = None
        still_window = []

        for frame_index in range(last_sample, min_frame - 1, -sample_interval):
            left_frame = _read_frame(left_cap, frame_index)
            right_frame = _read_frame(right_cap, frame_index)
            if left_frame is None or right_frame is None:
                continue

            if prev_left is None or prev_right is None:
                prev_left = left_frame
                prev_right = right_frame
                continue

            left_diff = _frame_diff(prev_left, left_frame)
            right_diff = _frame_diff(prev_right, right_frame)
            combined_diff = max(left_diff, right_diff)
            is_still = combined_diff < diff_threshold
            still_window.append(is_still)
            if len(still_window) > STILLNESS_WINDOW:
                still_window.pop(0)

            if _is_idle_window(still_window):
                if idle_start is None:
                    idle_start = frame_index + (sample_interval * len(still_window))
            elif idle_start is not None:
                idle_frames = idle_start - frame_index
                if idle_frames >= min_idle_frames:
                    trim_frame = min(total_frames - 1, frame_index + sample_interval)
                    trim_ms = _get_timestamp_ms(
                        left_video.parent / "timestamps.csv", trim_frame
                    )
                    return trim_frame, trim_ms
                return None, None
            elif len(still_window) == STILLNESS_WINDOW:
                return None, None

            prev_left = left_frame
            prev_right = right_frame

        return None, None
    finally:
        left_cap.release()
        right_cap.release()


def detect_head_tail_stillness(episode_path: Path) -> dict:
    """Detect idle periods at head and tail of episode using wrist cameras.

    Returns dict with:
        has_head_idle: bool
        trim_start_ms: float | None  (None if no head idle detected)
        trim_start_frame: int | None
        has_tail_idle: bool
        trim_end_ms: float | None    (None if no tail idle detected)
        trim_end_frame: int | None
        checked: bool  (False if wrist cameras not found)
    """
    import cv2

    empty_result = {
        "has_head_idle": False,
        "trim_start_ms": None,
        "trim_start_frame": None,
        "has_tail_idle": False,
        "trim_end_ms": None,
        "trim_end_frame": None,
        "checked": False,
    }

    left_video = episode_path / "observation.image.left_wrist_view" / "video.mp4"
    right_video = episode_path / "observation.image.right_wrist_view" / "video.mp4"
    if not left_video.is_file() or not right_video.is_file():
        return empty_result

    left_cap = cv2.VideoCapture(str(left_video))
    right_cap = cv2.VideoCapture(str(right_video))
    try:
        if not left_cap.isOpened() or not right_cap.isOpened():
            return empty_result

        left_total = int(left_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        right_total = int(right_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        total_frames = min(left_total, right_total)
        fps = left_cap.get(cv2.CAP_PROP_FPS) or right_cap.get(cv2.CAP_PROP_FPS) or 30.0
    finally:
        left_cap.release()
        right_cap.release()

    if total_frames <= 0:
        return empty_result

    min_idle_frames = int(fps * STILLNESS_MIN_SECONDS)
    adaptive_threshold = _compute_adaptive_threshold(
        left_video, right_video, STILLNESS_SAMPLE_INTERVAL
    )
    trim_start_frame, trim_start_ms = _find_head_trim_point(
        left_video,
        right_video,
        STILLNESS_SAMPLE_INTERVAL,
        adaptive_threshold,
        min_idle_frames,
    )
    trim_end_frame, trim_end_ms = _find_tail_trim_point(
        left_video,
        right_video,
        total_frames,
        STILLNESS_SAMPLE_INTERVAL,
        adaptive_threshold,
        min_idle_frames,
    )

    return {
        "has_head_idle": trim_start_frame is not None,
        "trim_start_ms": trim_start_ms,
        "trim_start_frame": trim_start_frame,
        "has_tail_idle": trim_end_frame is not None,
        "trim_end_ms": trim_end_ms,
        "trim_end_frame": trim_end_frame,
        "checked": True,
    }


def _process_episode_worker(episode_path_str: str) -> tuple[str, dict, dict]:
    """Worker function for multiprocessing. Must be module-level for pickling.

    Returns (episode_path_str, alignment_result, stillness_result)
    """
    episode_path = Path(episode_path_str)
    alignment = analyze_episode(episode_path)
    stillness = detect_head_tail_stillness(episode_path)
    return episode_path_str, alignment, stillness


def _copy_trimmed_csv(source_path: Path, output_path: Path, target_count: int) -> None:
    with source_path.open("r", newline="") as source_file:
        reader = csv.reader(source_file)
        with output_path.open("w", newline="") as output_file:
            writer = csv.writer(output_file)
            try:
                writer.writerow(next(reader))
            except StopIteration:
                return
            for index, row in enumerate(reader):
                if index >= target_count:
                    break
                writer.writerow(row)


def trim_episode(
    episode_path: Path, target_count: int, dry_run: bool = True
) -> list[str]:
    """Trim all modality files to target_count rows.

    Returns list of files written (or would be written in dry_run mode).
    Writes *_trimmed.csv files alongside originals.
    Never overwrites original files.
    """
    output_paths = []

    for modality in _iter_modalities(episode_path):
        modality_path = episode_path / modality
        if _is_image_modality(modality):
            source_path = modality_path / "timestamps.csv"
            if not source_path.is_file():
                source_path = modality_path / "timestamps_raw.csv"
            output_path = modality_path / "timestamps_trimmed.csv"
        elif _is_table_modality(modality):
            source_path = modality_path / "data.csv"
            output_path = modality_path / "data_trimmed.csv"
        else:
            continue

        if not source_path.is_file():
            continue

        output_paths.append(str(output_path))
        if not dry_run:
            _copy_trimmed_csv(source_path, output_path, target_count)

    return output_paths


def discover_episodes(roots: list[Path], max_episodes: int | None = None) -> list[Path]:
    episodes = []
    seen = set()

    for root in roots:
        if not root.exists():
            print(f"Warning: root does not exist: {root}")
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

    episodes = sorted(episodes)
    if max_episodes is not None:
        return episodes[:max_episodes]
    return episodes


def _worst_modality(analysis: dict) -> tuple[str, int]:
    modality_counts = analysis["modality_counts"]
    if not modality_counts:
        return "", 0

    target = analysis["target_count"]
    modality, count = max(
        sorted(modality_counts.items()),
        key=lambda item: abs(item[1] - target),
    )
    return modality, count


def write_report(output_path: Path, analyses: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "episode_path",
        "status",
        "spread",
        "min_count",
        "max_count",
        "target_count",
        "file_strategy",
        "worst_modality",
        "worst_count",
        "trim_start_ms",
        "trim_end_ms",
        "reason",
    ]

    with output_path.open("w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for analysis in analyses:
            worst_modality, worst_count = _worst_modality(analysis)
            writer.writerow(
                {
                    "episode_path": analysis["episode_path"],
                    "status": analysis["status"],
                    "spread": analysis["spread"],
                    "min_count": analysis["min_count"],
                    "max_count": analysis["max_count"],
                    "target_count": analysis["target_count"],
                    "file_strategy": analysis["file_strategy"],
                    "worst_modality": worst_modality,
                    "worst_count": worst_count,
                    "trim_start_ms": analysis.get("trim_start_ms") or "",
                    "trim_end_ms": analysis.get("trim_end_ms") or "",
                    "reason": analysis["reason"],
                }
            )


def _print_progress(current: int, total: int, width: int = 40) -> None:
    filled = int(width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r  [{bar}] {current}/{total}", end="", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check frame count alignment across episode modalities."
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        required=True,
        type=Path,
        help="One or more task or episode directories.",
    )
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=None,
        help="Optional episode limit for testing.",
    )
    parser.add_argument(
        "--trim",
        action="store_true",
        help="Enable trimming of needs_trim episodes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be trimmed without writing files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/frame_alignment_report.csv"),
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
    args = parse_args(argv)
    start_time = time.time()

    episodes = discover_episodes(args.roots, args.max_episodes)
    analyses = []
    trim_outputs = []
    should_trim = args.trim or args.dry_run
    workers = args.workers

    if workers > 1:
        with Pool(processes=workers) as pool:
            for index, (episode_path_str, analysis, stillness) in enumerate(
                pool.imap_unordered(
                    _process_episode_worker,
                    [str(episode_path) for episode_path in episodes],
                )
            ):
                analysis.update(stillness)
                analyses.append(analysis)

                if should_trim and analysis["status"] == "needs_trim":
                    written = trim_episode(
                        Path(episode_path_str),
                        analysis["target_count"],
                        dry_run=not args.trim or args.dry_run,
                    )
                    trim_outputs.extend(written)
                    if args.dry_run:
                        for path in written:
                            print(f"\nWould write: {path}")

                _print_progress(index + 1, len(episodes))
    else:
        for index, episode_path in enumerate(episodes):
            analysis = analyze_episode(episode_path)
            if analysis["status"] != "fail":
                stillness = detect_head_tail_stillness(episode_path)
            else:
                stillness = {
                    "has_head_idle": False,
                    "trim_start_ms": None,
                    "trim_start_frame": None,
                    "has_tail_idle": False,
                    "trim_end_ms": None,
                    "trim_end_frame": None,
                    "checked": False,
                }
            analysis.update(stillness)
            analyses.append(analysis)

            if should_trim and analysis["status"] == "needs_trim":
                written = trim_episode(
                    episode_path,
                    analysis["target_count"],
                    dry_run=not args.trim or args.dry_run,
                )
                trim_outputs.extend(written)
                if args.dry_run:
                    for path in written:
                        print(f"\nWould write: {path}")

            _print_progress(index + 1, len(episodes))

    print()
    write_report(args.output, analyses)

    elapsed = time.time() - start_time
    pass_count = sum(1 for analysis in analyses if analysis["status"] == "pass")
    needs_trim_count = sum(
        1 for analysis in analyses if analysis["status"] == "needs_trim"
    )
    fail_count = sum(1 for analysis in analyses if analysis["status"] == "fail")
    head_idle_count = sum(1 for analysis in analyses if analysis["has_head_idle"])
    tail_idle_count = sum(1 for analysis in analyses if analysis["has_tail_idle"])
    files_written = 0 if args.dry_run or not args.trim else len(trim_outputs)

    print(f"Episodes checked: {len(episodes)}")
    print(f"Pass:        {pass_count}")
    print(f"Needs trim:  {needs_trim_count}")
    print(f"Fail:        {fail_count}")
    print()
    print(
        f"Head idle detected: {head_idle_count}  "
        "(suggested trim_start_ms available)"
    )
    print(
        f"Tail idle detected: {tail_idle_count}  "
        "(suggested trim_end_ms available)"
    )
    if args.dry_run:
        print(f"Files written: 0  (dry run; would write {len(trim_outputs)})")
    elif args.trim:
        print(f"Files written: {files_written}")
    else:
        print("Files written: 0  (use --trim to enable trimming)")
    print(f"Time elapsed: {elapsed:.1f}s")
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
