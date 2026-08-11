#!/usr/bin/env python3
"""Rerun detection for selected UMI episodes and rebuild queues."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gripper_recovery.gripper_recovery import load_model
from judgment.judge_trajectory import load_config as load_judgment_config
from judgment.judge_trajectory import load_transform_config
from umi_post.run_pipeline import (
    classify_episode_worker,
    read_episode_list,
    reindex_reports,
    report_path,
    run_parallel_episodes,
    utc_iso,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--target-list", type=Path, required=True)
    parser.add_argument("--all-episode-list", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--robot-type", default="umi")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--focus-threshold", type=float, default=350.0)
    parser.add_argument("--focus-lap-p90-threshold", type=float, default=450.0)
    parser.add_argument("--focus-edge-ratio-min", type=float, default=0.24)
    parser.add_argument("--focus-gray-std-min", type=float, default=68.0)
    parser.add_argument("--focus-frames", type=int, default=20)
    parser.add_argument("--model", type=Path, default=ROOT / "gripper_recovery" / "gripper_recovery_model.json")
    parser.add_argument("--trajectory-config", type=Path, default=ROOT / "judgment" / "judgment_config.json")
    parser.add_argument("--transform-config", type=Path, default=ROOT / "transform" / "ee_trajectory_config.json")
    parser.add_argument("--progress-name", default="rerun_target_detection_progress.json")
    return parser.parse_args()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    args = parse_args()
    data_root = args.data_root.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    target_episodes = read_episode_list(data_root, args.target_list, args.robot_type)
    all_episodes = read_episode_list(data_root, args.all_episode_list, args.robot_type)
    if not target_episodes:
        raise SystemExit(f"No target episodes found in {args.target_list}")
    if not all_episodes:
        raise SystemExit(f"No all episodes found in {args.all_episode_list}")

    # Attributes consumed by classify_episode.
    args.apply = True
    args.force = True
    args.skip_prechecks = False
    args.detect_only = True
    args.fast_first = False
    args.repair_only = False
    args.precheck_only = False
    args.reindex_only = False
    args.skip_final_sync = False

    judgment_config = load_judgment_config(args.trajectory_config if args.trajectory_config.exists() else None)
    transform_config = load_transform_config(args.transform_config)
    gripper_model = load_model(args.model)
    progress_path = args.output_root / args.progress_name
    results_path = args.output_root / "rerun_target_detection_results.jsonl"
    if results_path.exists():
        results_path.unlink()

    counts: Counter[str] = Counter()

    def make_job(episode: Path):
        return (str(data_root), str(episode), args, judgment_config, transform_config, gripper_model)

    def on_result(i: int, total: int, report: dict) -> None:
        rel = report.get("relative_path", "")
        queue = report.get("queue_status") or report.get("status") or "unknown"
        if report.get("overall_quality") is True:
            queue = "success"
        counts[str(queue)] += 1
        with results_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "relative_path": rel,
                        "queue_status": queue,
                        "status": report.get("status"),
                        "overall_quality": report.get("overall_quality"),
                        "decision_reason": report.get("decision_reason"),
                        "repair_needed": report.get("repair_needed") or [],
                        "report": str(report_path(args.output_root, rel)),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if i == 1 or i % 100 == 0 or i == total:
            write_json(
                progress_path,
                {
                    "generated_at": utc_iso(),
                    "phase": "detect",
                    "processed": i,
                    "total": total,
                    "remaining": total - i,
                    "counts": dict(counts),
                },
            )
        print(f"[rerun-detect {i}/{total}] {queue} {rel}", flush=True)

    reports = run_parallel_episodes(
        target_episodes,
        args.workers,
        make_job,
        classify_episode_worker,
        on_result,
    )
    write_json(
        progress_path,
        {
            "generated_at": utc_iso(),
            "phase": "reindex",
            "processed": len(reports),
            "total": len(target_episodes),
            "remaining": 0,
            "counts": dict(counts),
        },
    )
    reindex_counts = reindex_reports(data_root, all_episodes, args.output_root)
    write_json(
        progress_path,
        {
            "generated_at": utc_iso(),
            "phase": "complete",
            "processed": len(reports),
            "total": len(target_episodes),
            "remaining": 0,
            "counts": dict(counts),
            "reindex": reindex_counts,
        },
    )
    print(json.dumps({"rerun_detected": len(reports), "reindex": reindex_counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
