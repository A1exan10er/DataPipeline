#!/usr/bin/env python3
"""Batch trajectory judgment for curated UMI episodes.

This is a thin batch runner around ``judge_trajectory.py``. It scans curated
paths shaped as ``task/date/umi/name/episode_XXX``, writes one full report per
episode under the same relative path, and writes aggregate summary/index files.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from judgment.judge_trajectory import (  # noqa: E402
    ACTION_DIR,
    analyze_side,
    assess_static_drift,
    build_summary,
    judge_episode,
    load_config,
    load_transformed_episode,
    load_transform_config,
    now_utc_iso,
    quality_for_episode,
    recoverability_for_episode,
    write_json,
)


DEFAULT_SOURCE_ROOT = Path("/data/curated")
DEFAULT_OUTPUT_ROOT = Path("/data/curated/umi_report/2026-06-24")
REPORT_SUBDIR = "report"
SUMMARY_NAME = "summary.json"
INDEX_NAME = "quality_by_episode.csv"

INDEX_FIELDS = [
    "relative_path",
    "episode",
    "task",
    "date",
    "platform",
    "collector",
    "ok",
    "trajectory_quality",
    "quality_status",
    "recoverable",
    "recoverability_status",
    "static_drift",
    "static_drift_sides",
    "bad_frame_count",
    "bad_frame_ratio",
    "frame_count",
    "report_path",
    "error",
]

_CONFIG: dict[str, Any] | None = None
_TRANSFORM_CONFIG: dict[str, Any] | None = None
_GENERATED_AT: str | None = None
_SOURCE_ROOT: Path | None = None
_OUTPUT_ROOT: Path | None = None
_COMPACT: bool = False


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_umi_episodes(source_root: Path) -> list[Path]:
    episodes: list[Path] = []
    for task_dir in sorted(source_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name == "umi_report":
            continue
        for date_dir in sorted(task_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            umi_dir = date_dir / "umi"
            if not umi_dir.is_dir():
                continue
            for collector_dir in sorted(umi_dir.iterdir()):
                if not collector_dir.is_dir():
                    continue
                for episode_dir in sorted(collector_dir.iterdir()):
                    if episode_dir.is_dir() and episode_dir.name.startswith("episode_"):
                        episodes.append(episode_dir)
    return episodes


def report_path_for_episode(source_root: Path, episode_dir: Path, output_root: Path) -> Path:
    rel = episode_dir.relative_to(source_root)
    return output_root / REPORT_SUBDIR / rel.with_suffix(".json")


def summarize_report(source_root: Path, report_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    episode_path = Path(str(report.get("path", "")))
    try:
        rel = episode_path.relative_to(source_root)
        parts = rel.parts
        rel_text = str(rel)
    except ValueError:
        parts = ()
        rel_text = str(episode_path)

    quality = report.get("quality") or {}
    recoverability = report.get("recoverability") or {}
    static_drift = report.get("static_drift") or {}
    return {
        "relative_path": rel_text,
        "episode": report.get("episode"),
        "task": parts[0] if len(parts) > 0 else "",
        "date": parts[1] if len(parts) > 1 else "",
        "platform": parts[2] if len(parts) > 2 else "",
        "collector": parts[3] if len(parts) > 3 else "",
        "ok": report.get("ok"),
        "trajectory_quality": report.get("trajectory_quality"),
        "quality_status": quality.get("status"),
        "recoverable": report.get("recoverable"),
        "recoverability_status": recoverability.get("status"),
        "static_drift": static_drift.get("has_drift"),
        "static_drift_sides": ",".join(static_drift.get("drift_sides") or []),
        "bad_frame_count": report.get("bad_frame_count"),
        "bad_frame_ratio": report.get("bad_frame_ratio"),
        "frame_count": report.get("frame_count"),
        "report_path": str(report_path),
        "error": report.get("error"),
    }


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    if "frames" not in report or report.get("frames_omitted") is True:
        return report
    out = dict(report)
    frame_count = len(report.get("frames") or [])
    out["frames"] = []
    out["frames_omitted"] = True
    out["omitted_frame_count"] = frame_count
    out["compact_report"] = True
    return out


def judge_episode_compact(
    episode_dir: Path,
    config: dict[str, Any],
    transform_config: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "type": "trajectory_judgment",
        "generated_at": generated_at,
        "episode": episode_dir.name,
        "path": str(episode_dir),
        "action_source": f"{ACTION_DIR}/data.csv",
        "coordinate_frame": "world_eef",
        "config": config,
        "compact_report": True,
        "frames_omitted": True,
        "frames": [],
    }
    try:
        timestamps_ms, positions = load_transformed_episode(episode_dir, transform_config)
    except Exception as exc:
        base.update(
            {
                "ok": False,
                "trajectory_quality": False,
                "recoverable": False,
                "recoverability": {
                    "recoverable": False,
                    "status": "error",
                    "reasons": [str(exc)],
                    "sides": {},
                },
                "error": str(exc),
                "frame_count": 0,
                "omitted_frame_count": 0,
                "bad_frame_count": 0,
                "bad_frame_ratio": 0.0,
                "sides": {},
            }
        )
        return base

    side_results = {side: analyze_side(timestamps_ms, pos, config) for side, pos in positions.items()}
    bad_frames = np.zeros(len(timestamps_ms), dtype=bool)
    for result in side_results.values():
        bad_frames |= result["bad"]
    static_drift = assess_static_drift(episode_dir, positions, config)
    quality = quality_for_episode(side_results, config, static_drift)
    recoverability = recoverability_for_episode(side_results, bad_frames, config, quality, static_drift)

    base.update(
        {
            "ok": True,
            "trajectory_quality": bool(quality["trajectory_quality"]),
            "quality": quality,
            "recoverable": bool(recoverability["recoverable"]),
            "recoverability": recoverability,
            "static_drift": static_drift,
            "frame_count": int(len(timestamps_ms)),
            "omitted_frame_count": int(len(timestamps_ms)),
            "bad_frame_count": int(np.sum(bad_frames)),
            "bad_frame_ratio": round(float(np.mean(bad_frames)) if len(bad_frames) else 0.0, 9),
            "sides": {
                side: {
                    "trajectory_quality": bool(quality["sides"][side]["trajectory_quality"]),
                    "quality": quality["sides"][side],
                    "recoverable": (
                        bool(recoverability["sides"][side]["recoverable"])
                        if quality["sides"][side]["trajectory_quality"] is not True
                        else False
                    ),
                    "recoverability": recoverability["sides"][side],
                    "metrics": side_results[side]["metrics"],
                    "segments": side_results[side]["segments"],
                }
                for side in side_results
            },
        }
    )
    return base


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def init_worker(
    config_path: str | None,
    transform_config_path: str,
    generated_at: str,
    source_root: str,
    output_root: str,
    compact: bool,
) -> None:
    global _CONFIG, _TRANSFORM_CONFIG, _GENERATED_AT, _SOURCE_ROOT, _OUTPUT_ROOT, _COMPACT
    _CONFIG = load_config(Path(config_path) if config_path else None)
    _TRANSFORM_CONFIG = load_transform_config(Path(transform_config_path))
    _GENERATED_AT = generated_at
    _SOURCE_ROOT = Path(source_root)
    _OUTPUT_ROOT = Path(output_root)
    _COMPACT = compact


def process_episode(episode_text: str) -> dict[str, Any]:
    assert _CONFIG is not None
    assert _TRANSFORM_CONFIG is not None
    assert _GENERATED_AT is not None
    assert _SOURCE_ROOT is not None
    assert _OUTPUT_ROOT is not None

    episode_dir = Path(episode_text)
    report_path = report_path_for_episode(_SOURCE_ROOT, episode_dir, _OUTPUT_ROOT)
    try:
        if _COMPACT:
            report = judge_episode_compact(episode_dir, _CONFIG, _TRANSFORM_CONFIG, _GENERATED_AT)
        else:
            report = judge_episode(episode_dir, _CONFIG, _TRANSFORM_CONFIG, _GENERATED_AT)
        row = summarize_report(_SOURCE_ROOT, report_path, report)
        write_json(report_path, compact_report(report) if _COMPACT else report)
        return row
    except Exception as exc:  # pragma: no cover - used for batch robustness.
        report = {
            "schema_version": 1,
            "type": "trajectory_judgment",
            "generated_at": _GENERATED_AT,
            "episode": episode_dir.name,
            "path": str(episode_dir),
            "ok": False,
            "trajectory_quality": False,
            "recoverable": False,
            "recoverability": {
                "recoverable": False,
                "status": "error",
                "reasons": [str(exc)],
                "sides": {},
            },
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "frame_count": 0,
            "bad_frame_count": 0,
            "bad_frame_ratio": 0.0,
            "frames": [],
            "sides": {},
        }
        write_json(report_path, report)
        return summarize_report(_SOURCE_ROOT, report_path, report)


def existing_summary_item(source_root: Path, output_root: Path, episode_dir: Path) -> dict[str, Any] | None:
    report_path = report_path_for_episode(source_root, episode_dir, output_root)
    if not report_path.exists() or report_path.stat().st_size <= 0:
        return None
    try:
        with report_path.open("r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if report.get("episode") != episode_dir.name:
        return None
    return summarize_report(source_root, report_path, report)


def write_index(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in INDEX_FIELDS})
    tmp_path.replace(path)


def build_batch_summary(
    source_root: Path,
    output_root: Path,
    rows: list[dict[str, Any]],
    generated_at: str,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    reports = [
        (
            Path(str(row.get("report_path", ""))),
            {
                "episode": row.get("episode"),
                "path": str(source_root / str(row.get("relative_path", ""))),
                "ok": boolish(row.get("ok")),
                "trajectory_quality": boolish(row.get("trajectory_quality")),
                "recoverable": boolish(row.get("recoverable")),
                "bad_frame_count": row.get("bad_frame_count"),
                "bad_frame_ratio": row.get("bad_frame_ratio"),
                "static_drift": {
                    "has_drift": boolish(row.get("static_drift")),
                    "drift_sides": (
                        str(row.get("static_drift_sides") or "").split(",")
                        if row.get("static_drift_sides")
                        else []
                    ),
                },
                "error": row.get("error"),
            },
        )
        for row in rows
    ]
    summary = build_summary(source_root, reports, generated_at)
    summary.update(
        {
            "batch_type": "curated_umi_trajectory_judgment",
            "started_at": started_at,
            "finished_at": finished_at,
            "output_root": str(output_root),
            "index_csv": str(output_root / INDEX_NAME),
        }
    )
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run trajectory judgment over curated UMI episodes.")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--config", type=Path, help="Optional detector config JSON merged over judgment_config.json.")
    parser.add_argument(
        "--transform-config",
        type=Path,
        default=ROOT / "transform" / "ee_trajectory_config.json",
        help="Transform config JSON.",
    )
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)))
    parser.add_argument("--limit", type=int, help="Process only the first N selected episodes.")
    parser.add_argument("--force", action="store_true", help="Recompute reports even when a report JSON already exists.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Omit per-frame labels from episode JSON reports. Quality, segments, metrics, and summaries are kept.",
    )
    parser.add_argument("--summary-every", type=int, default=500, help="Rewrite index and summary every N completed episodes.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    generated_at = now_utc_iso()
    started_at = utc_iso()

    episodes = iter_umi_episodes(source_root)
    if args.limit is not None:
        episodes = episodes[: args.limit]
    if not episodes:
        raise ValueError(f"No UMI episode directories found under {source_root}")

    rows: list[dict[str, Any]] = []
    todo: list[Path] = []
    if args.force:
        todo = episodes
    else:
        for episode_dir in episodes:
            existing = existing_summary_item(source_root, output_root, episode_dir)
            if existing is None:
                todo.append(episode_dir)
            else:
                rows.append(existing)

    print(
        f"selected={len(episodes)} existing={len(rows)} todo={len(todo)} "
        f"workers={args.workers} output={output_root}",
        flush=True,
    )

    def flush_outputs() -> None:
        ordered = sorted(rows, key=lambda item: str(item.get("relative_path", "")))
        write_index(output_root / INDEX_NAME, ordered)
        summary = build_batch_summary(source_root, output_root, ordered, generated_at, started_at, utc_iso())
        write_json(output_root / SUMMARY_NAME, summary)
        write_json(output_root / REPORT_SUBDIR / SUMMARY_NAME, summary)

    completed = 0
    if todo:
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=init_worker,
            initargs=(
                str(args.config.expanduser().resolve()) if args.config else None,
                str(args.transform_config.expanduser().resolve()),
                generated_at,
                str(source_root),
                str(output_root),
                bool(args.compact),
            ),
        ) as executor:
            futures = {executor.submit(process_episode, str(path)): path for path in todo}
            for future in as_completed(futures):
                rows.append(future.result())
                completed += 1
                if completed == 1 or completed % args.summary_every == 0:
                    flush_outputs()
                    print(f"completed={completed}/{len(todo)} total_indexed={len(rows)}", flush=True)

    flush_outputs()
    print(f"done selected={len(episodes)} indexed={len(rows)} output={output_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
