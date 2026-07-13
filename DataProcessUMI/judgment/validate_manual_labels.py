#!/usr/bin/env python3
"""Validate trajectory judgment reports against extensible manual labels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from judgment.judge_trajectory import (  # noqa: E402
    DEFAULT_TRANSFORM_CONFIG,
    build_summary,
    find_episode_dirs,
    judge_episode,
    load_config,
    load_transform_config,
    now_utc_iso,
    report_path_for,
    write_json,
)

DEFAULT_LABELS = Path(__file__).with_name("manual_labels.json")


def load_labels(path: Path) -> dict[str, Any]:
    with path.expanduser().open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if "labels" not in payload or not isinstance(payload["labels"], list):
        raise ValueError(f"labels[] missing in {path}")
    return payload


def expand_episode_path(entry: dict[str, Any]) -> Path:
    value = entry.get("episode_path")
    if not value:
        raise ValueError(f"manual label entry {entry.get('id', '<unknown>')} missing episode_path")
    return Path(value).expanduser().resolve()


def expand_label_entries(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded = []
    for entry in labels:
        root_value = entry.get("episode_root")
        if not root_value:
            expanded.append(entry)
            continue

        root = Path(root_value).expanduser().resolve()
        if not root.exists():
            item = dict(entry)
            item["episode_path"] = str(root)
            item["_missing_root"] = True
            expanded.append(item)
            continue

        for episode in find_episode_dirs(root):
            item = dict(entry)
            item.pop("episode_root", None)
            item["episode_path"] = str(episode)
            item["id"] = f"{entry.get('id', root.name)}::{episode.name}"
            expanded.append(item)
    return expanded


def read_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_existing_report(report_root: Path, episode_path: Path, entry: dict[str, Any]) -> Path | None:
    if entry.get("report_path"):
        path = Path(entry["report_path"]).expanduser()
        return path.resolve() if path.exists() else None

    canonical = report_path_for(episode_path, episode_path, report_root)
    if canonical.exists():
        return canonical

    report_dir = report_root / "report"
    if not report_dir.exists():
        return None
    candidates = sorted(report_dir.rglob(f"{episode_path.name}.json"))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    for candidate in candidates:
        try:
            report = read_report(candidate)
        except (OSError, json.JSONDecodeError):
            continue
        if Path(str(report.get("path", ""))).expanduser().resolve() == episode_path:
            return candidate
    return None


def side_labels(report: dict[str, Any], side: str) -> list[int]:
    labels = []
    for frame in report.get("frames", []):
        side_payload = frame.get("sides", {}).get(side, {})
        labels.append(int(side_payload.get("label", 0)))
    return labels


def evaluate_segment(report: dict[str, Any], segment: dict[str, Any], default_min_coverage: float) -> dict[str, Any]:
    side = segment["side"]
    expected = int(segment.get("label", 1))
    start = int(segment["start_frame"])
    end = int(segment["end_frame"])
    min_coverage = float(segment.get("min_coverage", default_min_coverage))

    labels = side_labels(report, side)
    if not labels:
        return {
            "pass": False,
            "side": side,
            "start_frame": start,
            "end_frame": end,
            "error": f"missing side labels for {side}",
        }
    clipped_start = max(0, start)
    clipped_end = min(len(labels) - 1, end)
    if clipped_start > clipped_end:
        return {
            "pass": False,
            "side": side,
            "start_frame": start,
            "end_frame": end,
            "error": f"segment outside report frame range 0..{len(labels) - 1}",
        }

    values = labels[clipped_start:clipped_end + 1]
    matching = sum(1 for value in values if value == expected)
    coverage = matching / len(values)
    passed = coverage >= min_coverage
    return {
        "pass": passed,
        "side": side,
        "start_frame": start,
        "end_frame": end,
        "clipped_start_frame": clipped_start,
        "clipped_end_frame": clipped_end,
        "expected_label": expected,
        "type": segment.get("type"),
        "coverage": round(coverage, 6),
        "min_coverage": min_coverage,
        "matched_frames": matching,
        "total_frames": len(values),
    }


def extra_bad_ratio(report: dict[str, Any], entry: dict[str, Any], side: str) -> float:
    labels = side_labels(report, side)
    if not labels:
        return 0.0
    expected_bad = [False] * len(labels)
    for segment in entry.get("segments", []):
        if segment.get("side") != side or int(segment.get("label", 1)) != 1:
            continue
        start = max(0, int(segment["start_frame"]))
        end = min(len(labels) - 1, int(segment["end_frame"]))
        for index in range(start, end + 1):
            expected_bad[index] = True
    outside = [index for index, is_expected in enumerate(expected_bad) if not is_expected]
    if not outside:
        return 0.0
    extra = sum(1 for index in outside if labels[index] == 1)
    return extra / len(outside)


def evaluate_entry(
    entry: dict[str, Any],
    report: dict[str, Any] | None,
    report_path: Path | None,
    default_min_coverage: float,
) -> dict[str, Any]:
    if report is None:
        return {
            "id": entry.get("id"),
            "episode_path": entry.get("episode_path"),
            "status": "skipped",
            "pass": None,
            "reason": "episode/report missing",
        }

    segment_results = [
        evaluate_segment(report, segment, default_min_coverage)
        for segment in entry.get("segments", [])
    ]
    quality_expected = entry.get("expected_trajectory_quality")
    quality_ok = True
    if quality_expected is not None:
        quality_ok = report.get("trajectory_quality") is bool(quality_expected)
        if bool(quality_expected) is False and report.get("trajectory_quality") is True:
            expected_bad_segments = [
                result
                for result in segment_results
                if int(result.get("expected_label", 0)) == 1
            ]
            if expected_bad_segments and all(result.get("pass") for result in expected_bad_segments):
                quality_ok = True

    recoverable_expected = entry.get("expected_recoverable")
    recoverable_ok = True
    if recoverable_expected is not None:
        recoverable_ok = report.get("recoverable") is bool(recoverable_expected)
        if bool(recoverable_expected) is True and report.get("recoverable") is False:
            expected_bad_segments = [
                result
                for result in segment_results
                if int(result.get("expected_label", 0)) == 1
            ]
            if expected_bad_segments and all(result.get("pass") for result in expected_bad_segments):
                recoverable_ok = True

    extra_results = {}
    max_extra = entry.get("max_extra_bad_ratio")
    extra_ok = True
    if max_extra is not None:
        for side in ("left", "right"):
            ratio = extra_bad_ratio(report, entry, side)
            extra_results[side] = round(ratio, 6)
            if ratio > float(max_extra):
                extra_ok = False

    passed = (
        all(result.get("pass") for result in segment_results)
        and quality_ok
        and recoverable_ok
        and extra_ok
    )
    return {
        "id": entry.get("id"),
        "episode_path": entry.get("episode_path"),
        "report": str(report_path) if report_path else None,
        "status": "checked",
        "pass": passed,
        "expected_trajectory_quality": quality_expected,
        "actual_trajectory_quality": report.get("trajectory_quality"),
        "quality_pass": quality_ok,
        "expected_recoverable": recoverable_expected,
        "actual_recoverable": report.get("recoverable"),
        "recoverable_pass": recoverable_ok,
        "extra_bad_ratio": extra_results or None,
        "max_extra_bad_ratio": max_extra,
        "extra_bad_pass": extra_ok,
        "segments": segment_results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate judgment reports against manual frame-range labels.",
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="Manual labels JSON.")
    parser.add_argument(
        "-o",
        "--output-root",
        type=Path,
        default=Path("outputs/judgment_validation"),
        help="Output root used when --run-detector is set, and searched for existing reports.",
    )
    parser.add_argument("--run-detector", action="store_true", help="Run judge_trajectory for each labeled episode before validation.")
    parser.add_argument("--config", type=Path, help="Detector config override JSON.")
    parser.add_argument("--transform-config", type=Path, default=DEFAULT_TRANSFORM_CONFIG)
    parser.add_argument("--min-coverage", type=float, default=0.7, help="Default minimum coverage for labeled ranges.")
    parser.add_argument("--fail-on-missing", action="store_true", help="Return failure when a labeled episode/report is missing.")
    parser.add_argument("--result-json", type=Path, help="Optional path for validation result JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    label_payload = load_labels(args.labels)
    output_root = args.output_root.expanduser().resolve()
    generated_at = now_utc_iso()

    detector_config = load_config(args.config)
    transform_config = load_transform_config(args.transform_config)

    results = []
    generated_reports: list[tuple[Path, dict[str, Any]]] = []
    for entry in expand_label_entries(label_payload["labels"]):
        if entry.get("_missing_root"):
            results.append({
                "id": entry.get("id"),
                "episode_path": entry.get("episode_path"),
                "status": "skipped",
                "pass": None,
                "reason": "episode_root missing",
            })
            continue

        episode_path = expand_episode_path(entry)
        report = None
        report_path = None
        if args.run_detector and episode_path.exists():
            # Validate this exact episode, not the whole containing folder.
            episode = find_episode_dirs(episode_path)[0]
            report = judge_episode(episode, detector_config, transform_config, generated_at)
            report_path = report_path_for(episode_path, episode, output_root)
            write_json(report_path, report)
            generated_reports.append((report_path, report))
        elif episode_path.exists():
            report_path = find_existing_report(output_root, episode_path, entry)
            if report_path is not None:
                report = read_report(report_path)

        result = evaluate_entry(entry, report, report_path, args.min_coverage)
        results.append(result)

    if generated_reports:
        summary_path = output_root / "report" / "summary.json"
        write_json(summary_path, build_summary(output_root, generated_reports, generated_at))

    checked = [result for result in results if result["status"] == "checked"]
    skipped = [result for result in results if result["status"] == "skipped"]
    failed = [result for result in checked if result["pass"] is False]
    payload = {
        "schema_version": 1,
        "type": "manual_label_validation",
        "generated_at": generated_at,
        "labels": str(args.labels),
        "checked": len(checked),
        "skipped": len(skipped),
        "failed": len(failed),
        "pass": not failed and (not skipped or not args.fail_on_missing),
        "results": results,
    }

    result_json = args.result_json or (output_root / "report" / "manual_label_validation.json")
    write_json(result_json.expanduser().resolve(), payload)

    for result in results:
        status = "SKIP" if result["status"] == "skipped" else ("PASS" if result["pass"] else "FAIL")
        print(f"{status} {result.get('id')}: {result.get('report') or result.get('reason')}")
    print(f"validation -> {result_json.expanduser().resolve()}")
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
