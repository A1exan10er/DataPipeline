#!/usr/bin/env python3
"""Report episode durations from metadata.json files under a directory."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


NORMAL_QUALITY_LABEL = "完全正常"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    whole_seconds = int(round(seconds))
    minutes, secs = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def read_duration(metadata_path: Path, root: Path) -> dict[str, Any]:
    with metadata_path.open("r", encoding="utf-8") as file:
        metadata = json.load(file)

    duration_seconds = metadata.get("duration_seconds")
    if duration_seconds is None:
        total_frames = metadata.get("total_frames")
        fps_actual = metadata.get("fps_actual") or metadata.get("fps_config")
        if total_frames is not None and fps_actual:
            duration_seconds = float(total_frames) / float(fps_actual)

    if duration_seconds is not None:
        duration_seconds = float(duration_seconds)

    episode_dir = metadata_path.parent
    operator = episode_dir.parent.name
    task_name = metadata.get("task_key") or episode_dir.parent.parent.parent.name
    return {
        "episode_index": metadata.get("episode_index"),
        "episode_id": metadata.get("episode_id") or episode_dir.name,
        "task_key": metadata.get("task_key", ""),
        "task_name": task_name,
        "operator": operator,
        "username": metadata.get("username", ""),
        "start_time": metadata.get("start_time", ""),
        "quality_labels": metadata.get("quality", {}).get("labels", []),
        "duration_seconds": duration_seconds,
        "duration": format_duration(duration_seconds),
        "total_frames": metadata.get("total_frames", ""),
        "fps_actual": metadata.get("fps_actual", ""),
        "path": str(episode_dir.relative_to(root)),
    }


def has_normal_quality_label(row: dict[str, Any]) -> bool:
    labels = row.get("quality_labels")
    return isinstance(labels, list) and NORMAL_QUALITY_LABEL in labels


def find_metadata_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("metadata.json")
        if path.parent.name.startswith("episode_")
    )


def infer_task_root(root: Path) -> Path:
    metadata_path = root / "metadata.json"
    if not metadata_path.is_file() or not root.name.startswith("episode_"):
        return root

    try:
        with metadata_path.open("r", encoding="utf-8") as file:
            metadata = json.load(file)
    except (OSError, json.JSONDecodeError):
        return root

    task_key = metadata.get("task_key")
    if not task_key:
        return root

    for parent in [root, *root.parents]:
        if parent.name == task_key:
            return parent
    return root


def group_rows_by_task(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_name = str(row.get("task_name") or row.get("task_key") or "")
        grouped.setdefault(task_name, []).append(row)
    return grouped


def percentile(sorted_values: list[float], position: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")

    index = (len(sorted_values) - 1) * position
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = index - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def unusual_duration_bounds(
    rows: list[dict[str, Any]],
    min_seconds: float | None,
    max_seconds: float | None,
) -> tuple[float | None, float | None, str]:
    durations = sorted(
        row["duration_seconds"]
        for row in rows
        if isinstance(row.get("duration_seconds"), float)
    )
    if not durations:
        return min_seconds, max_seconds, "fixed thresholds"

    lower = min_seconds
    upper = max_seconds
    method_parts: list[str] = []

    if lower is None or upper is None:
        q1 = percentile(durations, 0.25)
        q3 = percentile(durations, 0.75)
        iqr = q3 - q1
        if lower is None:
            lower = max(0.0, q1 - 1.5 * iqr)
        if upper is None:
            upper = q3 + 1.5 * iqr
        method_parts.append(f"IQR outlier rule (Q1={q1:.2f}s, Q3={q3:.2f}s)")

    if min_seconds is not None or max_seconds is not None:
        method_parts.append("fixed threshold override")

    return lower, upper, ", ".join(method_parts)


def mark_unusual_rows(
    rows: list[dict[str, Any]],
    lower_bound: float | None,
    upper_bound: float | None,
) -> list[dict[str, Any]]:
    unusual_rows = []
    for row in rows:
        duration = row.get("duration_seconds")
        if not isinstance(duration, float):
            row["unusual_reason"] = "missing duration"
            unusual_rows.append(row)
            continue

        reasons = []
        if lower_bound is not None and duration < lower_bound:
            reasons.append(f"shorter than {lower_bound:.2f}s")
        if upper_bound is not None and duration > upper_bound:
            reasons.append(f"longer than {upper_bound:.2f}s")

        row["unusual_reason"] = "; ".join(reasons)
        if reasons:
            unusual_rows.append(row)
    return unusual_rows


def mark_unusual_rows_by_task(
    rows: list[dict[str, Any]],
    min_seconds: float | None,
    max_seconds: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    unusual_rows: list[dict[str, Any]] = []
    bounds_by_task: list[dict[str, Any]] = []
    method_by_task: dict[str, str] = {}

    for task_name, task_rows in sorted(group_rows_by_task(rows).items()):
        lower_bound, upper_bound, method = unusual_duration_bounds(
            task_rows,
            min_seconds,
            max_seconds,
        )
        method_by_task[method] = method
        bounds_by_task.append(
            {
                "task_name": task_name,
                "episodes": len(task_rows),
                "lower_bound": lower_bound,
                "upper_bound": upper_bound,
                "method": method,
            }
        )
        unusual_rows.extend(mark_unusual_rows(task_rows, lower_bound, upper_bound))

    method = "per-task " + "; ".join(method_by_task) if method_by_task else "per-task"
    unusual_rows.sort(key=lambda row: (str(row["path"]), row.get("episode_index") or -1))
    return unusual_rows, bounds_by_task, method


def operator_unusual_stats(
    rows: list[dict[str, Any]],
    unusual_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], int] = {}
    unusual_totals: dict[tuple[str, str], int] = {}

    for row in rows:
        task_name = str(row.get("task_name") or "")
        operator = str(row.get("operator") or "")
        key = (task_name, operator)
        totals[key] = totals.get(key, 0) + 1

    for row in unusual_rows:
        task_name = str(row.get("task_name") or "")
        operator = str(row.get("operator") or "")
        key = (task_name, operator)
        unusual_totals[key] = unusual_totals.get(key, 0) + 1

    stats = []
    for task_name, operator in sorted(totals):
        total = totals[(task_name, operator)]
        unusual = unusual_totals.get((task_name, operator), 0)
        percentage = unusual / total * 100 if total else 0.0
        stats.append(
            {
                "task_name": task_name,
                "operator": operator,
                "episodes": total,
                "unusual_episodes": unusual,
                "unusual_percentage": percentage,
            }
        )
    return stats


def print_operator_stats(stats: list[dict[str, Any]]) -> None:
    if not stats:
        return

    print("\nUnusual percentage by task and operator:")
    for stat in stats:
        print(
            f"{stat['task_name']} / {stat['operator']}: "
            f"{stat['unusual_episodes']}/{stat['episodes']} "
            f"({stat['unusual_percentage']:.2f}%)"
        )


def print_table(rows: list[dict[str, Any]]) -> None:
    columns = [
        ("episode_index", "idx"),
        ("task_name", "task"),
        ("episode_id", "episode_id"),
        ("operator", "operator"),
        ("duration_seconds", "seconds"),
        ("duration", "duration"),
        ("total_frames", "frames"),
        ("fps_actual", "fps"),
        ("path", "path"),
    ]
    widths = {
        key: max(
            len(header),
            *(
                len(f"{row.get(key, ''):.2f}")
                if isinstance(row.get(key), float)
                else len(str(row.get(key, "")))
                for row in rows
            ),
        )
        for key, header in columns
    }

    print("  ".join(header.ljust(widths[key]) for key, header in columns))
    print("  ".join("-" * widths[key] for key, _ in columns))
    for row in rows:
        values = []
        for key, _ in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                value = f"{value:.2f}"
            values.append(str(value).ljust(widths[key]))
        print("  ".join(values))


def print_summary(rows: list[dict[str, Any]]) -> None:
    durations = [
        row["duration_seconds"]
        for row in rows
        if isinstance(row.get("duration_seconds"), float)
    ]
    if not durations:
        print("\nNo duration values found.")
        return

    total = sum(durations)
    print()
    print(f"Episodes: {len(rows)}")
    print(f"With duration: {len(durations)}")
    print(f"Total: {total:.2f}s ({format_duration(total)})")
    print(f"Average: {total / len(durations):.2f}s ({format_duration(total / len(durations))})")
    print(f"Shortest: {min(durations):.2f}s ({format_duration(min(durations))})")
    print(f"Longest: {max(durations):.2f}s ({format_duration(max(durations))})")


def write_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    fieldnames = [
        "episode_index",
        "episode_id",
        "task_key",
        "task_name",
        "operator",
        "username",
        "start_time",
        "quality_labels",
        "duration_seconds",
        "duration",
        "total_frames",
        "fps_actual",
        "path",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_unusual_report(
    rows: list[dict[str, Any]],
    operator_stats: list[dict[str, Any]],
    bounds_by_task: list[dict[str, Any]],
    output_path: Path,
    method: str,
) -> None:
    fieldnames = [
        "episode_index",
        "episode_id",
        "task_key",
        "task_name",
        "operator",
        "username",
        "start_time",
        "quality_labels",
        "duration_seconds",
        "duration",
        "total_frames",
        "fps_actual",
        "unusual_reason",
        "path",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    stats_path = output_path.with_name(f"{output_path.stem}_operator_stats.csv")
    with stats_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "task_name",
                "operator",
                "episodes",
                "unusual_episodes",
                "unusual_percentage",
            ],
        )
        writer.writeheader()
        writer.writerows(operator_stats)

    summary_path = output_path.with_suffix(".txt")
    with summary_path.open("w", encoding="utf-8") as file:
        file.write("Unusual episode duration report\n")
        file.write(f"Method: {method}\n")
        file.write(f"Unusual episodes: {len(rows)}\n\n")

        file.write("Task duration bounds:\n")
        for bounds in bounds_by_task:
            lower_bound = bounds.get("lower_bound")
            upper_bound = bounds.get("upper_bound")
            lower_text = f"{lower_bound:.2f}s" if isinstance(lower_bound, float) else "none"
            upper_text = f"{upper_bound:.2f}s" if isinstance(upper_bound, float) else "none"
            file.write(
                f"{bounds['task_name']}: episodes={bounds['episodes']} "
                f"lower={lower_text} upper={upper_text}\n"
            )

        file.write("\nUnusual percentage by task and operator:\n")
        for stat in operator_stats:
            file.write(
                f"{stat['task_name']} / {stat['operator']}: "
                f"{stat['unusual_episodes']}/{stat['episodes']} "
                f"({stat['unusual_percentage']:.2f}%)\n"
            )
        file.write("\nUnusual episodes:\n")
        for row in rows:
            duration = row.get("duration_seconds")
            duration_text = f"{duration:.2f}s" if isinstance(duration, float) else "missing"
            file.write(
                f"task={row.get('task_name')} "
                f"operator={row.get('operator')} "
                f"episode_index={row.get('episode_index')} "
                f"episode_id={row.get('episode_id')} "
                f"duration={duration_text} "
                f"reason={row.get('unusual_reason')} "
                f"path={row.get('path')}\n"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check durations for all episode metadata.json files under a root directory."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Directory to scan. Use an episode, operator, date, task, or verified root folder.",
    )
    parser.add_argument(
        "--no-task-infer",
        action="store_true",
        help="Scan exactly the given root instead of expanding an episode folder to its task.",
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        help="Optional path to write the report as CSV.",
    )
    parser.add_argument(
        "--unusual-report",
        default="unusual_episode_durations.csv",
        help="Write unusual duration episodes to this CSV. Defaults to the task folder.",
    )
    parser.add_argument(
        "--no-unusual-report",
        action="store_true",
        help="Do not write the unusual duration report.",
    )
    parser.add_argument(
        "--min-seconds",
        type=float,
        help="Treat episodes shorter than this as unusual.",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        help="Treat episodes longer than this as unusual.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print aggregate duration statistics.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Root is not a directory: {root}", file=sys.stderr)
        return 2
    if not args.no_task_infer:
        root = infer_task_root(root)

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for metadata_path in find_metadata_files(root):
        try:
            rows.append(read_duration(metadata_path, root))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(f"{metadata_path}: {exc}")

    total_rows = len(rows)
    rows = [row for row in rows if has_normal_quality_label(row)]

    rows.sort(key=lambda row: (str(row["path"]), row.get("episode_index") or -1))

    if not rows:
        print(
            f"No matching episode metadata.json files found under {root}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Quality filter: {NORMAL_QUALITY_LABEL} "
        f"({len(rows)} of {total_rows} episodes)"
    )

    if not args.summary_only:
        print_table(rows)
    print_summary(rows)

    if args.csv_path:
        csv_path = Path(args.csv_path).expanduser().resolve()
        write_csv(rows, csv_path)
        print(f"\nCSV written: {csv_path}")

    if not args.no_unusual_report:
        unusual_rows, bounds_by_task, method = mark_unusual_rows_by_task(
            rows,
            args.min_seconds,
            args.max_seconds,
        )
        stats = operator_unusual_stats(rows, unusual_rows)
        report_path = Path(args.unusual_report).expanduser()
        if not report_path.is_absolute():
            report_path = root / report_path
        report_path = report_path.resolve()
        write_unusual_report(
            unusual_rows,
            stats,
            bounds_by_task,
            report_path,
            method,
        )
        print_operator_stats(stats)
        print(f"\nUnusual duration report written: {report_path}")
        print(f"Report summary written: {report_path.with_suffix('.txt')}")
        print(
            "Operator stats written: "
            f"{report_path.with_name(f'{report_path.stem}_operator_stats.csv')}"
        )
        print(f"Unusual episodes: {len(unusual_rows)}")

    if errors:
        print("\nSkipped files with errors:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)

    return 0 if not errors else 3


if __name__ == "__main__":
    raise SystemExit(main())
