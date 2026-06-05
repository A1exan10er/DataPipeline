#!/usr/bin/env python3
"""Read-only prototype checker for motion abnormality detection.

The script is intentionally conservative: it reports findings and never moves,
deletes, or rewrites dataset files.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MAX_EPISODES_PER_TASK = 20
DEFAULT_MAX_ROWS_PER_CSV = 0


@dataclass(frozen=True)
class Thresholds:
    timestamp_gap_warn_ms: int = 200
    timestamp_gap_major_ms: int = 1000
    joint_position_abs_warn: float = 8.0
    joint_position_abs_fail: float = 12.0
    joint_step_warn: float = 0.35
    joint_step_fail: float = 1.0
    joint_velocity_warn: float = 4.0
    joint_velocity_fail: float = 8.0
    joint_accel_warn: float = 80.0
    joint_accel_fail: float = 200.0
    eef_position_abs_warn: float = 3.0
    eef_position_abs_fail: float = 10.0
    eef_step_warn: float = 0.08
    eef_step_fail: float = 0.25
    eef_velocity_warn: float = 2.0
    eef_velocity_fail: float = 5.0
    numeric_abs_fail: float = 1_000_000.0


def is_motion_csv(path: Path) -> bool:
    modality = path.parent.name
    return (
        "eef_pose" in modality
        or "joint_position" in modality
        or "joint_velocity" in modality
        or modality == "observation.state.gripper"
    )


def find_episodes(root: Path, max_episodes_per_task: int) -> list[Path]:
    if (root / "metadata.json").is_file() and root.name.startswith("episode_"):
        return [root]

    task_counts: dict[str, int] = {}
    episodes: list[Path] = []
    for metadata_path in sorted(root.rglob("metadata.json")):
        episode_dir = metadata_path.parent
        if not episode_dir.name.startswith("episode_"):
            continue

        try:
            task = episode_dir.relative_to(root).parts[0]
        except ValueError:
            task = episode_dir.parent.parent.parent.name if len(episode_dir.parents) >= 3 else ""

        count = task_counts.get(task, 0)
        if max_episodes_per_task and count >= max_episodes_per_task:
            continue
        task_counts[task] = count + 1
        episodes.append(episode_dir)
    return episodes


def parse_metadata(episode_dir: Path) -> dict[str, Any]:
    metadata_path = episode_dir / "metadata.json"
    if not metadata_path.is_file():
        return {}
    try:
        with metadata_path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def classify_columns(headers: list[str], modality: str) -> dict[str, list[int]]:
    groups = {
        "timestamp": [],
        "joint_position": [],
        "joint_velocity": [],
        "eef_position": [],
        "eef_rotation": [],
        "gripper": [],
        "other_numeric": [],
    }
    is_joint_position = "joint_position" in modality
    is_joint_velocity = "joint_velocity" in modality
    is_eef_pose = "eef_pose" in modality
    is_gripper = modality == "observation.state.gripper"

    for idx, header in enumerate(headers):
        name = header.lower()
        if name == "timestamp_ms":
            groups["timestamp"].append(idx)
        elif "gripper" in name or is_gripper:
            groups["gripper"].append(idx)
        elif is_joint_position and (name.endswith(".pos") or "_j" in name or name.startswith("j")):
            groups["joint_position"].append(idx)
        elif is_joint_velocity and (name.endswith(".vel") or "_v" in name or name.startswith("v")):
            groups["joint_velocity"].append(idx)
        elif is_eef_pose and (
            name.endswith("_x")
            or name.endswith("_y")
            or name.endswith("_z")
            or name in {"x", "y", "z"}
        ):
            groups["eef_position"].append(idx)
        elif is_eef_pose and ("_r" in name or name.startswith("r")):
            groups["eef_rotation"].append(idx)
        else:
            groups["other_numeric"].append(idx)
    return groups


def finding(
    episode_dir: Path,
    csv_path: Path,
    check_name: str,
    severity: str,
    message: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "episode_path": str(episode_dir),
        "csv_path": str(csv_path),
        "check_name": check_name,
        "severity": severity,
        "message": message,
        "details": details,
    }


def maybe_add_threshold_finding(
    findings: list[dict[str, Any]],
    episode_dir: Path,
    csv_path: Path,
    check_name: str,
    column: str,
    value: float,
    warn_threshold: float,
    fail_threshold: float,
    timestamp_ms: int | None,
    unit: str,
) -> None:
    abs_value = abs(value)
    if abs_value >= fail_threshold:
        severity = "critical"
        threshold = fail_threshold
    elif abs_value >= warn_threshold:
        severity = "major"
        threshold = warn_threshold
    else:
        return

    findings.append(
        finding(
            episode_dir,
            csv_path,
            check_name,
            severity,
            f"{column} reached {value:.6g} {unit}",
            {
                "column": column,
                "value": value,
                "abs_value": abs_value,
                "threshold": threshold,
                "timestamp_ms": timestamp_ms,
                "unit": unit,
            },
        )
    )


def read_float(row: list[str], idx: int) -> float | None:
    if idx >= len(row):
        return None
    try:
        value = float(row[idx])
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def analyze_csv(
    episode_dir: Path,
    csv_path: Path,
    thresholds: Thresholds,
    max_rows: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.reader(file)
            headers = next(reader)
            groups = classify_columns(headers, csv_path.parent.name)
            timestamp_idx = groups["timestamp"][0] if groups["timestamp"] else None
            previous_ts: int | None = None
            previous_values: dict[int, float] = {}
            previous_velocity: dict[int, float] = {}
            rows = 0

            for row in reader:
                rows += 1
                if max_rows and rows > max_rows:
                    break

                timestamp_ms: int | None = None
                dt_s: float | None = None
                if timestamp_idx is not None and timestamp_idx < len(row):
                    try:
                        timestamp_ms = int(float(row[timestamp_idx]))
                    except ValueError:
                        findings.append(
                            finding(
                                episode_dir,
                                csv_path,
                                "timestamp_parse",
                                "critical",
                                "timestamp_ms is not numeric",
                                {"row_number": rows, "value": row[timestamp_idx]},
                            )
                        )

                    if timestamp_ms is not None and previous_ts is not None:
                        delta_ms = timestamp_ms - previous_ts
                        if delta_ms <= 0:
                            findings.append(
                                finding(
                                    episode_dir,
                                    csv_path,
                                    "timestamp_monotonic",
                                    "critical",
                                    "timestamp_ms is not strictly increasing",
                                    {
                                        "row_number": rows,
                                        "previous_timestamp_ms": previous_ts,
                                        "timestamp_ms": timestamp_ms,
                                        "delta_ms": delta_ms,
                                    },
                                )
                            )
                        else:
                            dt_s = delta_ms / 1000.0
                            if delta_ms >= thresholds.timestamp_gap_major_ms:
                                severity = "major"
                                threshold = thresholds.timestamp_gap_major_ms
                            elif delta_ms >= thresholds.timestamp_gap_warn_ms:
                                severity = "minor"
                                threshold = thresholds.timestamp_gap_warn_ms
                            else:
                                severity = ""
                                threshold = 0
                            if severity:
                                findings.append(
                                    finding(
                                        episode_dir,
                                        csv_path,
                                        "timestamp_gap",
                                        severity,
                                        f"timestamp gap is {delta_ms} ms",
                                        {
                                            "row_number": rows,
                                            "previous_timestamp_ms": previous_ts,
                                            "timestamp_ms": timestamp_ms,
                                            "delta_ms": delta_ms,
                                            "threshold_ms": threshold,
                                        },
                                    )
                                )

                    if timestamp_ms is not None:
                        previous_ts = timestamp_ms

                numeric_indices = (
                    groups["joint_position"]
                    + groups["joint_velocity"]
                    + groups["eef_position"]
                    + groups["eef_rotation"]
                    + groups["gripper"]
                    + groups["other_numeric"]
                )
                for idx in numeric_indices:
                    value = read_float(row, idx)
                    if value is None:
                        findings.append(
                            finding(
                                episode_dir,
                                csv_path,
                                "numeric_parse",
                                "critical",
                                f"{headers[idx]} is missing, non-numeric, NaN, or Inf",
                                {
                                    "row_number": rows,
                                    "column": headers[idx],
                                    "value": row[idx] if idx < len(row) else "",
                                    "timestamp_ms": timestamp_ms,
                                },
                            )
                        )
                        continue
                    maybe_add_threshold_finding(
                        findings,
                        episode_dir,
                        csv_path,
                        "numeric_abs_limit",
                        headers[idx],
                        value,
                        thresholds.numeric_abs_fail,
                        thresholds.numeric_abs_fail,
                        timestamp_ms,
                        "raw",
                    )

                for idx in groups["joint_position"]:
                    value = read_float(row, idx)
                    if value is None:
                        continue
                    maybe_add_threshold_finding(
                        findings,
                        episode_dir,
                        csv_path,
                        "joint_position_abs",
                        headers[idx],
                        value,
                        thresholds.joint_position_abs_warn,
                        thresholds.joint_position_abs_fail,
                        timestamp_ms,
                        "rad",
                    )
                    prev = previous_values.get(idx)
                    if prev is not None:
                        step = value - prev
                        maybe_add_threshold_finding(
                            findings,
                            episode_dir,
                            csv_path,
                            "joint_position_step",
                            headers[idx],
                            step,
                            thresholds.joint_step_warn,
                            thresholds.joint_step_fail,
                            timestamp_ms,
                            "rad/frame",
                        )
                        if dt_s and dt_s > 0:
                            derived_velocity = step / dt_s
                            maybe_add_threshold_finding(
                                findings,
                                episode_dir,
                                csv_path,
                                "joint_velocity_derived",
                                headers[idx],
                                derived_velocity,
                                thresholds.joint_velocity_warn,
                                thresholds.joint_velocity_fail,
                                timestamp_ms,
                                "rad/s",
                            )
                    previous_values[idx] = value

                for idx in groups["joint_velocity"]:
                    value = read_float(row, idx)
                    if value is None:
                        continue
                    maybe_add_threshold_finding(
                        findings,
                        episode_dir,
                        csv_path,
                        "joint_velocity_reported",
                        headers[idx],
                        value,
                        thresholds.joint_velocity_warn,
                        thresholds.joint_velocity_fail,
                        timestamp_ms,
                        "rad/s",
                    )
                    prev_vel = previous_velocity.get(idx)
                    if prev_vel is not None and dt_s and dt_s > 0:
                        accel = (value - prev_vel) / dt_s
                        maybe_add_threshold_finding(
                            findings,
                            episode_dir,
                            csv_path,
                            "joint_acceleration_derived",
                            headers[idx],
                            accel,
                            thresholds.joint_accel_warn,
                            thresholds.joint_accel_fail,
                            timestamp_ms,
                            "rad/s^2",
                        )
                    previous_velocity[idx] = value

                for idx in groups["eef_position"]:
                    value = read_float(row, idx)
                    if value is None:
                        continue
                    maybe_add_threshold_finding(
                        findings,
                        episode_dir,
                        csv_path,
                        "eef_position_abs",
                        headers[idx],
                        value,
                        thresholds.eef_position_abs_warn,
                        thresholds.eef_position_abs_fail,
                        timestamp_ms,
                        "m",
                    )
                    prev = previous_values.get(idx)
                    if prev is not None:
                        step = value - prev
                        maybe_add_threshold_finding(
                            findings,
                            episode_dir,
                            csv_path,
                            "eef_position_step",
                            headers[idx],
                            step,
                            thresholds.eef_step_warn,
                            thresholds.eef_step_fail,
                            timestamp_ms,
                            "m/frame",
                        )
                        if dt_s and dt_s > 0:
                            velocity = step / dt_s
                            maybe_add_threshold_finding(
                                findings,
                                episode_dir,
                                csv_path,
                                "eef_velocity_derived",
                                headers[idx],
                                velocity,
                                thresholds.eef_velocity_warn,
                                thresholds.eef_velocity_fail,
                                timestamp_ms,
                                "m/s",
                            )
                    previous_values[idx] = value
    except StopIteration:
        findings.append(
            finding(episode_dir, csv_path, "empty_csv", "critical", "CSV has no header", {})
        )
    except OSError as exc:
        findings.append(
            finding(
                episode_dir,
                csv_path,
                "read_error",
                "critical",
                f"Could not read CSV: {exc}",
                {},
            )
        )
    return findings


def episode_summary(episode_dir: Path, findings: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = parse_metadata(episode_dir)
    severity_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for item in findings:
        severity = item["severity"]
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

    if severity_counts["critical"]:
        status = "fail_candidate"
    elif severity_counts["major"]:
        status = "needs_review"
    elif severity_counts["minor"]:
        status = "warning"
    else:
        status = "pass"

    return {
        "episode_path": str(episode_dir),
        "task_key": metadata.get("task_key", ""),
        "episode_id": metadata.get("episode_id", episode_dir.name),
        "robot": metadata.get("robot", ""),
        "controller": metadata.get("controller", ""),
        "duration_seconds": metadata.get("duration_seconds", ""),
        "status": status,
        "critical": severity_counts["critical"],
        "major": severity_counts["major"],
        "minor": severity_counts["minor"],
        "findings": len(findings),
    }


def write_outputs(
    findings: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "motion_findings.jsonl").open("w", encoding="utf-8") as file:
        for item in findings:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary_fields = [
        "episode_path",
        "task_key",
        "episode_id",
        "robot",
        "controller",
        "duration_seconds",
        "status",
        "critical",
        "major",
        "minor",
        "findings",
    ]
    with (output_dir / "motion_episode_summary.csv").open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summaries)

    status_counts: dict[str, int] = {}
    for row in summaries:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    with (output_dir / "motion_summary.txt").open("w", encoding="utf-8") as file:
        file.write("Motion abnormality prototype report\n")
        file.write(f"Episodes checked: {len(summaries)}\n")
        file.write(f"Findings: {len(findings)}\n")
        file.write("Status counts:\n")
        for status, count in sorted(status_counts.items()):
            file.write(f"- {status}: {count}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only prototype motion abnormality checker."
    )
    parser.add_argument("root", help="Dataset root, task folder, or episode folder.")
    parser.add_argument(
        "--output",
        default="/tmp/motion_abnormality_report",
        help="Output directory for reports.",
    )
    parser.add_argument(
        "--max-episodes-per-task",
        type=int,
        default=DEFAULT_MAX_EPISODES_PER_TASK,
        help="Cap episodes per task. Use 0 for no cap.",
    )
    parser.add_argument(
        "--max-rows-per-csv",
        type=int,
        default=DEFAULT_MAX_ROWS_PER_CSV,
        help="Cap rows read from each CSV. Use 0 for no cap.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    thresholds = Thresholds()

    if not root.is_dir():
        print(f"Root is not a directory: {root}")
        return 2

    episodes = find_episodes(root, args.max_episodes_per_task)
    all_findings: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for episode_dir in episodes:
        episode_findings: list[dict[str, Any]] = []
        for csv_path in sorted(episode_dir.rglob("data.csv")):
            if is_motion_csv(csv_path):
                episode_findings.extend(
                    analyze_csv(episode_dir, csv_path, thresholds, args.max_rows_per_csv)
                )
        all_findings.extend(episode_findings)
        summaries.append(episode_summary(episode_dir, episode_findings))

    write_outputs(all_findings, summaries, output_dir)
    print(f"Episodes checked: {len(summaries)}")
    print(f"Findings: {len(all_findings)}")
    print(f"Reports written to: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
