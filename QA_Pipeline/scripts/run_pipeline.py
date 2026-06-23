"""Command-line entry point for the QA pipeline."""

from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
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
    Finding,
    discover_episodes_with_report,
    export_findings_jsonl,
    export_quality_report,
    export_summary_md,
    infer_context,
    init_db,
    load_episode_state,
    load_metadata,
    prune_db_to_episode_paths,
    replace_discovery_findings,
    save_findings,
    save_episode_state,
)
from scripts.pipeline.resource_guard import ResourceGuard, ResourceGuardError
from scripts.pipeline.run_monitor import RunMonitor


STATUS_ORDER = ("pass", "fail", "warning", "needs_review")
FINAL_STATUS_ORDER = ("pass", "warning", "fail", "needs_review")
PREFLIGHT_PROGRESS_INTERVAL_SECONDS = 5.0
PREFLIGHT_PROGRESS_INTERVAL_ITEMS = 5000

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

EPISODE_SELECTION_CACHE = "selected_episodes.jsonl"
EPISODE_SELECTION_META = "selected_episodes_meta.json"
STREAMING_PROGRESS_INTERVAL_SECONDS = 10.0
STREAMING_PROGRESS_INTERVAL_SCANNED = 5000


@dataclass
class QualityFilterSummary:
    kept: int
    skipped: int
    unreadable_metadata: int
    skipped_label_counts: Counter[str]


class GracefulStopRequested(RuntimeError):
    """Raised when the user requested a safe stop via a stop file."""


