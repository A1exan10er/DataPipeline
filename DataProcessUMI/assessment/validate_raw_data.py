import argparse
import csv
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


GRIPPER_DIR = "observation.state.gripper"
RAW_GRIPPER_ROTATION_DIR = "observation.state.raw_gripper_rotation"
IMAGE_PREFIX = "observation.image."
SIDES = ("left", "right")

# Image streams subjected to the out-of-focus (blur) check. Only the wrist
# cameras are checked; tactile and other streams are skipped.
FOCUS_VIEWS = (
    "observation.image.left_wrist_view",
    "observation.image.right_wrist_view",
)

# Action / end-effector pose data and the x,y,z columns whose absolute value
# is range-checked.
ACTION_DIR = "actions.eef_pose"
ACTION_XYZ_COLUMNS = ("left_x", "left_y", "left_z", "right_x", "right_y", "right_z")

# Per-side wrist_view streams, used by the motion-consistency cross-check.
WRIST_VIEW = {
    "left": "observation.image.left_wrist_view",
    "right": "observation.image.right_wrist_view",
}

REPORT_SCHEMA_VERSION = 1
EPISODE_REPORT_SUFFIX = ".validation.json"
CLASS_SUMMARY_NAME = "summary.validation.json"

VALIDATE_CONFIG_PATH = Path(__file__).with_name("validate_raw_data_config.json")
DEFAULT_VALIDATE_CONFIG = {
    "video": {
        "max_duplicate_frame_proportion": 0.1,
        "max_near_duplicate_frame": 10,
    },
    # Out-of-focus detection on the wrist views. A defocused video lacks
    # high-frequency detail, so the median variance of the Laplacian over
    # sampled frames collapses well below the in-focus range. Calibrated on
    # 256x256 grayscale frames: in-focus ~680-880, defocused ~70-200.
    "focus": {
        "lap_var_threshold": 350.0,
        "num_sample_frames": 20,
    },
    # Cross-class label check (wrist_view <-> tactile swap) from the first
    # frame. A stream whose median first-frame HSV-histogram correlation to
    # the tactile-named streams is >= this looks like tactile; below, view.
    "label": {
        "tactile_affinity_threshold": 0.40,
    },
    # Action / eef-pose quality. End-effector x,y,z whose absolute value
    # exceeds this (metres) is an implausible outlier.
    "action": {
        "xyz_abs_threshold": 1.5,
    },
    # Motion-consistency cross-check. The wrist_view video is ground truth for
    # whether an arm physically moves: a truly static arm's wrist video is
    # near-frozen (~0.1) while even a lightly-moving arm reads ~8+, so a video
    # below video_static_threshold means the device really is static. For such
    # a static side the action MUST stay still the whole run -- both small path
    # (catches large jumps) and small extent (peak-to-peak; catches a slow
    # drift away from the origin). When one wrist_view is static:
    #   - that side's action moves AND the other side's action also moves
    #       -> drift (fluctuation on static device) on the static side
    #   - that side's action moves but the other side's is still
    #       -> the two wrist_view videos are left/right swapped
    #   - that side's action is also still -> consistent
    "motion": {
        "video_static_threshold": 4.0,
        "action_path_active": 5.0,
        "action_extent_active": 0.3,
        "num_sample_frames": 30,
    },
}

# Sections of DEFAULT_VALIDATE_CONFIG that load_validate_config merges.
_CONFIG_SECTIONS = ("video", "focus", "label", "action", "motion")


def load_validate_config(path=None):
    target = Path(path) if path is not None else VALIDATE_CONFIG_PATH
    merged = {key: dict(DEFAULT_VALIDATE_CONFIG[key]) for key in _CONFIG_SECTIONS}
    if not target.exists():
        return merged
    with target.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        for key in _CONFIG_SECTIONS:
            if isinstance(loaded.get(key), dict):
                merged[key].update(loaded[key])
    return merged


class MissingDependencyError(RuntimeError):
    """A required third-party library (OpenCV/NumPy) is not installed."""


def _missing_dep_message(check_name, skip_flag, exc):
    return (
        f"The {check_name} check requires OpenCV and NumPy, which are not "
        f"installed ({exc}).\n"
        "Install them with:\n"
        "    pip install opencv-python numpy\n"
        f"Or re-run with {skip_flag} to skip this check."
    )


