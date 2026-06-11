"""Calibrate Phase 5 robot-state thresholds from known-good episodes."""

import argparse
import csv
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path


PRIMARY_MODALITY = "observation.state.joint_position"
FALLBACK_MODALITY = "actions.joint_position"
DEFAULT_DB_PATH = Path("outputs/qa_pipeline.db")


def main() -> int:
    """Run Phase 5 calibration and write a JSON report."""
    args = _parse_args()
    roots = [Path(root) for root in args.roots]
    episodes = _discover_episodes(roots)
    print(f"Episodes found: {len(episodes)}")

    if args.pass_only:
        episodes = _filter_pass_episodes(episodes, DEFAULT_DB_PATH)
        print(f"Episodes after --pass-only filter: {len(episodes)}")

    report = _build_report(episodes, args.robot, roots)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Episodes read successfully: {report['episodes_analyzed']}")
    print(f"Rows analyzed: {report['total_rows_analyzed']}")
    print(f"Wrote calibration report: {args.output}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate Phase 5 thresholds.")
    parser.add_argument("--roots", nargs="+", required=True, help="Episode roots to scan.")
    parser.add_argument("--robot", required=True, help="Robot key, e.g. arx5 or flexiv.")
    parser.add_argument("--output", required=True, type=Path, help="Output JSON path.")
    parser.add_argument(
        "--pass-only",
        action="store_true",
        help="Only include episodes with final_status='pass' in outputs/qa_pipeline.db.",
    )
    return parser.parse_args()


def _discover_episodes(roots: list[Path]) -> list[Path]:
    episodes = set()
    for root in roots:
        if not root.exists():
            print(f"Warning: root does not exist: {root}")
            continue
        if root.is_dir() and root.name.startswith("episode_"):
            episodes.add(root)
        for path in root.rglob("*"):
            if "_quarantine" in path.parts:
                continue
            if path.is_dir() and path.name.startswith("episode_"):
                episodes.add(path)
    return sorted(episodes, key=lambda item: str(item))


def _filter_pass_episodes(episodes: list[Path], db_path: Path) -> list[Path]:
    if not db_path.exists():
        print(f"Warning: {db_path} not found; --pass-only ignored.")
        return episodes
    pass_paths = _load_pass_episode_paths(db_path)
    return [
        episode
        for episode in episodes
        if str(episode) in pass_paths or str(episode.resolve()) in pass_paths
    ]


def _load_pass_episode_paths(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT episode_path FROM episodes WHERE final_status = ?",
            ("pass",),
        ).fetchall()
    return {str(row[0]) for row in rows}


def _build_report(episodes: list[Path], robot: str, roots: list[Path]) -> dict:
    aggregates = _new_aggregates()
    successful = 0
    for index, episode in enumerate(episodes, start=1):
        print(f"Reading episode {index}/{len(episodes)}: {episode.name}")
        if _read_episode(episode, robot, aggregates):
            successful += 1

    return _report_from_aggregates(aggregates, successful, robot, roots)


def _new_aggregates() -> dict:
    return {
        "joint_positions": {},
        "joint_steps": {},
        "joint_velocities": {},
        "gripper_positions": {},
        "gripper_steps": {},
        "gripper_velocities": {},
        "durations": [],
        "total_frames": [],
        "joint_columns": set(),
        "gripper_columns": set(),
        "total_rows": 0,
    }


def _read_episode(episode: Path, robot: str, aggregates: dict) -> bool:
    csv_path = _episode_csv_path(episode)
    if csv_path is None:
        print(f"Warning: no joint position CSV found: {episode}")
        return False
    data = _read_csv(csv_path)
    if data is None:
        print(f"Warning: could not read CSV: {csv_path}")
        return False
    headers, rows = data
    columns = _detect_columns(headers, robot)
    if not columns["joint_cols"] and not columns["gripper_cols"]:
        print(f"Warning: no joint or gripper columns detected: {csv_path}")
        return False

    _record_metadata_stats(episode, aggregates)
    _record_rows(rows, columns, aggregates)
    return True


def _episode_csv_path(episode: Path) -> Path | None:
    primary = episode / PRIMARY_MODALITY / "data.csv"
    if primary.is_file():
        return primary
    fallback = episode / FALLBACK_MODALITY / "data.csv"
    return fallback if fallback.is_file() else None


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, float | None]]] | None:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or [])
            rows = [
                {header: _float_or_none(row.get(header)) for header in headers}
                for row in reader
            ]
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return (headers, rows) if headers else None