class StopController:
    def __init__(self, stop_file: Path | None) -> None:
        self.stop_file = Path(stop_file) if stop_file else None

    def check(self, context: str) -> None:
        if self.stop_file is not None and self.stop_file.exists():
            raise GracefulStopRequested(f"Stop requested during {context}: {self.stop_file}")


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
    active_phases = _active_phases(phases, args.defer_umi_phase6)
    if not _validate_date_filters(args):
        return 1
    stop_controller = StopController(_stop_file_path(args, Path(args.output_dir)))

    resource_guard = _make_resource_guard(args)
    effective_workers = resource_guard.effective_workers(args.workers)

    if not args.dry_run:
        try:
            _validate_phase_dependencies(active_phases)
        except PipelineConfigurationError as exc:
            _print_error(str(exc))
            return 1

    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    if args.episode_list:
        episodes = _read_episode_list(Path(args.episode_list))
        missing_episodes = [episode for episode in episodes if not episode.exists()]
        if missing_episodes:
            _print_error(
                "Episode list contains missing path(s): "
                + ", ".join(str(path) for path in missing_episodes[:5])
            )
            if len(missing_episodes) > 5:
                print(f"  ... {len(missing_episodes) - 5} more missing path(s)")
            return 1
        invalid_episodes = [episode for episode in episodes if not episode.name.startswith("episode_")]
        if invalid_episodes:
            _print_error(
                "Episode list contains non-episode path(s): "
                + ", ".join(str(path) for path in invalid_episodes[:5])
            )
            if len(invalid_episodes) > 5:
                print(f"  ... {len(invalid_episodes) - 5} more invalid path(s)")
            return 1
        print(f"Loaded episode list: {len(episodes)} episodes", flush=True)
        if args.date or args.date_from or args.date_to:
            episodes = [
                episode_path for episode_path in episodes
                if _episode_matches_date_filters(episode_path, args)
            ]
            print(
                "After date filter "
                f"(--date={args.date or ''}, --date-from={args.date_from or ''}, "
                f"--date-to={args.date_to or ''}): {len(episodes)} episodes",
                flush=True,
            )
        if args.task:
            task_lower = args.task.lower()
            episodes = [
                episode_path for episode_path in episodes
                if task_lower in str(episode_path).lower()
            ]
            print(f"After --task filter ({args.task}): {len(episodes)} episodes", flush=True)
        if not args.disable_quality_label_filter:
            print(
                f"Applying quality label filter ({args.quality_label}) to {len(episodes)} episodes...",
                flush=True,
            )
            episodes, quality_filter_summary = _filter_by_quality_label(episodes, args.quality_label)
            print(
                f"After quality label filter ({args.quality_label}): {len(episodes)} episodes "
                f"({quality_filter_summary.skipped} skipped)"
            )
            if quality_filter_summary.unreadable_metadata:
                print(
                    "  skipped due to missing/unreadable metadata: "
                    f"{quality_filter_summary.unreadable_metadata}"
                )
            for label_summary, count in quality_filter_summary.skipped_label_counts.most_common(5):
                print(f"  skipped label {label_summary}: {count}")
            remaining_labels = len(quality_filter_summary.skipped_label_counts) - 5
            if remaining_labels > 0:
                print(f"  ... {remaining_labels} more skipped label group(s)")
        if args.max_episodes is not None:
            episodes = episodes[: args.max_episodes]
        discovery_findings = []
        group_key_cache = _build_group_key_cache(episodes, roots) if episodes else {}
    elif args.streaming_discovery:
        if args.batch_size is None:
            _print_error("--streaming-discovery requires --batch-size.")
            return 1
        init_db(db_path)
        return _run_streaming_discovery(
            roots=roots,
            phases=active_phases,
            requested_phases=phases,
            args=args,
            db_path=db_path,
            output_dir=output_dir,
            workers=effective_workers,
            resource_guard=resource_guard,
            stop_controller=stop_controller,
        )

    else:
        cache_meta = _episode_selection_cache_meta(args, roots)
        episodes, discovery_findings, group_key_cache = (
            ([], [], {})
            if args.disable_episode_selection_cache
            else _load_episode_selection_cache(output_dir, cache_meta)
        )
        if episodes:
            print(f"Loaded episode selection cache: {len(episodes)} episodes", flush=True)
        else:
            discovery = discover_episodes_with_report(roots)
            episodes = discovery.episodes
            print(f"Episodes discovered: {len(episodes)}", flush=True)
            if discovery.skipped_hidden_dirs:
                print(f"Hidden directories skipped: {len(discovery.skipped_hidden_dirs)}", flush=True)
                for path in discovery.skipped_hidden_dirs[:5]:
                    print(f"  skipped hidden dir: {path}")
                if len(discovery.skipped_hidden_dirs) > 5:
                    print(f"  ... {len(discovery.skipped_hidden_dirs) - 5} more")
            if args.date or args.date_from or args.date_to:
                episodes = [
                    episode_path for episode_path in episodes
                    if _episode_matches_date_filters(episode_path, args)
                ]
                print(
                    "After date filter "
                    f"(--date={args.date or ''}, --date-from={args.date_from or ''}, "
                    f"--date-to={args.date_to or ''}): {len(episodes)} episodes",
                    flush=True,
                )
            if args.task:
                task_lower = args.task.lower()
                episodes = [
                    episode_path for episode_path in episodes
                    if task_lower in str(episode_path).lower()
                ]
                print(f"After --task filter ({args.task}): {len(episodes)} episodes", flush=True)
            quality_filter_summary: QualityFilterSummary | None = None
            if not args.disable_quality_label_filter:
                print(
                    f"Applying quality label filter ({args.quality_label}) to {len(episodes)} episodes...",
                    flush=True,
                )
                episodes, quality_filter_summary = _filter_by_quality_label(episodes, args.quality_label)
                print(
                    f"After quality label filter ({args.quality_label}): {len(episodes)} episodes "
                    f"({quality_filter_summary.skipped} skipped)"
                )
                if quality_filter_summary.unreadable_metadata:
                    print(
                        "  skipped due to missing/unreadable metadata: "
                        f"{quality_filter_summary.unreadable_metadata}"
                    )
                for label_summary, count in quality_filter_summary.skipped_label_counts.most_common(5):
                    print(f"  skipped label {label_summary}: {count}")
                remaining_labels = len(quality_filter_summary.skipped_label_counts) - 5
                if remaining_labels > 0:
                    print(f"  ... {remaining_labels} more skipped label group(s)")
            if args.max_episodes is not None:
                episodes = episodes[: args.max_episodes]
            discovery_findings = _discovery_findings(discovery.skipped_hidden_dirs)
            if not args.dry_run and not args.disable_episode_selection_cache:
                group_key_cache = _build_group_key_cache(episodes, roots)
                _write_episode_selection_cache(output_dir, episodes, cache_meta, group_key_cache)

    if args.dry_run:
        print("Dry run: no phases executed and no reports written.")
        print("Phases selected: " + ",".join(str(phase) for phase in active_phases))
        return 0

    init_db(db_path)
    prune_db_to_episode_paths(db_path, episodes)

    if args.batch_size is not None:
        return _run_batched(
            episodes,
            roots,
            active_phases,
            phases,
            args,
            db_path,
            output_dir,
            effective_workers,
            resource_guard,
            discovery_findings,
            group_key_cache,
            stop_controller,
        )

    states = _load_or_create_states(episodes, roots, db_path, args.force_rerun, active_phases)
    monitor = None
    if not args.disable_live_monitor:
        run_id = args.run_id or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        monitor = RunMonitor(
            db_path=db_path,
            output_root=output_dir,
            run_id=run_id,
            roots=roots,
            phases=active_phases,
            workers=effective_workers,
            refresh_interval_seconds=args.live_report_interval,
        )
        monitor.start(states)
        print(f"Live run monitor: {monitor.run_dir}")
        _print_live_dashboard_command(args, db_path, output_dir)
    _record_discovery_findings(db_path, discovery_findings, monitor)
    try:
        for phase_number in active_phases:
            stop_controller.check(f"before Phase {phase_number}")
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
                stop_controller,
            )
        _mark_deferred_umi_pending(states, db_path, args.defer_umi_phase6, phases)
    except GracefulStopRequested as exc:
        print(str(exc))
        _write_final_verdicts(states, db_path)
        if monitor is not None:
            monitor.finish_run(states, status="stopped")
        return 130
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
    parser.add_argument(
        "--episode-list",
        default=None,
        help=(
            "Optional text file containing exact episode directories to process, one per line. "
            "When set, normal full-root discovery and streaming discovery are skipped."
        ),
    )
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
    parser.add_argument(
        "--streaming-discovery",
        action="store_true",
        help=(
            "Discover and process episodes incrementally in --batch-size chunks. "
            "This avoids building a full selected-episode cache for very large roots. "
            "Resume is based on existing DB phase completion."
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
        "--date-from",
        type=str,
        default=None,
        help="Only process episodes whose path date is on or after YYYYMMDD. Inclusive.",
    )
    parser.add_argument(
        "--date-to",
        type=str,
        default=None,
        help="Only process episodes whose path date is on or before YYYYMMDD. Inclusive.",
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Only process episodes whose task name contains this string "
        "(case-insensitive). Filters after discovery.",
    )
    parser.add_argument(
        "--quality-label",
        default="完全正常",
        help=(
            "Only process episodes whose metadata quality.labels contains this label. "
            "Default: 完全正常."
        ),
    )
    parser.add_argument(
        "--disable-quality-label-filter",
        action="store_true",
        help="Process episodes regardless of metadata quality.labels. Use for full audits only.",
    )
    parser.add_argument(
        "--disable-episode-selection-cache",
        action="store_true",
        help=(
            "Do not reuse or write the selected episode list cache under output-dir. "
            "Use this when the NAS folder contents changed and you need fresh discovery."
        ),
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
        default=0.0,
        help=(
            "Maximum seconds to wait for load/memory recovery before stopping. "
            "0 means wait indefinitely. Default: 0."
        ),
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
        default=5.0,
        help=(
            "Compatibility option only: sets the --interval value in the "
            "independent live_dashboard.py command printed at run start. "
            "It does not refresh the dashboard inside the pipeline. Default: 5."
        ),
    )
    parser.add_argument(
        "--live-dashboard-max-episodes",
        type=int,
        default=5000,
        help=(
            "Compatibility option only: sets --max-episodes in the printed "
            "live_dashboard.py command. 0 means unlimited. Default: 5000."
        ),
    )
    parser.add_argument(
        "--live-dashboard-max-findings",
        type=int,
        default=10000,
        help=(
            "Compatibility option only: sets --max-findings in the printed "
            "live_dashboard.py command. 0 means unlimited. Default: 10000."
        ),
    )
    parser.add_argument(
        "--disable-live-monitor",
        action="store_true",
        help="Disable run_status.json, issue_events.jsonl, and live_summary.md output.",
    )
    parser.add_argument(
        "--defer-umi-phase6",
        action="store_true",
        help=(
            "Do not run Phase 6 in the main pipeline. Mark eligible UMI episodes "
            "as umi_pending so a separate deferred UMI worker can process them."
        ),
    )
    parser.add_argument(
        "--stop-file",
        default=None,
        help=(
            "Gracefully stop when this file exists. Default: <output-dir>/STOP_REQUESTED. "
            "Set to 'none' to disable stop-file checks."
        ),
    )
    return parser.parse_args(argv)


def _run_batched(
    episodes: list[Path],
    roots: list[Path],
    phases: list[int],
    requested_phases: list[int],
    args: argparse.Namespace,
    db_path: Path,
    output_dir: Path,
    workers: int,
    resource_guard: ResourceGuard,
    discovery_findings: list[Finding],
    group_key_cache: dict[str, tuple[str, str]],
    stop_controller: StopController,
) -> int:
    batch_size = max(1, int(args.batch_size))
    total = len(episodes)
    batch_plan = _make_batch_plan(episodes, roots, phases, batch_size, args.batch_mode, group_key_cache)
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
        )
        monitor.start([])
        print(f"Live run monitor: {monitor.run_dir}")
        _print_live_dashboard_command(args, db_path, output_dir)
    _record_discovery_findings(db_path, discovery_findings, monitor)

    processed = 0
    try:
        processed_before = 0
        for batch_index, batch in enumerate(batch_plan.batches, start=1):
            stop_controller.check(f"before batch {batch_index}")
            print()
            batch_start = processed_before + 1
            batch_end = processed_before + len(batch)
            print(f"=== Batch {batch_index}/{batch_count}: episodes {batch_start}-{batch_end} of {total} ===")
            if batch_plan.mode == "group-aware":
                group_count = len({_group_key_for_path(roots, episode_path, phases) for episode_path in batch})
                print(f"Group-aware batch: groups={group_count}, episodes={len(batch)}")
            resource_guard.wait_if_needed(f"before batch {batch_index}", force=True)
            states = _load_or_create_states(batch, roots, db_path, args.force_rerun, phases)
            for phase_index, phase_number in enumerate(phases, start=1):
                stop_controller.check(f"before Phase {phase_number}")
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
                    stop_controller,
                    overall_offset=(processed_before * len(phases)) + ((phase_index - 1) * len(states)),
                    overall_total=total * len(phases),
                )
                if monitor is not None:
                    monitor.overall_progress(
                        (processed_before * len(phases)) + (phase_index * len(states)),
                        total * len(phases),
                        f"batch {batch_index}/{batch_count}",
                    )
            _mark_deferred_umi_pending(states, db_path, args.defer_umi_phase6, requested_phases)
            _write_final_verdicts(states, db_path)
            processed += len(states)
            processed_before += len(batch)
            if monitor is not None:
                monitor.refresh(states, force=True)
            print(f"Batch {batch_index}/{batch_count} complete. Processed so far: {processed}/{total}")
            del states
            gc.collect()
    except GracefulStopRequested as exc:
        print(str(exc))
        if monitor is not None:
            monitor.finish_run([], status="stopped")
        return 130
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


