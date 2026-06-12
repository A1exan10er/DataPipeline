"""Command-line entry point for the QA pipeline."""

from __future__ import annotations

import argparse
import gc
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline import (
    phase1_metadata,
    phase2_duration,
    phase3_timestamp,
    phase4_video,
    phase5_robot_state,
    phase6_umi_processing,
)
from scripts.generate_dashboard import generate_dashboard
from scripts.pipeline.qa_core import (
    EpisodeState,
    PipelineConfigurationError,
    discover_episodes,
    export_findings_jsonl,
    export_excel_report,
    export_quality_report,
    export_summary_md,
    infer_context,
    init_db,
    load_episode_state,
    load_metadata,
    save_episode_state,
)
from scripts.pipeline.resource_guard import ResourceGuard, ResourceGuardError
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
    6: phase6_umi_processing.run_phase,
}

PHASE_MODULES = {
    1: phase1_metadata,
    2: phase2_duration,
    3: phase3_timestamp,
    4: phase4_video,
    5: phase5_robot_state,
    6: phase6_umi_processing,
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

    resource_guard = _make_resource_guard(args)
    effective_workers = resource_guard.effective_workers(args.workers)

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

    if args.batch_size is not None:
        return _run_batched(
            episodes,
            roots,
            phases,
            args,
            db_path,
            output_dir,
            effective_workers,
            resource_guard,
        )

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
            workers=effective_workers,
            refresh_interval_seconds=args.live_report_interval,
            dashboard_interval_seconds=args.live_dashboard_interval,
        )
        monitor.start(states)
        print(f"Live run monitor: {monitor.run_dir}")
    try:
        for phase_number in phases:
            _run_phase_with_retries(
                phase_number,
                states,
                db_path,
                effective_workers,
                args.continue_after_fail,
                monitor,
                resource_guard,
                args.resource_error_retries,
                args.resource_retry_delay_seconds,
            )
    except ResourceGuardError as exc:
        _print_error(str(exc))
        if monitor is not None:
            monitor.finish_run(states)
        return 2

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
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Process episodes in batches to limit memory use, e.g. 1000. Default: load all selected episodes.",
    )
    parser.add_argument(
        "--batch-mode",
        choices=("auto", "fixed", "group-aware"),
        default="auto",
        help=(
            "Batching strategy. auto uses group-aware batches when Phase 2 or 3 "
            "is selected, otherwise fixed-size batches. Default: auto."
        ),
    )
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
        help="Requested parallel worker processes. Resource guard may lower this value. Default: 1.",
    )
    parser.add_argument(
        "--disable-resource-guard",
        action="store_true",
        help="Disable worker limiting and load/memory checks. Not recommended on shared servers.",
    )
    parser.add_argument(
        "--max-load-ratio",
        type=float,
        default=0.75,
        help="Pause when 1-minute load average exceeds CPU cores times this ratio. Default: 0.75.",
    )
    parser.add_argument(
        "--min-free-mem-gb",
        type=float,
        default=3.0,
        help="Pause when available memory drops below this many GB. Default: 3.0.",
    )
    parser.add_argument(
        "--resource-check-interval",
        type=float,
        default=30.0,
        help="Seconds between in-run resource checks. Default: 30.",
    )
    parser.add_argument(
        "--resource-max-wait-seconds",
        type=float,
        default=120.0,
        help="Maximum seconds to wait for load/memory recovery before stopping. Default: 120.",
    )
    parser.add_argument(
        "--resource-error-retries",
        type=int,
        default=3,
        help="Retry a phase this many times after a resource-guard stop. Default: 3.",
    )
    parser.add_argument(
        "--resource-retry-delay-seconds",
        type=float,
        default=30.0,
        help="Seconds to wait before retrying after a resource-guard stop. Default: 30.",
    )
    parser.add_argument(
        "--overload-action",
        choices=("pause", "stop"),
        default="pause",
        help="Action when host load or memory is unsafe. Default: pause.",
    )
    parser.add_argument(
        "--max-workers-safe",
        type=int,
        default=None,
        help="Maximum workers allowed by resource guard. Default: half of CPU cores.",
    )
    parser.add_argument("--run-id", default=None, help="Optional live-monitor run ID. Defaults to timestamp.")
    parser.add_argument(
        "--live-report-interval",
        type=float,
        default=2.0,
        help="Seconds between live monitor refreshes. Default: 2.0.",
    )
    parser.add_argument(
        "--live-dashboard-interval",
        type=float,
        default=30.0,
        help="Seconds between live dashboard HTML refreshes. Default: 30.",
    )
    parser.add_argument(
        "--disable-live-monitor",
        action="store_true",
        help="Disable run_status.json, issue_events.jsonl, and live_summary.md output.",
    )
    return parser.parse_args(argv)