def _require_cv2_numpy(check_name, skip_flag):
    """Import cv2 and numpy, raising a clear install hint if unavailable."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        raise MissingDependencyError(
            _missing_dep_message(check_name, skip_flag, exc)
        ) from exc


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Validate one episode (or every episode in a class) for gripper-reading "
            "and video-frame integrity. Reports problem types and their locations."
        )
    )
    parser.add_argument(
        "input_path_arg",
        nargs="?",
        type=Path,
        help="Dataset class directory or a single episode_XXX directory.",
    )
    parser.add_argument("-i", "--input-path", type=Path, help="Class or episode directory.")
    parser.add_argument(
        "-o",
        "--output-root",
        type=Path,
        default=Path("outputs"),
        help=(
            "Output root for structured JSON reports. Each episode produces "
            "<output-root>/<class_name>/<episode_XXX>.validation.json, and the "
            "class produces <output-root>/<class_name>/summary.validation.json. "
            "Defaults to outputs."
        ),
    )
    parser.add_argument(
        "--no-reports",
        action="store_true",
        help="Skip writing per-episode and per-class JSON reports to --output-root.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        help="Optional extra path to also write the full validation report as JSON.",
    )
    parser.add_argument("--skip-gripper", action="store_true", help="Skip gripper validation.")
    parser.add_argument("--skip-video", action="store_true", help="Skip video validation.")
    parser.add_argument("--skip-action", action="store_true", help="Skip action/eef-pose validation.")
    parser.add_argument(
        "--skip-motion",
        action="store_true",
        help="Skip the action<->wrist_view motion-consistency cross-check "
             "(left/right view swap and static-device drift).",
    )
    parser.add_argument(
        "--skip-focus",
        action="store_true",
        help="Skip the out-of-focus (blur) check on the wrist views.",
    )
    parser.add_argument(
        "--skip-label",
        action="store_true",
        help="Skip the cross-class (wrist_view<->tactile) mislabel check.",
    )

    gripper = parser.add_argument_group("gripper checks")
    gripper.add_argument("--raw-static-deg", type=float, default=2.0,
                         help="Raw rotation range below this is static.")
    gripper.add_argument("--gripper-static-m", type=float, default=0.001,
                         help="Gripper range below this is static.")
    gripper.add_argument("--min-correlation", type=float, default=0.8,
                         help="Minimum |pearson| and |spearman| for dynamic data.")
    gripper.add_argument("--min-r2", type=float, default=0.8,
                         help="Minimum linear-fit R^2 for dynamic data.")
    gripper.add_argument("--min-gripper-m", type=float, default=-0.002,
                         help="Minimum plausible gripper distance, metres.")
    gripper.add_argument("--max-gripper-m", type=float, default=0.09,
                         help="Maximum plausible gripper distance, metres.")
    gripper.add_argument("--recompute-tolerance-m", type=float, default=0.003,
                         help="Median |error| tolerance when comparing against metadata calibration.")
    gripper.add_argument("--disable-metadata-check", action="store_true",
                         help="Skip the metadata-calibration recompute check.")

    video = parser.add_argument_group("video checks")
    video.add_argument("--fps", type=float, help="Expected FPS. Defaults to metadata fps_config or 30.")
    video.add_argument("--gap-tolerance-ratio", type=float, default=0.25,
                       help="Allowed timestamp gap tolerance as a fraction of one frame period.")
    video.add_argument(
        "--validate-config",
        type=Path,
        help=(
            "Optional JSON config with video-quality and focus thresholds "
            "(max_duplicate_frame_proportion, max_near_duplicate_frame, "
            "focus.lap_var_threshold, focus.num_sample_frames). "
            "Defaults to validate_raw_data_config.json next to this script."
        ),
    )

    focus = parser.add_argument_group("focus checks")
    focus.add_argument(
        "--lap-var-threshold",
        type=float,
        help="Laplacian-variance threshold; a wrist view scoring below this is "
             "flagged as defocused. Overrides the value from --validate-config.",
    )
    focus.add_argument(
        "--focus-frames",
        type=int,
        help="Number of frames sampled per wrist view for the focus check. "
             "Overrides the value from --validate-config.",
    )

    label = parser.add_argument_group("label checks")
    label.add_argument(
        "--label-threshold",
        type=float,
        help="Tactile-affinity threshold for the cross-class mislabel check; "
             ">= looks tactile, < looks view. Overrides --validate-config.",
    )

    action = parser.add_argument_group("action checks")
    action.add_argument(
        "--action-abs-threshold",
        type=float,
        help="End-effector x,y,z absolute value above which a sample is an "
             "outlier (metres). Overrides --validate-config.",
    )

    motion = parser.add_argument_group("motion-consistency checks")
    motion.add_argument(
        "--motion-video-static-threshold",
        type=float,
        help="A wrist_view whose frame-to-frame motion is below this is treated "
             "as physically static. Overrides --validate-config.",
    )

    return parser.parse_args(argv)


def apply_validate_config(args):
    """Resolve config-derived fields on a parsed ``args`` namespace.

    Mirrors the setup ``main`` does after :func:`parse_args` so the namespace is
    ready for :func:`validate_episode`. Exposed so callers that drive validation
    in-process (e.g. the data pipeline) can reuse the exact same configuration
    path instead of duplicating it.
    """
    validate_config = load_validate_config(args.validate_config)
    args.video_config = validate_config["video"]
    args.focus_config = dict(validate_config["focus"])
    if args.lap_var_threshold is not None:
        args.focus_config["lap_var_threshold"] = args.lap_var_threshold
    if args.focus_frames is not None:
        args.focus_config["num_sample_frames"] = args.focus_frames
    args.label_config = dict(validate_config["label"])
    if args.label_threshold is not None:
        args.label_config["tactile_affinity_threshold"] = args.label_threshold
    args.action_config = dict(validate_config["action"])
    if args.action_abs_threshold is not None:
        args.action_config["xyz_abs_threshold"] = args.action_abs_threshold
    args.motion_config = dict(validate_config["motion"])
    if args.motion_video_static_threshold is not None:
        args.motion_config["video_static_threshold"] = args.motion_video_static_threshold
    return args


def require_check_dependencies(args):
    """Fail fast (before touching any episode) when an enabled check needs
    OpenCV/NumPy but they are not installed."""
    if not args.skip_video and not args.skip_focus:
        _require_cv2_numpy("out-of-focus", "--skip-focus")
    if not args.skip_video and not args.skip_label:
        _require_cv2_numpy("cross-class mislabel", "--skip-label")
    if not args.skip_video and not args.skip_action and not args.skip_motion:
        _require_cv2_numpy("motion-consistency", "--skip-motion")


def _walk_episode_dirs(root):
    """Recursively collect ``episode_*`` directories anywhere under ``root``.

    Directories named ``episode_*`` are collected but not descended into (an
    episode's own subdirectories are data streams, never nested episodes).
    Results are returned grouped by directory and lexically ordered.
    """
    found = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("episode_"):
            found.append(entry)
        else:
            found.extend(_walk_episode_dirs(entry))
    return found


def find_episode_dirs(input_path):
    """Return a list of episode_* directories for ``input_path``.

    ``input_path`` may be:
      - an ``episode_XXX`` directory itself -> just that episode;
      - a class directory directly containing ``episode_XXX`` subdirectories
        -> those episodes (non-recursive, legacy behavior);
      - any other directory -> every ``episode_XXX`` found *recursively*
        beneath it (at any depth).

    Raises ``FileNotFoundError`` if the path does not exist and ``ValueError``
    if it is not a directory or no episodes can be found anywhere under it.
    """
    if not input_path.exists():
        raise FileNotFoundError(
            f"Input path does not exist: {input_path}\n"
            "Expected an episode_XXX directory, or a directory containing "
            "episode_XXX subdirectories (at any depth)."
        )
    if input_path.is_dir() and input_path.name.startswith("episode_"):
        return [input_path]
    if not input_path.is_dir():
        raise ValueError(
            f"Invalid input path: {input_path}\n"
            "Expected an episode_XXX directory or a directory containing "
            "episode_XXX subdirectories (at any depth)."
        )

    # A class directory: episodes sit directly inside it (legacy fast path).
    direct = sorted(
        path for path in input_path.iterdir()
        if path.is_dir() and path.name.startswith("episode_")
    )
    if direct:
        return direct

    # Otherwise search recursively for episodes nested at any depth.
    nested = _walk_episode_dirs(input_path)
    if nested:
        return nested

    raise ValueError(
        f"No episode_XXX directories found under: {input_path}\n"
        "Expected one of:\n"
        "  - an episode_XXX directory (e.g. .../class_name/episode_0001)\n"
        "  - a directory containing episode_XXX subdirectories at any depth "
        "(e.g. .../some/A/B/episode_0001)"
    )


def read_csv_rows(path):
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader)


# ---------------------------------------------------------------------------
# Gripper validation (port of validate_gripper_readings.py)
# ---------------------------------------------------------------------------


def unwrap_degrees(values):
    unwrapped = []
    previous = None
    current = None
    for value in values:
        if previous is None:
            current = value
        else:
            delta = (value - previous + 180.0) % 360.0 - 180.0
            current += delta
        unwrapped.append(current)
        previous = value
    return unwrapped


def pearson_corr(xs, ys):
    if len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return cov / math.sqrt(var_x * var_y)


def ranks(values):
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    out = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        rank = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            out[indexed[k][0]] = rank
        i = j
    return out


def spearman_corr(xs, ys):
    if len(xs) < 2:
        return None
    return pearson_corr(ranks(xs), ranks(ys))


def linear_r2(xs, ys):
    if len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0.0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot <= 0.0:
        return None
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    return 1.0 - ss_res / ss_tot


def angle_diff_for_direction(start_deg, current_deg, direction):
    if direction == "reverse":
        return (start_deg - current_deg) % 360.0
    return (current_deg - start_deg) % 360.0


def select_direction(calibration):
    direction = calibration.get("angular_direction")
    if direction in {"forward", "reverse"}:
        return direction

    a0 = calibration.get("a0")
    a85 = calibration.get("a85")
    if a0 is None or a85 is None:
        return "forward"

    a0 = float(a0)
    a85 = float(a85)
    max_distance_mm = float(calibration.get("max_distance_mm", 85.0))
    midpoint_angle = calibration.get("a_mid")
    points = calibration.get("points", [])
    if midpoint_angle is None and isinstance(points, list):
        for point in points:
            try:
                distance_mm = float(point["distance_mm"])
            except (KeyError, TypeError, ValueError):
                continue
            if distance_mm not in {0.0, max_distance_mm}:
                midpoint_angle = point.get("angle_deg")
                break

    if midpoint_angle is not None:
        try:
            a_mid = float(midpoint_angle)
            forward_mid = angle_diff_for_direction(a0, a_mid, "forward")
            forward_max = angle_diff_for_direction(a0, a85, "forward")
            reverse_mid = angle_diff_for_direction(a0, a_mid, "reverse")
            reverse_max = angle_diff_for_direction(a0, a85, "reverse")
        except (TypeError, ValueError):
            pass
        else:
            eps = 1e-6
            forward_valid = forward_max > eps and eps < forward_mid < forward_max - eps
            reverse_valid = reverse_max > eps and eps < reverse_mid < reverse_max - eps
            if forward_valid and not reverse_valid:
                return "forward"
            if reverse_valid and not forward_valid:
                return "reverse"
            if forward_valid and reverse_valid:
                forward_score = abs((forward_mid / forward_max) - 0.5)
                reverse_score = abs((reverse_mid / reverse_max) - 0.5)
                return "forward" if forward_score <= reverse_score else "reverse"

    forward = angle_diff_for_direction(a0, a85, "forward")
    reverse = angle_diff_for_direction(a0, a85, "reverse")
    return "forward" if forward <= reverse else "reverse"


def angle_to_distance_m(angle_deg, calibration):
    if not calibration:
        return None
    try:
        a0 = float(calibration["a0"])
        max_distance_mm = float(calibration.get("max_distance_mm", 85.0))
        direction = select_direction(calibration)
    except (KeyError, TypeError, ValueError):
        return None

    a_max = calibration.get("a85")
    points = calibration.get("points", [])
    if isinstance(points, list):
        by_distance = {}
        for item in points:
            try:
                by_distance[float(item["distance_mm"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        max_point = by_distance.get(max_distance_mm)
        if max_point is not None:
            a_max = max_point.get("angle_deg")
        zero_point = by_distance.get(0.0)
        if zero_point is not None:
            try:
                a0 = float(zero_point["angle_deg"])
            except (KeyError, TypeError, ValueError):
                pass

    try:
        delta_max = angle_diff_for_direction(a0, float(a_max), direction)
    except (TypeError, ValueError):
        return None
    if delta_max <= 1e-8:
        return None
    slope = max_distance_mm / delta_max

    traveled = angle_diff_for_direction(a0, angle_deg, direction)
    wrap_zero_threshold = delta_max + (360.0 - delta_max) / 2.0
    if traveled >= wrap_zero_threshold:
        distance_mm = 0.0
    elif traveled > delta_max:
        distance_mm = max_distance_mm
    else:
        distance_mm = slope * traveled
    distance_mm = max(0.0, min(max_distance_mm, distance_mm))
    return distance_mm / 1000.0


def load_calibrations(episode_dir):
    metadata_path = episode_dir / "metadata.json"
    if not metadata_path.exists():
        return {}
    try:
        with metadata_path.open("r") as f:
            metadata = json.load(f)
    except json.JSONDecodeError:
        return {}
    device_bindings = metadata.get("calibration_config", {}).get("device_bindings", {})
    out = {}
    for side in SIDES:
        calibration = device_bindings.get(side, {}).get("mag_calibration")
        if isinstance(calibration, dict):
            out[side] = calibration
    return out


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def align_side_samples(gripper_rows, raw_rows, side):
    grip_col = f"{side}_gripper"
    raw_col = f"{side}_raw_gripper_rotation"
    gripper_by_ts = {
        row["timestamp_ms"]: float(row[grip_col])
        for row in gripper_rows
        if row.get("timestamp_ms") and row.get(grip_col) not in {None, ""}
    }
    raw_by_ts = {
        row["timestamp_ms"]: float(row[raw_col])
        for row in raw_rows
        if row.get("timestamp_ms") and row.get(raw_col) not in {None, ""}
    }
    timestamps = sorted(set(gripper_by_ts) & set(raw_by_ts), key=lambda item: int(float(item)))
    return (
        timestamps,
        [raw_by_ts[ts] for ts in timestamps],
        [gripper_by_ts[ts] for ts in timestamps],
    )


def first_offending_indices(values, predicate, limit=10):
    locations = []
    for index, value in enumerate(values):
        if predicate(value):
            locations.append({"row": index, "value": value})
            if len(locations) >= limit:
                break
    return locations


def classify_side(timestamps, raw_values, gripper_values, calibration, args):
    if not raw_values or not gripper_values:
        return {
            "type": "MISSING_DATA",
            "correct": False,
            "problem_type": "MISSING_DATA",
            "reason": "missing aligned raw/gripper samples",
            "locations": [],
        }

    raw_unwrapped = unwrap_degrees(raw_values)
    raw_range = max(raw_unwrapped) - min(raw_unwrapped)
    gripper_range = max(gripper_values) - min(gripper_values)
    gripper_min = min(gripper_values)
    gripper_max = max(gripper_values)
    pearson = pearson_corr(raw_unwrapped, gripper_values)
    spearman = spearman_corr(raw_unwrapped, gripper_values)
    r2 = linear_r2(raw_unwrapped, gripper_values)

    metrics = {
        "samples": len(raw_values),
        "raw_range_deg": raw_range,
        "gripper_range_m": gripper_range,
        "gripper_min_m": gripper_min,
        "gripper_max_m": gripper_max,
        "pearson": pearson,
        "spearman": spearman,
        "linear_r2": r2,
    }

    if gripper_min < args.min_gripper_m or gripper_max > args.max_gripper_m:
        out_of_range = first_offending_indices(
            gripper_values,
            lambda v: v < args.min_gripper_m or v > args.max_gripper_m,
        )
        return {
            "type": "MAPPING_BAD",
            "correct": False,
            "problem_type": "MAPPING_BAD",
            "reason": "gripper values exceed plausible physical range",
            "metrics": metrics,
            "locations": [
                {"row": loc["row"], "timestamp_ms": timestamps[loc["row"]], "gripper_m": loc["value"]}
                for loc in out_of_range
            ],
        }

    if raw_range <= args.raw_static_deg:
        if gripper_range <= args.gripper_static_m:
            return {
                "type": "STATIC_OK",
                "correct": True,
                "problem_type": None,
                "reason": "raw rotation is static and gripper is also static",
                "metrics": metrics,
                "locations": [],
            }
        return {
            "type": "MAPPING_BAD",
            "correct": False,
            "problem_type": "MAPPING_BAD",
            "reason": "raw rotation is static but gripper changes",
            "metrics": metrics,
            "locations": [
                {"row": 0, "timestamp_ms": timestamps[0]},
                {"row": len(timestamps) - 1, "timestamp_ms": timestamps[-1]},
            ],
        }

    if gripper_range <= args.gripper_static_m:
        return {
            "type": "ALL_ZERO_BAD",
            "correct": False,
            "problem_type": "ALL_ZERO_BAD",
            "reason": "raw rotation changes but gripper is static or nearly zero",
            "metrics": metrics,
            "locations": [
                {"row": 0, "timestamp_ms": timestamps[0]},
                {"row": len(timestamps) - 1, "timestamp_ms": timestamps[-1]},
            ],
        }

    abs_pearson = abs(pearson) if pearson is not None else 0.0
    abs_spearman = abs(spearman) if spearman is not None else 0.0
    fit_ok = (
        abs_pearson >= args.min_correlation
        and abs_spearman >= args.min_correlation
        and (r2 is not None and r2 >= args.min_r2)
    )
    if not fit_ok:
        return {
            "type": "MAPPING_BAD",
            "correct": False,
            "problem_type": "MAPPING_BAD",
            "reason": "gripper does not have a consistent monotonic/linear relation to raw rotation",
            "metrics": metrics,
            "locations": [],
        }

    metadata_check = None
    if calibration and not args.disable_metadata_check:
        recomputed = [angle_to_distance_m(value, calibration) for value in raw_values]
        paired = [
            (idx, abs(recorded - expected))
            for idx, (recorded, expected) in enumerate(zip(gripper_values, recomputed))
            if expected is not None
        ]
        if paired:
            errors = [error for _, error in paired]
            metadata_check = {
                "median_abs_error_m": median(errors),
                "max_abs_error_m": max(errors),
            }
            metrics["metadata_recompute"] = metadata_check
            if metadata_check["median_abs_error_m"] > args.recompute_tolerance_m:
                worst = sorted(paired, key=lambda item: -item[1])[:10]
                return {
                    "type": "MAPPING_BAD",
                    "correct": False,
                    "problem_type": "MAPPING_BAD",
                    "reason": "recorded gripper differs from metadata calibration recomputation",
                    "metrics": metrics,
                    "locations": [
                        {"row": idx, "timestamp_ms": timestamps[idx], "abs_error_m": error}
                        for idx, error in worst
                    ],
                }

    return {
        "type": "DYNAMIC_OK",
        "correct": True,
        "problem_type": None,
        "reason": "gripper follows raw rotation with a consistent mapping",
        "metrics": metrics,
        "locations": [],
    }


def validate_gripper(episode_dir, args):
    gripper_csv = episode_dir / GRIPPER_DIR / "data.csv"
    raw_csv = episode_dir / RAW_GRIPPER_ROTATION_DIR / "data.csv"
    if not gripper_csv.exists() or not raw_csv.exists():
        missing = []
        if not gripper_csv.exists():
            missing.append(str(gripper_csv.relative_to(episode_dir)))
        if not raw_csv.exists():
            missing.append(str(raw_csv.relative_to(episode_dir)))
        return {
            "correct": False,
            "error": "missing required CSV file(s)",
            "missing": missing,
            "sides": {},
        }

    gripper_rows = read_csv_rows(gripper_csv)
    raw_rows = read_csv_rows(raw_csv)
    calibrations = load_calibrations(episode_dir)

    sides = {}
    for side in SIDES:
        timestamps, raw_values, gripper_values = align_side_samples(gripper_rows, raw_rows, side)
        sides[side] = classify_side(timestamps, raw_values, gripper_values, calibrations.get(side), args)

    return {
        "correct": all(result["correct"] for result in sides.values()),
        "sides": sides,
    }


# ---------------------------------------------------------------------------
# Video validation (port of validate_video_frames.py)
# ---------------------------------------------------------------------------


def image_dirs(episode_dir):
    return sorted(
        path for path in episode_dir.iterdir()
        if path.is_dir() and path.name.startswith(IMAGE_PREFIX)
    )


def _load_focus_scorer():
    """Return ``check_focus.score_video``, importing it lazily.

    The scorer depends on OpenCV/NumPy; importing it lazily keeps the rest of
    the validator runnable when those are not installed. The sibling module
    lives next to this script, so its directory is added to ``sys.path``.
    """
    import sys

    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from check_focus import score_video

    return score_video


def assess_focus(video_path, focus_config):
    """Score one wrist-view video and label it in_focus / defocused.

    Returns a dict carrying the median Laplacian variance, the threshold used
    and the resulting label, or ``available: False`` with a reason when the
    video cannot be scored (e.g. OpenCV missing or unreadable frames). An
    unavailable focus check never fails validation; only a confirmed
    ``defocused`` verdict does.
    """
    threshold = float(focus_config.get("lap_var_threshold", 350.0))
    num_frames = int(focus_config.get("num_sample_frames", 20))
    base = {"available": False, "label": "unknown", "lap_var": None,
            "tenengrad": None, "frames_scored": 0, "lap_var_threshold": threshold}

    try:
        score_video = _load_focus_scorer()
    except ImportError as exc:
        raise MissingDependencyError(
            _missing_dep_message("out-of-focus", "--skip-focus", exc)
        ) from exc

    metrics = score_video(str(video_path), num_frames)
    if "error" in metrics:
        base["error"] = metrics["error"]
        return base

    lap_var = metrics["lap_var"]
    return {
        "available": True,
        "label": "defocused" if lap_var < threshold else "in_focus",
        "lap_var": round(lap_var, 2),
        "tenengrad": round(metrics["tenengrad"], 1),
        "frames_scored": metrics["frames_scored"],
        "lap_var_threshold": threshold,
    }


def read_timestamps(path):
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "timestamp_ms" not in reader.fieldnames:
            raise ValueError(f"timestamps.csv has no timestamp_ms header: {path}")
        return [(row_index, int(round(float(row["timestamp_ms"]))))
                for row_index, row in enumerate(reader)]


def load_episode_fps(episode_dir):
    metadata_path = episode_dir / "metadata.json"
    if metadata_path.exists():
        try:
            with metadata_path.open("r") as f:
                metadata = json.load(f)
            fps = metadata.get("fps_config")
            if fps:
                return float(fps)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return 30.0


def ffprobe_frame_count(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames,nb_frames,r_frame_rate,avg_frame_rate,duration",
        "-of", "json", str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        return {"available": False, "error": str(exc), "frame_count": None, "stream": {}}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"available": False, "error": f"ffprobe returned invalid JSON: {exc}",
                "frame_count": None, "stream": {}}

    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    frame_count = stream.get("nb_read_frames") or stream.get("nb_frames")
    try:
        frame_count = int(frame_count) if frame_count is not None else None
    except (TypeError, ValueError):
        frame_count = None
    return {"available": True, "error": "", "frame_count": frame_count, "stream": stream}


def infer_missing_timestamps(timestamp_pairs, fps, tolerance_ratio):
    if len(timestamp_pairs) < 2:
        return [], []
    period_ms = 1000.0 / fps
    tolerance_ms = period_ms * tolerance_ratio
    missing = []
    large_gaps = []
    for (prev_index, prev_ts), (index, ts) in zip(timestamp_pairs, timestamp_pairs[1:]):
        gap = ts - prev_ts
        if gap <= 0:
            continue
        expected_steps = int(math.floor((gap + tolerance_ms) / period_ms))
        missing_count = max(0, expected_steps - 1)
        if missing_count <= 0:
            continue
        inferred = [int(round(prev_ts + period_ms * step)) for step in range(1, missing_count + 1)]
        missing.extend(inferred)
        large_gaps.append({
            "previous_row": prev_index,
            "row": index,
            "previous_timestamp_ms": prev_ts,
            "timestamp_ms": ts,
            "gap_ms": gap,
            "missing_count": missing_count,
            "missing_timestamps_ms": inferred,
        })
    return missing, large_gaps


def duplicate_or_nonmonotonic_timestamps(timestamp_pairs):
    anomalies = []
    for (prev_index, prev_ts), (index, ts) in zip(timestamp_pairs, timestamp_pairs[1:]):
        if ts <= prev_ts:
            anomalies.append({
                "previous_row": prev_index,
                "row": index,
                "previous_timestamp_ms": prev_ts,
                "timestamp_ms": ts,
                "gap_ms": ts - prev_ts,
                "type": "duplicate" if ts == prev_ts else "nonmonotonic",
            })
    return anomalies


def assess_duplicate_quality(duplicate_anomalies, total_frames, video_config):
    """Decide whether the duplicate-frame counts stay within configured limits."""
    max_prop = float(video_config.get("max_duplicate_frame_proportion", 0.1))
    max_near = int(video_config.get("max_near_duplicate_frame", 10))

    count = len(duplicate_anomalies)
    proportion = (count / total_frames) if total_frames else 0.0
    reasons = []
    if proportion >= max_prop:
        reasons.append({
            "type": "duplicate_proportion_exceeded",
            "duplicate_count": count,
            "total_frames": total_frames,
            "proportion": proportion,
            "threshold": max_prop,
        })

    duplicate_rows = sorted(d["row"] for d in duplicate_anomalies)
    near_violations = []
    for prev_row, cur_row in zip(duplicate_rows, duplicate_rows[1:]):
        gap = cur_row - prev_row
        if gap < max_near:
            near_violations.append({
                "previous_row": prev_row,
                "row": cur_row,
                "gap_frames": gap,
            })
    if near_violations:
        reasons.append({
            "type": "near_duplicate_frames",
            "threshold_frames": max_near,
            "violation_count": len(near_violations),
            "violations": near_violations[:20],
        })

    return {
        "duplicate_count": count,
        "total_frames": total_frames,
        "proportion": proportion,
        "max_duplicate_frame_proportion": max_prop,
        "max_near_duplicate_frame": max_near,
        "passes": not reasons,
        "reasons": reasons,
    }


def _side_of_stream(stream_key):
    """Return 'left'/'right' for an ``observation.image.*`` stream, else None."""
    name = stream_key[len(IMAGE_PREFIX):] if stream_key.startswith(IMAGE_PREFIX) else stream_key
    if name.startswith("left_"):
        return "left"
    if name.startswith("right_"):
        return "right"
    return None


def label_unverified_sides(mislabeled_streams):
    """Sides whose wrist_view label cannot be trusted.

    When the cross-class check flags *any* stream on a side as mislabeled, that
    side's wrist_view / tactile assignment is confused: the video sitting in
    ``{side}_wrist_view`` may actually be a tactile feed (and vice versa).
    Content-dependent checks that assume the wrist_view holds a real
    wide-angle view -- the focus check and the action<->video motion
    cross-check -- therefore cannot be trusted for that side and are reported
    as ``unverified`` rather than producing a (wrong) verdict.
    """
    sides = set()
    for key in mislabeled_streams:
        side = _side_of_stream(key)
        if side is not None:
            sides.add(side)
    return sides


def validate_stream(image_dir, fps, tolerance_ratio, video_config,
                    focus_config=None, check_focus=False,
                    unverified_sides=None):
    video_path = image_dir / "video.mp4"
    timestamps_path = image_dir / "timestamps.csv"
    result = {
        "key": image_dir.name,
        "correct": False,
        "video_path": str(video_path),
        "timestamps_path": str(timestamps_path),
        "fps": fps,
        "video_frame_count": None,
        "timestamp_count": None,
        "frame_count_match": False,
        "missing_timestamps_ms": [],
        "large_gaps": [],
        "duplicate_or_nonmonotonic_timestamps": [],
        "duplicate_assessment": None,
        "focus": None,
        "problems": [],
    }

    if not video_path.exists():
        result["problems"].append("missing_video_mp4")
    if not timestamps_path.exists():
        result["problems"].append("missing_timestamps_csv")
    if result["problems"]:
        return result

    # Out-of-focus check on the wrist views only. A defocused verdict is a
    # validation problem; an unavailable check (missing OpenCV, etc.) is not.
    # If this side's wrist_view label is unverified (view/tactile confused),
    # the focus reference video is not a real view, so focus is reported as
    # ``unverified`` and never produces a defocus problem.
    if check_focus and image_dir.name in FOCUS_VIEWS:
        side = _side_of_stream(image_dir.name)
        if unverified_sides and side in unverified_sides:
            result["focus"] = {
                "available": False,
                "label": "unverified",
                "lap_var": None,
                "tenengrad": None,
                "frames_scored": 0,
                "reason": ("wrist_view label unverified (view/tactile may be "
                           "swapped on this side); focus not assessed"),
            }
        else:
            focus = assess_focus(video_path, focus_config or DEFAULT_VALIDATE_CONFIG["focus"])
            result["focus"] = focus
            if focus.get("label") == "defocused":
                result["problems"].append("defocused_video")

    timestamp_pairs = read_timestamps(timestamps_path)
    result["timestamp_count"] = len(timestamp_pairs)
    probe = ffprobe_frame_count(video_path)
    result["ffprobe"] = {"available": probe["available"], "error": probe["error"], "stream": probe["stream"]}
    result["video_frame_count"] = probe["frame_count"]
    result["frame_count_match"] = (
        probe["frame_count"] is not None and probe["frame_count"] == len(timestamp_pairs)
    )
    if not result["frame_count_match"]:
        result["problems"].append("video_frame_count_mismatch")

    anomalies = duplicate_or_nonmonotonic_timestamps(timestamp_pairs)
    missing, large_gaps = infer_missing_timestamps(timestamp_pairs, fps, tolerance_ratio)
    result["duplicate_or_nonmonotonic_timestamps"] = anomalies
    result["missing_timestamps_ms"] = missing
    result["large_gaps"] = large_gaps

    duplicate_only = [a for a in anomalies if a["type"] == "duplicate"]
    nonmonotonic_only = [a for a in anomalies if a["type"] == "nonmonotonic"]

    assessment = assess_duplicate_quality(duplicate_only, len(timestamp_pairs), video_config)
    result["duplicate_assessment"] = assessment

    if nonmonotonic_only:
        result["problems"].append("nonmonotonic_timestamps")
    if not assessment["passes"]:
        result["problems"].append("duplicate_frames_exceed_thresholds")
    if missing:
        result["problems"].append("missing_timestamps")

    result["correct"] = not result["problems"]
    return result


def validate_video(episode_dir, args):
    fps = args.fps if args.fps is not None else load_episode_fps(episode_dir)
    video_config = getattr(args, "video_config", DEFAULT_VALIDATE_CONFIG["video"])
    focus_config = getattr(args, "focus_config", DEFAULT_VALIDATE_CONFIG["focus"])
    label_config = getattr(args, "label_config", DEFAULT_VALIDATE_CONFIG["label"])
    check_focus = not getattr(args, "skip_focus", False)

    # Cross-class (wrist_view <-> tactile) mislabel check runs FIRST: it needs
    # all six streams together, and its result decides which sides have an
    # unverified wrist_view label. The focus check (below) must know this so it
    # does not score a tactile feed sitting in a *_wrist_view directory.
    label = None
    mislabeled = set()
    unverified_sides = set()
    if not getattr(args, "skip_label", False):
        label = assess_label_similarity(episode_dir, label_config)
        mislabeled = set(label.get("mislabeled_streams", []))
        unverified_sides = label_unverified_sides(mislabeled)

    streams = [
        validate_stream(path, fps, args.gap_tolerance_ratio, video_config,
                        focus_config=focus_config, check_focus=check_focus,
                        unverified_sides=unverified_sides)
        for path in image_dirs(episode_dir)
    ]
    result = {
        "correct": all(stream["correct"] for stream in streams) if streams else True,
        "fps": fps,
        "streams": streams,
        "video_config": video_config,
        "focus_config": focus_config if check_focus else None,
        "label": label,
        "label_unverified_sides": sorted(unverified_sides),
    }

    # A mislabel forces the offending stream (and thus the video) incorrect.
    if label is not None:
        for stream in streams:
            if stream["key"] in mislabeled:
                if "mislabeled_stream" not in stream["problems"]:
                    stream["problems"].append("mislabeled_stream")
                stream["correct"] = not stream["problems"]
        result["correct"] = all(stream["correct"] for stream in streams) if streams else True

    return result


def assess_label_similarity(episode_dir, label_config):
    """Run the first-frame cross-class mislabel check for one episode.

    Lazily imports the cv2-based checker from check_label_similarity so the
    validator still runs without OpenCV. Returns the checker's episode result,
    or ``available: False`` with a reason when it cannot run.
    """
    threshold = float((label_config or {}).get("tactile_affinity_threshold", 0.40))
    try:
        scorer = _load_label_checker()
    except ImportError as exc:
        raise MissingDependencyError(
            _missing_dep_message("cross-class mislabel", "--skip-label", exc)
        ) from exc
    result = scorer(str(episode_dir), threshold)
    result["available"] = True
    return result


def _load_label_checker():
    """Return ``check_label_similarity.assess_episode``, imported lazily."""
    import sys

    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from check_label_similarity import assess_episode

    return assess_episode


# ---------------------------------------------------------------------------
# Action / eef-pose validation
# ---------------------------------------------------------------------------


def _parse_float(text):
    """Parse a CSV cell to float; return None when blank or non-numeric."""
    if text is None or text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def validate_action(episode_dir, args):
    """Validate the action/eef-pose trajectory.

    Current checks:
      - basic integrity: file present, parseable, x,y,z finite (no NaN/Inf).
      - absolute-value range: |x|,|y|,|z| must not exceed the configured
        threshold (default 1.5 m); larger values are implausible outliers.

    Returns a dict carrying ``correct`` plus the detailed ``abs_value_check``
    evidence (threshold, columns, every offending sample).
    """
    action_config = getattr(args, "action_config", DEFAULT_VALIDATE_CONFIG["action"])
    threshold = float(action_config.get("xyz_abs_threshold", 1.5))
    data_csv = episode_dir / ACTION_DIR / "data.csv"

    abs_check = {
        "description": "end-effector x,y,z absolute value must not exceed threshold",
        "threshold": threshold,
        "columns_checked": list(ACTION_XYZ_COLUMNS),
        "total_rows": 0,
        "outlier_count": 0,
        "outliers": [],
    }

    if not data_csv.exists():
        return {
            "correct": False,
            "error": "missing action data.csv",
            "missing": [str(data_csv.relative_to(episode_dir))],
            "abs_value_check": abs_check,
        }

    rows = read_csv_rows(data_csv)
    abs_check["total_rows"] = len(rows)
    present_cols = [c for c in ACTION_XYZ_COLUMNS if rows and c in rows[0]]
    abs_check["columns_checked"] = present_cols

    nan_locations = []
    outliers = []
    for index, row in enumerate(rows):
        timestamp = row.get("timestamp_ms")
        for col in present_cols:
            value = _parse_float(row.get(col))
            if value is None or value != value or value in (float("inf"), float("-inf")):
                if len(nan_locations) < 20:
                    nan_locations.append({"row": index, "timestamp_ms": timestamp, "column": col})
                continue
            if abs(value) > threshold:
                outliers.append({
                    "row": index,
                    "timestamp_ms": timestamp,
                    "column": col,
                    "value": value,
                })

    abs_check["outlier_count"] = len(outliers)
    abs_check["outliers"] = outliers[:50]

    problems = []
    if not present_cols:
        problems.append("missing_xyz_columns")
    if nan_locations:
        problems.append("non_finite_xyz")
    if outliers:
        problems.append("xyz_abs_outlier")

    return {
        "correct": not problems,
        "problems": problems,
        "non_finite": nan_locations,
        "abs_value_check": abs_check,
    }


# ---------------------------------------------------------------------------
# Motion-consistency cross-check (action motion vs wrist_view video motion)
# ---------------------------------------------------------------------------


def action_motion_per_side(rows):
    """Per-side end-effector motion measures over the run.

    Returns, for each side, the cumulative 3D ``path`` length (sensitive to
    large jumps and jitter) and the ``extent`` = summed per-axis peak-to-peak
    range (sensitive to a slow drift away from the origin). A genuinely static
    device has both near zero; either one being large means the trajectory
    moved.
    """
    out = {}
    for side in SIDES:
        prev = None
        path = 0.0
        mins = [None, None, None]
        maxs = [None, None, None]
        for row in rows:
            xyz = (_parse_float(row.get(f"{side}_x")),
                   _parse_float(row.get(f"{side}_y")),
                   _parse_float(row.get(f"{side}_z")))
            if any(v is None for v in xyz):
                continue
            if prev is not None:
                path += math.dist(prev, xyz)
            prev = xyz
            for i, v in enumerate(xyz):
                mins[i] = v if mins[i] is None else min(mins[i], v)
                maxs[i] = v if maxs[i] is None else max(maxs[i], v)
        extent = sum((maxs[i] - mins[i]) for i in range(3)) if prev is not None else 0.0
        out[side] = {"path": path, "extent": extent}
    return out


def wrist_view_video_motion(video_path, num_frames):
    """Mean frame-to-frame absolute difference over sampled frames.

    A static camera/scene yields a small value; an arm-mounted camera in
    motion yields a large one. Returns None if the video cannot be read.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 1:
        cap.release()
        return None
    idxs = np.linspace(0, total - 1, min(num_frames, total)).astype(int)
    prev = None
    diffs = []
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


def validate_motion_consistency(episode_dir, args):
    """Cross-check per-side action motion against per-wrist_view video motion.

    Two failure modes are detected from the relative (ratio) motion, which is
    far more robust than absolute thresholds:

      - swap : actions show one dominant moving side and the videos also show
               one dominant moving side, but they are OPPOSITE sides -> the
               two wrist_view videos are left/right swapped (video problem).
      - drift: the videos show one wrist_view essentially static while BOTH
               action sides move -> the static side's action is fluctuating on
               a static device (action problem).

    Returns the verdict plus all measured evidence, or ``available: False``
    with a reason when it cannot run.
    """
    cfg = getattr(args, "motion_config", DEFAULT_VALIDATE_CONFIG["motion"])
    video_static_thr = float(cfg.get("video_static_threshold", 4.0))
    path_active = float(cfg.get("action_path_active", 5.0))
    extent_active = float(cfg.get("action_extent_active", 0.3))
    num_frames = int(cfg.get("num_sample_frames", 30))

    base = {
        "available": False,
        "verdict": "inconclusive",
        "drift_side": None,
        "thresholds": {"video_static_threshold": video_static_thr,
                       "action_path_active": path_active,
                       "action_extent_active": extent_active},
        "num_sample_frames": num_frames,
    }

    action_csv = episode_dir / ACTION_DIR / "data.csv"
    left_video = episode_dir / WRIST_VIEW["left"] / "video.mp4"
    right_video = episode_dir / WRIST_VIEW["right"] / "video.mp4"
    if not action_csv.exists() or not left_video.exists() or not right_video.exists():
        base["error"] = "missing action data.csv or wrist_view video(s)"
        return base

    rows = read_csv_rows(action_csv)
    motion = action_motion_per_side(rows)
    _require_cv2_numpy("motion-consistency", "--skip-motion")

    v_left = wrist_view_video_motion(left_video, num_frames)
    v_right = wrist_view_video_motion(right_video, num_frames)
    if v_left is None or v_right is None:
        base["error"] = "unreadable wrist_view video"
        return base

    video_motion = {"left": v_left, "right": v_right}
    # The video tells us which arm is physically static.
    video_static = {s: video_motion[s] < video_static_thr for s in SIDES}
    # A static arm's action must stay still: small path AND small extent. If
    # either is large the trajectory moved (jump -> path, slow drift -> extent).
    action_moving = {
        s: (motion[s]["path"] > path_active or motion[s]["extent"] > extent_active)
        for s in SIDES
    }

    static_sides = [s for s in SIDES if video_static[s]]
    verdict = "consistent"
    drift_side = None
    if len(static_sides) == 1:
        static_side = static_sides[0]
        other_side = "right" if static_side == "left" else "left"
        if action_moving[static_side] and action_moving[other_side]:
            # One view static but both devices move -> drift on the static side.
            verdict = "drift"
            drift_side = static_side
        elif action_moving[static_side] and not action_moving[other_side]:
            # The static view's own device moves while the other is still ->
            # the two wrist_view videos are left/right swapped.
            verdict = "swap"
    elif len(static_sides) == 2:
        # Both arms static: any side whose action moves is drifting.
        moving = [s for s in SIDES if action_moving[s]]
        if moving:
            verdict = "drift"
            drift_side = moving[0] if len(moving) == 1 else "both"

    base.update({
        "available": True,
        "verdict": verdict,
        "drift_side": drift_side,
        "static_video_side": static_sides[0] if len(static_sides) == 1 else (
            "both" if len(static_sides) == 2 else None),
        "action_path": {s: round(motion[s]["path"], 4) for s in SIDES},
        "action_extent": {s: round(motion[s]["extent"], 4) for s in SIDES},
        "video_motion": {s: round(video_motion[s], 3) for s in SIDES},
        "video_static": video_static,
        "action_moving": action_moving,
    })
    return base


def unverified_motion(confused_sides, args):
    """Motion-consistency stub for when a wrist_view label is unverified.

    The cross-check relies on the wrist_view videos being real wide-angle
    views. When a side's view/tactile labels are confused that assumption
    breaks, so the verdict is ``unverified`` (available=False) and no swap or
    drift problem is derived from it.
    """
    cfg = getattr(args, "motion_config", DEFAULT_VALIDATE_CONFIG["motion"])
    return {
        "available": False,
        "verdict": "unverified",
        "drift_side": None,
        "unverified_sides": sorted(confused_sides),
        "reason": ("wrist_view label unverified for side(s): "
                   f"{', '.join(sorted(confused_sides))}; video motion is not a "
                   "real view, motion-consistency not assessed"),
        "num_sample_frames": int(cfg.get("num_sample_frames", 30)),
    }


# ---------------------------------------------------------------------------
# Episode-level orchestration & reporting
# ---------------------------------------------------------------------------


def validate_episode(episode_dir, args):
    gripper = None if args.skip_gripper else validate_gripper(episode_dir, args)
    video = None if args.skip_video else validate_video(episode_dir, args)
    action = None if args.skip_action else validate_action(episode_dir, args)

    # Motion-consistency cross-check needs both action and video; its verdict
    # is applied back to whichever dimension it implicates.
    run_motion = (video is not None and action is not None
                  and not getattr(args, "skip_motion", False))
    # If any side's wrist_view label is unverified (view/tactile confused), the
    # video motion is not a real view, so the cross-check cannot be trusted and
    # is reported as ``unverified`` instead of emitting a swap/drift verdict.
    confused_sides = set(video.get("label_unverified_sides", [])) if video is not None else set()
    if run_motion and confused_sides:
        motion = unverified_motion(confused_sides, args)
    elif run_motion:
        motion = validate_motion_consistency(episode_dir, args)
    else:
        motion = None
    if motion is not None:
        # Attach the evidence to both dimensions; apply the verdict to the one
        # it implicates (swap -> video, drift -> action). An unverified verdict
        # has available=False, so neither problem is applied.
        video["motion_consistency"] = motion
        action["motion_consistency"] = motion
        if motion.get("available") and motion["verdict"] == "swap":
            video.setdefault("problems", []).append("wrist_view_lr_swap")
            video["correct"] = False
        elif motion.get("available") and motion["verdict"] == "drift":
            action.setdefault("problems", []).append("fluctuation_on_static_device")
            action["correct"] = False

    correct = True
    for part in (gripper, video, action):
        if part is not None:
            correct = correct and part["correct"]
    return {
        "episode": episode_dir.name,
        "path": str(episode_dir),
        "correct": correct,
        "gripper": gripper,
        "video": video,
        "action": action,
    }


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_report(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def episode_output_layout(input_path, episode_dir):
    """Map one episode to its (relative output subdir, class name).

    The output mirrors the input layout:
      - single ``episode_XXX`` input -> grouped under its parent dir's name
        (legacy behavior), so reports land in ``<output_root>/<parent>/``;
      - episodes directly inside the input dir (a class dir) -> grouped under
        the input dir's own name (legacy behavior);
      - episodes nested deeper -> the directory path of the episode's parent
        *relative to the input dir* is reproduced verbatim, so that
        ``<input>/A/B/episode_*`` -> ``<output_root>/A/B/episode_*``.

    Returns a ``(Path, str)`` of (relative output subdir, class_name) where
    class_name is the immediate parent directory's name.
    """
    if input_path.name.startswith("episode_"):
        class_name = input_path.parent.name or "class"
        return Path(class_name), class_name
    rel_parent = episode_dir.parent.relative_to(input_path)
    if rel_parent == Path("."):
        class_name = input_path.name or "class"
        return Path(class_name), class_name
    class_name = episode_dir.parent.name or "class"
    return rel_parent, class_name


def checks_run_flags(args):
    """Which checks were enabled for this run (batch-level configuration)."""
    return {
        "camera_label_consistency": not args.skip_video and not args.skip_label,
        "motion_tracker_consistency": (not args.skip_video and not args.skip_action
                                       and not args.skip_motion),
        "gripper": not args.skip_gripper,
        "video": not args.skip_video,
        "action": not args.skip_action,
        "focus": not args.skip_video and not args.skip_focus,
    }


def checks_run_status(episode_result, args):
    """Per-episode outcome of each check.

    Each value is one of: ``true`` (ran and passed), ``false`` (ran and
    failed), ``"unverified"`` (ran but could not be confirmed -- e.g. a
    confused camera/tactile label invalidates the reference video), or
    ``null`` (the check was skipped). The two cross-modal consistency checks
    are listed first because they gate the others.
    """
    video = episode_result.get("video")
    gripper = episode_result.get("gripper")
    action = episode_result.get("action")

    # Camera-label consistency: false when any stream is mislabeled.
    label_status = None
    if not args.skip_video and not args.skip_label and video is not None:
        label = video.get("label")
        if label is not None:
            label_status = not bool(label.get("mislabeled_streams"))

    # Motion-tracker consistency: unverified / false (swap|drift) / true.
    motion_status = None
    if (not args.skip_video and not args.skip_action and not args.skip_motion
            and video is not None and action is not None):
        motion = video.get("motion_consistency")
        if motion is not None:
            verdict = motion.get("verdict")
            if verdict == "unverified":
                motion_status = "unverified"
            elif verdict in ("swap", "drift"):
                motion_status = False
            elif motion.get("available"):
                motion_status = True

    # Focus: false if any wrist_view is defocused, unverified if any is
    # unverified (label confused), else true when at least one was scored.
    focus_status = None
    if not args.skip_video and not args.skip_focus and video is not None:
        labels = [(s.get("focus") or {}).get("label")
                  for s in video.get("streams", []) if s.get("focus")]
        if any(l == "defocused" for l in labels):
            focus_status = False
        elif any(l == "unverified" for l in labels):
            focus_status = "unverified"
        elif any(l == "in_focus" for l in labels):
            focus_status = True

    return {
        "camera_label_consistency": label_status,
        "motion_tracker_consistency": motion_status,
        "gripper": gripper["correct"] if gripper is not None else None,
        "video": video["correct"] if video is not None else None,
        "action": action["correct"] if action is not None else None,
        "focus": focus_status,
    }


def _bool_or_none(part):
    return part["correct"] if part is not None else None


def build_result_tier(episode_result):
    """Tier 1: the top-level quality verdicts (true / false / "unverified" / null).

    ``pose_quality`` is ``false`` when the action data itself fails (outliers,
    non-finite values, or a confirmed static-device drift). When the action
    data is otherwise clean but the motion-consistency cross-check could not be
    confirmed (label confused), the pose cannot be certified either way, so it
    is reported as ``"unverified"`` rather than ``true``.
    """
    action = episode_result.get("action")
    if action is None:
        pose_quality = None
    elif not action["correct"]:
        pose_quality = False
    else:
        motion = action.get("motion_consistency") or {}
        pose_quality = "unverified" if motion.get("verdict") == "unverified" else True
    return {
        "video_quality": _bool_or_none(episode_result.get("video")),
        "gripper_quality": _bool_or_none(episode_result.get("gripper")),
        "pose_quality": pose_quality,
    }


def _video_metrics(video):
    """Condensed video metrics: frame drop, mislabel, defocus."""
    dropped = 0
    total = 0
    for stream in video.get("streams", []):
        missing = len(stream.get("missing_timestamps_ms") or [])
        counted = stream.get("timestamp_count") or 0
        dropped += missing
        total += counted + missing
    drop_pct = round(dropped / total, 6) if total else 0.0

    defocused = [s["key"] for s in video.get("streams", [])
                 if (s.get("focus") or {}).get("label") == "defocused"]
    focus_unverified = [s["key"] for s in video.get("streams", [])
                        if (s.get("focus") or {}).get("label") == "unverified"]
    label = video.get("label") or {}
    mislabeled = list(label.get("mislabeled_streams") or [])
    unverified_sides = list(video.get("label_unverified_sides") or [])

    motion = video.get("motion_consistency") or {}
    needs_swap = motion.get("verdict") == "swap"
    motion_unverified = motion.get("verdict") == "unverified"

    return {
        "passed": video["correct"],
        "frame_drop": {
            "has_drop": dropped > 0,
            "dropped_frames": dropped,
            "total_frames": total,
            "drop_percentage": drop_pct,
        },
        "mislabel": {
            "has_mislabel": bool(mislabeled),
            "streams": mislabeled,
            "unverified_sides": unverified_sides,
        },
        "defocus": {
            "defocused": bool(defocused),
            "streams": defocused,
            "unverified": focus_unverified,
        },
        "wrist_view_swap": {
            "needs_swap": needs_swap,
            "unverified": motion_unverified,
            "note": ("swap left_wrist_view and right_wrist_view"
                     if needs_swap else ""),
        },
    }


def _gripper_metrics(gripper):
    """Condensed gripper metrics: overall label + per-side labels."""
    if gripper.get("error"):
        return {"passed": False, "label": "ERROR", "error": gripper["error"], "sides": {}}
    sides = {}
    bad = []
    for side, result in gripper.get("sides", {}).items():
        if result is None:
            continue
        sides[side] = result.get("type")
        if not result.get("correct"):
            bad.append(result.get("problem_type") or result.get("type"))
    label = bad[0] if bad else "OK"
    return {"passed": gripper["correct"], "label": label, "sides": sides}


def _action_metrics(action):
    """Condensed action metrics: abs-value outlier summary."""
    abs_check = action.get("abs_value_check", {})
    if action.get("error"):
        return {"passed": False, "error": action["error"],
                "abs_value_outlier": {"has_outlier": False, "count": 0,
                                      "threshold": abs_check.get("threshold")}}
    motion = action.get("motion_consistency") or {}
    is_drift = motion.get("verdict") == "drift"
    motion_unverified = motion.get("verdict") == "unverified"
    return {
        "passed": action["correct"],
        "abs_value_outlier": {
            "has_outlier": abs_check.get("outlier_count", 0) > 0,
            "count": abs_check.get("outlier_count", 0),
            "threshold": abs_check.get("threshold"),
        },
        "non_finite": bool(action.get("non_finite")),
        "static_drift": {
            "has_drift": is_drift,
            "unverified": motion_unverified,
            "side": motion.get("drift_side"),
            "error": "fluctuation on static device" if is_drift else "",
        },
    }


def build_metrics_tier(episode_result):
    """Tier 2: condensed metrics / labels per dimension."""
    metrics = {}
    if episode_result.get("video") is not None:
        metrics["video"] = _video_metrics(episode_result["video"])
    if episode_result.get("gripper") is not None:
        metrics["gripper"] = _gripper_metrics(episode_result["gripper"])
    if episode_result.get("action") is not None:
        metrics["action"] = _action_metrics(episode_result["action"])
    return metrics


def build_info_tier(episode_result):
    """Tier 3: full per-check evidence (values, thresholds, locations)."""
    info = {}
    if episode_result.get("video") is not None:
        info["video"] = episode_result["video"]
    if episode_result.get("gripper") is not None:
        info["gripper"] = episode_result["gripper"]
    if episode_result.get("action") is not None:
        info["action"] = episode_result["action"]
    return info


def episode_report_payload(class_name, episode_result, args, generated_at):
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "type": "validation",
        "class_name": class_name,
        "episode": episode_result["episode"],
        "source_path": episode_result["path"],
        "generated_at": generated_at,
        "checks_run": checks_run_status(episode_result, args),
        "correct": episode_result["correct"],
        "result": build_result_tier(episode_result),
        "metrics": build_metrics_tier(episode_result),
        "info": build_info_tier(episode_result),
    }


def class_summary_payload(class_name, input_path, args, episodes, episode_report_paths, generated_at):
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "type": "validation_summary",
        "class_name": class_name,
        "input_path": str(input_path),
        "generated_at": generated_at,
        "checks_run": checks_run_flags(args),
        "processed": len(episodes),
        "correct": sum(1 for ep in episodes if ep["correct"]),
        "incorrect": sum(1 for ep in episodes if not ep["correct"]),
        "episodes": [
            {
                "episode": ep["episode"],
                "correct": ep["correct"],
                "problems": collect_problem_summary(ep),
            }
            for ep in episodes
        ],
    }