def _run_streaming_discovery(
    roots: list[Path],
    phases: list[int],
    requested_phases: list[int],
    args: argparse.Namespace,
    db_path: Path,
    output_dir: Path,
    workers: int,
    resource_guard: ResourceGuard,
    stop_controller: StopController,
) -> int:
    batch_size = max(1, int(args.batch_size))
    streaming_mode = _streaming_batch_mode(args.batch_mode, phases)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.disable_episode_selection_cache:
        print("Streaming discovery: episode selection cache is not used in streaming mode.")
    print(
        "Streaming discovery enabled: "
        f"mode={streaming_mode}, batch_size={batch_size}, roots={len(roots)}, "
        f"phases={','.join(str(p) for p in phases)}"
    )
    if streaming_mode == "group-aware":
        if 2 in phases:
            print("Streaming group-aware mode: Phase 2 task groups are kept together.")
        elif 3 in phases:
            print("Streaming group-aware mode: Phase 3 task+robot groups are kept together.")
    elif any(phase in phases for phase in (2, 3)):
        print(
            "Warning: streaming discovery computes Phase 2/3 group outlier checks within "
            "each fixed-size streamed batch. Use --batch-mode group-aware or auto to avoid "
            "splitting groups."
        )
    if args.max_episodes is not None:
        print(f"Streaming discovery max selected episodes: {args.max_episodes}")

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
        )
        monitor.start([])
        print(f"Live run monitor: {monitor.run_dir}")
        _print_live_dashboard_command(args, db_path, output_dir)

    skipped_hidden_dirs: list[Path] = []
    batch: list[Path] = []
    current_group_key: tuple[str, ...] | None = None
    current_group: list[Path] = []
    scanned = 0
    selected = 0
    processed = 0
    completed_groups = 0
    oversized_groups = 0
    skipped_completed = 0
    skipped_quality = 0
    skipped_unreadable = 0
    batch_index = 0
    started = time.perf_counter()
    last_report = started
    max_selected = args.max_episodes

    def flush_batch(force: bool = False) -> bool:
        nonlocal batch, processed, batch_index
        stop_controller.check("before streaming batch flush")
        if not batch:
            return True
        if not force and len(batch) < batch_size:
            return True
        batch_index += 1
        print()
        print(
            f"=== Streaming batch {batch_index}: episodes={len(batch)}, "
            f"processed_so_far={processed}, scanned={scanned} ==="
        )
        resource_guard.wait_if_needed(f"before streaming batch {batch_index}", force=True)
        states = _load_or_create_states(batch, roots, db_path, args.force_rerun, phases)
        try:
            for phase_index, phase_number in enumerate(phases, start=1):
                stop_controller.check(f"before Phase {phase_number}")
                overall_total = max_selected * len(phases) if max_selected is not None else 0
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
                    stop_controller,
                    overall_offset=(processed * len(phases)) + ((phase_index - 1) * len(states)),
                    overall_total=overall_total,
                )
                if monitor is not None:
                    monitor.overall_progress(
                        (processed * len(phases)) + (phase_index * len(states)),
                        overall_total,
                        f"streaming batch {batch_index}",
                    )
            _mark_deferred_umi_pending(states, db_path, args.defer_umi_phase6, requested_phases)
            _write_final_verdicts(states, db_path)
            processed += len(states)
            if monitor is not None:
                monitor.refresh(states, force=True)
            print(
                f"Streaming batch {batch_index} complete. "
                f"Processed so far: {processed}; scanned={scanned}; selected={selected}; "
                f"skipped_completed={skipped_completed}; skipped_quality={skipped_quality}; "
                f"skipped_unreadable={skipped_unreadable}"
            )
        finally:
            batch = []
            del states
            gc.collect()
        return True

    def add_group_to_batch(group_key: tuple[str, ...], group: list[Path]) -> None:
        nonlocal batch, completed_groups, oversized_groups
        if not group:
            return
        completed_groups += 1
        if len(group) > batch_size:
            oversized_groups += 1
            if batch:
                flush_batch(force=True)
            batch.extend(group)
            print(
                "Streaming group-aware oversized group: "
                f"{'/'.join(group_key)} has {len(group)} episodes; "
                "running as a complete batch."
            )
            flush_batch(force=True)
            return
        if batch and len(batch) + len(group) > batch_size:
            flush_batch(force=True)
        batch.extend(group)
        if len(batch) >= batch_size:
            flush_batch(force=True)

    try:
        for episode_path, hidden_dirs in _iter_episode_paths_streaming(roots):
            stop_controller.check("streaming discovery")
            if hidden_dirs:
                skipped_hidden_dirs.extend(hidden_dirs)
                continue
            scanned += 1
            if not _episode_matches_date_filters(episode_path, args):
                last_report = _print_streaming_progress(
                    scanned,
                    selected,
                    processed,
                    skipped_completed,
                    skipped_quality,
                    skipped_unreadable,
                    started,
                    last_report,
                )
                continue
            if args.task and args.task.lower() not in str(episode_path).lower():
                last_report = _print_streaming_progress(
                    scanned,
                    selected,
                    processed,
                    skipped_completed,
                    skipped_quality,
                    skipped_unreadable,
                    started,
                    last_report,
                )
                continue
            keep, unreadable = _streaming_quality_filter(episode_path, args)
            if not keep:
                if unreadable:
                    skipped_unreadable += 1
                else:
                    skipped_quality += 1
                last_report = _print_streaming_progress(
                    scanned,
                    selected,
                    processed,
                    skipped_completed,
                    skipped_quality,
                    skipped_unreadable,
                    started,
                    last_report,
                )
                continue
            if _streaming_episode_completed(db_path, episode_path, phases, args.force_rerun):
                skipped_completed += 1
                last_report = _print_streaming_progress(
                    scanned,
                    selected,
                    processed,
                    skipped_completed,
                    skipped_quality,
                    skipped_unreadable,
                    started,
                    last_report,
                )
                continue

            selected += 1
            if streaming_mode == "group-aware":
                group_key = _group_key_for_path(roots, episode_path, phases)
                if current_group_key is None:
                    current_group_key = group_key
                elif group_key != current_group_key:
                    add_group_to_batch(current_group_key, current_group)
                    current_group = []
                    current_group_key = group_key
                current_group.append(episode_path)
            else:
                batch.append(episode_path)
            if max_selected is not None and selected >= max_selected:
                if streaming_mode == "group-aware" and current_group_key is not None:
                    add_group_to_batch(current_group_key, current_group)
                    current_group = []
                    current_group_key = None
                flush_batch(force=True)
                break
            if streaming_mode == "fixed" and len(batch) >= batch_size:
                flush_batch(force=True)
            last_report = _print_streaming_progress(
                scanned,
                selected,
                processed,
                skipped_completed,
                skipped_quality,
                skipped_unreadable,
                started,
                last_report,
            )

        if streaming_mode == "group-aware" and current_group_key is not None:
            add_group_to_batch(current_group_key, current_group)
            current_group = []
            current_group_key = None
        flush_batch(force=True)
    except GracefulStopRequested as exc:
        print(str(exc))
        if monitor is not None:
            monitor.finish_run([], status="stopped")
        return 130
    except ResourceGuardError as exc:
        _print_error(str(exc))
        if monitor is not None:
            monitor.finish_run([])
        return 2

    _record_discovery_findings(db_path, _discovery_findings(skipped_hidden_dirs), monitor)
    if monitor is not None:
        monitor.finish_run([])
    report_paths = _export_reports(db_path, output_dir)
    if monitor is not None:
        report_paths.extend(_export_reports(db_path, monitor.run_dir / "final"))
    for report_path in report_paths:
        print(f"Wrote report: {report_path}")
    print(
        "Streaming discovery complete: "
        f"scanned={scanned}, selected={selected}, processed={processed}, "
        f"groups={completed_groups}, oversized_groups={oversized_groups}, "
        f"skipped_completed={skipped_completed}, skipped_quality={skipped_quality}, "
        f"skipped_unreadable={skipped_unreadable}, hidden_dirs={len(skipped_hidden_dirs)}"
    )
    _print_db_final_summary(db_path, output_dir)
    return 0