def _run_batched(
    episodes: list[Path],
    roots: list[Path],
    phases: list[int],
    args: argparse.Namespace,
    db_path: Path,
    output_dir: Path,
    workers: int,
    resource_guard: ResourceGuard,
) -> int:
    batch_size = max(1, int(args.batch_size))
    total = len(episodes)
    batch_plan = _make_batch_plan(episodes, roots, phases, batch_size, args.batch_mode)
    batch_count = len(batch_plan.batches)
    print(
        "Batch mode enabled: "
        f"mode={batch_plan.mode}, batch_size={batch_size}, batches={batch_count}, episodes={total}"
    )
    if args.max_episodes is not None and any(phase in phases for phase in (2, 3)):
        print(
            "Warning: --max-episodes was applied before group-aware batching. "
            "Phase 2/3 outlier groups may still be incomplete because episodes outside "
            "the max-episodes limit were excluded."
        )
    for warning in batch_plan.warnings:
        print(f"Warning: {warning}")

    monitor = None
    if not args.disable_live_monitor:
        run_id = args.run_id or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        monitor = RunMonitor(
            db_path=db_path,
            output_root=output_dir,
            run_id=run_id,
            roots=roots,
            phases=phases,
            workers=workers,
            refresh_interval_seconds=args.live_report_interval,
            dashboard_interval_seconds=args.live_dashboard_interval,
        )
        monitor.start([])
        print(f"Live run monitor: {monitor.run_dir}")

    processed = 0
    try:
        processed_before = 0
        for batch_index, batch in enumerate(batch_plan.batches, start=1):
            print()
            batch_start = processed_before + 1
            batch_end = processed_before + len(batch)
            print(f"=== Batch {batch_index}/{batch_count}: episodes {batch_start}-{batch_end} of {total} ===")
            if batch_plan.mode == "group-aware":
                group_count = len({_group_key_for_path(roots, episode_path, phases) for episode_path in batch})
                print(f"Group-aware batch: groups={group_count}, episodes={len(batch)}")
            resource_guard.wait_if_needed(f"before batch {batch_index}", force=True)
            states = _load_or_create_states(batch, roots, db_path, args.force_rerun)
            for phase_number in phases:
                _run_phase_with_retries(
                    phase_number,
                    states,
                    db_path,
                    workers,
                    args.continue_after_fail,
                    monitor,
                    resource_guard,
                    args.resource_error_retries,
                    args.resource_retry_delay_seconds,
                )
            _write_final_verdicts(states, db_path)
            processed += len(states)
            processed_before += len(batch)
            if monitor is not None:
                monitor.refresh(states, force=True)
            generate_dashboard(db_path, output_dir / "dashboard.html")
            print(f"Batch {batch_index}/{batch_count} complete. Processed so far: {processed}/{total}")
            del states
            gc.collect()
    except ResourceGuardError as exc:
        _print_error(str(exc))
        if monitor is not None:
            monitor.finish_run([])
        return 2

    if monitor is not None:
        monitor.finish_run([])
    report_paths = _export_reports(db_path, output_dir)
    if monitor is not None:
        report_paths.extend(_export_reports(db_path, monitor.run_dir / "final"))
    for report_path in report_paths:
        print(f"Wrote report: {report_path}")
    _print_db_final_summary(db_path, output_dir)
    return 0


class BatchPlan:
    def __init__(self, mode: str, batches: list[list[Path]], warnings: list[str]) -> None:
        self.mode = mode
        self.batches = batches
        self.warnings = warnings


def _make_batch_plan(
    episodes: list[Path],
    roots: list[Path],
    phases: list[int],
    batch_size: int,
    requested_mode: str,
) -> BatchPlan:
    if requested_mode == "auto":
        mode = "group-aware" if any(phase in phases for phase in (2, 3)) else "fixed"
    else:
        mode = requested_mode
    if mode == "group-aware":
        return _make_group_aware_batch_plan(episodes, roots, phases, batch_size)
    warnings = []
    if any(phase in phases for phase in (2, 3)):
        warnings.append(
            "Phase 2/3 group-level outlier checks are computed within each fixed-size batch. "
            "Use --batch-mode group-aware or --batch-mode auto to avoid splitting outlier groups."
        )
    return BatchPlan("fixed", _fixed_batches(episodes, batch_size), warnings)


def _fixed_batches(episodes: list[Path], batch_size: int) -> list[list[Path]]:
    return [episodes[start : start + batch_size] for start in range(0, len(episodes), batch_size)]


def _make_group_aware_batch_plan(
    episodes: list[Path],
    roots: list[Path],
    phases: list[int],
    batch_size: int,
) -> BatchPlan:
    if not episodes:
        return BatchPlan("group-aware", [], [])
    grouped: dict[tuple[str, ...], list[Path]] = {}
    for episode_path in episodes:
        grouped.setdefault(_group_key_for_path(roots, episode_path, phases), []).append(episode_path)

    batches: list[list[Path]] = []
    current: list[Path] = []
    oversized_groups: list[tuple[tuple[str, ...], int]] = []
    for group_key in sorted(grouped):
        group = grouped[group_key]
        if len(group) > batch_size:
            oversized_groups.append((group_key, len(group)))
        if current and len(current) + len(group) > batch_size:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)

    warnings = [
        "Using group-aware batching because Phase 2 or 3 was selected. "
        "Outlier groups will not be split across batches."
    ]
    if 2 in phases:
        warnings.append("Phase 2 group-aware batching uses complete task groups.")
    elif 3 in phases:
        warnings.append("Phase 3 group-aware batching uses complete task+robot groups.")
    if oversized_groups:
        examples = ", ".join(f"{'/'.join(key)}={count}" for key, count in oversized_groups[:5])
        warnings.append(
            "Some groups exceed --batch-size and will run as oversized complete batches "
            f"to keep outlier statistics correct: {examples}"
        )
    return BatchPlan("group-aware", batches, warnings)