def collect_problem_summary(episode_result):
    """A flat list of short problem strings for one episode (empty if clean)."""
    problems = []
    gripper = episode_result.get("gripper")
    if gripper:
        if gripper.get("error"):
            problems.append(f"gripper: {gripper['error']}")
        for side, result in gripper.get("sides", {}).items():
            if result and not result.get("correct"):
                problems.append(
                    f"gripper/{side}: {result.get('problem_type') or result.get('type')}")
    video = episode_result.get("video")
    if video:
        for stream in video.get("streams", []):
            if not stream.get("correct"):
                key = (stream.get("key") or "").replace(IMAGE_PREFIX, "")
                problems.append(f"video/{key}: {','.join(stream.get('problems') or [])}")
    action = episode_result.get("action")
    if action and not action.get("correct"):
        if action.get("error"):
            problems.append(f"action: {action['error']}")
        for item in action.get("problems") or []:
            problems.append(f"action: {item}")
    return problems


def format_optional(value, digits=4):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def summarize_locations(locations, limit=5):
    if not locations:
        return ""
    shown = locations[:limit]
    bits = []
    for loc in shown:
        parts = [f"row={loc['row']}"]
        if "timestamp_ms" in loc:
            parts.append(f"t={loc['timestamp_ms']}ms")
        if "gripper_m" in loc:
            parts.append(f"gripper={loc['gripper_m']:.6f}m")
        if "abs_error_m" in loc:
            parts.append(f"|err|={loc['abs_error_m']:.6f}m")
        bits.append("(" + ", ".join(parts) + ")")
    suffix = f" ... +{len(locations) - limit} more" if len(locations) > limit else ""
    return ", ".join(bits) + suffix


