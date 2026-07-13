#!/usr/bin/env python3
"""Export judged sample CSVs and reports into flat episode folders.

The mapping file is written before any CSV copy starts. Re-running the command
reuses the existing mapping, which makes interrupted copies resumable.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_REPORT_ROOT = Path("outputs/judgment_samples/report")
DEFAULT_DATA_ROOT = Path("outputs/data")
MAPPING_NAME = "episode_mapping.csv"
QUALITY_INDEX_NAME = "quality_by_episode.csv"
REPORT_DIR_NAME = "report"

MAPPING_FIELDS = [
    "split",
    "new_episode",
    "new_episode_path",
    "original_episode_path",
    "original_relative_path",
    "original_episode",
    "report_path",
    "trajectory_quality",
]

QUALITY_INDEX_FIELDS = [
    "split",
    "new_episode",
    "trajectory_quality",
    "flat_report_path",
    "flat_episode_path",
    "original_episode_path",
    "original_relative_path",
    "source_report_path",
]


@dataclass(frozen=True)
class MappingRow:
    split: str
    new_episode: str
    new_episode_path: Path
    original_episode_path: Path
    original_relative_path: str
    original_episode: str
    report_path: Path
    trajectory_quality: bool

    def as_csv_row(self) -> dict[str, str]:
        return {
            "split": self.split,
            "new_episode": self.new_episode,
            "new_episode_path": str(self.new_episode_path),
            "original_episode_path": str(self.original_episode_path),
            "original_relative_path": self.original_relative_path,
            "original_episode": self.original_episode,
            "report_path": str(self.report_path),
            "trajectory_quality": str(self.trajectory_quality).lower(),
        }


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def original_relative_path(path: Path, source_root: Path | None) -> str:
    if source_root is None:
        return path.name
    try:
        return str(path.relative_to(source_root))
    except ValueError:
        return str(path)


def build_mapping(report_root: Path, data_root: Path, source_root: Path | None) -> list[MappingRow]:
    counters = {"success": 0, "failure": 0}
    rows: list[MappingRow] = []

    for report_path in sorted(report_root.rglob("episode_*.json")):
        report = read_json(report_path)
        quality = report.get("trajectory_quality")
        if not isinstance(quality, bool):
            continue

        split = "success" if quality else "failure"
        new_episode = f"episode_{counters[split]:04d}"
        counters[split] += 1

        source_path = Path(str(report["path"])).expanduser().resolve()
        rows.append(
            MappingRow(
                split=split,
                new_episode=new_episode,
                new_episode_path=(data_root / split / new_episode).resolve(),
                original_episode_path=source_path,
                original_relative_path=original_relative_path(source_path, source_root),
                original_episode=source_path.name,
                report_path=report_path.resolve(),
                trajectory_quality=quality,
            )
        )

    return rows


def write_mapping(path: Path, rows: list[MappingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MAPPING_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv_row())
    tmp_path.replace(path)


def flat_report_path(data_root: Path, row: MappingRow) -> Path:
    return data_root / REPORT_DIR_NAME / row.split / f"{row.new_episode}.json"


def write_quality_index(path: Path, data_root: Path, rows: list[MappingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=QUALITY_INDEX_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "split": row.split,
                    "new_episode": row.new_episode,
                    "trajectory_quality": str(row.trajectory_quality).lower(),
                    "flat_report_path": str(flat_report_path(data_root, row).resolve()),
                    "flat_episode_path": str(row.new_episode_path),
                    "original_episode_path": str(row.original_episode_path),
                    "original_relative_path": row.original_relative_path,
                    "source_report_path": str(row.report_path),
                }
            )
    tmp_path.replace(path)


def bool_from_string(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(f"Invalid boolean value in mapping: {value!r}")


def load_mapping(path: Path) -> list[MappingRow]:
    rows: list[MappingRow] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(MAPPING_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
        for item in reader:
            rows.append(
                MappingRow(
                    split=item["split"],
                    new_episode=item["new_episode"],
                    new_episode_path=Path(item["new_episode_path"]),
                    original_episode_path=Path(item["original_episode_path"]),
                    original_relative_path=item["original_relative_path"],
                    original_episode=item["original_episode"],
                    report_path=Path(item["report_path"]),
                    trajectory_quality=bool_from_string(item["trajectory_quality"]),
                )
            )
    return rows


def copy_csv_tree(row: MappingRow) -> tuple[int, int, int]:
    copied = 0
    skipped = 0
    errors = 0

    if not row.original_episode_path.exists():
        return copied, skipped, errors + 1

    for source_file in sorted(row.original_episode_path.rglob("*.csv")):
        rel_path = source_file.relative_to(row.original_episode_path)
        dest_file = row.new_episode_path / rel_path
        try:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            if dest_file.exists() and dest_file.stat().st_size == source_file.stat().st_size:
                skipped += 1
                continue
            tmp_file = dest_file.with_name(dest_file.name + ".tmp")
            shutil.copy2(source_file, tmp_file)
            tmp_file.replace(dest_file)
            copied += 1
        except OSError as exc:
            errors += 1
            print(f"ERROR copying {source_file} -> {dest_file}: {exc}")

    return copied, skipped, errors


def copy_report(data_root: Path, row: MappingRow) -> tuple[int, int, int]:
    dest_file = flat_report_path(data_root, row)
    try:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if dest_file.exists() and dest_file.stat().st_size == row.report_path.stat().st_size:
            return 0, 1, 0
        tmp_file = dest_file.with_name(dest_file.name + ".tmp")
        shutil.copy2(row.report_path, tmp_file)
        tmp_file.replace(dest_file)
        return 1, 0, 0
    except OSError as exc:
        print(f"ERROR copying report {row.report_path} -> {dest_file}: {exc}")
        return 0, 0, 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy judged sample CSVs into outputs/data/success and outputs/data/failure.",
    )
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("~/data/samples"),
        help="Used only for the original_relative_path column.",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Write or reuse the mapping table without copying CSV files.",
    )
    parser.add_argument(
        "--rebuild-mapping",
        action="store_true",
        help="Regenerate the mapping even when outputs/data/episode_mapping.csv exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_root = args.report_root.expanduser().resolve()
    data_root = args.data_root.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve() if args.source_root else None
    mapping_path = data_root / MAPPING_NAME

    if mapping_path.exists() and not args.rebuild_mapping:
        rows = load_mapping(mapping_path)
        print(f"Reusing mapping: {mapping_path} ({len(rows)} episodes)")
    else:
        rows = build_mapping(report_root, data_root, source_root)
        write_mapping(mapping_path, rows)
        print(f"Wrote mapping: {mapping_path} ({len(rows)} episodes)")

    success_count = sum(1 for row in rows if row.split == "success")
    failure_count = sum(1 for row in rows if row.split == "failure")
    print(f"Mapped success={success_count} failure={failure_count}")
    quality_index_path = data_root / QUALITY_INDEX_NAME
    write_quality_index(quality_index_path, data_root, rows)
    print(f"Wrote quality index: {quality_index_path}")

    if args.manifest_only:
        return 0

    copied = 0
    skipped = 0
    errors = 0
    for row in rows:
        row_copied, row_skipped, row_errors = copy_csv_tree(row)
        copied += row_copied
        skipped += row_skipped
        errors += row_errors

    print(f"CSV copy complete: copied={copied} skipped={skipped} errors={errors}")

    report_copied = 0
    report_skipped = 0
    report_errors = 0
    for row in rows:
        row_copied, row_skipped, row_errors = copy_report(data_root, row)
        report_copied += row_copied
        report_skipped += row_skipped
        report_errors += row_errors

    print(
        "Report copy complete: "
        f"copied={report_copied} skipped={report_skipped} errors={report_errors}"
    )
    errors += report_errors
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