def _iter_episode_paths_streaming(roots: list[Path]):
    for root in roots:
        stack = [Path(root)]
        while stack:
            current = stack.pop()
            if current.name == "_quarantine":
                continue
            if current.name.startswith("episode_"):
                yield current, []
                continue
            try:
                children = sorted(
                    (child for child in current.iterdir() if child.is_dir()),
                    key=lambda item: item.name,
                    reverse=True,
                )
            except OSError:
                continue
            for child in children:
                if child.name == "_quarantine":
                    continue
                if child.name.startswith("."):
                    yield current, [child]
                    continue
                stack.append(child)


def _read_episode_list(path: Path) -> list[Path]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"Unable to read --episode-list {path}: {exc}") from exc
    episodes: list[Path] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        episodes.append(Path(stripped))
    return episodes


def _streaming_batch_mode(requested_mode: str, phases: list[int]) -> str:
    if requested_mode == "auto":
        return "group-aware" if any(phase in phases for phase in (2, 3)) else "fixed"
    return requested_mode


def _active_phases(phases: list[int], defer_umi_phase6: bool) -> list[int]:
    if not defer_umi_phase6:
        return phases
    return [phase for phase in phases if phase != 6]


def _stop_file_path(args: argparse.Namespace, output_dir: Path) -> Path | None:
    if args.stop_file == "none":
        return None
    if args.stop_file:
        return Path(args.stop_file)
    return output_dir / "STOP_REQUESTED"