def summarize_timestamps(items, limit=20):
    if len(items) <= limit:
        return str(items)
    shown = ", ".join(str(item) for item in items[:limit])
    return f"[{shown}, ...] total={len(items)}"


def print_gripper(gripper):
    print("  gripper:")
    if gripper.get("error"):
        print(f"    error: {gripper['error']} (missing: {', '.join(gripper.get('missing', []))})")
        return
    for side in SIDES:
        result = gripper["sides"].get(side)
        if result is None:
            continue
        side_status = "correct" if result["correct"] else "incorrect"
        problem = result["problem_type"] or "-"
        metrics = result.get("metrics", {})
        print(f"    {side}: {side_status} type={result['type']} problem={problem}")
        print(f"      reason: {result['reason']}")
        if metrics:
            print(
                "      metrics: "
                f"raw_range={metrics.get('raw_range_deg', 0.0):.3f} deg, "
                f"gripper_range={metrics.get('gripper_range_m', 0.0):.6f} m, "
                f"gripper=[{metrics.get('gripper_min_m', 0.0):.6f}, "
                f"{metrics.get('gripper_max_m', 0.0):.6f}] m, "
                f"pearson={format_optional(metrics.get('pearson'))}, "
                f"spearman={format_optional(metrics.get('spearman'))}, "
                f"r2={format_optional(metrics.get('linear_r2'))}"
            )
            recompute = metrics.get("metadata_recompute")
            if recompute:
                print(
                    "      metadata_recompute: "
                    f"median_abs_error={recompute['median_abs_error_m']:.6f} m, "
                    f"max_abs_error={recompute['max_abs_error_m']:.6f} m"
                )
        if result.get("locations"):
            print(f"      locations: {summarize_locations(result['locations'])}")


