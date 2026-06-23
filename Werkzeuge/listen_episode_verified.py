"""Listen for verified episode events and enqueue QA pipeline jobs.

The intended server flow is:
event -> local SQLite job -> process one exact episode -> write local QA report.

The listener uses the DCS event bus when running in ``serve`` or ``listen`` mode.
Queue inspection and worker-only modes do not import the DCS SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_QUEUE_NAME = "qa_pipeline.listen_episode_verified"
DEFAULT_ROUTING_KEY = "collector_platform.episode_verified"
DEFAULT_JOB_DB = "outputs/event_listener/jobs.db"
DEFAULT_OUTPUT_DIR = "outputs/event_listener"
DEFAULT_MOUNT_PREFIX = "/mnt/nas/database/verified"
DEFAULT_NAS_PREFIX = "/volume1/database/verified,/database/verified"
DEFAULT_DC_ROOT = str(Path(__file__).resolve().parents[1] / "dcp-sdk")
DEFAULT_QA_PYTHON = "datapipeline-env/bin/python3"

PENDING_STATUSES = ("pending", "retry")


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def init_job_db(job_db: Path) -> None:
    job_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(job_db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                record_id TEXT,
                session_id TEXT,
                verified_path TEXT NOT NULL,
                mounted_path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                run_id TEXT,
                output_dir TEXT,
                db_path TEXT,
                error TEXT,
                received_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_mounted_path ON jobs(mounted_path)"
        )


def add_dc_root(dc_root: str | Path) -> None:
    root = Path(dc_root).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"DCS root does not exist: {root}")
    root_str = str(root.resolve())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def load_event_bus(dc_root: str | Path):
    add_dc_root(dc_root)
    from dcs_sdk.config import get_section
    from event_center.bus.event_bus import EventBus

    rabbitmq = get_section("rabbitmq")
    url = str(rabbitmq.get("url") or "").strip()
    if not url:
        raise RuntimeError(
            "DCS rabbitmq.url is empty. Set DCS_CONFIG_FILE/DCS_ENV or provide a valid DCS config."
        )
    exchange = str(rabbitmq.get("exchange") or "event_center.events")
    return EventBus, url, exchange


def normalize_verified_path(raw_path: str, nas_prefix: str, mount_prefix: str) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError("verified_path is empty")

    mount_root = Path(mount_prefix).expanduser().resolve(strict=False)
    nas_roots = [prefix.strip().rstrip("/") for prefix in nas_prefix.split(",") if prefix.strip()]

    if raw.startswith(str(mount_root)):
        candidate = Path(raw)
    else:
        candidate = None
        for nas_root in nas_roots:
            if raw == nas_root or raw.startswith(nas_root + "/"):
                relative = raw[len(nas_root) :].lstrip("/")
                candidate = mount_root / relative
                break
        if candidate is None:
            if raw.startswith("/"):
                candidate = Path(raw)
            else:
                candidate = mount_root / raw

    resolved = candidate.expanduser().resolve(strict=False)
    if resolved != mount_root and not str(resolved).startswith(str(mount_root) + os.sep):
        raise ValueError(f"verified_path is outside mount prefix: {resolved}")
    return resolved


def paths_from_payload(payload: dict[str, Any]) -> list[str]:
    for key in ("verified_path", "path", "episode_path", "nas_verified_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    value = payload.get("verified_paths")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def episode_paths_for_target(target: Path) -> list[Path]:
    if target.name.startswith("episode_"):
        return [target]
    if not target.exists():
        return [target]
    return sorted(path for path in target.rglob("episode_*") if path.is_dir())


def enqueue_job(
    job_db: Path,
    *,
    event_id: str,
    payload: dict[str, Any],
    verified_path: str,
    mounted_path: Path,
) -> bool:
    now = utc_now()
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    record_id = str(payload.get("record_id") or "")
    session_id = str(payload.get("session_id") or payload.get("recording_session_id") or "")
    with sqlite3.connect(job_db) as conn:
        try:
            conn.execute(
                """
                INSERT INTO jobs (
                    event_id, record_id, session_id, verified_path, mounted_path,
                    payload_json, status, attempts, received_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event_id,
                    record_id,
                    session_id,
                    verified_path,
                    str(mounted_path),
                    payload_json,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute(
                """
                UPDATE jobs
                SET payload_json = ?, record_id = ?, session_id = ?, updated_at = ?
                WHERE event_id = ? OR mounted_path = ?
                """,
                (payload_json, record_id, session_id, now, event_id, str(mounted_path)),
            )
            return False
    return True


def claim_next_job(job_db: Path, max_attempts: int) -> dict[str, Any] | None:
    jobs = claim_next_jobs(job_db, max_attempts, 1)
    return jobs[0] if jobs else None


def claim_next_jobs(job_db: Path, max_attempts: int, limit: int) -> list[dict[str, Any]]:
    now = utc_now()
    limit = max(1, int(limit))
    with sqlite3.connect(job_db) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('pending', 'retry') AND attempts < ?
            ORDER BY id
            LIMIT ?
            """,
            (max_attempts, limit),
        ).fetchall()
        if not rows:
            conn.commit()
            return []
        for row in rows:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, int(row["id"])),
            )
        conn.commit()
        return [dict(row) for row in rows]


def finish_job(
    job_db: Path,
    job_id: int,
    *,
    status: str,
    run_id: str,
    output_dir: Path,
    db_path: Path,
    error: str = "",
) -> None:
    now = utc_now()
    with sqlite3.connect(job_db) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, run_id = ?, output_dir = ?, db_path = ?,
                error = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, run_id, str(output_dir), str(db_path), error, now, now, job_id),
        )


def requeue_job(job_db: Path, job_id: int, error: str) -> None:
    now = utc_now()
    with sqlite3.connect(job_db) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'retry', error = ?, updated_at = ?
            WHERE id = ?
            """,
            (error, now, job_id),
        )


