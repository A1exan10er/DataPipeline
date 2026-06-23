"""Run deferred UMI Phase 6 work from an existing QA database."""

from __future__ import annotations

import argparse
import gc
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline import phase6_umi_processing
from scripts.pipeline.qa_core import EpisodeState, init_db, load_all_states, save_episode_state
from scripts.pipeline.resource_guard import ResourceGuard
from scripts.pipeline.run_monitor import RunMonitor


class GracefulStopRequested(RuntimeError):
    """Raised when a stop file asks the worker to exit cleanly."""


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    stop_file = _stop_file_path(args, output_dir)
    init_db(db_path)

    resource_guard = ResourceGuard(
        enabled=not args.disable_resource_guard,
        max_load_ratio=args.max_load_ratio,
        min_free_mem_gb=args.min_free_mem_gb,
        check_interval_seconds=args.resource_check_interval,
        max_wait_seconds=args.resource_max_wait_seconds,
        overload_action=args.overload_action,
        max_workers_safe=args.max_workers_safe,
    )
    workers = resource_guard.effective_workers(args.workers)
    all_states = load_all_states(db_path)
    pending = _pending_umi_states(all_states, args.force_rerun)
    if args.max_episodes is not None:
        pending = pending[: args.max_episodes]
    print(f"Deferred UMI Phase 6 candidates: {len(pending)}")
    if not pending:
        return 0

    monitor = None
    if not args.disable_live_monitor:
        run_id = args.run_id or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        monitor = RunMonitor(
            db_path=db_path,
            output_root=output_dir,
            run_id=run_id,
            roots=[],
            phases=[6],
            workers=workers,
            refresh_interval_seconds=args.live_report_interval,
        )
        monitor.start(pending)
        print(f"Live run monitor: {monitor.run_dir}")

    processed = 0
    try:
        for batch_index, batch in enumerate(_chunks(pending, max(1, args.batch_size)), start=1):
            _check_stop(stop_file, f"before deferred UMI batch {batch_index}")
            print()
            print(f"=== Deferred UMI batch {batch_index}: episodes={len(batch)} ===")
            resource_guard.wait_if_needed(f"before deferred UMI batch {batch_index}", force=True)
            if monitor is not None:
                monitor.start_phase(6, len(batch), 0, batch)
            started = time.perf_counter()

            def progress(current: int, total: int) -> None:
                _print_progress(current, total, started)
                if monitor is not None:
                    monitor.progress(6, current, total, batch)
                _check_stop(stop_file, f"deferred UMI progress {current}/{total}")
                resource_guard.wait_if_needed(f"deferred UMI progress {current}/{total}")

            phase6_umi_processing.run_phase(batch, db_path, progress_callback=progress, workers=workers)
            _write_final_verdicts(batch, db_path)
            processed += len(batch)
            if monitor is not None:
                monitor.finish_phase(6, batch)
            counts = Counter(state.final_status for state in batch)
            print()
            print(
                f"Deferred UMI batch {batch_index} complete: "
                f"pass={counts['pass']} fail={counts['fail']} "
                f"warning={counts['warning']} needs_review={counts['needs_review']}"
            )
            del batch
            gc.collect()
    except GracefulStopRequested as exc:
        print(str(exc))
        if monitor is not None:
            monitor.finish_run([], status="stopped")
        return 130

    if monitor is not None:
        monitor.finish_run([], status="complete")
    print(f"Deferred UMI Phase 6 complete. Processed {processed} episode(s).")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deferred UMI Phase 6 from an existing QA DB.")
    parser.add_argument("--db-path", required=True, help="Existing QA SQLite database.")
    parser.add_argument("--output-dir", required=True, help="Pipeline output directory for monitor files.")
    parser.add_argument("--workers", type=int, default=1, help="Phase 6 workers. Default: 1.")
    parser.add_argument("--batch-size", type=int, default=50, help="Deferred worker DB batch size. Default: 50.")
    parser.add_argument("--max-episodes", type=int, default=None, help="Limit episodes for smoke tests.")
    parser.add_argument("--force-rerun", action="store_true", help="Run Phase 6 even when already completed.")
    parser.add_argument("--disable-resource-guard", action="store_true")
    parser.add_argument("--max-load-ratio", type=float, default=0.75)
    parser.add_argument("--min-free-mem-gb", type=float, default=6.0)
    parser.add_argument("--resource-check-interval", type=float, default=30.0)
    parser.add_argument("--resource-max-wait-seconds", type=float, default=0.0)
    parser.add_argument("--overload-action", choices=("pause", "stop"), default="pause")
    parser.add_argument("--max-workers-safe", type=int, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--live-report-interval", type=float, default=2.0)
    parser.add_argument("--disable-live-monitor", action="store_true")
    parser.add_argument(
        "--stop-file",
        default=None,
        help="Gracefully stop when this file exists. Default: <output-dir>/STOP_REQUESTED.",
    )
    return parser.parse_args(argv)


def _pending_umi_states(states: list[EpisodeState], force_rerun: bool) -> list[EpisodeState]:
    pending: list[EpisodeState] = []
    for state in states:
        if _has_failed_prior_phase(state, 6):
            continue
        if not force_rerun and 6 in {int(phase) for phase in state.phases_completed}:
            continue
        if force_rerun:
            if phase6_umi_processing.is_umi_state(state):
                state.phases_completed = [phase for phase in state.phases_completed if int(phase) != 6]
                state.phase_status.pop(6, None)
                state.phase_status.pop("6", None)
                pending.append(state)
            continue
        if state.metrics.get("p6_umi_status") == "pending":
            pending.append(state)
    return pending


def _has_failed_prior_phase(state: EpisodeState, current_phase: int) -> bool:
    return any(
        status == "fail"
        for phase_num, status in state.phase_status.items()
        if int(phase_num) < current_phase
    )


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


def _chunks(items: list[EpisodeState], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _stop_file_path(args: argparse.Namespace, output_dir: Path) -> Path:
    return Path(args.stop_file) if args.stop_file else output_dir / "STOP_REQUESTED"


def _check_stop(stop_file: Path, context: str) -> None:
    if stop_file.exists():
        raise GracefulStopRequested(f"Stop requested during {context}: {stop_file}")


def _print_progress(current: int, total: int, started: float, width: int = 40) -> None:
    filled = int(width * current / total) if total > 0 else 0
    bar = "#" * filled + "-" * (width - filled)
    elapsed = max(0.001, time.perf_counter() - started)
    rate = current / elapsed if current else 0.0
    print(f"\r  [{bar}] {current}/{total} {rate:.2f}/s elapsed={elapsed:.0f}s", end="", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