def print_video(video):
    print("  video:")
    if not video["streams"]:
        print("    no image streams found")
        return
    print(f"    fps={video['fps']:.3f}")
    label = video.get("label")
    if label is not None:
        if not label.get("available", True):
            print(f"    label_check: unavailable ({label.get('error', 'unknown')})")
        elif label.get("has_mislabel"):
            print(f"    label_check: CROSS-CLASS MISLABEL -> "
                  f"{', '.join(label.get('mislabeled_streams', []))}")
        else:
            print("    label_check: ok (no cross-class mislabel)")
    print_motion(video.get("motion_consistency"))
    for stream in video["streams"]:
        stream_status = "correct" if stream["correct"] else "incorrect"
        problems = ",".join(stream["problems"]) if stream["problems"] else "-"
        print(
            f"    {stream['key']}: {stream_status} problems={problems} "
            f"video_frames={stream['video_frame_count']} "
            f"timestamps={stream['timestamp_count']} "
            f"frame_count_match={stream['frame_count_match']}"
        )
        focus = stream.get("focus")
        if focus:
            if focus.get("available"):
                marker = " <-- DEFOCUSED" if focus["label"] == "defocused" else ""
                print(
                    f"      focus: {focus['label']} "
                    f"lap_var={format_optional(focus['lap_var'], 1)} "
                    f"(threshold={focus['lap_var_threshold']}, "
                    f"tenengrad={format_optional(focus['tenengrad'], 1)}, "
                    f"frames={focus['frames_scored']}){marker}"
                )
            elif focus.get("label") == "unverified":
                print(f"      focus: unverified ({focus.get('reason', 'label unverified')})")
            else:
                print(f"      focus: unavailable ({focus.get('error', 'unknown')})")
        if stream["missing_timestamps_ms"]:
            print(f"      missing_timestamps_ms: {summarize_timestamps(stream['missing_timestamps_ms'])}")
        if stream["large_gaps"]:
            preview = stream["large_gaps"][:5]
            for gap in preview:
                print(
                    f"      gap: row {gap['previous_row']}->{gap['row']} "
                    f"({gap['previous_timestamp_ms']}->{gap['timestamp_ms']} ms, "
                    f"gap={gap['gap_ms']} ms, missing={gap['missing_count']})"
                )
            if len(stream["large_gaps"]) > len(preview):
                print(f"      ... +{len(stream['large_gaps']) - len(preview)} more gap(s)")
        if stream["duplicate_or_nonmonotonic_timestamps"]:
            preview = stream["duplicate_or_nonmonotonic_timestamps"][:5]
            for item in preview:
                print(
                    f"      {item['type']}: row {item['previous_row']}->{item['row']} "
                    f"({item['previous_timestamp_ms']}->{item['timestamp_ms']} ms, "
                    f"gap={item['gap_ms']} ms)"
                )
            if len(stream["duplicate_or_nonmonotonic_timestamps"]) > len(preview):
                extra = len(stream["duplicate_or_nonmonotonic_timestamps"]) - len(preview)
                print(f"      ... +{extra} more duplicate/non-monotonic timestamp(s)")
        assessment = stream.get("duplicate_assessment")
        if assessment:
            verdict = "ok" if assessment["passes"] else "FAIL"
            print(
                f"      duplicates: {verdict} count={assessment['duplicate_count']}/"
                f"{assessment['total_frames']} "
                f"proportion={assessment['proportion']:.4f} "
                f"(max_prop={assessment['max_duplicate_frame_proportion']}, "
                f"max_near_frames={assessment['max_near_duplicate_frame']})"
            )
            for reason in assessment["reasons"]:
                if reason["type"] == "duplicate_proportion_exceeded":
                    print(
                        "        proportion_exceeded: "
                        f"{reason['proportion']:.4f} >= {reason['threshold']}"
                    )
                elif reason["type"] == "near_duplicate_frames":
                    sample = reason["violations"][:3]
                    sample_str = ", ".join(
                        f"(row {v['previous_row']}->{v['row']}, gap={v['gap_frames']} frames)"
                        for v in sample
                    )
                    suffix = (
                        f" ... +{reason['violation_count'] - len(sample)} more"
                        if reason["violation_count"] > len(sample)
                        else ""
                    )
                    print(
                        "        near_duplicate_frames "
                        f"(< {reason['threshold_frames']} frames apart): "
                        f"{sample_str}{suffix}"
                    )