def _mark_deferred_umi_pending(
    states: list[EpisodeState],
    db_path: Path,
    defer_umi_phase6: bool,
    requested_phases: list[int],
) -> None:
    if not defer_umi_phase6 or 6 not in requested_phases:
        return
    marked = 0
    for state in states:
        if 6 in {int(phase) for phase in state.phases_completed}:
            continue
        if _has_failed_prior_phase(state, 6):
            continue
        if not phase6_umi_processing.is_umi_state(state):
            continue
        state.metrics["p6_umi_status"] = "pending"
        state.metrics["p6_umi_reason"] = "Deferred UMI Phase 6 is pending."
        state.phase_status[6] = "pending"
        state.last_updated = datetime.now().isoformat()
        save_episode_state(db_path, state)
        save_findings(
            db_path,
            [
                Finding(
                    episode_path=str(state.episode_path),
                    phase=6,
                    check_name="umi_phase6_deferred",
                    severity="info",
                    status="pending",
                    message="UMI Phase 6 processing is deferred to a separate worker.",
                    details={},
                )
            ],
            phase=6,
            episode_path=str(state.episode_path),
        )
        marked += 1
    if marked:
        print(f"Deferred UMI Phase 6: marked {marked} episode(s) as umi_pending.")


def _has_failed_prior_phase(state: EpisodeState, current_phase: int) -> bool:
    return any(
        status == "fail"
        for phase_num, status in state.phase_status.items()
        if int(phase_num) < current_phase
    )


