#!/usr/bin/env python3
"""Unified UMI post-processing pipeline.

The pipeline is intentionally a thin orchestrator over the existing tools under
``tools/data``.  It keeps all stage reports under one output tree and writes a
final success/failure index for the processed episodes.

Default mode is dry-run.  Use ``--apply`` to modify episode data.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROBOT_CURATION = Path("/home/ubuntu/workspace/robot-data-curation/ingest")
if ROBOT_CURATION.exists() and str(ROBOT_CURATION) not in sys.path:
    sys.path.insert(0, str(ROBOT_CURATION))

from assessment.check_focus import is_defocused, score_video  # noqa: E402
from assessment.validate_raw_data import (  # noqa: E402
    DEFAULT_VALIDATE_CONFIG,
    assess_label_similarity,
    label_unverified_sides,
    validate_action,
    validate_gripper,
    validate_motion_consistency,
)
from gripper_recovery.gripper_recovery import (  # noqa: E402
    ensure_gripper_original,
    load_model,
    recover_predictions,
    refresh_checksum_file,
    rewrite_gripper_csvs,
)
from judgment.run_umi_curated_report import judge_episode_compact  # noqa: E402
from judgment.judge_trajectory import load_config as load_judgment_config  # noqa: E402
from judgment.judge_trajectory import load_transform_config, now_utc_iso, write_json  # noqa: E402
from static_check.check_static import assess_video, thresholds_from_args  # noqa: E402
from traj_smooth import smooth_trajectory  # noqa: E402

try:
    import cv2  # noqa: E402

    cv2.setNumThreads(1)
except Exception:
    pass


SIDES = ("left", "right")
WRIST_VIEW = {
    "left": "observation.image.left_wrist_view",
    "right": "observation.image.right_wrist_view",
}
SIDE_IMAGE_DIRS = {
    "left": (
        "observation.image.left_wrist_view",
        "observation.image.left_wrist_left_tactile",
        "observation.image.left_wrist_right_tactile",
    ),
    "right": (
        "observation.image.right_wrist_view",
        "observation.image.right_wrist_left_tactile",
        "observation.image.right_wrist_right_tactile",
    ),
}
SIDE_CSV_DIR_GLOBS = (
    "observation.state.*",
    "actions*",
    "episode.eef_pose_smooth",
)
STATIC_SIDE_COLUMN_DROP_GLOBS = (
    "actions*",
    "episode.eef_pose_smooth",
)
REPORT_NAME = "umi_post_report.json"


@dataclass
class StageResult:
    quality: bool
    status: str
    report: dict[str, Any]


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("/home/ubuntu/data/curated_samples/success"),
        help="Dataset root or a single episode directory.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "umi_post" / "reports",
        help="Unified report output root.",
    )
    parser.add_argument(
        "--robot-type",
        default="umi",
        help=(
            "Only process episodes whose path under data-root is "
            "task_name/date/<robot_type>/name/episode_*. Use an empty string to disable."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Modify episode data. Default is dry-run.")
    parser.add_argument("--limit", type=int, help="Process only the first N episodes.")
    parser.add_argument(
        "--episode-list",
        type=Path,
        help="Optional newline-separated episode paths. Relative paths are resolved under --data-root.",
    )
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel episode workers.")
    parser.add_argument(
        "--fast-first",
        action="store_true",
        help="Run high-concurrency detection first, then process repairable episodes from a separate queue.",
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="With --fast-first, stop after detection and do not process the repair queue.",
    )
    parser.add_argument(
        "--repair-only",
        action="store_true",
        help="Only process episodes listed in repair_queue.jsonl or --repair-list.",
    )
    parser.add_argument(
        "--precheck-only",
        action="store_true",
        help="Only run focus, static-frame, and camera/trajectory matching checks and merge them into existing reports.",
    )
    parser.add_argument(
        "--reindex-only",
        action="store_true",
        help="Rebuild success/failure/repair JSONL queues from episode reports and exit.",
    )
    parser.add_argument(
        "--repair-list",
        type=Path,
        help="Optional JSONL repair queue. Defaults to <output-root>/repair_queue.jsonl.",
    )
    parser.add_argument(
        "--repair-workers",
        type=int,
        default=4,
        help="Repair worker count used with --fast-first.",
    )
    parser.add_argument(
        "--repair-chmod",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run sudo chmod -R 777 on each episode directory before repair.",
    )
    parser.add_argument(
        "--repair-sudo-password-env",
        default="UMI_POST_SUDO_PASSWORD",
        help="Environment variable containing the sudo password used by --repair-chmod.",
    )
    parser.add_argument(
        "--skip-prechecks",
        action="store_true",
        help="Skip focus, static-frame, and camera/trajectory matching checks; only judge trajectory and gripper.",
    )
    parser.add_argument("--force", action="store_true", help="Recompute existing reports.")
    parser.add_argument("--focus-threshold", type=float, default=350.0)
    parser.add_argument("--focus-lap-p90-threshold", type=float, default=450.0)
    parser.add_argument("--focus-edge-ratio-min", type=float, default=0.24)
    parser.add_argument("--focus-gray-std-min", type=float, default=68.0)
    parser.add_argument("--focus-frames", type=int, default=20)
    parser.add_argument("--model", type=Path, default=ROOT / "gripper_recovery" / "gripper_recovery_model.json")
    parser.add_argument("--trajectory-config", type=Path, default=ROOT / "judgment" / "judgment_config.json")
    parser.add_argument("--transform-config", type=Path, default=ROOT / "transform" / "ee_trajectory_config.json")
    parser.add_argument("--skip-final-sync", action="store_true")
    return parser.parse_args()


def is_episode_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("episode_")


def matches_robot_type(data_root: Path, episode: Path, robot_type: str | None) -> bool:
    if not robot_type:
        return True
    try:
        parts = episode.relative_to(data_root).parts
    except ValueError:
        return True
    if len(parts) < 5:
        return True
    return parts[2] == robot_type


def iter_episode_dirs(root: Path, robot_type: str | None = "umi") -> list[Path]:
    root = root.expanduser().resolve()
    if is_episode_dir(root):
        return [root]
    return sorted(
        path
        for path in root.rglob("episode_*")
        if is_episode_dir(path) and matches_robot_type(root, path, robot_type)
    )


def read_episode_list(data_root: Path, path: Path, robot_type: str | None = "umi") -> list[Path]:
    episodes = []
    with path.expanduser().open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            episode = Path(line)
            if not episode.is_absolute():
                episode = data_root / episode
            episode = episode.expanduser().resolve()
            if is_episode_dir(episode) and matches_robot_type(data_root, episode, robot_type):
                episodes.append(episode)
    return sorted(dict.fromkeys(episodes))


def relative_episode_path(data_root: Path, episode: Path) -> str:
    try:
        return episode.relative_to(data_root).as_posix()
    except ValueError:
        return episode.name


def report_path(output_root: Path, rel: str) -> Path:
    return output_root / "report" / rel / REPORT_NAME


def locked_update_json(path: Path, update_fn) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    current = {}
            else:
                current = {}
            updated = update_fn(current)
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            return updated
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def stage_is_real(report: dict[str, Any], stage: str) -> bool:
    stage_report = (report.get("stages") or {}).get(stage) or {}
    return bool(stage_report) and stage_report.get("status") != "skip" and not stage_report.get("skipped")


def merge_episode_report(out_path: Path, report: dict[str, Any], preserve_prechecks: bool = False) -> dict[str, Any]:
    def update(existing: dict[str, Any]) -> dict[str, Any]:
        merged = dict(report)
        if preserve_prechecks and existing:
            merged.setdefault("stages", {})
            merged.setdefault("quality", {})
            for stage in ("focus", "label_static"):
                if stage_is_real(existing, stage):
                    merged["stages"][stage] = existing["stages"][stage]
                    if stage in (existing.get("quality") or {}):
                        merged["quality"][stage] = existing["quality"][stage]
            if stage_is_real(existing, "focus") and stage_is_real(existing, "label_static"):
                merged["quality"]["video"] = bool(
                    existing.get("quality", {}).get("focus") and existing.get("quality", {}).get("label_static")
                )
            merged["precheck_updated_at"] = existing.get("precheck_updated_at", merged.get("precheck_updated_at"))
        return merged

    return locked_update_json(out_path, update)


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]], apply: bool) -> None:
    if not apply:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def repair_permission_stage(episode: Path, args: argparse.Namespace) -> StageResult:
    enabled = getattr(args, "repair_chmod", True)
    report = {
        "stage": "repair_permissions",
        "operation": "sudo_chmod_episode",
        "path": str(episode),
        "mode": "777",
        "recursive": True,
        "enabled": bool(enabled),
        "applied": False,
    }
    if not enabled:
        report.update({"quality": True, "status": "skip", "skipped": True, "reason": "--no-repair-chmod"})
        return StageResult(quality=True, status="skip", report=report)
    if not args.apply:
        report.update({"quality": True, "status": "dry_run", "reason": "dry run"})
        return StageResult(quality=True, status="dry_run", report=report)

    password_env = getattr(args, "repair_sudo_password_env", "UMI_POST_SUDO_PASSWORD")
    password = os.environ.get(password_env or "")
    if not password:
        report.update(
            {
                "quality": False,
                "status": "false",
                "error": f"missing sudo password env: {password_env}",
            }
        )
        return StageResult(quality=False, status="false", report=report)

    try:
        result = subprocess.run(
            ["sudo", "-S", "-p", "", "chmod", "-R", "777", str(episode)],
            input=f"{password}\n",
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        report.update({"quality": False, "status": "false", "error": "sudo chmod timed out"})
        return StageResult(quality=False, status="false", report=report)
    except Exception as exc:
        report.update({"quality": False, "status": "false", "error": str(exc)})
        return StageResult(quality=False, status="false", report=report)

    stderr = (result.stderr or "").strip()
    report.update(
        {
            "quality": result.returncode == 0,
            "status": "ok" if result.returncode == 0 else "false",
            "applied": result.returncode == 0,
            "returncode": result.returncode,
        }
    )
    if stderr:
        report["stderr"] = stderr[-1000:]
    return StageResult(quality=result.returncode == 0, status=report["status"], report=report)


def backup_or_move(path: Path, episode: Path, operation: str, apply: bool) -> dict[str, Any]:
    rel = path.relative_to(episode)
    dst = episode / ".umi_post_deleted" / operation / rel
    result = {"path": str(rel), "backup_path": str(dst.relative_to(episode)), "applied": False}
    if not apply or not path.exists():
        return result
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.move(str(path), str(dst))
    result["applied"] = True
    return result


def side_partner_columns(fieldnames: list[str]) -> list[tuple[str, str]]:
    pairs = []
    names = set(fieldnames)
    for col in fieldnames:
        if col.startswith("left_"):
            right = "right_" + col[len("left_") :]
            if right in names:
                pairs.append((col, right))
    return pairs


def swap_left_right_columns(path: Path, apply: bool) -> dict[str, Any] | None:
    if not path.exists():
        return None
    fieldnames, rows = read_csv_rows(path)
    pairs = side_partner_columns(fieldnames)
    if not pairs:
        return None
    if apply:
        for row in rows:
            for left, right in pairs:
                row[left], row[right] = row.get(right, ""), row.get(left, "")
        write_csv_rows(path, fieldnames, rows, apply=True)
    return {"path": str(path), "pairs": len(pairs), "applied": bool(apply)}


def apply_lr_column_swap(episode: Path, apply: bool) -> dict[str, Any]:
    files = []
    for pattern in SIDE_CSV_DIR_GLOBS:
        for csv_path in sorted(episode.glob(f"{pattern}/data.csv")):
            item = swap_left_right_columns(csv_path, apply)
            if item:
                item["path"] = str(csv_path.relative_to(episode))
                files.append(item)
    marker = episode / ".umi_post_lr_swap.json"
    if apply and files:
        marker.write_text(
            json.dumps({"workflow": "umi_post", "operation": "lr_column_swap", "files": files}, indent=2) + "\n",
            encoding="utf-8",
        )
    return {"mode": "columns", "files": files, "marker": str(marker.relative_to(episode))}


def drop_side_columns(path: Path, side: str, apply: bool) -> dict[str, Any] | None:
    if not path.exists():
        return None
    fieldnames, rows = read_csv_rows(path)
    prefix = f"{side}_"
    drop = [col for col in fieldnames if col.startswith(prefix)]
    if not drop:
        return None
    kept = [col for col in fieldnames if col not in set(drop)]
    if apply:
        out_rows = [{col: row.get(col, "") for col in kept} for row in rows]
        write_csv_rows(path, kept, out_rows, apply=True)
    return {"path": str(path), "dropped_columns": drop, "applied": bool(apply)}


def delete_side_data(episode: Path, side: str, apply: bool) -> dict[str, Any]:
    moved = []
    csv_updates = []
    for rel in SIDE_IMAGE_DIRS[side]:
        path = episode / rel
        if path.exists():
            moved.append(backup_or_move(path, episode, f"static_{side}", apply))
    for pattern in STATIC_SIDE_COLUMN_DROP_GLOBS:
        for csv_path in sorted(episode.glob(f"{pattern}/data.csv")):
            item = drop_side_columns(csv_path, side, apply)
            if item:
                item["path"] = str(csv_path.relative_to(episode))
                csv_updates.append(item)
    return {"side": side, "moved_dirs": moved, "csv_updates": csv_updates}


def focus_stage(episode: Path, args: argparse.Namespace) -> StageResult:
    videos = {}
    failures = []
    present_sides = []
    for side in SIDES:
        path = episode / WRIST_VIEW[side] / "video.mp4"
        if not path.exists():
            videos[side] = {
                "available": False,
                "quality": None,
                "label": "missing",
                "error": "missing wrist-view video",
                "ignored_for_quality": True,
            }
            continue
        present_sides.append(side)
        result = score_video(str(path), args.focus_frames)
        label = result.get("label")
        if label is None:
            label = (
                "defocused"
                if is_defocused(
                    result,
                    threshold=args.focus_threshold,
                    lap_p90_threshold=args.focus_lap_p90_threshold,
                    edge_ratio_min=args.focus_edge_ratio_min,
                    gray_std_min=args.focus_gray_std_min,
                )
                else "in_focus"
            )
        quality = label != "defocused" and "error" not in result
        videos[side] = {
            "available": "error" not in result,
            "quality": quality,
            "label": label,
            "threshold": args.focus_threshold,
            "lap_p90_threshold": args.focus_lap_p90_threshold,
            "edge_ratio_min": args.focus_edge_ratio_min,
            "gray_std_min": args.focus_gray_std_min,
            **result,
        }
        if not quality:
            failures.append(side)
    if not present_sides:
        failures.append("all")
    quality = not failures
    return StageResult(
        quality=quality,
        status="ok" if quality else "false",
        report={
            "stage": "focus",
            "quality": quality,
            "failed_sides": failures,
            "present_sides": present_sides,
            "videos": videos,
        },
    )


def validation_args() -> argparse.Namespace:
    return argparse.Namespace(
        raw_static_deg=2.0,
        gripper_static_m=0.001,
        min_correlation=0.8,
        min_r2=0.8,
        min_gripper_m=-0.002,
        max_gripper_m=0.09,
        recompute_tolerance_m=0.003,
        disable_metadata_check=False,
        gap_tolerance_ratio=0.25,
        fps=None,
        skip_focus=True,
        skip_label=False,
        skip_motion=False,
        skip_video=False,
        skip_action=False,
        skip_gripper=False,
        video_config=DEFAULT_VALIDATE_CONFIG["video"],
        focus_config=DEFAULT_VALIDATE_CONFIG["focus"],
        label_config=DEFAULT_VALIDATE_CONFIG["label"],
        action_config=DEFAULT_VALIDATE_CONFIG["action"],
        motion_config=DEFAULT_VALIDATE_CONFIG["motion"],
    )


def static_args() -> argparse.Namespace:
    return argparse.Namespace(
        max_corner_px=4.0,
        max_translation_px=3.0,
        max_rotation_deg=1.0,
        max_scale_change=0.015,
        min_keypoints=80,
        min_matches=24,
        min_inliers=18,
        min_inlier_ratio=0.35,
        min_grid_cells=5,
        min_bbox_area=0.08,
        moved_pair_ratio=0.15,
        min_moved_pairs=1,
        strong_local_corner_px=20.0,
        strong_local_translation_px=15.0,
        strong_local_scale_change=0.05,
        min_strong_local_pairs=3,
        min_valid_pairs=3,
    )


def label_static_stage(episode: Path, args: argparse.Namespace) -> StageResult:
    v_args = validation_args()
    label = assess_label_similarity(episode, v_args.label_config)
    confused_sides = label_unverified_sides(set(label.get("mislabeled_streams") or []))
    action = validate_action(episode, v_args)
    if confused_sides:
        motion = {
            "available": False,
            "verdict": "unverified",
            "unverified_sides": sorted(confused_sides),
        }
    else:
        motion = validate_motion_consistency(episode, v_args)

    operations = []
    if motion.get("available") and motion.get("verdict") == "swap":
        operations.append({"operation": "lr_column_swap", **apply_lr_column_swap(episode, args.apply)})

    s_args = static_args()
    thresholds = thresholds_from_args(s_args)
    static_results = {}
    static_sides = []
    for side in SIDES:
        path = episode / WRIST_VIEW[side] / "video.mp4"
        if not path.exists():
            static_results[side] = {"available": False, "verdict": "missing", "error": "missing wrist-view video"}
            continue
        task = {"scope": episode.parent.name, "episode": episode.name, "view": f"{side}_wrist_view", "path": str(path)}
        result = assess_video(
            task,
            num_frames=24,
            pair_gap=10,
            max_side=640,
            max_features=2000,
            max_pairs=64,
            thresholds=thresholds,
        )
        static_results[side] = result
        if result.get("verdict") == "static":
            static_sides.append(side)

    for side in static_sides:
        operations.append({"operation": "delete_static_side", **delete_side_data(episode, side, args.apply)})

    quality = (
        not label.get("mislabeled_streams")
        and motion.get("verdict") not in {"drift", "unverified"}
        and len(static_sides) < len(SIDES)
    )
    return StageResult(
        quality=quality,
        status="ok" if quality else "false",
        report={
            "stage": "label_static",
            "quality": quality,
            "label": label,
            "action": action,
            "motion_consistency": motion,
            "static": {"static_sides": static_sides, "views": static_results},
            "operations": operations,
        },
    )


def trajectory_stage(
    episode: Path,
    rel: str,
    report_dir: Path,
    args: argparse.Namespace,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
) -> StageResult:
    report = judge_episode_compact(episode, judgment_config, transform_config, now_utc_iso())
    trajectory_report_path = report_dir / "trajectory_report.json"
    write_json(trajectory_report_path, report)

    smooth_args = SimpleNamespace(
        source="auto",
        padding_frames=10,
        merge_gap_frames=8,
        savgol_window=31,
        savgol_polyorder=3,
        cluster_savgol_window=151,
        cluster_median_window=31,
        cluster_despike_threshold_m=0.004,
        micro_savgol_window=101,
        micro_median_window=21,
        micro_despike_threshold_m=0.0025,
        include_bad_quality=True,
        overwrite=True,
        skip_clean_output=False,
        dry_run=not args.apply,
    )
    smooth_stats = None
    operations = []
    if report.get("ok") and (report.get("trajectory_quality") is True or report.get("recoverable") is True):
        segments = smooth_trajectory.selected_segments(report, include_bad_quality=True)
        smooth_stats = smooth_trajectory.smooth_episode(episode, trajectory_report_path, segments, smooth_args)
        operations.append({"operation": "smooth_trajectory", **smooth_trajectory.stats_to_dict(smooth_stats)})

    quality = bool(report.get("ok")) and (report.get("trajectory_quality") is True or report.get("recoverable") is True)
    status = "true" if report.get("trajectory_quality") is True else ("recoverable" if report.get("recoverable") else "false")
    return StageResult(
        quality=quality,
        status=status,
        report={
            "stage": "trajectory",
            "quality": quality,
            "status": status,
            "report_path": str(trajectory_report_path),
            "trajectory_report": report,
            "operations": operations,
        },
    )


def gripper_stage(episode: Path, args: argparse.Namespace, model: dict[str, Any]) -> StageResult:
    v_args = validation_args()
    before = validate_gripper(episode, v_args)
    operations = []
    side_reports = {}
    for side in SIDES:
        side_result = (before.get("sides") or {}).get(side) or {}
        if side_result.get("correct"):
            side_reports[side] = {"initial": side_result, "status": "true", "quality": True}
            continue
        pred, err = recover_predictions(episode, side, model, 64, 0.012, 30.0, 0.03)
        if pred is None or not pred.get("agreement_ok"):
            side_reports[side] = {
                "initial": side_result,
                "status": "false",
                "quality": False,
                "error": err or "recovery agreement failed",
            }
            continue
        copy_status = None
        rewritten = {}
        if args.apply:
            backup = episode / ".gripper_recovery_backup"
            backup.mkdir(exist_ok=True)
            _copied, copy_status = ensure_gripper_original(episode, backup)
            rewritten = rewrite_gripper_csvs(episode, side, pred["timestamps"], pred["predicted"], backup)
        operations.append(
            {
                "operation": "recover_gripper",
                "side": side,
                "mode": pred.get("recovery_mode"),
                "applied": bool(args.apply and rewritten),
                "rows_rewritten": sum(rewritten.values()) if rewritten else 0,
                "files_rewritten": rewritten,
                "original_status": copy_status,
            }
        )
        side_reports[side] = {
            "initial": side_result,
            "status": "recovered",
            "quality": True,
            "prediction": {k: v for k, v in pred.items() if k != "predicted"},
        }

    quality = all(item.get("quality") is True for item in side_reports.values())
    after = validate_gripper(episode, v_args) if args.apply else None
    return StageResult(
        quality=quality,
        status="ok" if quality else "false",
        report={
            "stage": "gripper",
            "quality": quality,
            "initial": before,
            "after": after,
            "sides": side_reports,
            "operations": operations,
        },
    )


def detection_args(args: argparse.Namespace) -> argparse.Namespace:
    values = vars(args).copy()
    values["apply"] = False
    return argparse.Namespace(**values)


def trajectory_check_stage(
    episode: Path,
    report_dir: Path,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
) -> StageResult:
    report = judge_episode_compact(episode, judgment_config, transform_config, now_utc_iso())
    trajectory_report_path = report_dir / "trajectory_report.json"
    write_json(trajectory_report_path, report)
    clean = bool(report.get("ok")) and report.get("trajectory_quality") is True
    recoverable = bool(report.get("ok")) and report.get("recoverable") is True
    quality = clean or recoverable
    status = "true" if clean else ("recoverable" if recoverable else "false")
    return StageResult(
        quality=quality,
        status=status,
        report={
            "stage": "trajectory",
            "quality": quality,
            "status": status,
            "clean": clean,
            "recoverable": recoverable,
            "report_path": str(trajectory_report_path),
            "trajectory_report": report,
            "operations": [],
        },
    )


def gripper_check_stage(episode: Path, model: dict[str, Any]) -> StageResult:
    before = validate_gripper(episode, validation_args())
    side_reports = {}
    for side in SIDES:
        side_result = (before.get("sides") or {}).get(side) or {}
        if side_result.get("correct"):
            side_reports[side] = {"initial": side_result, "status": "true", "quality": True, "clean": True}
            continue
        pred, err = recover_predictions(episode, side, model, 64, 0.012, 30.0, 0.03)
        recoverable = pred is not None and pred.get("agreement_ok")
        side_reports[side] = {
            "initial": side_result,
            "status": "recoverable" if recoverable else "false",
            "quality": bool(recoverable),
            "clean": False,
            "recoverable": bool(recoverable),
            "error": None if recoverable else (err or "recovery agreement failed"),
            "prediction": (
                {k: v for k, v in pred.items() if k != "predicted"}
                if recoverable
                else None
            ),
        }

    quality = bool(side_reports) and all(item.get("quality") is True for item in side_reports.values())
    clean = bool(side_reports) and all(item.get("clean") is True for item in side_reports.values())
    recoverable = quality and not clean
    status = "true" if clean else ("recoverable" if recoverable else "false")
    return StageResult(
        quality=quality,
        status=status,
        report={
            "stage": "gripper",
            "quality": quality,
            "status": status,
            "clean": clean,
            "recoverable": recoverable,
            "initial": before,
            "sides": side_reports,
            "operations": [],
        },
    )


def repair_verification_stage(
    episode: Path,
    report_dir: Path,
    args: argparse.Namespace,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
    gripper_model: dict[str, Any],
    repair_stage_reports: dict[str, Any] | None = None,
) -> StageResult:
    check_args = detection_args(args)
    repair_stage_reports = repair_stage_reports or {}
    checks: dict[str, Any] = {}
    quality: dict[str, bool | None] = {}
    failures: list[str] = []

    if args.skip_prechecks:
        checks["focus"] = skipped_stage("focus", "--skip-prechecks").report
        checks["label_static"] = skipped_stage("label_static", "--skip-prechecks").report
        quality["focus"] = None
        quality["label_static"] = None
        quality["video"] = None
    else:
        focus_report = repair_stage_reports.get("focus") or {}
        label_static = label_static_stage(episode, check_args)
        focus_quality = focus_report.get("quality") is True
        checks["focus"] = focus_report or {
            "stage": "focus",
            "quality": False,
            "status": "false",
            "error": "missing original focus result",
        }
        checks["label_static"] = label_static.report
        quality["focus"] = focus_quality
        quality["label_static"] = label_static.quality
        quality["video"] = bool(focus_quality and label_static.quality)
        if not focus_quality:
            failures.append("focus")
        if not label_static.quality:
            failures.append("label_static")

    trajectory_repair = repair_stage_reports.get("trajectory") or {}
    gripper_repair = repair_stage_reports.get("gripper") or {}
    trajectory_accepted = trajectory_repair.get("quality") is True
    gripper_accepted = gripper_repair.get("quality") is True
    checks["trajectory"] = {
        "stage": "trajectory",
        "quality": trajectory_accepted,
        "status": "accepted_after_repair" if trajectory_accepted else "false",
        "accepted_recovery_as_true": True,
        "source_status": trajectory_repair.get("status"),
        "source_quality": trajectory_repair.get("quality"),
        "operations": trajectory_repair.get("operations") or [],
    }
    checks["gripper"] = {
        "stage": "gripper",
        "quality": gripper_accepted,
        "status": "accepted_after_repair" if gripper_accepted else "false",
        "accepted_recovery_as_true": True,
        "source_status": gripper_repair.get("status"),
        "source_quality": gripper_repair.get("quality"),
        "operations": gripper_repair.get("operations") or [],
    }
    quality["trajectory"] = trajectory_accepted
    quality["gripper"] = gripper_accepted
    if not trajectory_accepted:
        failures.append("trajectory")
    if not gripper_accepted:
        failures.append("gripper")

    passed = not failures
    return StageResult(
        quality=passed,
        status="ok" if passed else "false",
        report={
            "stage": "repair_verification",
            "quality": passed,
            "status": "ok" if passed else "false",
            "strict": False,
            "trajectory_gripper_policy": "accept_completed_recovery_as_true",
            "quality_by_stage": quality,
            "failed_stages": failures,
            "checks": checks,
        },
    )


def skipped_stage(name: str, reason: str) -> StageResult:
    return StageResult(
        quality=True,
        status="skip",
        report={
            "stage": name,
            "quality": None,
            "status": "skip",
            "skipped": True,
            "reason": reason,
            "operations": [],
        },
    )


def classify_episode(
    data_root: Path,
    episode: Path,
    args: argparse.Namespace,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
    gripper_model: dict[str, Any],
) -> dict[str, Any]:
    rel = relative_episode_path(data_root, episode)
    out_path = report_path(args.output_root, rel)
    out_dir = out_path.parent
    if out_path.exists() and not args.force:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        existing.setdefault("queue_status", existing.get("status"))
        return existing

    detect_args_obj = detection_args(args)
    report: dict[str, Any] = {
        "schema_version": 1,
        "type": "umi_post_episode_report",
        "phase": "detect",
        "generated_at": utc_iso(),
        "episode": episode.name,
        "relative_path": rel,
        "path": str(episode),
        "apply": False,
        "quality": {},
        "stages": {},
        "operations": [],
        "errors": [],
    }

    try:
        if args.skip_prechecks:
            focus = skipped_stage("focus", "--skip-prechecks")
            label_static = skipped_stage("label_static", "--skip-prechecks")
            report["stages"]["focus"] = focus.report
            report["stages"]["label_static"] = label_static.report
            report["quality"]["focus"] = None
            report["quality"]["label_static"] = None
        else:
            focus = focus_stage(episode, detect_args_obj)
            report["stages"]["focus"] = focus.report
            report["quality"]["focus"] = focus.quality
            if not focus.quality:
                report["quality"].update({"video": False, "trajectory": None, "gripper": None})
                report["overall_quality"] = False
                report["status"] = "false"
                report["queue_status"] = "failure"
                report["decision_reason"] = "focus/video failed"
                write_json(out_path, report)
                return report

            label_static = label_static_stage(episode, detect_args_obj)
            report["stages"]["label_static"] = label_static.report
            report["quality"]["label_static"] = label_static.quality
            report["operations"].extend(label_static.report.get("operations") or [])

        trajectory = trajectory_check_stage(episode, out_dir, judgment_config, transform_config)
        report["stages"]["trajectory"] = trajectory.report
        report["quality"]["trajectory"] = trajectory.quality

        gripper = gripper_check_stage(episode, gripper_model)
        report["stages"]["gripper"] = gripper.report
        report["quality"]["gripper"] = gripper.quality

        video_quality = None if args.skip_prechecks else bool(focus.quality and label_static.quality)
        label_ops = [] if args.skip_prechecks else (label_static.report.get("operations") or [])
        trajectory_clean = trajectory.report.get("clean") is True
        trajectory_recoverable = trajectory.report.get("recoverable") is True
        gripper_clean = gripper.report.get("clean") is True
        gripper_recoverable = gripper.report.get("recoverable") is True

        report["quality"]["video"] = video_quality
        if video_quality is False:
            report["overall_quality"] = False
            report["status"] = "false"
            report["queue_status"] = "failure"
            report["decision_reason"] = "label/motion/static video check failed"
        elif trajectory_clean and gripper_clean and not label_ops:
            report["overall_quality"] = True
            report["status"] = "true"
            report["queue_status"] = "success"
            report["decision_reason"] = (
                "trajectory and gripper passed; prechecks skipped"
                if args.skip_prechecks
                else "video, trajectory, and gripper all passed without repair"
            )
        elif trajectory.quality and gripper.quality:
            report["overall_quality"] = None
            report["status"] = "queued"
            report["queue_status"] = "repair"
            needs = []
            if label_ops:
                needs.append("label_static_operations")
            if trajectory_recoverable:
                needs.append("trajectory_smoothing")
            if gripper_recoverable:
                needs.append("gripper_recovery")
            report["repair_needed"] = needs or ["final_sync"]
            report["decision_reason"] = "recoverable defects queued for repair"
        else:
            report["overall_quality"] = False
            report["status"] = "false"
            report["queue_status"] = "failure"
            report["decision_reason"] = "trajectory or gripper failed and is not recoverable"
    except Exception as exc:
        report["overall_quality"] = False
        report["status"] = "false"
        report["queue_status"] = "failure"
        report["errors"].append(str(exc))

    write_json(out_path, report)
    return report


def copy_gripper_columns_from_gripper_file(episode: Path, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    gripper_path = episode / "observation.state.gripper" / "data.csv"
    if not gripper_path.exists() or not rows:
        return rows
    _fields, grip_rows = read_csv_rows(gripper_path)
    by_ts = {row.get("timestamp_ms"): row for row in grip_rows if row.get("timestamp_ms")}
    out = [dict(row) for row in rows]
    for row in out:
        src = by_ts.get(row.get("timestamp_ms"))
        if not src:
            continue
        for side in SIDES:
            col = f"{side}_gripper"
            if col in row and col in src:
                row[col] = src[col]
    return out


def sync_final_eef_pose(episode: Path, apply: bool) -> dict[str, Any]:
    operations = []
    state_smooth = episode / "observation.state.eef_pose_smooth" / "data.csv"
    state_final = episode / "observation.state.eef_pose" / "data.csv"
    if state_smooth.exists():
        fields, rows = read_csv_rows(state_smooth)
        rows = copy_gripper_columns_from_gripper_file(episode, rows)
        write_csv_rows(state_final, fields, rows, apply)
        operations.append(
            {
                "operation": "sync_state_eef_pose_from_smooth",
                "source": str(state_smooth.relative_to(episode)),
                "target": str(state_final.relative_to(episode)),
                "rows": len(rows),
                "applied": bool(apply),
            }
        )

    if state_final.exists():
        state_fields, state_rows = read_csv_rows(state_final)
        for action_rel in ("actions.eef_pose/data.csv", "actions.eef_pose_smooth/data.csv"):
            action_path = episode / action_rel
            if not action_path.exists():
                continue
            action_fields, action_rows = read_csv_rows(action_path)
            out_rows = [dict(row) for row in action_rows]
            limit = min(len(out_rows), max(0, len(state_rows) - 1))
            common = [col for col in action_fields if col in state_fields and col != "timestamp_ms"]
            for i in range(limit):
                for col in common:
                    out_rows[i][col] = state_rows[i + 1].get(col, out_rows[i].get(col, ""))
            write_csv_rows(action_path, action_fields, out_rows, apply)
            operations.append(
                {
                    "operation": "sync_actions_eef_pose_one_frame_shift",
                    "source": str(state_final.relative_to(episode)),
                    "target": action_rel,
                    "rows_synced": limit,
                    "applied": bool(apply),
                }
            )
    return {"stage": "final_sync", "operations": operations}


def metadata_update(episode: Path, final_quality: bool, report: dict[str, Any], apply: bool) -> dict[str, Any]:
    metadata_path = episode / "metadata.json"
    if not metadata_path.exists():
        return {"updated": False, "error": "missing metadata.json"}
    if not apply:
        return {"updated": False, "dry_run": True}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"updated": False, "error": f"invalid metadata.json: {exc}"}
    quality = metadata.setdefault("quality", {})
    quality["umi_post_quality"] = bool(final_quality)
    quality["umi_post_report"] = {
        "overall_quality": bool(final_quality),
        "report_generated_at": report["generated_at"],
        "stage_quality": report["quality"],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"updated": True, "path": str(metadata_path)}


def process_episode(
    data_root: Path,
    episode: Path,
    args: argparse.Namespace,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
    gripper_model: dict[str, Any],
) -> dict[str, Any]:
    rel = relative_episode_path(data_root, episode)
    out_path = report_path(args.output_root, rel)
    out_dir = out_path.parent
    existing_report = {}
    if out_path.exists():
        try:
            existing_report = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_report = {}
    if out_path.exists() and not args.force:
        return existing_report

    report: dict[str, Any] = {
        "schema_version": 1,
        "type": "umi_post_episode_report",
        "generated_at": utc_iso(),
        "episode": episode.name,
        "relative_path": rel,
        "path": str(episode),
        "apply": bool(args.apply),
        "quality": {},
        "stages": {},
        "operations": [],
        "errors": [],
    }

    try:
        if args.skip_prechecks:
            focus = skipped_stage("focus", "--skip-prechecks")
            label_static = skipped_stage("label_static", "--skip-prechecks")
            report["stages"]["focus"] = focus.report
            report["stages"]["label_static"] = label_static.report
            report["quality"]["focus"] = None
            report["quality"]["label_static"] = None
        else:
            original_focus = (existing_report.get("stages") or {}).get("focus") or {}
            original_focus_quality = (existing_report.get("quality") or {}).get("focus")
            if not original_focus or original_focus.get("status") == "skip" or original_focus.get("skipped"):
                report["stages"]["focus"] = {
                    "stage": "focus",
                    "quality": False,
                    "status": "false",
                    "error": "missing original focus result; repair skipped",
                }
                report["quality"].update({"focus": False, "video": False, "trajectory": None, "gripper": None})
                report["overall_quality"] = False
                report["status"] = "false"
                report["queue_status"] = "failure"
                report["decision_reason"] = "missing original focus result; repair skipped"
                report["metadata"] = metadata_update(episode, False, report, args.apply)
                return merge_episode_report(out_path, report, preserve_prechecks=False)

            report["stages"]["focus"] = original_focus
            report["quality"]["focus"] = original_focus_quality
            if original_focus_quality is not True:
                report["quality"].update({"video": False, "trajectory": None, "gripper": None})
                report["overall_quality"] = False
                report["status"] = "false"
                report["queue_status"] = "failure"
                report["decision_reason"] = "original focus failed; repair skipped"
                report["metadata"] = metadata_update(episode, False, report, args.apply)
                return merge_episode_report(out_path, report, preserve_prechecks=False)

            permissions = repair_permission_stage(episode, args)
            report["stages"]["repair_permissions"] = permissions.report
            report["operations"].append(
                {
                    "operation": "sudo_chmod_episode",
                    "stage": "repair_permissions",
                    "applied": permissions.report.get("applied", False),
                    "status": permissions.status,
                }
            )
            if not permissions.quality:
                report["overall_quality"] = False
                report["status"] = "false"
                report["queue_status"] = "failure"
                report["errors"].append(permissions.report.get("error", "repair permission stage failed"))
                return merge_episode_report(out_path, report, preserve_prechecks=False)

            label_static = label_static_stage(episode, args)
            report["stages"]["label_static"] = label_static.report
            report["quality"]["label_static"] = label_static.quality
            report["operations"].extend(label_static.report.get("operations") or [])

        trajectory = trajectory_stage(episode, rel, out_dir, args, judgment_config, transform_config)
        report["stages"]["trajectory"] = trajectory.report
        report["quality"]["trajectory"] = trajectory.quality
        report["operations"].extend(trajectory.report.get("operations") or [])

        gripper = gripper_stage(episode, args, gripper_model)
        report["stages"]["gripper"] = gripper.report
        report["quality"]["gripper"] = gripper.quality
        report["operations"].extend(gripper.report.get("operations") or [])

        if not args.skip_final_sync:
            final_sync = sync_final_eef_pose(episode, args.apply)
            report["stages"]["final_sync"] = final_sync
            report["operations"].extend(final_sync.get("operations") or [])

        verification = repair_verification_stage(
            episode,
            out_dir,
            args,
            judgment_config,
            transform_config,
            gripper_model,
            report["stages"],
        )
        report["stages"]["repair_verification"] = verification.report
        report["quality"].update(verification.report.get("quality_by_stage") or {})
        report["overall_quality"] = bool(verification.quality)
        report["status"] = "true" if verification.quality else "false"
        report["queue_status"] = "success" if verification.quality else "failure"
        report["verification_status"] = verification.status
        report["verification_failed_stages"] = verification.report.get("failed_stages") or []
        report["metadata"] = metadata_update(episode, verification.quality, report, args.apply)
        if args.apply:
            try:
                refresh_checksum_file(episode)
            except Exception as exc:  # checksum failure should be visible but not hide report.
                report["errors"].append(f"checksum refresh failed: {exc}")
    except Exception as exc:
        report["overall_quality"] = False
        report["status"] = "false"
        report["queue_status"] = "failure"
        report["errors"].append(str(exc))

    return merge_episode_report(out_path, report, preserve_prechecks=args.skip_prechecks)


def write_final_indexes(output_root: Path, reports: list[dict[str, Any]]) -> dict[str, Any]:
    success = sorted(r["relative_path"] for r in reports if r.get("overall_quality") is True)
    failure = sorted(r["relative_path"] for r in reports if r.get("overall_quality") is not True)
    index = {"success": success, "failure": failure}
    write_json(output_root / "success_failure.json", index)
    summary = {
        "generated_at": utc_iso(),
        "counts": {"success": len(success), "failure": len(failure), "total": len(reports)},
    }
    write_json(output_root / "summary.json", summary)
    summary_rows = [
        {
            "relative_path": r.get("relative_path", ""),
            "overall_quality": str(r.get("overall_quality") is True).lower(),
            "focus": r.get("quality", {}).get("focus"),
            "video": r.get("quality", {}).get("video"),
            "trajectory": r.get("quality", {}).get("trajectory"),
            "gripper": r.get("quality", {}).get("gripper"),
            "report_path": str(report_path(output_root, r.get("relative_path", ""))),
        }
        for r in reports
    ]
    csv_path = output_root / "summary.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        fields = ["relative_path", "overall_quality", "focus", "video", "trajectory", "gripper", "report_path"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)
    return index


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_fast_first_index(
    output_root: Path,
    success: list[str],
    failure: list[str],
    repair: list[str],
    phase: str,
) -> None:
    payload = {
        "generated_at": utc_iso(),
        "phase": phase,
        "counts": {
            "success": len(success),
            "failure": len(failure),
            "repair": len(repair),
            "total": len(success) + len(failure) + len(repair),
        },
        "success": sorted(success),
        "failure": sorted(failure),
        "repair": sorted(repair),
    }
    write_json(output_root / "fast_first_index.json", payload)


def derive_queue_from_report(report: dict[str, Any]) -> tuple[str, list[str], str]:
    stages = report.get("stages") or {}
    quality = report.get("quality") or {}
    repair_needed = list(report.get("repair_needed") or [])

    focus_report = stages.get("focus") or {}
    label_report = stages.get("label_static") or {}
    focus_quality = quality.get("focus")
    label_quality = quality.get("label_static")
    trajectory_quality = quality.get("trajectory")
    gripper_quality = quality.get("gripper")

    if stage_is_real(report, "focus") and focus_quality is not True:
        return "failure", repair_needed, "focus/precheck failed"
    if stage_is_real(report, "label_static") and label_quality is not True:
        return "failure", repair_needed, "static/camera-trajectory precheck failed"

    precheck_ops = label_report.get("operations") or []
    if precheck_ops and "precheck_operations" not in repair_needed:
        repair_needed.append("precheck_operations")

    trajectory_report = stages.get("trajectory") or {}
    trajectory_status = trajectory_report.get("status")
    if trajectory_quality is not True:
        return "failure", repair_needed, "trajectory failed"
    if trajectory_status == "recoverable" and "trajectory_smoothing" not in repair_needed:
        repair_needed.append("trajectory_smoothing")

    gripper_report = stages.get("gripper") or {}
    gripper_status = gripper_report.get("status")
    if gripper_quality is not True:
        return "failure", repair_needed, "gripper failed"
    if gripper_status == "recoverable" and "gripper_recovery" not in repair_needed:
        repair_needed.append("gripper_recovery")

    if repair_needed:
        return "repair", repair_needed, "recoverable operations pending"
    return "success", [], "all required checks passed"


def reindex_reports(data_root: Path, episodes: list[Path], output_root: Path) -> dict[str, Any]:
    success_jsonl = output_root / "success_detected.jsonl"
    failure_jsonl = output_root / "failure_detected.jsonl"
    repair_jsonl = output_root / "repair_queue.jsonl"
    for path in (success_jsonl, failure_jsonl, repair_jsonl):
        if path.exists():
            path.unlink()

    counts = {"success": 0, "failure": 0, "repair": 0, "missing": 0, "error": 0}
    progress_path = output_root / "reindex_progress.json"

    for i, episode in enumerate(episodes, 1):
        rel = relative_episode_path(data_root, episode)
        out_path = report_path(output_root, rel)
        if not out_path.exists():
            counts["missing"] += 1
            append_jsonl(failure_jsonl, {"relative_path": rel, "reason": "missing report"})
            continue

        def update(report: dict[str, Any]) -> dict[str, Any]:
            queue, repair_needed, reason = derive_queue_from_report(report)
            report["queue_status"] = queue
            report["status"] = "queued" if queue == "repair" else ("true" if queue == "success" else "false")
            report["overall_quality"] = True if queue == "success" else (None if queue == "repair" else False)
            report["repair_needed"] = repair_needed
            report["decision_reason"] = reason
            report["reindexed_at"] = utc_iso()
            return report

        try:
            report = locked_update_json(out_path, update)
            queue = report.get("queue_status")
            counts[queue] = counts.get(queue, 0) + 1
            payload = {"relative_path": rel, "report": str(out_path), "reason": report.get("decision_reason")}
            if queue == "success":
                append_jsonl(success_jsonl, payload)
            elif queue == "repair":
                payload["repair_needed"] = report.get("repair_needed") or []
                append_jsonl(repair_jsonl, payload)
            else:
                append_jsonl(failure_jsonl, payload)
        except Exception as exc:
            counts["error"] += 1
            append_jsonl(failure_jsonl, {"relative_path": rel, "reason": f"reindex error: {exc}"})

        if i == 1 or i % 1000 == 0 or i == len(episodes):
            write_json(
                progress_path,
                {
                    "generated_at": utc_iso(),
                    "processed": i,
                    "total": len(episodes),
                    "remaining": len(episodes) - i,
                    "counts": counts,
                },
            )

    write_fast_first_index(
        output_root,
        [json.loads(line)["relative_path"] for line in success_jsonl.open(encoding="utf-8")] if success_jsonl.exists() else [],
        [json.loads(line)["relative_path"] for line in failure_jsonl.open(encoding="utf-8")] if failure_jsonl.exists() else [],
        [json.loads(line)["relative_path"] for line in repair_jsonl.open(encoding="utf-8")] if repair_jsonl.exists() else [],
        "reindexed",
    )
    return counts


def classify_episode_worker(
    item: tuple[
        str,
        str,
        argparse.Namespace,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ],
) -> dict[str, Any]:
    data_root, episode, args, judgment_config, transform_config, gripper_model = item
    return classify_episode(
        Path(data_root),
        Path(episode),
        args,
        judgment_config,
        transform_config,
        gripper_model,
    )


def process_episode_worker(
    item: tuple[
        str,
        str,
        argparse.Namespace,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ],
) -> dict[str, Any]:
    data_root, episode, args, judgment_config, transform_config, gripper_model = item
    return process_episode(
        Path(data_root),
        Path(episode),
        args,
        judgment_config,
        transform_config,
        gripper_model,
    )


def run_parallel_episodes(
    episodes: list[Path],
    workers: int,
    make_job,
    worker_fn,
    on_result,
) -> list[dict[str, Any]]:
    reports = []
    workers = max(1, int(workers or 1))
    if workers == 1:
        for i, episode in enumerate(episodes, 1):
            report = worker_fn(make_job(episode))
            reports.append(report)
            on_result(i, len(episodes), report)
        return reports

    with ProcessPoolExecutor(max_workers=workers) as executor:
        episode_iter = iter(episodes)
        pending = {}

        def submit_next() -> bool:
            try:
                episode = next(episode_iter)
            except StopIteration:
                return False
            future = executor.submit(worker_fn, make_job(episode))
            pending[future] = episode
            return True

        for _ in range(workers * 2):
            if not submit_next():
                break

        i = 0
        while pending:
            for future in as_completed(list(pending)):
                pending.pop(future)
                i += 1
                report = future.result()
                reports.append(report)
                on_result(i, len(episodes), report)
                submit_next()
                break
    return reports


def run_fast_first(
    data_root: Path,
    episodes: list[Path],
    args: argparse.Namespace,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
    gripper_model: dict[str, Any],
) -> list[dict[str, Any]]:
    success: list[str] = []
    failure: list[str] = []
    repair: list[str] = []
    args.output_root.mkdir(parents=True, exist_ok=True)

    success_jsonl = args.output_root / "success_detected.jsonl"
    failure_jsonl = args.output_root / "failure_detected.jsonl"
    repair_jsonl = args.output_root / "repair_queue.jsonl"
    for path in (success_jsonl, failure_jsonl, repair_jsonl):
        if path.exists():
            path.unlink()

    def make_detect_job(episode: Path) -> tuple[str, str, argparse.Namespace, dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (str(data_root), str(episode), args, judgment_config, transform_config, gripper_model)

    def on_detect(i: int, total: int, report: dict[str, Any]) -> None:
        rel = report["relative_path"]
        queue_status = report.get("queue_status") or report.get("status")
        if queue_status == "success" or report.get("overall_quality") is True:
            success.append(rel)
            append_jsonl(success_jsonl, {"relative_path": rel, "report": str(report_path(args.output_root, rel))})
        elif queue_status == "repair" or report.get("status") == "queued":
            repair.append(rel)
            append_jsonl(
                repair_jsonl,
                {
                    "relative_path": rel,
                    "report": str(report_path(args.output_root, rel)),
                    "repair_needed": report.get("repair_needed") or [],
                },
            )
        else:
            failure.append(rel)
            append_jsonl(
                failure_jsonl,
                {
                    "relative_path": rel,
                    "report": str(report_path(args.output_root, rel)),
                    "reason": report.get("decision_reason") or report.get("errors") or "failed",
                },
            )
        if i == 1 or i % 100 == 0 or i == total:
            write_fast_first_index(args.output_root, success, failure, repair, "detect")
        print(f"[detect {i}/{total}] {queue_status} {rel}", flush=True)

    detect_reports = run_parallel_episodes(
        episodes,
        args.workers,
        make_detect_job,
        classify_episode_worker,
        on_detect,
    )
    if args.detect_only:
        write_fast_first_index(args.output_root, success, failure, repair, "detect_complete")
        return detect_reports

    write_fast_first_index(args.output_root, success, failure, repair, "repair")

    repair_by_rel = {relative_episode_path(data_root, episode): episode for episode in episodes}
    repair_episodes = [repair_by_rel[rel] for rel in repair if rel in repair_by_rel]
    repair_args = argparse.Namespace(**vars(args))
    repair_args.force = True

    def make_repair_job(episode: Path) -> tuple[str, str, argparse.Namespace, dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (str(data_root), str(episode), repair_args, judgment_config, transform_config, gripper_model)

    repaired_reports: list[dict[str, Any]] = []

    def on_repair(i: int, total: int, report: dict[str, Any]) -> None:
        print(f"[repair {i}/{total}] {report['status']} {report['relative_path']}", flush=True)

    if repair_episodes:
        repaired_reports = run_parallel_episodes(
            repair_episodes,
            args.repair_workers,
            make_repair_job,
            process_episode_worker,
            on_repair,
        )

    repaired_by_rel = {r["relative_path"]: r for r in repaired_reports}
    final_reports = []
    for report in detect_reports:
        final_reports.append(repaired_by_rel.get(report["relative_path"], report))
    return final_reports


def read_repair_queue(path: Path) -> list[str]:
    rels = []
    if not path.exists():
        return rels
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rel = item.get("relative_path")
            if rel:
                rels.append(rel)
    return sorted(dict.fromkeys(rels))


def report_needs_repair(output_root: Path, rel: str, force: bool) -> bool:
    if force:
        return True
    path = report_path(output_root, rel)
    if not path.exists():
        return True
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    queue_status = report.get("queue_status") or report.get("status")
    return queue_status == "repair" or report.get("status") == "queued"


def report_needs_precheck(output_root: Path, rel: str, force: bool) -> bool:
    if force:
        return True
    path = report_path(output_root, rel)
    if not path.exists():
        return True
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    return not (stage_is_real(report, "focus") and stage_is_real(report, "label_static"))


def precheck_episode(
    data_root: Path,
    episode: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    rel = relative_episode_path(data_root, episode)
    out_path = report_path(args.output_root, rel)
    if not report_needs_precheck(args.output_root, rel, args.force):
        return {"relative_path": rel, "status": "skipped", "precheck_quality": None}

    check_args = detection_args(args)
    check_args.apply = False
    try:
        focus = focus_stage(episode, check_args)
        label_static = label_static_stage(episode, check_args)
        precheck_quality = bool(focus.quality and label_static.quality)
        status = "true" if precheck_quality else "false"
        error = None
    except Exception as exc:
        focus = skipped_stage("focus", "precheck failed")
        label_static = skipped_stage("label_static", "precheck failed")
        precheck_quality = False
        status = "error"
        error = str(exc)

    def update(existing: dict[str, Any]) -> dict[str, Any]:
        report = dict(existing) if existing else {}
        report.setdefault("schema_version", 1)
        report.setdefault("type", "umi_post_episode_report")
        report.setdefault("episode", episode.name)
        report.setdefault("relative_path", rel)
        report.setdefault("path", str(episode))
        report.setdefault("stages", {})
        report.setdefault("quality", {})
        report.setdefault("operations", [])
        report.setdefault("errors", [])
        report["stages"]["focus"] = focus.report
        report["stages"]["label_static"] = label_static.report
        report["quality"]["focus"] = focus.quality if status != "error" else False
        report["quality"]["label_static"] = label_static.quality if status != "error" else False
        report["quality"]["video"] = precheck_quality
        report["precheck_quality"] = precheck_quality
        report["precheck_status"] = status
        report["precheck_updated_at"] = utc_iso()
        if error:
            report["errors"].append(f"precheck failed: {error}")
        return report

    merged = locked_update_json(out_path, update)
    return {
        "relative_path": rel,
        "status": status,
        "precheck_quality": precheck_quality,
        "report": str(out_path),
        "errors": merged.get("errors") or [],
    }


def precheck_episode_worker(item: tuple[str, str, argparse.Namespace]) -> dict[str, Any]:
    data_root, episode, args = item
    return precheck_episode(Path(data_root), Path(episode), args)


def run_precheck_only(
    data_root: Path,
    episodes: list[Path],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    targets = [
        episode
        for episode in episodes
        if report_needs_precheck(args.output_root, relative_episode_path(data_root, episode), args.force)
    ]
    progress_path = args.output_root / "precheck_progress.json"
    jsonl_path = args.output_root / "precheck_results.jsonl"
    if args.force and jsonl_path.exists():
        jsonl_path.unlink()

    counts = {"true": 0, "false": 0, "error": 0, "skipped": len(episodes) - len(targets)}

    def write_progress(done: int) -> None:
        write_json(
            progress_path,
            {
                "generated_at": utc_iso(),
                "total_episodes": len(episodes),
                "to_process": len(targets),
                "completed": done,
                "remaining": len(targets) - done,
                "counts": counts,
            },
        )

    def make_job(episode: Path) -> tuple[str, str, argparse.Namespace]:
        return (str(data_root), str(episode), args)

    def on_result(i: int, total: int, report: dict[str, Any]) -> None:
        status = report.get("status", "error")
        counts[status] = counts.get(status, 0) + 1
        append_jsonl(jsonl_path, report)
        if i == 1 or i % 100 == 0 or i == total:
            write_progress(i)
        print(f"[precheck {i}/{total}] {status} {report['relative_path']}", flush=True)

    write_progress(0)
    reports = run_parallel_episodes(targets, args.workers, make_job, precheck_episode_worker, on_result)
    counts = reindex_reports(data_root, episodes, args.output_root)
    print(json.dumps({"reindex": counts}, ensure_ascii=False), flush=True)
    return reports


def run_repair_only(
    data_root: Path,
    all_episodes: list[Path],
    args: argparse.Namespace,
    judgment_config: dict[str, Any],
    transform_config: dict[str, Any],
    gripper_model: dict[str, Any],
) -> list[dict[str, Any]]:
    repair_list = args.repair_list or (args.output_root / "repair_queue.jsonl")
    rels = read_repair_queue(repair_list)
    episode_by_rel = {relative_episode_path(data_root, episode): episode for episode in all_episodes}
    repair_episodes = [
        episode_by_rel[rel]
        for rel in rels
        if rel in episode_by_rel and report_needs_repair(args.output_root, rel, args.force)
    ]
    repair_args = argparse.Namespace(**vars(args))
    repair_args.force = True

    def make_repair_job(episode: Path) -> tuple[str, str, argparse.Namespace, dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (str(data_root), str(episode), repair_args, judgment_config, transform_config, gripper_model)

    def on_repair(i: int, total: int, report: dict[str, Any]) -> None:
        print(f"[repair-only {i}/{total}] {report['status']} {report['relative_path']}", flush=True)

    print(
        json.dumps(
            {
                "repair_list": str(repair_list),
                "queued": len(rels),
                "to_process": len(repair_episodes),
                "skipped_finished": len(rels) - len(repair_episodes),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return run_parallel_episodes(
        repair_episodes,
        args.repair_workers,
        make_repair_job,
        process_episode_worker,
        on_repair,
    )


def main() -> int:
    args = parse_args()
    data_root = args.data_root.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    robot_type = args.robot_type or None
    if args.episode_list:
        episodes = read_episode_list(data_root, args.episode_list, robot_type)
    else:
        episodes = iter_episode_dirs(data_root, robot_type)
    if args.limit:
        episodes = episodes[: args.limit]
    if not episodes:
        raise SystemExit(f"No episode_* directories found under {data_root}")

    judgment_config = load_judgment_config(args.trajectory_config if args.trajectory_config.exists() else None)
    transform_config = load_transform_config(args.transform_config)
    gripper_model = load_model(args.model)
    args.output_root.mkdir(parents=True, exist_ok=True)

    workers = max(1, int(args.workers or 1))
    if args.reindex_only:
        counts = reindex_reports(data_root, episodes, args.output_root)
        print(json.dumps({"reindex": counts}, ensure_ascii=False, indent=2))
        return 0
    elif args.precheck_only:
        reports = run_precheck_only(data_root, episodes, args)
        print(json.dumps({"prechecked": len(reports)}, ensure_ascii=False, indent=2))
        print("precheck-only: merged focus/static/camera-trajectory checks into episode reports")
        return 0
    elif args.repair_only:
        reports = run_repair_only(data_root, episodes, args, judgment_config, transform_config, gripper_model)
        print(json.dumps({"repaired_or_failed": len(reports)}, ensure_ascii=False, indent=2))
        print("repair-only: updated episode reports; global success_failure.json is not overwritten")
        return 0
    elif args.fast_first:
        reports = run_fast_first(data_root, episodes, args, judgment_config, transform_config, gripper_model)
        if args.detect_only:
            print(json.dumps({"detected": len(reports)}, ensure_ascii=False, indent=2))
            print("detect-only: repair queue was not processed; global success_failure.json is not overwritten")
            return 0
    else:
        def make_job(episode: Path) -> tuple[str, str, argparse.Namespace, dict[str, Any], dict[str, Any], dict[str, Any]]:
            return (str(data_root), str(episode), args, judgment_config, transform_config, gripper_model)

        def on_result(i: int, total: int, report: dict[str, Any]) -> None:
            print(f"[{i}/{total}] {report['status']} {report['relative_path']}", flush=True)

        reports = run_parallel_episodes(episodes, workers, make_job, process_episode_worker, on_result)

    index = write_final_indexes(args.output_root, reports)
    print(json.dumps({"success": len(index["success"]), "failure": len(index["failure"]), "total": len(reports)}, ensure_ascii=False, indent=2))
    print(f"reports: {args.output_root}")
    print(f"index: {args.output_root / 'success_failure.json'}")
    if not args.apply:
        print("dry-run: no episode data was modified; add --apply to execute operations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