def _fmt_quality(value):
    if value is None:
        return "skipped"
    if value == "unverified":
        return "unverified"
    return "true" if value else "false"


def print_report(report):
    for episode in report["episodes"]:
        status = "OK" if episode["correct"] else "BAD"
        result = build_result_tier(episode)
        print(f"{episode['episode']}: {status}  "
              f"[video_quality={_fmt_quality(result['video_quality'])}, "
              f"gripper_quality={_fmt_quality(result['gripper_quality'])}, "
              f"pose_quality={_fmt_quality(result['pose_quality'])}]")
        if episode.get("gripper") is not None:
            print_gripper(episode["gripper"])
        if episode.get("video") is not None:
            print_video(episode["video"])
        if episode.get("action") is not None:
            print_action(episode["action"])
    print(
        f"Summary: processed={report['processed']} "
        f"correct={report['correct']} incorrect={report['incorrect']}"
    )


def print_motion(motion):
    if not motion:
        return
    if not motion.get("available"):
        if motion.get("verdict") == "unverified":
            print(f"    motion_check: unverified ({motion.get('reason', 'label unverified')})")
        else:
            print(f"    motion_check: unavailable ({motion.get('error', 'unknown')})")
        return
    ap = motion.get("action_path", {})
    ae = motion.get("action_extent", {})
    vm = motion.get("video_motion", {})
    evidence = (f"video_motion(L={vm.get('left')}, R={vm.get('right')}) "
                f"action_path(L={ap.get('left')}, R={ap.get('right')}) "
                f"action_extent(L={ae.get('left')}, R={ae.get('right')})")
    verdict = motion["verdict"]
    if verdict == "swap":
        print(f"    motion_check: SWAP - wrist_view L/R reversed; {evidence}")
    elif verdict == "drift":
        print(f"    motion_check: DRIFT on {motion.get('drift_side')} "
              f"(fluctuation on static device); {evidence}")
    else:
        print(f"    motion_check: {verdict}; {evidence}")