def _episode_matches_date_filters(episode_path: Path, args: argparse.Namespace) -> bool:
    if args.date and args.date not in str(episode_path):
        return False
    if not args.date_from and not args.date_to:
        return True
    episode_date = _episode_path_date(episode_path)
    if episode_date is None:
        return False
    if args.date_from and episode_date < args.date_from:
        return False
    if args.date_to and episode_date > args.date_to:
        return False
    return True


def _episode_path_date(episode_path: Path) -> str | None:
    for part in episode_path.parts:
        if len(part) == 8 and part.isdigit():
            return part
    return None


def _streaming_quality_filter(episode_path: Path, args: argparse.Namespace) -> tuple[bool, bool]:
    if args.disable_quality_label_filter:
        return True, False
    metadata, findings = load_metadata(episode_path)
    if findings:
        return False, True
    return args.quality_label in _quality_labels(metadata), False


def _streaming_episode_completed(
    db_path: Path,
    episode_path: Path,
    phases: list[int],
    force_rerun: bool,
) -> bool:
    if force_rerun:
        return False
    state = load_episode_state(db_path, episode_path)
    if state is None:
        return False
    completed = {int(phase) for phase in state.phases_completed}
    return all(phase in completed for phase in phases)


def _print_streaming_progress(
    scanned: int,
    selected: int,
    processed: int,
    skipped_completed: int,
    skipped_quality: int,
    skipped_unreadable: int,
    started: float,
    last_report: float,
    force: bool = False,
) -> float:
    now = time.perf_counter()
    if (
        not force
        and scanned % STREAMING_PROGRESS_INTERVAL_SCANNED != 0
        and now - last_report < STREAMING_PROGRESS_INTERVAL_SECONDS
    ):
        return last_report
    elapsed = max(0.001, now - started)
    rate = scanned / elapsed if scanned else 0.0
    print(
        "  streaming discovery: "
        f"scanned={scanned}, selected={selected}, processed={processed}, "
        f"skipped_completed={skipped_completed}, skipped_quality={skipped_quality}, "
        f"skipped_unreadable={skipped_unreadable}, "
        f"scan_rate={rate:.1f}/s, elapsed={_format_duration(elapsed)}",
        flush=True,
    )
    return now


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
    group_key_cache: dict[str, tuple[str, str]] | None = None,
) -> BatchPlan:
    if requested_mode == "auto":
        mode = "group-aware" if any(phase in phases for phase in (2, 3)) else "fixed"
    else:
        mode = requested_mode
    if mode == "group-aware":
        return _make_group_aware_batch_plan(episodes, roots, phases, batch_size, group_key_cache or {})
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
    group_key_cache: dict[str, tuple[str, str]],
) -> BatchPlan:
    if not episodes:
        return BatchPlan("group-aware", [], [])
    print(
        f"Planning group-aware batches for {len(episodes)} episodes...",
        flush=True,
    )
    grouped: dict[tuple[str, ...], list[Path]] = {}
    total = len(episodes)
    started = time.perf_counter()
    last_report = started
    for index, episode_path in enumerate(episodes, start=1):
        grouped.setdefault(_group_key_for_path(roots, episode_path, phases, group_key_cache), []).append(episode_path)
        last_report = _print_prefilter_progress(
            "group-aware batch planning",
            index,
            total,
            started,
            last_report,
            len(grouped),
            force=index == total,
        )

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


def _group_key_for_path(
    roots: list[Path],
    episode_path: Path,
    phases: list[int],
    group_key_cache: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, ...]:
    if group_key_cache:
        cached = group_key_cache.get(str(episode_path))
        if cached:
            task, robot = cached
            if 2 in phases:
                return (task,)
            if 3 in phases:
                return (task, robot)
            return (task,)
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


def _print_live_dashboard_command(args: argparse.Namespace, db_path: Path, output_dir: Path) -> None:
    print("Live dashboard runs independently. Start it in another terminal with:")
    print(
        "  python3 QA_Pipeline/scripts/live_dashboard.py "
        f"--db-path {db_path} "
        f"--output-dir {output_dir} "
        f"--interval {args.live_dashboard_interval:g} "
        f"--max-episodes {args.live_dashboard_max_episodes} "
        f"--max-findings {args.live_dashboard_max_findings} "
        "--port 1234"
    )


def _filter_by_quality_label(
    episodes: list[Path],
    required_label: str,
) -> tuple[list[Path], QualityFilterSummary]:
    kept = []
    skipped_label_counts: Counter[str] = Counter()
    unreadable_metadata = 0
    total = len(episodes)
    started = time.perf_counter()
    last_report = started
    for index, episode_path in enumerate(episodes, start=1):
        metadata, metadata_findings = load_metadata(episode_path)
        if metadata_findings:
            unreadable_metadata += 1
            skipped_label_counts["(metadata_unreadable)"] += 1
            last_report = _print_prefilter_progress(
                "quality label filter",
                index,
                total,
                started,
                last_report,
                len(kept),
                force=index == total,
            )
            continue
        labels = _quality_labels(metadata)
        if required_label in labels:
            kept.append(episode_path)
        else:
            skipped_label_counts[_label_summary(labels)] += 1
        last_report = _print_prefilter_progress(
            "quality label filter",
            index,
            total,
            started,
            last_report,
            len(kept),
            force=index == total,
        )
    return kept, QualityFilterSummary(
        kept=len(kept),
        skipped=len(episodes) - len(kept),
        unreadable_metadata=unreadable_metadata,
        skipped_label_counts=skipped_label_counts,
    )


