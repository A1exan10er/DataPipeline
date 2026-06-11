"""Phase 4 video health checks."""

from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable
import csv

from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    PipelineConfigurationError,
    decide_status,
    is_positive_number,
    load_metadata,
    save_episode_state,
    save_findings,
)

try:
    import cv2

    CV2_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    cv2 = None
    CV2_IMPORT_ERROR = exc


PHASE_NUMBER = 4
SAMPLE_POSITIONS = [0.0, 0.15, 0.30, 0.45, 0.60, 0.75, 0.90, 1.0]
ARX_WRIST_VIEW_CAMERAS = [
    "observation.image.left_wrist_view",
    "observation.image.right_wrist_view",
]
WRIST_VIEW_DIFF_THRESHOLD = 5.0
WRIST_VIEW_STILL_RATIO = 0.80


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run Phase 4 video health checks for each unfinished episode."""
    validate_dependencies()
    if workers > 1:
        return _run_phase_parallel(states, db_path, progress_callback, workers)
    processed_count = 0
    total_count = len(states)
    for state in states:
        if PHASE_NUMBER in state.phases_completed:
            processed_count += 1
            if progress_callback:
                progress_callback(processed_count, total_count)
            continue
        _ensure_metadata(state)
        new_findings = _episode_findings(state)
        _finish_state(state, db_path, new_findings)
        processed_count += 1
        if progress_callback:
            progress_callback(processed_count, total_count)
    return states


def _process_episode_worker(
    args: tuple[str, str],
) -> tuple[str, list[dict], dict]:
    """Worker function for multiprocessing. Must be module-level for pickling.

    Args:
        args: Tuple of (episode_path_str, robot_str)

    Returns:
        Tuple of (episode_path_str, findings_as_dicts, metrics_dict)
        findings_as_dicts: list of Finding fields as plain dicts.
    """
    episode_path_str, robot = args
    episode_path = Path(episode_path_str)
    findings = []
    metrics = {}

    validate_dependencies()
    state = EpisodeState(
        episode_path=episode_path,
        task="",
        date="",
        operator="",
        robot=robot,
        controller="",
    )
    frames_by_modality = {}
    video_modalities = _discover_video_modalities(episode_path)
    for modality in video_modalities:
        video_path = episode_path / modality / "video.mp4"
        mod_findings, mod_metrics, frames = _check_video(episode_path, modality, video_path, robot)
        findings.extend(mod_findings)
        metrics.update(mod_metrics)
        frames_by_modality[modality] = frames
    findings.extend(_check_arx_wrist_consistency(state, frames_by_modality))

    findings_dicts = [
        {
            "episode_path": f.episode_path,
            "phase": f.phase,
            "check_name": f.check_name,
            "severity": f.severity,
            "status": f.status,
            "message": f.message,
            "details": f.details,
        }
        for f in findings
    ]
    return episode_path_str, findings_dicts, metrics


def _run_phase_parallel(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None,
    workers: int,
) -> list[EpisodeState]:
    pending = _pending_states(states)
    completed = len(states) - len(pending)
    total = len(states)
    if progress_callback and completed:
        progress_callback(completed, total)
    if not pending:
        return states
    args = [(str(state.episode_path), state.robot) for state in pending]
    states_by_path = {str(state.episode_path): state for state in pending}
    with Pool(processes=workers) as pool:
        for index, (episode_path_str, findings_dicts, metrics) in enumerate(
            pool.imap_unordered(_process_episode_worker, args)
        ):
            state = states_by_path[episode_path_str]
            new_findings = [Finding(**item) for item in findings_dicts]
            state.metrics.update(metrics)
            _finish_state(state, db_path, new_findings)
            if progress_callback:
                progress_callback(completed + index + 1, total)
    return states


def _pending_states(states: list[EpisodeState]) -> list[EpisodeState]:
    return [
        state
        for state in states
        if PHASE_NUMBER not in state.phases_completed
    ]


def _ensure_metadata(state: EpisodeState) -> None:
    if state.metadata:
        return
    metadata, findings = load_metadata(state.episode_path)
    if not findings:
        state.metadata = metadata


def validate_dependencies() -> None:
    """Fail the pipeline before writing episode QA results if OpenCV is missing."""
    if CV2_IMPORT_ERROR is None:
        return
    raise PipelineConfigurationError(
        "Phase 4 video health checks require opencv-python-headless. "
        "Install it in the active environment, for example: "
        "datapipeline-env/bin/python -m pip install opencv-python-headless"
    ) from CV2_IMPORT_ERROR


def _episode_findings(state: EpisodeState) -> list[Finding]:
    findings = []
    frames_by_modality = {}
    for modality in _discover_video_modalities(state.episode_path):
        video_path = state.episode_path / modality / "video.mp4"
        mod_findings, mod_metrics, frames = _check_video(state.episode_path, modality, video_path, state.robot)
        findings.extend(mod_findings)
        state.metrics.update(mod_metrics)
        frames_by_modality[modality] = frames
    findings.extend(_check_arx_wrist_consistency(state, frames_by_modality))
    return findings


def _check_video(
    episode_path: Path,
    modality: str,
    video_path: Path,
    robot: str,
) -> tuple[list[Finding], dict, list]:
    """Run all video checks for one modality. Returns (findings, metrics)."""
    metrics = {}
    cap = cv2.VideoCapture(str(video_path))
    try:
        openable_findings = _check_video_openable(episode_path, modality, video_path, cap)
        if openable_findings:
            metrics.update(_unopenable_metrics(modality))
            return openable_findings, metrics, []

        properties = _video_properties(cap)
        metrics.update(_video_metrics(modality, properties))
        findings = _property_findings(episode_path, modality, properties)
        sample = _sample_frames(cap, properties["frame_count"])
        metrics.update(_sample_metrics(modality, sample))
        findings.extend(_check_sampled_frames(episode_path, modality, sample))
        return findings, metrics, sample["frames"]
    finally:
        cap.release()


def _check_video_openable(
    episode_path: Path, modality: str, video_path: Path, cap: Any
) -> list[Finding]:
    if cap.isOpened():
        return []
    return [
        _finding(
            episode_path,
            "video_not_openable",
            "critical",
            "fail",
            "video.mp4 could not be opened.",
            {"modality": modality, "file": str(video_path)},
        )
    ]


def _property_findings(episode_path: Path, modality: str, properties: dict[str, Any]) -> list[Finding]:
    findings = []
    findings.extend(_check_video_frame_count(episode_path, modality, properties["frame_count"]))
    findings.extend(_check_video_duration(episode_path, modality, properties["frame_count"], properties["fps"]))
    findings.extend(_check_video_resolution(episode_path, modality, properties["width"], properties["height"]))
    return findings


def _check_video_frame_count(
    episode_path: Path, modality: str, frame_count: int
) -> list[Finding]:
    if frame_count <= 0:
        return [
            _finding(
                episode_path,
                "video_frame_count_unreadable",
                "major",
                "fail",
                "Video frame count could not be read.",
                {"modality": modality},
            )
        ]

    metadata_frames = _metadata_total_frames(episode_path)
    if metadata_frames is None:
        return []
    error_ratio = abs(frame_count - metadata_frames) / metadata_frames
    if error_ratio <= 0.10:
        return []
    return [
        _finding(
            episode_path,
            "video_frame_count_mismatch",
            "major",
            "fail",
            "Video frame count differs from metadata total_frames.",
            {
                "modality": modality,
                "video_frames": frame_count,
                "metadata_frames": metadata_frames,
                "error_ratio": error_ratio,
            },
        )
    ]


def _check_video_duration(
    episode_path: Path, modality: str, frame_count: int, fps: float
) -> list[Finding]:
    metadata_duration = _metadata_duration(episode_path)
    if frame_count <= 0 or fps <= 0 or metadata_duration is None:
        return []
    video_duration = frame_count / fps
    error_ratio = abs(video_duration - metadata_duration) / metadata_duration
    if error_ratio <= 0.10:
        return []
    return [
        _finding(
            episode_path,
            "video_duration_mismatch",
            "minor",
            "warning",
            "Video duration differs from metadata duration_seconds.",
            {
                "modality": modality,
                "video_duration_s": video_duration,
                "metadata_duration_s": metadata_duration,
                "error_ratio": error_ratio,
            },
        )
    ]


def _check_video_resolution(
    episode_path: Path, modality: str, width: int, height: int
) -> list[Finding]:
    expected = _expected_resolution(episode_path, modality)
    if expected is None or width <= 0 or height <= 0:
        return []
    expected_width, expected_height = expected
    if width == expected_width and height == expected_height:
        return []
    # If width matches and actual height is larger, assume letterboxing padding.
    # This is expected when the capture platform adds black bars to fit a fixed
    # aspect ratio container (e.g. 640x360 content stored in 640x480 container).
    if width == expected_width and height >= expected_height:
        return []
    return [
        _finding(
            episode_path,
            "video_resolution_mismatch",
            "minor",
            "warning",
            "Video resolution differs from expected camera resolution.",
            {
                "modality": modality,
                "actual_width": width,
                "actual_height": height,
                "expected_width": expected_width,
                "expected_height": expected_height,
            },
        )
    ]


def _check_sampled_frames(
    episode_path: Path, modality: str, sample: dict[str, Any]
) -> list[Finding]:
    sampled_count = sample["sampled_count"]
    if sampled_count == 0:
        return []
    findings = _black_white_findings(episode_path, modality, sample)
    if _is_frozen_sample(sample):
        findings.append(
            _finding(
                episode_path,
                "video_frozen",
                "major",
                "fail",
                "Sampled frames appear frozen.",
                {
                    "modality": modality,
                    "sampled_count": sampled_count,
                    "max_frame_diff": sample["max_frame_diff"],
                },
            )
        )
    return findings


def _black_white_findings(
    episode_path: Path, modality: str, sample: dict[str, Any]
) -> list[Finding]:
    findings = []
    sampled_count = sample["sampled_count"]
    bad_count = sample["black_frame_count"] + sample["white_frame_count"]
    severity, status = _bad_frame_status(bad_count, sampled_count)
    common = {
        "modality": modality,
        "sampled_count": sampled_count,
        "combined_bad_frame_count": bad_count,
        "bad_frame_ratio": bad_count / sampled_count,
    }
    if sample["black_frame_count"] > 0:
        details = common | {"black_frame_count": sample["black_frame_count"]}
        findings.append(_finding(episode_path, "video_black_frames", severity, status, "Black sampled frames were found.", details))
    if sample["white_frame_count"] > 0:
        details = common | {"white_frame_count": sample["white_frame_count"]}
        findings.append(_finding(episode_path, "video_white_frames", severity, status, "White sampled frames were found.", details))
    return findings


def _bad_frame_status(bad_count: int, sampled_count: int) -> tuple[str, str]:
    if bad_count > sampled_count / 2:
        return "critical", "fail"
    return "major", "needs_review"


def _is_frozen_sample(sample: dict[str, Any]) -> bool:
    diff_values = sample["diff_values"]
    return bool(diff_values) and all(diff < 1.0 for diff in diff_values)


def _check_arx_wrist_consistency(
    state: EpisodeState,
    frames_by_modality: dict[str, list],
) -> list[Finding]:
    """Check that both ARX wrist_view cameras are not simultaneously still.

    Only checks left_wrist_view and right_wrist_view. Tactile cameras are excluded
    because their natural diff range is too small to distinguish motion reliably.
    Only runs for arx5 robot.
    """
    if (state.robot or "").lower() != "arx5":
        return []

    present = {
        camera: frames_by_modality[camera]
        for camera in ARX_WRIST_VIEW_CAMERAS
        if camera in frames_by_modality and frames_by_modality[camera]
    }
    if len(present) < 2:
        return []

    still_map = {
        camera: _is_camera_still(frames, WRIST_VIEW_DIFF_THRESHOLD, WRIST_VIEW_STILL_RATIO)
        for camera, frames in present.items()
    }

    if all(still_map.values()):
        return [
            _finding(
                state,
                "both_wrist_views_still",
                "major",
                "fail",
                "Both ARX wrist_view cameras appear completely still. Possible operator idle.",
                {"cameras": list(still_map.keys())},
            )
        ]
    return []


def _is_camera_still(frames: list, threshold: float, still_ratio: float) -> bool:
    """Determine if a camera is still based on sampled frames."""
    if len(frames) < 2:
        return False
    pairs = list(zip(frames, frames[1:]))
    still_count = sum(
        1
        for first, second in pairs
        if _mean_frame_diff(first, second) < threshold
    )
    return still_count / len(pairs) > still_ratio


def _mean_frame_diff(first: Any, second: Any) -> float:
    """Compute mean absolute pixel difference between two grayscale frames."""
    import cv2
    import numpy as np

    diff = cv2.absdiff(first, second)
    return float(np.mean(diff))


def _sample_frames(cap: Any, frame_count: int) -> dict[str, Any]:
    brightness_values = []
    diff_values = []
    frames = []
    black_count = 0
    white_count = 0
    previous_gray = None

    for position in _sample_positions(frame_count):
        ok, frame = _read_frame_at(cap, position)
        if not ok or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(cv2.mean(gray)[0])
        brightness_values.append(brightness)
        frames.append(gray)
        black_count += int(brightness < 5.0)
        white_count += int(brightness > 250.0)
        if previous_gray is not None:
            diff_values.append(float(cv2.mean(cv2.absdiff(previous_gray, gray))[0]))
        previous_gray = gray

    return _sample_summary(brightness_values, diff_values, black_count, white_count, frames)


def _read_frame_at(cap: Any, position: int) -> tuple[bool, Any]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, position)
    return cap.read()


def _sample_positions(frame_count: int) -> list[int]:
    if frame_count <= 0:
        return []
    if frame_count <= 8:
        return list(range(frame_count))
    last = frame_count - 1
    return sorted({int(last * ratio) for ratio in SAMPLE_POSITIONS})


def _sample_summary(
    brightness_values: list[float],
    diff_values: list[float],
    black_count: int,
    white_count: int,
    frames: list,
) -> dict[str, Any]:
    sampled_count = len(brightness_values)
    return {
        "sampled_count": sampled_count,
        "black_frame_count": black_count,
        "white_frame_count": white_count,
        "diff_values": diff_values,
        "frames": frames,
        "mean_brightness": sum(brightness_values) / sampled_count if sampled_count else 0.0,
        "min_frame_diff": min(diff_values) if diff_values else 0.0,
        "max_frame_diff": max(diff_values) if diff_values else 0.0,
    }


def _discover_video_modalities(episode_path: Path) -> list[str]:
    """Return image modality names that have a video.mp4 file to check."""
    metadata = _episode_metadata(episode_path)
    modalities = set(_metadata_modalities(metadata))
    try:
        modalities.update(child.name for child in episode_path.iterdir() if child.is_dir())
    except OSError:
        pass
    return [
        modality
        for modality in sorted(modalities)
        if _is_image_modality(modality) and (episode_path / modality / "video.mp4").is_file()
    ]


def _metadata_modalities(metadata: dict) -> list[str]:
    modalities = metadata.get("modalities")
    return list(modalities) if isinstance(modalities, dict) else []


def _is_image_modality(modality: str) -> bool:
    return modality.startswith("observation.image.") and not modality.startswith("observation.image.flow_")


def _video_properties(cap: Any) -> dict[str, Any]:
    frame_count = _positive_int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps = _positive_float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
    width = _positive_int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
    height = _positive_int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
    return {"frame_count": frame_count, "fps": fps, "width": width, "height": height}


def _metadata_total_frames(episode_path: Path) -> int | None:
    return _positive_int(_episode_metadata(episode_path).get("total_frames"))


def _metadata_duration(episode_path: Path) -> float | None:
    return _positive_float(_episode_metadata(episode_path).get("duration_seconds"))


def _expected_resolution(episode_path: Path, modality: str) -> tuple[int, int] | None:
    metadata_resolution = _metadata_resolution(_episode_metadata(episode_path), modality)
    if metadata_resolution is not None:
        return metadata_resolution
    return _config_resolution(episode_path / modality / "config.csv")


def _episode_metadata(episode_path: Path) -> dict:
    metadata, findings = load_metadata(episode_path)
    return {} if findings else metadata


def _metadata_resolution(metadata: dict, modality: str) -> tuple[int, int] | None:
    cameras = metadata.get("cameras")
    if not isinstance(cameras, dict):
        return None
    camera = cameras.get(modality)
    if not isinstance(camera, dict):
        return None
    return _resolution_from_mapping(camera)


def _config_resolution(path: Path) -> tuple[int, int] | None:
    if not path.is_file():
        return None
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle), None)
    except (OSError, csv.Error):
        return None
    return _resolution_from_mapping(row or {})


def _resolution_from_mapping(values: dict[str, Any]) -> tuple[int, int] | None:
    for width_key, height_key in _resolution_key_pairs():
        width = _positive_int(values.get(width_key))
        height = _positive_int(values.get(height_key))
        if width is not None and height is not None:
            return width, height
    return None


def _resolution_key_pairs() -> tuple[tuple[str, str], ...]:
    return (
        ("width", "height"),
        ("actual_width", "actual_height"),
        ("configured_width", "configured_height"),
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> float | None:
    if is_positive_number(value):
        return float(value)
    return None


def _unopenable_metrics(modality: str) -> dict:
    key = _modality_key(modality)
    return {f"p4_{key}_openable": False}


def _video_metrics(modality: str, properties: dict[str, Any]) -> dict:
    key = _modality_key(modality)
    frame_count = properties["frame_count"]
    fps = properties["fps"]
    return {
        f"p4_{key}_openable": True,
        f"p4_{key}_video_frames": frame_count,
        f"p4_{key}_video_fps": fps,
        f"p4_{key}_video_duration_s": frame_count / fps if fps > 0 else 0.0,
        f"p4_{key}_width": properties["width"],
        f"p4_{key}_height": properties["height"],
    }


def _sample_metrics(modality: str, sample: dict[str, Any]) -> dict:
    key = _modality_key(modality)
    return {
        f"p4_{key}_mean_brightness": sample["mean_brightness"],
        f"p4_{key}_min_frame_diff": sample["min_frame_diff"],
    }


def _modality_key(modality: str) -> str:
    return modality.replace(".", "_")


def _finish_state(state: EpisodeState, db_path: Path, new_findings: list[Finding]) -> None:
    if PHASE_NUMBER not in state.phases_completed:
        state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(new_findings)
    state.findings.extend(new_findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, new_findings, phase=PHASE_NUMBER, episode_path=str(state.episode_path))


def _finding(
    episode_path: Path | EpisodeState,
    check_name: str,
    severity: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> Finding:
    path = episode_path.episode_path if isinstance(episode_path, EpisodeState) else episode_path
    return Finding(
        episode_path=str(path),
        phase=PHASE_NUMBER,
        check_name=check_name,
        severity=severity,
        status=status,
        message=message,
        details=details or {},
    )