def fail_exhausted_jobs(job_db: Path, max_attempts: int) -> None:
    now = utc_now()
    with sqlite3.connect(job_db) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'failed', error = COALESCE(NULLIF(error, ''), 'max attempts exhausted'),
                finished_at = COALESCE(finished_at, ?), updated_at = ?
            WHERE status IN ('pending', 'retry') AND attempts >= ?
            """,
            (now, now, max_attempts),
        )


def recover_running_jobs(job_db: Path) -> int:
    now = utc_now()
    with sqlite3.connect(job_db) as conn:
        cursor = conn.execute(
            """
            UPDATE jobs
            SET status = 'retry',
                error = 'Recovered from stale running state after listener restart.',
                updated_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )
        return int(cursor.rowcount or 0)


def path_snapshot(path: Path) -> tuple[int, int, float]:
    file_count = 0
    total_bytes = 0
    latest_mtime = 0.0
    for item in path.rglob("*"):
        try:
            stat = item.stat()
        except OSError:
            continue
        if item.is_file():
            file_count += 1
            total_bytes += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
    return file_count, total_bytes, latest_mtime


def wait_until_stable(path: Path, interval: float, timeout: float) -> None:
    if not path.exists():
        raise FileNotFoundError(f"episode path does not exist: {path}")
    if not (path / "metadata.json").is_file():
        raise FileNotFoundError(f"metadata.json not found under: {path}")
    deadline = time.monotonic() + timeout
    previous = path_snapshot(path)
    while time.monotonic() < deadline:
        time.sleep(interval)
        current = path_snapshot(path)
        if current == previous:
            return
        previous = current
    raise TimeoutError(f"path did not become stable within {timeout:g}s: {path}")


def wait_until_all_stable(paths: list[Path], interval: float, timeout: float) -> None:
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"episode path does not exist: {path}")
        if not (path / "metadata.json").is_file():
            raise FileNotFoundError(f"metadata.json not found under: {path}")
    deadline = time.monotonic() + timeout
    previous = {path: path_snapshot(path) for path in paths}
    while time.monotonic() < deadline:
        time.sleep(interval)
        unstable = []
        current = {}
        for path in paths:
            snapshot = path_snapshot(path)
            current[path] = snapshot
            if snapshot != previous[path]:
                unstable.append(path)
        if not unstable:
            return
        previous = current
    raise TimeoutError(f"{len(paths)} episode path(s) did not become stable within {timeout:g}s")


def run_pipeline_for_job(args: argparse.Namespace, job: dict[str, Any]) -> tuple[bool, str]:
    ok, detail, _count = run_pipeline_for_jobs(args, [job])
    return ok, detail


def _resolve_job_episode(job: dict[str, Any]) -> Path:
    mounted = Path(str(job["mounted_path"]))
    episodes = episode_paths_for_target(mounted)
    if not episodes:
        raise FileNotFoundError(f"no episode directories found under {mounted}")
    if len(episodes) > 1:
        raise ValueError(f"event target resolved to {len(episodes)} episodes; expected one: {mounted}")
    return episodes[0]