def print_action(action):
    print("  action:")
    if action.get("error"):
        print(f"    error: {action['error']}")
        return
    abs_check = action.get("abs_value_check", {})
    status = "correct" if action["correct"] else "incorrect"
    problems = ",".join(action.get("problems") or []) or "-"
    print(f"    {status} problems={problems} "
          f"(eef_pose, {abs_check.get('total_rows', 0)} rows)")
    print(
        f"      abs_value_check: |x,y,z| <= {abs_check.get('threshold')} m, "
        f"outliers={abs_check.get('outlier_count', 0)} "
        f"cols={','.join(abs_check.get('columns_checked') or [])}"
    )
    for out in (abs_check.get("outliers") or [])[:5]:
        print(f"        outlier: row {out['row']} t={out['timestamp_ms']}ms "
              f"{out['column']}={out['value']:.4f}")
    extra = abs_check.get("outlier_count", 0) - min(5, len(abs_check.get("outliers") or []))
    if extra > 0:
        print(f"        ... +{extra} more outlier(s)")
    if action.get("non_finite"):
        nf = action["non_finite"]
        print(f"      non_finite xyz: {len(nf)} sample(s), e.g. "
              f"row {nf[0]['row']} {nf[0]['column']}")


def main():
    args = parse_args()
    input_path = args.input_path or args.input_path_arg
    if input_path is None:
        raise ValueError(
            "Input path is required. Expected an episode_XXX directory "
            "(e.g. .../class_name/episode_0001) or a class directory containing "
            "episode_XXX subdirectories (e.g. .../class_name)."
        )
    if args.skip_gripper and args.skip_video and args.skip_action:
        raise ValueError("--skip-gripper, --skip-video and --skip-action are all set; "
                         "nothing to validate.")

    apply_validate_config(args)
    require_check_dependencies(args)

    episode_dirs = find_episode_dirs(input_path)
    generated_at = now_utc_iso()

    use_bar = tqdm is not None and len(episode_dirs) > 1
    iterator = (
        tqdm(episode_dirs, unit="episode", desc=f"Validating {input_path.name}")
        if use_bar
        else episode_dirs
    )

    # Episodes are grouped by their mirrored output subdirectory so that the
    # input layout is reproduced under --output-root. Each group keeps insertion
    # order via a dict keyed on the relative subdir.
    episodes = []
    groups = {}
    for path in iterator:
        if use_bar:
            iterator.set_postfix_str(path.name, refresh=False)
        rel_subdir, class_name = episode_output_layout(input_path, path)
        result = validate_episode(path, args)
        episodes.append(result)

        out_dir = ((args.output_root / rel_subdir).resolve()
                   if not args.no_reports else None)
        report_path = None
        if out_dir is not None:
            report_path = out_dir / f"{result['episode']}{EPISODE_REPORT_SUFFIX}"
            write_json_report(
                report_path,
                episode_report_payload(class_name, result, args, generated_at),
            )

        group = groups.setdefault(str(rel_subdir), {
            "class_name": class_name,
            "source_dir": path.parent,
            "out_dir": out_dir,
            "episodes": [],
            "report_paths": [],
        })
        group["episodes"].append(result)
        group["report_paths"].append(report_path)

    # One summary per group, written into the same mirrored directory.
    summaries = []
    summary_paths = []
    for group in groups.values():
        summary = class_summary_payload(
            group["class_name"],
            group["source_dir"],
            args,
            group["episodes"],
            group["report_paths"],
            generated_at,
        )
        summaries.append(summary)
        if group["out_dir"] is not None:
            summary_path = group["out_dir"] / CLASS_SUMMARY_NAME
            write_json_report(summary_path, summary)
            summary_paths.append(summary_path)

    legacy_report = {
        "input_path": str(input_path),
        "processed": len(episodes),
        "correct": sum(1 for ep in episodes if ep["correct"]),
        "incorrect": sum(1 for ep in episodes if not ep["correct"]),
        "episodes": episodes,
    }

    print_report(legacy_report)
    if summary_paths:
        written = sum(1 for g in groups.values()
                      for p in g["report_paths"] if p is not None)
        print(f"Wrote {written} per-episode report(s) across "
              f"{len(summary_paths)} group(s) under: {args.output_root.resolve()}")
        for summary_path in summary_paths:
            print(f"  summary: {summary_path}")
    if args.json_path:
        # A single group keeps the flat summary shape (backward compatible);
        # multiple groups are wrapped in a combined, multi-group report.
        if len(summaries) == 1:
            payload = summaries[0]
        else:
            payload = {
                "schema_version": REPORT_SCHEMA_VERSION,
                "type": "validation_summary_multi",
                "input_path": str(input_path),
                "generated_at": generated_at,
                "checks_run": checks_run_flags(args),
                "processed": len(episodes),
                "correct": sum(1 for ep in episodes if ep["correct"]),
                "incorrect": sum(1 for ep in episodes if not ep["correct"]),
                "groups": summaries,
            }
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        with args.json_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Wrote JSON report: {args.json_path}")


if __name__ == "__main__":
    try:
        main()
    except MissingDependencyError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