def _episode_selection_cache_meta(args: argparse.Namespace, roots: list[Path]) -> dict:
    return {
        "version": 1,
        "roots": [str(root) for root in roots],
        "date": args.date or "",
        "date_from": args.date_from or "",
        "date_to": args.date_to or "",
        "task": args.task or "",
        "quality_label_filter_enabled": not args.disable_quality_label_filter,
        "quality_label": args.quality_label if not args.disable_quality_label_filter else "",
        "max_episodes": args.max_episodes,
    }


def _load_episode_selection_cache(
    output_dir: Path,
    expected_meta: dict,
) -> tuple[list[Path], list[Finding], dict[str, tuple[str, str]]]:
    meta_path = output_dir / EPISODE_SELECTION_META
    paths_path = output_dir / EPISODE_SELECTION_CACHE
    if not meta_path.exists() or not paths_path.exists():
        return [], [], {}
    try:
        actual_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], [], {}
    if actual_meta != expected_meta:
        return [], [], {}
    episodes = []
    group_key_cache: dict[str, tuple[str, str]] = {}
    try:
        for line in paths_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            path = str(record["episode_path"])
            episodes.append(Path(path))
            group_key_cache[path] = (
                str(record.get("task") or "(unknown_task)"),
                str(record.get("robot") or "(unknown_robot)"),
            )
    except (OSError, KeyError, json.JSONDecodeError):
        return [], [], {}
    return episodes, [], group_key_cache


