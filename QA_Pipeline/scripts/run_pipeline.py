"""Command-line entry point for the QA pipeline."""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline import phase1_metadata, phase2_duration, phase3_timestamp, phase4_video, phase5_robot_state
from scripts.generate_dashboard import generate_dashboard
from scripts.pipeline.qa_core import (
    EpisodeState,
    PipelineConfigurationError,
    discover_episodes,
    export_findings_jsonl,
    export_quality_report,
    export_summary_md,
    infer_context,
    init_db,
    load_episode_state,
    load_metadata,
    save_episode_state,
)
from scripts.pipeline.run_monitor import RunMonitor


STATUS_ORDER = ("pass", "fail", "warning", "needs_review")
FINAL_STATUS_ORDER = ("pass", "warning", "fail", "needs_review")

# To add a new phase:
# 1. Create scripts/pipeline/phaseN_<name>.py with a run_phase(states, db_path) function.
# 2. Import it here.
# 3. Add it to PHASE_RUNNERS below with its phase number as the key.

PHASE_RUNNERS: dict[int, Callable[..., list[EpisodeState]]] = {
    1: phase1_metadata.run_phase,
    2: phase2_duration.run_phase,
    3: phase3_timestamp.run_phase,
    4: phase4_video.run_phase,
    5: phase5_robot_state.run_phase,
    # 6: phase6_ik.run_phase,
}