def run_pipeline_for_jobs(
    args: argparse.Namespace,
    jobs: list[dict[str, Any]],
) -> tuple[bool, str, int]:
    valid_jobs: list[dict[str, Any]] = []
    episodes: list[Path] = []
    for job in jobs:
        try:
            episode = _resolve_job_episode(job)
        except Exception as exc:
            detail = str(exc)
            if int(job["attempts"]) + 1 >= args.max_attempts:
                finish_job(
                    Path(args.job_db),
                    int(job["id"]),
                    status="failed",
                    run_id="",
                    output_dir=Path(args.output_dir),
                    db_path=Path(args.job_db),
                    error=detail,
                )
            else:
                requeue_job(Path(args.job_db), int(job["id"]), detail)
            print(f"job={job['id']} failed before pipeline: {detail}", flush=True)
            continue
        valid_jobs.append(job)
        episodes.append(episode)

    if not valid_jobs:
        return False, "no valid jobs in claimed batch", 0
    try:
        wait_until_all_stable(episodes, args.stability_interval, args.stability_timeout)
    except Exception as exc:
        detail = str(exc)
        for job in valid_jobs:
            if int(job["attempts"]) + 1 >= args.max_attempts:
                finish_job(
                    Path(args.job_db),
                    int(job["id"]),
                    status="failed",
                    run_id="",
                    output_dir=Path(args.output_dir),
                    db_path=Path(args.job_db),
                    error=detail,
                )
            else:
                requeue_job(Path(args.job_db), int(job["id"]), detail)
        return False, detail, 0

    first_id = int(valid_jobs[0]["id"])
    last_id = int(valid_jobs[-1]["id"])
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if len(valid_jobs) == 1:
        run_id = f"{args.run_id_prefix}-{timestamp}-job{first_id}"
        job_output = Path(args.output_dir) / "jobs" / f"job_{first_id:08d}"
    else:
        run_id = f"{args.run_id_prefix}-{timestamp}-jobs{first_id}-{last_id}"
        job_output = Path(args.output_dir) / "batches" / f"batch_{timestamp}_jobs_{first_id:08d}_{last_id:08d}"
    job_output.mkdir(parents=True, exist_ok=True)
    episode_list = job_output / "episodes.txt"
    episode_list.write_text("".join(f"{episode}\n" for episode in episodes), encoding="utf-8")
    qa_db = job_output / "qa.db"
    console_log = job_output / "pipeline.log"

    command = [
        args.qa_python,
        "QA_Pipeline/scripts/run_pipeline.py",
        "--roots",
        args.mount_prefix,
        "--episode-list",
        str(episode_list),
        "--phases",
        args.phases,
        "--db-path",
        str(qa_db),
        "--output-dir",
        str(job_output),
        "--run-id",
        run_id,
        "--workers",
        str(args.workers),
        "--max-load-ratio",
        str(args.max_load_ratio),
        "--min-free-mem-gb",
        str(args.min_free_mem_gb),
        "--overload-action",
        args.overload_action,
        "--resource-error-retries",
        str(args.resource_error_retries),
        "--resource-retry-delay-seconds",
        str(args.resource_retry_delay_seconds),
        "--resource-max-wait-seconds",
        str(args.resource_max_wait_seconds),
        "--disable-quality-label-filter",
    ]
    if args.disable_live_monitor:
        command.append("--disable-live-monitor")
    if args.force_rerun:
        command.append("--force-rerun")

    command_path = job_output / "pipeline_command.json"
    command_path.write_text(json.dumps(command, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with console_log.open("w", encoding="utf-8") as log:
        log.write(" ".join(command) + "\n\n")
        log.flush()
        completed = subprocess.run(command, cwd=args.repo_root, stdout=log, stderr=subprocess.STDOUT)

    finish_job(
        Path(args.job_db),
        int(valid_jobs[0]["id"]),
        status="done" if completed.returncode == 0 else "failed",
        run_id=run_id,
        output_dir=job_output,
        db_path=qa_db,
        error="" if completed.returncode == 0 else f"pipeline exit code {completed.returncode}",
    )
    for job in valid_jobs[1:]:
        finish_job(
            Path(args.job_db),
            int(job["id"]),
            status="done" if completed.returncode == 0 else "failed",
            run_id=run_id,
            output_dir=job_output,
            db_path=qa_db,
            error="" if completed.returncode == 0 else f"pipeline exit code {completed.returncode}",
        )
    return completed.returncode == 0, str(console_log), len(valid_jobs)


async def listen_events(args: argparse.Namespace) -> None:
    EventBus, url, exchange = load_event_bus(args.dc_root)
    bus = EventBus(url=url, exchange_name=exchange)
    await bus.connect()

    async def on_event(event: Any) -> None:
        payload = dict(getattr(event, "payload", {}) or {})
        event_id = str(getattr(event, "event_id", "") or "")
        if not event_id:
            event_id = "event-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        raw_paths = paths_from_payload(payload)
        if not raw_paths:
            print(f"ignored event without verified_path: event_id={event_id}", flush=True)
            return
        for raw_path in raw_paths:
            try:
                mounted = normalize_verified_path(raw_path, args.nas_prefix, args.mount_prefix)
            except ValueError as exc:
                print(f"ignored event path: event_id={event_id} error={exc}", flush=True)
                continue
            inserted = enqueue_job(
                Path(args.job_db),
                event_id=event_id if len(raw_paths) == 1 else f"{event_id}:{raw_path}",
                payload=payload,
                verified_path=raw_path,
                mounted_path=mounted,
            )
            action = "enqueued" if inserted else "updated"
            print(f"{action} event_id={event_id} mounted_path={mounted}", flush=True)

    print(f"queue={args.queue_name}", flush=True)
    print(f"routing_key={args.routing_key}", flush=True)
    print(f"job_db={args.job_db}", flush=True)
    await bus.subscribe(
        handler=on_event,
        queue_name=args.queue_name,
        routing_keys=[args.routing_key],
        max_retries=args.event_max_retries,
        retry_delay_ms=args.event_retry_delay_ms,
    )
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await bus.close()


async def worker_loop(args: argparse.Namespace) -> None:
    if args.recover_running:
        recovered = recover_running_jobs(Path(args.job_db))
        if recovered:
            print(f"recovered stale running jobs: {recovered}", flush=True)
    while True:
        fail_exhausted_jobs(Path(args.job_db), args.max_attempts)
        jobs = claim_next_jobs(Path(args.job_db), args.max_attempts, args.batch_size)
        if not jobs:
            if args.once:
                return
            await asyncio.sleep(args.poll_interval)
            continue
        first_job = jobs[0]
        last_job = jobs[-1]
        print(
            f"claimed jobs={len(jobs)} id_range={first_job['id']}-{last_job['id']} "
            f"first_path={first_job['mounted_path']}",
            flush=True,
        )
        try:
            ok, detail, processed = await asyncio.to_thread(run_pipeline_for_jobs, args, jobs)
        except Exception as exc:
            detail = str(exc)
            for job in jobs:
                if int(job["attempts"]) + 1 >= args.max_attempts:
                    finish_job(
                        Path(args.job_db),
                        int(job["id"]),
                        status="failed",
                        run_id="",
                        output_dir=Path(args.output_dir),
                        db_path=Path(args.job_db),
                        error=detail,
                    )
                else:
                    requeue_job(Path(args.job_db), int(job["id"]), detail)
            print(f"jobs={first_job['id']}-{last_job['id']} failed before pipeline: {detail}", flush=True)
            continue
        print(
            f"jobs={first_job['id']}-{last_job['id']} processed={processed} "
            f"status={'done' if ok else 'failed'} log={detail}",
            flush=True,
        )
        if args.once:
            return


def print_status(job_db: Path, limit: int) -> None:
    init_job_db(job_db)
    with sqlite3.connect(job_db) as conn:
        conn.row_factory = sqlite3.Row
        counts = conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
        ).fetchall()
        print("Job counts:")
        if not counts:
            print("  (none)")
        for row in counts:
            print(f"  {row['status']}: {row['count']}")
        rows = conn.execute(
            """
            SELECT id, status, attempts, mounted_path, run_id, error, updated_at
            FROM jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        print("\nRecent jobs:")
        for row in rows:
            error = str(row["error"] or "")
            if len(error) > 120:
                error = error[:117] + "..."
            print(
                f"  id={row['id']} status={row['status']} attempts={row['attempts']} "
                f"updated={row['updated_at']} path={row['mounted_path']}"
            )
            if row["run_id"]:
                print(f"    run_id={row['run_id']}")
            if error:
                print(f"    error={error}")


def recover_command(args: argparse.Namespace) -> int:
    init_job_db(Path(args.job_db))
    recovered = recover_running_jobs(Path(args.job_db))
    print(f"recovered stale running jobs: {recovered}")
    return 0


def enqueue_manual(args: argparse.Namespace) -> int:
    init_job_db(Path(args.job_db))
    mounted = normalize_verified_path(args.verified_path, args.nas_prefix, args.mount_prefix)
    payload = {
        "verified_path": args.verified_path,
        "record_id": args.record_id or "",
        "session_id": args.session_id or "",
        "manual": True,
    }
    event_id = args.event_id or "manual-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    inserted = enqueue_job(
        Path(args.job_db),
        event_id=event_id,
        payload=payload,
        verified_path=args.verified_path,
        mounted_path=mounted,
    )
    print(f"{'enqueued' if inserted else 'updated'} event_id={event_id} mounted_path={mounted}")
    return 0


async def serve(args: argparse.Namespace) -> None:
    worker = asyncio.create_task(worker_loop(args))
    listener = asyncio.create_task(listen_events(args))
    done, pending = await asyncio.wait({worker, listener}, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-db", default=DEFAULT_JOB_DB)
    parser.add_argument("--mount-prefix", default=DEFAULT_MOUNT_PREFIX)
    parser.add_argument("--nas-prefix", default=DEFAULT_NAS_PREFIX)


def add_worker_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--qa-python", default=DEFAULT_QA_PYTHON)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--phases", default="1,2,3")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Number of queued episode jobs to process in one pipeline run. Default: 1.",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--once", action="store_true", help="Process at most one job, then exit.")
    parser.add_argument("--stability-interval", type=float, default=5.0)
    parser.add_argument("--stability-timeout", type=float, default=600.0)
    parser.add_argument("--max-load-ratio", type=float, default=0.75)
    parser.add_argument("--min-free-mem-gb", type=float, default=6.0)
    parser.add_argument("--overload-action", choices=("pause", "stop"), default="stop")
    parser.add_argument("--resource-error-retries", type=int, default=2)
    parser.add_argument("--resource-retry-delay-seconds", type=float, default=60.0)
    parser.add_argument("--resource-max-wait-seconds", type=float, default=300.0)
    parser.add_argument("--run-id-prefix", default="event-verified")
    parser.add_argument("--disable-live-monitor", action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument(
        "--recover-running",
        action="store_true",
        help="Requeue jobs left in running state from a previous interrupted listener.",
    )


def add_listener_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dc-root", default=DEFAULT_DC_ROOT)
    parser.add_argument("--queue-name", default=DEFAULT_QUEUE_NAME)
    parser.add_argument("--routing-key", default=DEFAULT_ROUTING_KEY)
    parser.add_argument("--event-max-retries", type=int, default=10)
    parser.add_argument("--event-retry-delay-ms", type=int, default=30000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DCS verified-event listener for QA pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Listen for events and process queued jobs.")
    add_common_args(serve_parser)
    add_listener_args(serve_parser)
    add_worker_args(serve_parser)

    listen_parser = subparsers.add_parser("listen", help="Only listen and enqueue jobs.")
    add_common_args(listen_parser)
    add_listener_args(listen_parser)

    worker_parser = subparsers.add_parser("worker", help="Only process queued jobs.")
    add_common_args(worker_parser)
    add_worker_args(worker_parser)

    status_parser = subparsers.add_parser("status", help="Print queue status.")
    add_common_args(status_parser)
    status_parser.add_argument("--limit", type=int, default=20)

    enqueue_parser = subparsers.add_parser("enqueue", help="Manually enqueue one verified path.")
    add_common_args(enqueue_parser)
    enqueue_parser.add_argument("--verified-path", required=True)
    enqueue_parser.add_argument("--event-id", default="")
    enqueue_parser.add_argument("--record-id", default="")
    enqueue_parser.add_argument("--session-id", default="")

    recover_parser = subparsers.add_parser("recover-running", help="Requeue stale running jobs.")
    add_common_args(recover_parser)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    init_job_db(Path(args.job_db))
    if args.command == "status":
        print_status(Path(args.job_db), args.limit)
        return 0
    if args.command == "enqueue":
        return enqueue_manual(args)
    if args.command == "recover-running":
        return recover_command(args)
    if args.command == "worker":
        asyncio.run(worker_loop(args))
        return 0
    if args.command == "listen":
        asyncio.run(listen_events(args))
        return 0
    if args.command == "serve":
        asyncio.run(serve(args))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