def _write_episode_selection_cache(
    output_dir: Path,
    episodes: list[Path],
    meta: dict,
    group_key_cache: dict[str, tuple[str, str]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    meta_path = output_dir / EPISODE_SELECTION_META
    paths_path = output_dir / EPISODE_SELECTION_CACHE
    _write_text_atomic(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    lines = []
    for episode in episodes:
        task, robot = group_key_cache.get(str(episode), ("(unknown_task)", "(unknown_robot)"))
        lines.append(
            json.dumps(
                {
                    "episode_path": str(episode),
                    "task": task,
                    "robot": robot,
                },
                ensure_ascii=False,
            )
        )
    _write_text_atomic(paths_path, "\n".join(lines) + ("\n" if lines else ""))


def _build_group_key_cache(episodes: list[Path], roots: list[Path]) -> dict[str, tuple[str, str]]:
    cache: dict[str, tuple[str, str]] = {}
    total = len(episodes)
    started = time.perf_counter()
    last_report = started
    for index, episode_path in enumerate(episodes, start=1):
        metadata, findings = load_metadata(episode_path)
        if findings:
            metadata = {}
        context = infer_context(roots, episode_path, metadata)
        task = str(context.get("task") or metadata.get("task_key") or "(unknown_task)")
        robot = str(context.get("robot") or metadata.get("robot") or "(unknown_robot)")
        cache[str(episode_path)] = (task, robot)
        last_report = _print_prefilter_progress(
            "episode selection cache",
            index,
            total,
            started,
            last_report,
            len(cache),
            force=index == total,
        )
    return cache


def _quality_labels(metadata: dict) -> list[str]:
    quality = metadata.get("quality")
    if not isinstance(quality, dict):
        return []
    labels = quality.get("labels")
    if not isinstance(labels, list):
        return []
    return [label for label in labels if isinstance(label, str)]


def _label_summary(labels: list[str]) -> str:
    if not labels:
        return "(missing_or_empty_quality_labels)"
    return "|".join(labels)


def _print_prefilter_progress(
    label: str,
    current: int,
    total: int,
    started: float,
    last_report: float,
    kept: int | None = None,
    force: bool = False,
) -> float:
    now = time.perf_counter()
    if (
        not force
        and current % PREFLIGHT_PROGRESS_INTERVAL_ITEMS != 0
        and now - last_report < PREFLIGHT_PROGRESS_INTERVAL_SECONDS
    ):
        return last_report
    elapsed = max(0.001, now - started)
    rate = current / elapsed
    eta = _eta_seconds(current, total, elapsed)
    percent = (current / total * 100.0) if total else 100.0
    kept_text = f", kept={kept}" if kept is not None else ""
    print(
        f"  {label}: {current}/{total} "
        f"({percent:.1f}%, {rate:.1f}/s, elapsed={_format_duration(elapsed)}, "
        f"eta={_format_duration(eta)}{kept_text})",
        flush=True,
    )
    return now


def _eta_seconds(current: int, total: int, elapsed_seconds: float) -> float:
    if current <= 0 or total <= 0 or current >= total:
        return 0.0
    rate = current / max(0.001, elapsed_seconds)
    return max(0.0, (total - current) / rate)


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _discovery_findings(skipped_hidden_dirs: list[Path]) -> list[Finding]:
    findings = []
    for hidden_dir in skipped_hidden_dirs:
        findings.append(
            Finding(
                episode_path=str(hidden_dir),
                phase=0,
                check_name="hidden_directory_skipped",
                severity="info",
                status="warning",
                message="Hidden directory skipped during episode discovery",
                details={
                    "path": str(hidden_dir),
                    "reason": "Directory name starts with '.' and is not treated as source episode data.",
                    "effect": "The directory and all nested episodes/files were excluded from QA processing.",
                },
            )
        )
    return findings


def _record_discovery_findings(
    db_path: Path,
    findings: list[Finding],
    monitor: RunMonitor | None,
) -> None:
    replace_discovery_findings(db_path, findings)
    if monitor is not None:
        monitor.refresh([], force=True)


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


def _validate_date_filters(args: argparse.Namespace) -> bool:
    for option_name, value in (
        ("--date-from", args.date_from),
        ("--date-to", args.date_to),
    ):
        if value and (len(value) != 8 or not value.isdigit()):
            _print_error(f"{option_name} must use YYYYMMDD format, got: {value}")
            return False
    if args.date_from and args.date_to and args.date_from > args.date_to:
        _print_error("--date-from must be earlier than or equal to --date-to")
        return False
    return True


def _validate_phase_dependencies(phases: list[int]) -> None:
    for phase_number in phases:
        validator = getattr(PHASE_MODULES[phase_number], "validate_dependencies", None)
        if validator is not None:
            validator()


def _load_or_create_states(
    episodes: list[Path],
    roots: list[Path],
    db_path: Path,
    force_rerun: bool,
    force_rerun_phases: list[int] | None = None,
) -> list[EpisodeState]:
    print("Loading episode states...")
    states = []
    total = len(episodes)
    started = time.perf_counter()
    for index, episode_path in enumerate(episodes):
        state = load_episode_state(db_path, episode_path)
        if state is None:
            state = _new_episode_state(roots, episode_path)
        if force_rerun:
            selected_phases = {int(phase) for phase in (force_rerun_phases or [])}
            if selected_phases:
                state.phases_completed = [
                    int(phase)
                    for phase in state.phases_completed
                    if int(phase) not in selected_phases
                ]
            else:
                state.phases_completed = []
        states.append(state)
        if (index + 1) % 500 == 0 or (index + 1) == total:
            current = index + 1
            elapsed = max(0.001, time.perf_counter() - started)
            rate = current / elapsed
            eta = _eta_seconds(current, total, elapsed)
            print(
                f"  {current}/{total} states loaded "
                f"({rate:.1f}/s, elapsed={_format_duration(elapsed)}, "
                f"eta={_format_duration(eta)})",
                flush=True,
            )
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
    stop_controller: StopController,
    overall_offset: int = 0,
    overall_total: int = 0,
) -> None:
    attempts = max(0, int(max_retries)) + 1
    for attempt in range(1, attempts + 1):
        try:
            stop_controller.check(f"before Phase {phase_number} attempt {attempt}")
            _run_phase(
                phase_number,
                states,
                db_path,
                workers,
                continue_after_fail,
                monitor,
                resource_guard,
                stop_controller,
                overall_offset,
                overall_total,
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
    stop_controller: StopController,
    overall_offset: int = 0,
    overall_total: int = 0,
) -> None:
    stop_controller.check(f"before Phase {phase_number}")
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
    callback = _make_progress_callback(
        len(runnable),
        monitor,
        phase_number,
        states,
        resource_guard,
        stop_controller,
        overall_offset,
        overall_total,
    )
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
        if not _has_failed_prior_phase(state, current_phase):
            runnable.append(state)
    return runnable


def _print_progress(
    current: int,
    total: int,
    started: float | None = None,
    width: int = 40,
) -> None:
    """Print an in-place progress bar like: [####----] 10/263"""
    filled = int(width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (width - filled)
    timing = ""
    if started is not None and current > 0:
        elapsed = max(0.001, time.perf_counter() - started)
        rate = current / elapsed
        eta = _eta_seconds(current, total, elapsed)
        timing = (
            f" {rate:.1f}/s elapsed={_format_duration(elapsed)} "
            f"eta={_format_duration(eta)}"
        )
    print(f"\r  [{bar}] {current}/{total}{timing}", end="", flush=True)


def _make_progress_callback(
    total: int,
    monitor: RunMonitor | None,
    phase_number: int,
    states: list[EpisodeState],
    resource_guard: ResourceGuard,
    stop_controller: StopController,
    overall_offset: int = 0,
    overall_total: int = 0,
) -> Callable[[int, int], None]:
    started = time.perf_counter()

    def callback(current: int, _total: int) -> None:
        _print_progress(current, total, started)
        if monitor is not None:
            monitor.progress(phase_number, current, total, states)
            if overall_total > 0:
                monitor.overall_progress(overall_offset + current, overall_total)
        stop_controller.check(f"Phase {phase_number} progress {current}/{total}")
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
    if state.metrics.get("p6_umi_status") == "pending":
        return "umi_pending"
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
    print(f"UMI pending        : {counts['umi_pending']}")
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
    print(f"UMI pending        : {counts['umi_pending']}")
    print(f"Pending/other      : {counts['pending']}")
    print(f"Reports written to : {output_dir}/")


def _print_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


def _none_if_non_positive(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def _write_text_atomic(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