def _detect_columns(headers: list[str], robot: str) -> dict:
    arx_joint_cols = [header for header in headers if _arx_joint_col(header)]
    flexiv_joint_cols = [header for header in headers if _flexiv_joint_col(header)]
    return {
        "joint_cols": arx_joint_cols or flexiv_joint_cols,
        "gripper_cols": [header for header in headers if "gripper" in header.lower()],
    }


def _arx_joint_col(header: str) -> bool:
    return (
        header.startswith("left_j") or header.startswith("right_j")
    ) and header.split("_j")[-1].isdigit()


def _flexiv_joint_col(header: str) -> bool:
    return (header.startswith("j") and header[1:].isdigit()) or (
        header.startswith("joint_") and header.endswith(".pos")
    )


def _record_metadata_stats(episode: Path, aggregates: dict) -> None:
    metadata = _load_metadata(episode)
    duration = _positive_float(metadata.get("duration_seconds"))
    total_frames = _positive_float(metadata.get("total_frames"))
    if duration is not None:
        aggregates["durations"].append(duration)
    if total_frames is not None:
        aggregates["total_frames"].append(total_frames)


def _load_metadata(episode: Path) -> dict:
    try:
        data = json.loads((episode / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_rows(rows: list[dict], columns: dict, aggregates: dict) -> None:
    aggregates["total_rows"] += len(rows)
    _record_column_group(rows, columns["joint_cols"], aggregates, "joint")
    _record_column_group(rows, columns["gripper_cols"], aggregates, "gripper")


def _record_column_group(
    rows: list[dict],
    columns: list[str],
    aggregates: dict,
    group: str,
) -> None:
    for column in columns:
        values = _finite_values(rows, column)
        steps = _steps(rows, column)
        velocities = _velocities(rows, column)
        aggregates[f"{group}_columns"].add(column)
        aggregates[f"{group}_positions"].setdefault(column, []).extend(values)
        aggregates[f"{group}_steps"].setdefault(column, []).extend(steps)
        aggregates[f"{group}_velocities"].setdefault(column, []).extend(velocities)


def _finite_values(rows: list[dict], column: str) -> list[float]:
    return [row[column] for row in rows if column in row and _finite(row[column])]


def _steps(rows: list[dict], column: str) -> list[float]:
    steps = []
    previous = None
    for row in rows:
        current = row.get(column)
        if _finite(previous) and _finite(current):
            steps.append(abs(current - previous))
        previous = current
    return steps


def _velocities(rows: list[dict], column: str) -> list[float]:
    velocities = []
    for previous, current in zip(rows, rows[1:]):
        dt = _row_dt(previous, current)
        prev_value = previous.get(column)
        curr_value = current.get(column)
        if dt > 0 and _finite(prev_value) and _finite(curr_value):
            velocities.append(abs(curr_value - prev_value) / dt)
    return velocities


def _row_dt(previous: dict, current: dict) -> float:
    prev_time = previous.get("timestamp_ms")
    curr_time = current.get("timestamp_ms")
    if not _finite(prev_time) or not _finite(curr_time):
        return 0.0
    return (curr_time - prev_time) / 1000.0


def _report_from_aggregates(
    aggregates: dict, successful: int, robot: str, roots: list[Path]
) -> dict:
    return {
        "robot": robot,
        "episodes_analyzed": successful,
        "total_rows_analyzed": aggregates["total_rows"],
        "roots": [str(root) for root in roots],
        "generated_at": datetime.now().isoformat(),
        "joint_columns": sorted(aggregates["joint_columns"]),
        "gripper_columns": sorted(aggregates["gripper_columns"]),
        "joint_position_stats": _position_stats_by_column(aggregates["joint_positions"]),
        "joint_step_stats": _step_stats_by_column(aggregates["joint_steps"]),
        "joint_velocity_stats": _velocity_stats_by_column(aggregates["joint_velocities"]),
        "gripper_position_stats": _position_stats_by_column(aggregates["gripper_positions"]),
        "gripper_step_stats": _step_stats_by_column(aggregates["gripper_steps"]),
        "gripper_velocity_stats": _velocity_stats_by_column(aggregates["gripper_velocities"]),
        "duration_stats": _duration_stats(aggregates["durations"]),
        "total_frame_stats": _duration_stats(aggregates["total_frames"]),
        "suggested_thresholds": _suggested_thresholds(aggregates),
    }


def _position_stats_by_column(values_by_column: dict[str, list[float]]) -> dict:
    return {
        column: _position_stats(values)
        for column, values in sorted(values_by_column.items())
        if values
    }


def _step_stats_by_column(values_by_column: dict[str, list[float]]) -> dict:
    return {
        column: _step_stats(values)
        for column, values in sorted(values_by_column.items())
        if values
    }


def _velocity_stats_by_column(values_by_column: dict[str, list[float]]) -> dict:
    return {
        column: _velocity_stats(values)
        for column, values in sorted(values_by_column.items())
        if values
    }


def _position_stats(values: list[float]) -> dict:
    return {
        "min": min(values),
        "max": max(values),
        "p1": _quantile(values, 0.01),
        "p5": _quantile(values, 0.05),
        "p25": _quantile(values, 0.25),
        "p50": _quantile(values, 0.50),
        "p75": _quantile(values, 0.75),
        "p95": _quantile(values, 0.95),
        "p99": _quantile(values, 0.99),
        "p99_9": _quantile(values, 0.999),
    }


def _step_stats(values: list[float]) -> dict:
    return {
        "mean": sum(values) / len(values),
        "p50": _quantile(values, 0.50),
        "p95": _quantile(values, 0.95),
        "p99": _quantile(values, 0.99),
        "p99_9": _quantile(values, 0.999),
        "max": max(values),
    }


def _velocity_stats(values: list[float]) -> dict:
    return {
        "p95": _quantile(values, 0.95),
        "p99": _quantile(values, 0.99),
        "p99_9": _quantile(values, 0.999),
        "max": max(values),
    }


def _duration_stats(values: list[float]) -> dict:
    if not values:
        return {}
    return {
        "min": min(values),
        "max": max(values),
        "median": _quantile(values, 0.50),
        "p5": _quantile(values, 0.05),
        "p95": _quantile(values, 0.95),
    }


def _suggested_thresholds(aggregates: dict) -> dict:
    joint_values = _flatten(aggregates["joint_positions"])
    gripper_values = _flatten(aggregates["gripper_positions"])
    joint_steps = _flatten(aggregates["joint_steps"])
    gripper_steps = _flatten(aggregates["gripper_steps"])
    joint_velocities = _flatten(aggregates["joint_velocities"])
    gripper_velocities = _flatten(aggregates["gripper_velocities"])
    return {
        "joint_limits_rad": _expanded_joint_limits(joint_values),
        "gripper_limits_m": _expanded_gripper_limits(gripper_values),
        "max_joint_step_rad": _quantile_or_zero(joint_steps, 0.999),
        "max_gripper_step_m": _quantile_or_zero(gripper_steps, 0.999),
        "max_joint_velocity_rad_s": _quantile_or_zero(joint_velocities, 0.999),
        "max_gripper_velocity_m_s": _quantile_or_zero(gripper_velocities, 0.999),
        "note": "Thresholds derived from p99.9 of observed data. Review before applying to ROBOT_CONFIGS.",
    }


def _expanded_joint_limits(values: list[float]) -> list[float]:
    if not values:
        return [0.0, 0.0]
    return [min(values) * 1.2, max(values) * 1.2]


def _expanded_gripper_limits(values: list[float]) -> list[float]:
    if not values:
        return [0.0, 0.0]
    return [min(values) - 0.002, max(values) + 0.002]


def _flatten(values_by_column: dict[str, list[float]]) -> list[float]:
    values = []
    for column_values in values_by_column.values():
        values.extend(column_values)
    return values


def _quantile_or_zero(values: list[float], q: float) -> float:
    return _quantile(values, q) if values else 0.0


def _quantile(values: list[float], q: float) -> float:
    """Compute quantile q from a sorted or unsorted list."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = q * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (index - lower) * (
        sorted_values[upper] - sorted_values[lower]
    )


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value) and not math.isinf(value)


if __name__ == "__main__":
    raise SystemExit(main())