def _group_key_for_path(roots: list[Path], episode_path: Path, phases: list[int]) -> tuple[str, ...]:
    metadata, findings = load_metadata(episode_path)
    if findings:
        metadata = {}
    context = infer_context(roots, episode_path, metadata)
    task = str(context.get("task") or metadata.get("task_key") or "(unknown_task)")
    robot = str(context.get("robot") or metadata.get("robot") or "(unknown_robot)")
    if 2 in phases:
        return (task,)
    if 3 in phases:
        return (task, robot)
    return (task,)


def _make_resource_guard(args: argparse.Namespace) -> ResourceGuard:
    return ResourceGuard(
        enabled=not args.disable_resource_guard,
        max_load_ratio=args.max_load_ratio,
        min_free_mem_gb=args.min_free_mem_gb,
        check_interval_seconds=args.resource_check_interval,
        max_wait_seconds=args.resource_max_wait_seconds,
        overload_action=args.overload_action,
        max_workers_safe=args.max_workers_safe,
    )


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


def _run_phase_with_retries(
    phase_number: int,
    states: list[EpisodeState],
    db_path: Path,
    workers: int,
    continue_after_fail: bool,
    monitor: RunMonitor | None,
    resource_guard: ResourceGuard,
    max_retries: int,
    retry_delay_seconds: float,
) -> None:
    attempts = max(0, int(max_retries)) + 1
    for attempt in range(1, attempts + 1):
        try:
            _run_phase(
                phase_number,
                states,
                db_path,
                workers,
                continue_after_fail,
                monitor,
                resource_guard,
            )
            return
        except ResourceGuardError as exc:
            if attempt >= attempts:
                raise
            print()
            print(
                "Resource guard: phase "
                f"{phase_number} stopped attempt {attempt}/{attempts}: {exc}"
            )
            _write_final_verdicts(states, db_path)
            if monitor is not None:
                monitor.refresh(states, force=True)
            delay = max(0.0, float(retry_delay_seconds))
            if delay:
                print(f"Resource guard: retrying Phase {phase_number} in {delay:.0f}s...")
                time.sleep(delay)
            else:
                print(f"Resource guard: retrying Phase {phase_number} now...")


def _run_phase(
    phase_number: int,
    states: list[EpisodeState],
    db_path: Path,
    workers: int,
    continue_after_fail: bool,
    monitor: RunMonitor | None,
    resource_guard: ResourceGuard,
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
    resource_guard.wait_if_needed(f"before Phase {phase_number}", force=True)
    runner = PHASE_RUNNERS[phase_number]
    if monitor is not None:
        monitor.start_phase(phase_number, len(runnable), skipped, states)
    callback = _make_progress_callback(len(runnable), monitor, phase_number, states, resource_guard)
    phase_start = time.time()
    if phase_number in (1, 2, 3, 4, 5, 6):
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
    resource_guard: ResourceGuard,
) -> Callable[[int, int], None]:
    def callback(current: int, _total: int) -> None:
        _print_progress(current, total)
        if monitor is not None:
            monitor.progress(phase_number, current, total, states)
        resource_guard.wait_if_needed(f"Phase {phase_number} progress {current}/{total}")

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
    excel_report = output_dir / "quality_report.xlsx"
    export_quality_report(db_path, quality_report)
    export_findings_jsonl(db_path, findings_jsonl)
    export_summary_md(db_path, summary_md)
    generate_dashboard(db_path, dashboard_html)
    export_excel_report(db_path, excel_report)
    return [quality_report, findings_jsonl, summary_md, dashboard_html, excel_report]


def _print_final_summary(states: list[EpisodeState], output_dir: Path) -> None:
    counts = Counter(state.final_status for state in states)
    print("=== QA Pipeline Complete ===")
    print(f"Episodes processed : {len(states)}")
    print(f"Pass               : {counts['pass']}")
    print(f"Warning            : {counts['warning']}")
    print(f"Fail               : {counts['fail']}")
    print(f"Needs review       : {counts['needs_review']}")
    print(f"Reports written to : {output_dir}/")


def _print_db_final_summary(db_path: Path, output_dir: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT final_status, COUNT(*)
            FROM episodes
            GROUP BY final_status
            """
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    counts = Counter({status or "pending": count for status, count in rows})
    print("=== QA Pipeline Complete ===")
    print(f"Episodes in DB     : {total}")
    print(f"Pass               : {counts['pass']}")
    print(f"Warning            : {counts['warning']}")
    print(f"Fail               : {counts['fail']}")
    print(f"Needs review       : {counts['needs_review']}")
    print(f"Pending/other      : {counts['pending']}")
    print(f"Reports written to : {output_dir}/")


def _print_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