PHASE_MODULES = {
    1: phase1_metadata,
    2: phase2_duration,
    3: phase3_timestamp,
    4: phase4_video,
    5: phase5_robot_state,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the QA pipeline from the command line."""
    args = _parse_args(argv)
    roots = [Path(root) for root in args.roots]
    missing_roots = [root for root in roots if not root.exists()]
    if missing_roots:
        _print_error("Missing root path(s): " + ", ".join(str(root) for root in missing_roots))
        return 1

    phases = _parse_phases(args.phases)
    if phases is None:
        return 1

    if not args.dry_run:
        try:
            _validate_phase_dependencies(phases)
        except PipelineConfigurationError as exc:
            _print_error(str(exc))
            return 1

    episodes = discover_episodes(roots)
    print(f"Episodes discovered: {len(episodes)}")
    if args.date:
        episodes = [
            episode_path for episode_path in episodes
            if args.date in str(episode_path)
        ]
        print(f"After --date filter ({args.date}): {len(episodes)} episodes")
    if args.task:
        task_lower = args.task.lower()
        episodes = [
            episode_path for episode_path in episodes
            if task_lower in str(episode_path).lower()
        ]
        print(f"After --task filter ({args.task}): {len(episodes)} episodes")
    if args.max_episodes is not None:
        episodes = episodes[: args.max_episodes]

    if args.dry_run:
        print("Dry run: no phases executed and no reports written.")
        print("Phases selected: " + ",".join(str(phase) for phase in phases))
        return 0

    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    init_db(db_path)

    states = _load_or_create_states(episodes, roots, db_path, args.force_rerun)
    monitor = None
    if not args.disable_live_monitor:
        run_id = args.run_id or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        monitor = RunMonitor(
            db_path=db_path,
            output_root=output_dir,
            run_id=run_id,
            roots=roots,
            phases=phases,
            workers=args.workers,
            refresh_interval_seconds=args.live_report_interval,
        )
        monitor.start(states)
        print(f"Live run monitor: {monitor.run_dir}")
    for phase_number in phases:
        _run_phase(
            phase_number,
            states,
            db_path,
            args.workers,
            args.continue_after_fail,
            monitor,
        )

    _write_final_verdicts(states, db_path)
    if monitor is not None:
        monitor.finish_run(states)
    report_paths = _export_reports(db_path, output_dir)
    if monitor is not None:
        report_paths.extend(_export_reports(db_path, monitor.run_dir / "final"))
    for report_path in report_paths:
        print(f"Wrote report: {report_path}")
    _print_final_summary(states, output_dir)
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the QA data quality pipeline.")
    parser.add_argument("--roots", nargs="+", required=True, help="One or more root directories to scan.")
    parser.add_argument("--db-path", default="outputs/qa_pipeline.db", help="SQLite state database path.")
    parser.add_argument("--output-dir", default="outputs", help="Directory for exported reports.")
    parser.add_argument("--phases", default=None, help="Comma-separated phase numbers to run, e.g. 1,2.")
    parser.add_argument("--max-episodes", type=int, default=None, help="Maximum number of episodes to process.")
    parser.add_argument("--force-rerun", action="store_true", help="Run selected phases even if completed.")
    parser.add_argument("--dry-run", action="store_true", help="Discover episodes without running phases.")
    parser.add_argument(
        "--continue-after-fail",
        action="store_true",
        default=False,
        help="Run all selected phases on every episode, even if an earlier "
        "phase already marked it as fail. Default: skip failed episodes.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Only process episodes under directories matching this date string "
        "(e.g. '20260606'). Filters after discovery.",
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Only process episodes whose task name contains this string "
        "(case-insensitive). Filters after discovery.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel worker processes for phases that support it (currently Phases 4 and 5). Default: 1.",
    )
    parser.add_argument("--run-id", default=None, help="Optional live-monitor run ID. Defaults to timestamp.")
    parser.add_argument(
        "--live-report-interval",
        type=float,
        default=2.0,
        help="Seconds between live monitor refreshes. Default: 2.0.",
    )
    parser.add_argument(
        "--disable-live-monitor",
        action="store_true",
        help="Disable run_status.json, issue_events.jsonl, and live_summary.md output.",
    )
    return parser.parse_args(argv)


def _parse_phases(value: str | None) -> list[int] | None:
    if value is None:
        return sorted(PHASE_RUNNERS)
    phases = []
    for item in value.split(","):
        phase_text = item.strip()
        if not phase_text:
            continue
        try:
            phase_number = int(phase_text)
        except ValueError:
            _print_error(f"Invalid phase number: {phase_text}")
            return None
        if phase_number not in PHASE_RUNNERS:
            _print_error(f"Phase {phase_number} is not implemented.")
            return None
        phases.append(phase_number)
    return sorted(set(phases))


def _validate_phase_dependencies(phases: list[int]) -> None:
    for phase_number in phases:
        validator = getattr(PHASE_MODULES[phase_number], "validate_dependencies", None)
        if validator is not None:
            validator()


def _load_or_create_states(
    episodes: list[Path], roots: list[Path], db_path: Path, force_rerun: bool
) -> list[EpisodeState]:
    print("Loading episode states...")
    states = []
    total = len(episodes)
    for index, episode_path in enumerate(episodes):
        state = load_episode_state(db_path, episode_path)
        if state is None:
            state = _new_episode_state(roots, episode_path)
        if force_rerun:
            state.phases_completed = []
        states.append(state)
        if (index + 1) % 500 == 0 or (index + 1) == total:
            print(f"  {index + 1}/{total} states loaded")
    return states


def _new_episode_state(roots: list[Path], episode_path: Path) -> EpisodeState:
    metadata, findings = load_metadata(episode_path)
    if findings:
        metadata = {}
    context = infer_context(roots, episode_path, metadata)
    return EpisodeState(
        episode_path=episode_path,
        task=context["task"],
        date=context["date"],
        operator=context["operator"],
        robot=context["robot"],
        controller=context["controller"],
        metadata=metadata,
    )


def _run_phase(
    phase_number: int,
    states: list[EpisodeState],
    db_path: Path,
    workers: int,
    continue_after_fail: bool,
    monitor: RunMonitor | None,
) -> None:
    runnable = _filter_runnable_states(states, phase_number, continue_after_fail)
    skipped = len(states) - len(runnable)
    if skipped:
        print(
            f"[Phase {phase_number}] Running on {len(runnable)} episodes "
            f"({skipped} skipped due to earlier fail)..."
        )
    else:
        print(f"[Phase {phase_number}] Running on {len(runnable)} episodes...")
    runner = PHASE_RUNNERS[phase_number]
    if monitor is not None:
        monitor.start_phase(phase_number, len(runnable), skipped, states)
    callback = _make_progress_callback(len(runnable), monitor, phase_number, states)
    phase_start = time.time()
    if phase_number in (1, 2, 3, 4, 5):
        runner(runnable, db_path, progress_callback=callback, workers=workers)
    else:
        runner(runnable, db_path, progress_callback=callback)
    elapsed = time.time() - phase_start
    print()
    counts = _phase_status_counts(states, phase_number)
    if monitor is not None:
        monitor.finish_phase(phase_number, states)
    print(
        f"[Phase {phase_number}] Done. "
        f"pass={counts['pass']} fail={counts['fail']} "
        f"warning={counts['warning']} needs_review={counts['needs_review']} "
        f"({elapsed:.1f}s)"
    )


def _filter_runnable_states(
    states: list[EpisodeState],
    current_phase: int,
    continue_after_fail: bool = False,
) -> list[EpisodeState]:
    """Return states that have not failed in any previously completed phase."""
    if continue_after_fail:
        return [
            state
            for state in states
            if current_phase not in state.phases_completed
        ]
    runnable = []
    for state in states:
        failed = any(
            status == "fail"
            for phase_num, status in state.phase_status.items()
            if int(phase_num) < current_phase
        )
        if not failed:
            runnable.append(state)
    return runnable


def _print_progress(current: int, total: int, width: int = 40) -> None:
    """Print an in-place progress bar like: [####----] 10/263"""
    filled = int(width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r  [{bar}] {current}/{total}", end="", flush=True)


def _make_progress_callback(
    total: int,
    monitor: RunMonitor | None,
    phase_number: int,
    states: list[EpisodeState],
) -> Callable[[int, int], None]:
    def callback(current: int, _total: int) -> None:
        _print_progress(current, total)
        if monitor is not None:
            monitor.progress(phase_number, current, total, states)

    return callback


def _phase_status_counts(states: list[EpisodeState], phase_number: int) -> Counter:
    counts: Counter = Counter()
    for state in states:
        status = state.phase_status.get(phase_number)
        if status:
            counts[status] += 1
    return counts


def _write_final_verdicts(states: list[EpisodeState], db_path: Path) -> None:
    for state in states:
        state.final_status = _final_status(state)
        save_episode_state(db_path, state)


def _final_status(state: EpisodeState) -> str:
    statuses = set(state.phase_status.values())
    if "fail" in statuses:
        return "fail"
    if "needs_review" in statuses:
        return "needs_review"
    if "warning" in statuses:
        return "warning"
    return "pass"


def _export_reports(db_path: Path, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    quality_report = output_dir / "quality_report.csv"
    findings_jsonl = output_dir / "quality_findings.jsonl"
    summary_md = output_dir / "quality_summary.md"
    dashboard_html = output_dir / "dashboard.html"
    export_quality_report(db_path, quality_report)
    export_findings_jsonl(db_path, findings_jsonl)
    export_summary_md(db_path, summary_md)
    generate_dashboard(db_path, dashboard_html)
    return [quality_report, findings_jsonl, summary_md, dashboard_html]


def _print_final_summary(states: list[EpisodeState], output_dir: Path) -> None:
    counts = Counter(state.final_status for state in states)
    print("=== QA Pipeline Complete ===")
    print(f"Episodes processed : {len(states)}")
    print(f"Pass               : {counts['pass']}")
    print(f"Warning            : {counts['warning']}")
    print(f"Fail               : {counts['fail']}")
    print(f"Needs review       : {counts['needs_review']}")
    print(f"Reports written to : {output_dir}/")


def _print_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
