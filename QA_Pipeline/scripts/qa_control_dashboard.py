"""Central QA pipeline control dashboard.

This service is intentionally small and self-contained:
- serves a browser UI on one port
- tracks dashboard-started runs in a local SQLite registry
- starts/stops approved pipeline modes through tmux
- reports server load, event listener queue state, and per-run summaries
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR_FOR_IMPORT = Path(__file__).resolve().parent
if str(SCRIPT_DIR_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR_FOR_IMPORT))

from generate_work_session_report import (
    DEFAULT_CONFIG as WORK_SESSION_REPORT_CONFIG,
    SEVERITY_ORDER,
    STATUS_ORDER,
    build_cumulative_report as build_cumulative_work_session_report,
    build_report as build_work_session_report,
    core_issue_rows,
    action_rows,
    affected_episode_rows,
    operator_issue_episode_rows,
    operator_issue_rows,
    load_config as load_work_session_report_config,
    ordered_counter,
    query_all_rows,
    resolve_window as resolve_work_session_window,
    write_report as write_work_session_report,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT.parent
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANAGER_DIR = PROJECT_ROOT / "outputs" / "dashboard_manager"
DEFAULT_REGISTRY_DB = DEFAULT_MANAGER_DIR / "runs.db"
DEFAULT_ISSUE_HISTORY_DB = DEFAULT_MANAGER_DIR / "issue_history.db"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "dashboard_runs"
DEFAULT_VERIFIED_ROOT = Path("/mnt/nas/database/verified")
EVENT_CONTROL_SCRIPT = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "event_listener_control.sh"
EVENT_JOB_DB = PROJECT_ROOT / "outputs" / "event_listener" / "jobs.db"
RETENTION_MANIFEST = "retention_cleanup.jsonl"
ISSUE_TRANSLATIONS_PATH = SCRIPT_DIR / "issue_translations.json"
_ISSUE_TRANSLATIONS_CACHE: dict[str, str] | None = None
CONSECUTIVE_FAILURE_STREAK_LENGTH = 5
BAD_QA_FINAL_STATUSES = {"fail", "needs_review"}


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class DashboardState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.port = args.port
        self.registry_db = Path(args.registry_db)
        self.issue_history_db = DEFAULT_ISSUE_HISTORY_DB
        self.output_root = Path(args.output_root)
        self.verified_root = Path(args.verified_root)
        self.qa_python = Path(args.qa_python)
        self.refresh_seconds = max(1.0, float(args.refresh_seconds))
        self.max_discovered_runs = max(0, int(args.max_discovered_runs))
        self.auto_work_session_reports = not args.disable_auto_work_session_reports
        self.work_session_report_interval_seconds = max(60, int(args.work_session_report_interval_seconds))
        self.work_session_report_last_run = 0.0
        self.lock = threading.Lock()
        self.cache_lock = threading.RLock()
        self.cache: dict[str, tuple[float, Any]] = {}
        self.failure_warning_signature: tuple[int, int, str, str] | None = None
        self.failure_warning_cache: dict[str, Any] = {"count": 0, "warnings": [], "checked_jobs": 0}
        init_registry(self.registry_db)
        init_issue_history(self.issue_history_db)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = DashboardState(args)
    start_work_session_report_scheduler(state)
    server = DashboardHTTPServer((args.host, args.port), make_handler(state))
    print(f"QA control dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping QA control dashboard.", flush=True)
    finally:
        server.server_close()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the central QA control dashboard.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4131)
    parser.add_argument("--registry-db", default=str(DEFAULT_REGISTRY_DB))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--verified-root", default=str(DEFAULT_VERIFIED_ROOT))
    parser.add_argument("--qa-python", default="datapipeline-env/bin/python")
    parser.add_argument("--refresh-seconds", type=float, default=5.0)
    parser.add_argument("--max-discovered-runs", type=int, default=200)
    parser.add_argument(
        "--disable-auto-work-session-reports",
        action="store_true",
        help="Disable automatic event-listener Chinese work-session report generation.",
    )
    parser.add_argument(
        "--work-session-report-interval-seconds",
        type=int,
        default=600,
        help="Automatic event-listener work-session report refresh interval. Default: 600.",
    )
    return parser.parse_args(argv)


def make_handler(state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QAControlDashboard/1.0"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path in ("/", "/index.html"):
                    self._send_html(render_index_html(state))
                elif path == "/event-listener/work-session-report.html":
                    self._send_html(render_event_work_session_report_html())
                elif path == "/api/status":
                    self._send_json(api_status(state))
                elif path == "/api/runs":
                    self._send_json({"runs": list_runs(state)})
                elif path.startswith("/api/runs/"):
                    run_id = path.rsplit("/", 1)[-1]
                    self._send_json(api_run_detail(state, run_id))
                elif path == "/api/event-listener/status":
                    self._send_json(api_event_listener_status())
                elif path == "/api/event-listener/issue-summary":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["500"])[0] or "500")
                    self._send_json({"summary": event_issue_summary(limit=limit)})
                elif path == "/api/event-listener/work-session-report":
                    self._send_json({"report": latest_event_work_session_report()})
                elif path == "/api/event-listener/jobs":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["100"])[0] or "100")
                    issues_only = query.get("issues_only", ["0"])[0] in {"1", "true", "yes"}
                    self._send_json({"jobs": event_listener_jobs(limit=limit, issues_only=issues_only)})
                elif path.startswith("/api/event-listener/jobs/"):
                    job_id = int(path.rsplit("/", 1)[-1])
                    self._send_json({"job": event_listener_job_detail(job_id, state.verified_root)})
                elif path == "/api/server-load":
                    self._send_json(server_load())
                elif path == "/api/log-tail":
                    query = parse_qs(parsed.query)
                    target = query.get("target", ["event_listener"])[0]
                    self._send_json({"lines": log_tail(state, target)})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                payload = self._read_json()
                if path == "/api/start":
                    clear_cache(state)
                    self._send_json(api_start_run(state, payload))
                elif path.startswith("/api/runs/") and path.endswith("/work-session-report"):
                    run_id = path.split("/")[-2]
                    clear_cache(state)
                    self._send_json(api_generate_work_session_report(state, run_id, payload))
                elif path.startswith("/api/stop/"):
                    run_id = path.rsplit("/", 1)[-1]
                    clear_cache(state)
                    self._send_json(api_stop_run(state, run_id))
                elif path == "/api/event-listener/start":
                    clear_cache(state)
                    self._send_json(api_event_listener_action("start", payload))
                elif path == "/api/event-listener/stop":
                    clear_cache(state)
                    self._send_json(api_event_listener_action("stop", payload))
                elif path == "/api/event-listener/restart":
                    clear_cache(state)
                    self._send_json(api_event_listener_action("restart", payload))
                elif path == "/api/event-listener/work-session-report":
                    clear_cache(state)
                    self._send_json(api_generate_event_work_session_report(payload))
                elif path == "/api/consecutive-failures/resolve":
                    self._send_json(api_resolve_consecutive_failure(state, payload))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def init_registry(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                db_path TEXT NOT NULL,
                command TEXT NOT NULL,
                tmux_session TEXT,
                status TEXT NOT NULL,
                phases TEXT,
                workers INTEGER,
                batch_size INTEGER,
                root_path TEXT,
                date_from TEXT,
                date_to TEXT,
                task_filter TEXT,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                recorded_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )


def init_issue_history(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consecutive_failure_streaks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                robot TEXT NOT NULL,
                operator TEXT NOT NULL,
                episode_start INTEGER NOT NULL,
                episode_end INTEGER NOT NULL,
                streak_length INTEGER NOT NULL,
                issue_types TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_time_seconds INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_consecutive_failure_unresolved_identity
            ON consecutive_failure_streaks (
                task, robot, operator, episode_start, episode_end
            )
            WHERE resolved_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_consecutive_failure_unresolved_detected
            ON consecutive_failure_streaks (resolved_at, detected_at DESC, id DESC)
            """
        )


def cached_value(state: DashboardState, key: str, ttl_seconds: float, producer) -> Any:
    now = time.monotonic()
    with state.cache_lock:
        cached = state.cache.get(key)
        if cached and now - cached[0] < ttl_seconds:
            return cached[1]
        value = producer()
        state.cache[key] = (now, value)
        return value


def clear_cache(state: DashboardState) -> None:
    with state.cache_lock:
        state.cache.clear()



def api_status(state: DashboardState) -> dict[str, Any]:
    def produce() -> dict[str, Any]:
        sync_registered_run_statuses(state)
        return {
            "generated_at": now_iso(),
            "server": server_load(),
            "event_listener": event_listener_status(),
            "consecutive_failures": consecutive_failure_warnings(state),
            "runs": list_runs(state, limit=30),
        }

    return cached_value(state, "api_status", max(3.0, state.refresh_seconds), produce)


def list_runs(state: DashboardState, limit: int | None = None) -> list[dict[str, Any]]:
    registered = registered_runs(state.registry_db)
    discovered = discover_output_runs(state)
    by_key: dict[str, dict[str, Any]] = {}
    for run in discovered:
        by_key[run["run_id"]] = run
    for run in registered:
        run.update(run_summary(Path(run["db_path"]), Path(run["output_dir"])))
        run["tmux_running"] = tmux_session_running(run.get("tmux_session") or "")
        by_key[run["run_id"]] = run
    runs = list(by_key.values())
    runs.sort(key=lambda item: item.get("updated_at") or item.get("mtime") or "", reverse=True)
    return runs[:limit] if limit else runs


def registered_runs(registry_db: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(registry_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT run_id, mode, output_dir, db_path, tmux_session, status,
                   phases, workers, batch_size, root_path, date_from, date_to,
                   task_filter, started_at, finished_at, updated_at, notes
            FROM runs
            ORDER BY updated_at DESC
            LIMIT 500
            """
        ).fetchall()
    return [dict(row) for row in rows]


def discover_output_runs(state: DashboardState) -> list[dict[str, Any]]:
    if state.max_discovered_runs <= 0:
        return []
    candidates = []
    for outputs in (PROJECT_ROOT / "outputs", REPO_ROOT / "outputs"):
        if not outputs.is_dir():
            continue
        for db_path in likely_dashboard_databases(outputs):
            output_dir = db_path.parent
            stat = safe_stat(db_path)
            candidates.append((stat[0], db_path, output_dir))
    candidates.sort(reverse=True)
    runs = []
    seen_db_paths: set[str] = set()
    for _mtime, db_path, output_dir in candidates:
        db_key = str(db_path.resolve(strict=False))
        if db_key in seen_db_paths:
            continue
        seen_db_paths.add(db_key)
        if len(runs) >= state.max_discovered_runs:
            break
        run_id = discovered_run_id(output_dir)
        if db_path.name != "qa.db":
            run_id = sanitize_id(f"{run_id}-{db_path.stem}")
        item = {
            "run_id": run_id,
            "mode": "discovered",
            "output_dir": str(output_dir),
            "db_path": str(db_path),
            "tmux_session": "",
            "status": "complete",
            "updated_at": mtime_iso(output_dir),
            "mtime": mtime_iso(output_dir),
            "registered": False,
        }
        item.update(run_summary(db_path, output_dir))
        runs.append(item)
    return runs


def likely_dashboard_databases(outputs: Path) -> list[Path]:
    """Find common QA DB locations without recursively scanning huge output trees."""
    candidates: list[Path] = []
    patterns = (
        "qa.db",
        "qa_pipeline.db",
        "qa_pipeline *.db",
        "*/qa.db",
        "*/*/qa.db",
        "*/qa_pipeline.db",
        "*/qa_pipeline *.db",
    )
    for pattern in patterns:
        candidates.extend(path for path in outputs.glob(pattern) if path.is_file())
    return candidates


def api_run_detail(state: DashboardState, run_id: str) -> dict[str, Any]:
    sync_registered_run_statuses(state)
    run = find_run(state, run_id)
    if not run:
        raise ValueError(f"unknown run_id: {run_id}")
    db_path = Path(run["db_path"])
    output_dir = Path(run["output_dir"])
    detail = dict(run)
    detail.update(run_summary(db_path, output_dir, include_top_issues=True))
    detail["run_status"] = latest_run_status(output_dir)
    detail["recent_findings"] = recent_findings(db_path)
    detail["issue_episodes"] = issue_episodes(db_path)
    detail["latest_work_session_report"] = latest_work_session_report(output_dir)
    detail["logs"] = tail_file(output_dir / f"{run_id}_pipeline.log", 80)
    if not detail["logs"]:
        detail["logs"] = tail_file(output_dir / "pipeline.log", 80)
    return {"run": detail}


def find_run(state: DashboardState, run_id: str) -> dict[str, Any] | None:
    for run in list_runs(state):
        if run.get("run_id") == run_id:
            return run
    return None


def api_start_run(state: DashboardState, payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "").strip()
    if mode not in {"task_folder", "date_range"}:
        raise ValueError("mode must be task_folder or date_range")
    phases = validate_phases(str(payload.get("phases") or "1,2,3"))
    workers = clamp_int(payload.get("workers", 2), 1, 8)
    batch_size = clamp_int(payload.get("batch_size", 100), 1, 10000)
    max_load_ratio = clamp_float(payload.get("max_load_ratio", 0.75), 0.1, 2.0)
    min_free_mem_gb = clamp_float(payload.get("min_free_mem_gb", 6.0), 0.0, 128.0)
    resource_wait = clamp_int(payload.get("resource_max_wait_seconds", 300), 0, 86400)
    run_id = sanitize_id(str(payload.get("run_id") or make_run_id(mode)))
    output_dir = state.output_root / run_id
    db_path = output_dir / "qa.db"
    session = sanitize_id(f"qa_dash_{run_id}")[:80]
    root_path = ""
    date_from = ""
    date_to = ""
    task_filter = str(payload.get("task_filter") or "").strip()
    quality_label = str(payload.get("quality_label") or "完全正常").strip() or "完全正常"
    disable_quality_label_filter = bool(payload.get("disable_quality_label_filter", False))

    command = [
        str(state.qa_python),
        "QA_Pipeline/scripts/run_pipeline.py",
        "--phases",
        phases,
        "--db-path",
        str(db_path),
        "--output-dir",
        str(output_dir),
        "--run-id",
        run_id,
        "--workers",
        str(workers),
        "--batch-size",
        str(batch_size),
        "--batch-mode",
        "auto",
        "--max-load-ratio",
        str(max_load_ratio),
        "--min-free-mem-gb",
        str(min_free_mem_gb),
        "--resource-max-wait-seconds",
        str(resource_wait),
        "--overload-action",
        "stop",
    ]
    if disable_quality_label_filter:
        command.append("--disable-quality-label-filter")
    else:
        command.extend(["--quality-label", quality_label])
    if mode == "task_folder":
        root_path = normalize_verified_path(str(payload.get("root_path") or ""), state.verified_root)
        command[2:2] = ["--roots", root_path]
    else:
        root_path = str(state.verified_root)
        date_from = validate_date(str(payload.get("date_from") or ""), "date_from")
        date_to = validate_date(str(payload.get("date_to") or ""), "date_to")
        if date_from > date_to:
            raise ValueError("date_from must be earlier than or equal to date_to")
        command[2:2] = ["--roots", root_path]
        command.extend(["--date-from", date_from, "--date-to", date_to, "--streaming-discovery"])
    if task_filter:
        command.extend(["--task", task_filter])
    if bool(payload.get("force_rerun", False)):
        command.append("--force-rerun")

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{run_id}_pipeline.log"
    command_path = output_dir / "dashboard_command.sh"
    write_run_script(command_path, command, log_path)
    start_tmux(session, f"bash {shlex.quote(str(command_path))}")
    record_run(
        state.registry_db,
        {
            "run_id": run_id,
            "mode": mode,
            "output_dir": str(output_dir),
            "db_path": str(db_path),
            "command": " ".join(shlex.quote(part) for part in command),
            "tmux_session": session,
            "status": "running",
            "phases": phases,
            "workers": workers,
            "batch_size": batch_size,
            "root_path": root_path,
            "date_from": date_from,
            "date_to": date_to,
            "task_filter": task_filter,
            "started_at": now_iso(),
            "finished_at": "",
            "updated_at": now_iso(),
            "notes": "",
        },
    )
    append_run_event(state.registry_db, run_id, "start", f"Started tmux session {session}")
    return {"ok": True, "run_id": run_id, "tmux_session": session, "output_dir": str(output_dir)}


def api_stop_run(state: DashboardState, run_id: str) -> dict[str, Any]:
    run = find_run(state, run_id)
    if not run:
        raise ValueError(f"unknown run_id: {run_id}")
    session = run.get("tmux_session") or ""
    if session and tmux_session_running(session):
        subprocess.run(["tmux", "kill-session", "-t", session], cwd=PROJECT_ROOT, check=False)
    update_run_status(state.registry_db, run_id, "stopped", finished=True)
    append_run_event(state.registry_db, run_id, "stop", "Stopped by dashboard")
    return {"ok": True, "run_id": run_id, "status": "stopped"}


def api_generate_work_session_report(
    state: DashboardState,
    run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    run = find_run(state, run_id)
    if not run:
        raise ValueError(f"unknown run_id: {run_id}")
    db_path = Path(str(run.get("db_path") or ""))
    output_dir = Path(str(run.get("output_dir") or ""))
    if not db_path.is_file():
        raise ValueError(f"QA database does not exist for run {run_id}: {db_path}")

    config = load_work_session_report_config(WORK_SESSION_REPORT_CONFIG)
    session = str(payload.get("session") or "run_all")
    if session == "run_all":
        report = build_cumulative_work_session_report(
            db_path,
            config,
            label="当前运行累计",
            start=parse_event_listener_timestamp(str(run.get("started_at") or "")),
            end=parse_event_listener_timestamp(str(run.get("finished_at") or "")),
        )
    else:
        args = argparse.Namespace(
            session=session,
            start=str(payload.get("start") or "") or None,
            end=str(payload.get("end") or "") or None,
        )
        window = resolve_work_session_window(args, config)
        report = build_work_session_report(
            db_path,
            window,
            config,
            include_all_when_empty=bool(payload.get("include_all_when_empty", True)),
        )
    report_dir = write_work_session_report(output_dir / "reports" / "work_sessions", report, config)
    append_run_event(
        state.registry_db,
        run_id,
        "work_session_report",
        f"Generated Chinese work-session report: {report_dir}",
    )
    latest = work_session_report_payload(report_dir)
    return {"ok": True, "run_id": run_id, "report": latest}


def api_generate_event_work_session_report(payload: dict[str, Any]) -> dict[str, Any]:
    session = str(payload.get("session") or "current")
    config = load_work_session_report_config(WORK_SESSION_REPORT_CONFIG)
    args = argparse.Namespace(
        session=session,
        start=str(payload.get("start") or "") or None,
        end=str(payload.get("end") or "") or None,
    )
    window = resolve_work_session_window(args, config)
    report = build_event_work_session_report(window, config)
    report_dir = write_work_session_report(event_work_session_report_root(), report, config)
    cleanup_event_work_session_reports()
    return {"ok": True, "report": work_session_report_payload(report_dir)}


def build_event_work_session_report(window: Any, config: dict[str, Any]) -> dict[str, Any]:
    jobs = event_listener_report_jobs(window)
    episode_rows: list[dict[str, Any]] = []
    finding_rows: list[dict[str, Any]] = []
    seen_db_paths: set[str] = set()
    source_dbs: list[str] = []
    for job in jobs:
        db_path = resolve_project_path(str(job.get("db_path") or ""))
        if not db_path.is_file():
            continue
        db_key = str(db_path.resolve(strict=False))
        if db_key in seen_db_paths:
            continue
        seen_db_paths.add(db_key)
        source_dbs.append(str(db_path))
        try:
            db_episodes, db_findings = query_all_rows(db_path)
        except sqlite3.Error:
            continue
        for episode in db_episodes:
            episode["source_db"] = str(db_path)
        for finding in db_findings:
            finding["source_db"] = str(db_path)
        episode_rows.extend(db_episodes)
        finding_rows.extend(db_findings)

    findings_by_episode: dict[str, list[dict[str, Any]]] = {}
    for finding in finding_rows:
        findings_by_episode.setdefault(finding["episode_path"], []).append(finding)
    core_issues = core_issue_rows(
        finding_rows,
        config,
        total_episodes=len(episode_rows),
        total_issue_episodes=len(findings_by_episode),
        total_findings=len(finding_rows),
    )
    affected_episodes = affected_episode_rows(episode_rows, findings_by_episode, config)
    operator_issues = operator_issue_rows(
        episode_rows,
        findings_by_episode,
        total_issue_episodes=len(findings_by_episode),
        total_findings=len(finding_rows),
        config=config,
    )
    operator_issue_episodes = operator_issue_episode_rows(episode_rows, findings_by_episode, config)
    actions = action_rows(core_issues, config)
    status_counts = ordered_counter(Counter(row.get("final_status") or "pending" for row in episode_rows), STATUS_ORDER)
    severity_counts = ordered_counter(Counter(row.get("severity") or "unknown" for row in finding_rows), SEVERITY_ORDER)
    blocking_episodes = {row["episode_path"] for row in affected_episodes if row.get("blocks_training")}
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_db": str(EVENT_JOB_DB),
        "window": {
            "key": window.key,
            "label": f"事件监听{window.label}",
            "start": window.start.isoformat(timespec="seconds"),
            "end": window.end.isoformat(timespec="seconds"),
            "used_all_episode_fallback": False,
        },
        "summary": {
            "episode_count": len(episode_rows),
            "finding_count": len(finding_rows),
            "issue_episode_count": len(findings_by_episode),
            "training_blocking_episode_count": len(blocking_episodes),
            "event_job_count": len(jobs),
            "source_db_count": len(source_dbs),
            "status_counts": status_counts,
            "severity_counts": severity_counts,
        },
        "core_issues": core_issues,
        "operator_issues": operator_issues,
        "operator_issue_episodes": operator_issue_episodes,
        "affected_episodes": affected_episodes,
        "suggested_actions": actions,
        "metadata": {
            "source_dbs": source_dbs,
            "event_jobs": jobs,
        },
    }


def event_listener_report_jobs(window: Any, limit: int = 20000) -> list[dict[str, Any]]:
    if not EVENT_JOB_DB.exists():
        return []
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(EVENT_JOB_DB) as conn:
        conn.row_factory = sqlite3.Row
        candidates = conn.execute(
            """
            SELECT id, status, run_id, output_dir, db_path, updated_at, finished_at
            FROM jobs
            WHERE status = ?
              AND db_path IS NOT NULL
              AND db_path != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            ("done", limit),
        ).fetchall()
    for row in candidates:
        item = dict(row)
        timestamp = parse_event_listener_timestamp(str(item.get("finished_at") or item.get("updated_at") or ""))
        if timestamp is None:
            continue
        if window.start <= timestamp < window.end:
            item["report_timestamp"] = timestamp.isoformat(timespec="seconds")
            rows.append(item)
    rows.sort(key=lambda item: item.get("report_timestamp") or "")
    return rows


def parse_event_listener_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def event_work_session_report_root() -> Path:
    return PROJECT_ROOT / "outputs" / "event_listener" / "reports" / "work_sessions"


def latest_event_work_session_report() -> dict[str, Any]:
    reports_root = event_work_session_report_root()
    if not reports_root.is_dir():
        return {}
    report_dirs = [path for path in reports_root.iterdir() if path.is_dir()]
    report_dirs.sort(key=lambda path: safe_stat(path / "半日质检报告.md")[0], reverse=True)
    for report_dir in report_dirs:
        payload = work_session_report_payload(report_dir)
        if payload:
            return payload
    return {}


def cleanup_event_work_session_reports() -> None:
    """Apply event-listener retention policy to generated report folders."""
    settings = event_listener_settings()
    max_days = parse_float_setting(settings.get("retention_days"), 14.0)
    max_runs = parse_int_setting(settings.get("retention_max_runs"), 0)
    max_gb = parse_float_setting(settings.get("retention_max_gb"), 0.0)
    cleanup_report_outputs(
        event_work_session_report_root(),
        max_days=max_days,
        max_runs=max_runs,
        max_gb=max_gb,
    )


def cleanup_report_outputs(output_dir: Path, max_days: float, max_runs: int, max_gb: float) -> None:
    if max_runs <= 0 and max_days <= 0 and max_gb <= 0:
        return
    candidates = report_retention_candidates(output_dir, include_size=max_gb > 0)
    if not candidates:
        return
    now = time.time()
    keep: set[Path] = set()
    remove: dict[Path, str] = {}

    if max_days > 0:
        cutoff = now - (max_days * 86400)
        for item in candidates:
            if item["mtime"] < cutoff:
                remove[item["path"]] = f"older_than_{max_days:g}_days"

    remaining = [item for item in candidates if item["path"] not in remove]
    remaining.sort(key=lambda item: item["mtime"], reverse=True)
    if max_runs > 0:
        for item in remaining[:max_runs]:
            keep.add(item["path"])
        for item in remaining[max_runs:]:
            remove.setdefault(item["path"], f"over_{max_runs}_runs")

    if max_gb > 0:
        max_bytes = int(max_gb * 1024**3)
        size_items = [item for item in remaining if item["path"] not in remove]
        size_items.sort(key=lambda item: item["mtime"], reverse=True)
        total = 0
        for item in size_items:
            total += int(item["size"])
            if total > max_bytes and item["path"] not in keep:
                remove.setdefault(item["path"], f"over_{max_gb:g}_gb")

    if not remove:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / RETENTION_MANIFEST
    removed = 0
    with manifest_path.open("a", encoding="utf-8") as manifest:
        for path, reason in sorted(remove.items(), key=lambda item: str(item[0])):
            try:
                size = directory_size_bytes(path)
                shutil.rmtree(path)
                removed += 1
                manifest.write(
                    json.dumps(
                        {
                            "removed_at": now_iso(),
                            "path": str(path),
                            "reason": reason,
                            "size_bytes": size,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            except OSError as exc:
                print(f"report retention cleanup warning: could not remove {path}: {exc}", flush=True)
    if removed:
        print(f"report retention cleanup: removed {removed} old work-session report folder(s)", flush=True)


def report_retention_candidates(output_dir: Path, include_size: bool = False) -> list[dict[str, Any]]:
    if not output_dir.is_dir():
        return []
    candidates = []
    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        candidates.append(
            {
                "path": child,
                "mtime": stat.st_mtime,
                "size": directory_size_bytes(child) if include_size else 0,
            }
        )
    return candidates


def directory_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def parse_float_setting(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int_setting(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def render_event_work_session_report_html() -> str:
    report = latest_event_work_session_report()
    if not report:
        body = """
        <main>
          <h1>事件监听半日报告</h1>
          <p class="muted">尚未生成报告。请回到控制台点击“生成半日报告”。</p>
          <p><a href="/">返回控制台</a></p>
        </main>
        """
    else:
        summary = report.get("summary") or {}
        window = report.get("window") or {}
        body = f"""
        <main>
          <div class="topbar">
            <div>
              <h1>事件监听半日报告</h1>
              <p class="muted">{esc_html(window.get("start", ""))} 至 {esc_html(window.get("end", ""))}</p>
            </div>
            <p><a href="/">返回控制台</a></p>
          </div>
          <div class="metrics">
            <div><b>{int(summary.get("episode_count") or 0)}</b><span>episode</span></div>
            <div><b>{int(summary.get("issue_episode_count") or 0)}</b><span>问题 episode</span></div>
            <div><b>{int(summary.get("finding_count") or 0)}</b><span>finding</span></div>
            <div><b>{int(summary.get("training_blocking_episode_count") or 0)}</b><span>影响训练</span></div>
          </div>
          <p class="muted">报告文件：{esc_html(report.get("markdown_path", ""))}</p>
          <p class="muted">附件：{esc_html(report.get("core_issues_csv", ""))} | {esc_html(report.get("rule_explanations_csv", ""))} | {esc_html(report.get("operator_issues_csv", ""))} | {esc_html(report.get("operator_issue_episodes_csv", ""))} | {esc_html(report.get("affected_episodes_csv", ""))} | {esc_html(report.get("actions_csv", ""))}</p>
          <pre>{esc_html(report.get("markdown", ""))}</pre>
        </main>
        """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>事件监听半日报告</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: #f6f7f9; color: #1b1f24; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    a {{ color: #1769aa; text-decoration: none; }}
    .muted {{ color: #667085; font-size: 13px; }}
    .topbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 16px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; margin: 16px 0; }}
    .metrics div {{ background: white; border: 1px solid #d8dde6; border-radius: 6px; padding: 12px; }}
    .metrics b {{ display: block; font-size: 28px; }}
    .metrics span {{ color: #667085; font-size: 12px; }}
    pre {{ white-space: pre-wrap; background: white; border: 1px solid #d8dde6; border-radius: 6px; padding: 18px; line-height: 1.55; overflow: auto; }}
    @media (max-width: 760px) {{ .topbar, .metrics {{ display: block; }} .metrics div {{ margin-bottom: 10px; }} }}
  </style>
</head>
<body>{body}</body>
</html>"""


def esc_html(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def start_work_session_report_scheduler(state: DashboardState) -> None:
    if not state.auto_work_session_reports:
        return

    def worker() -> None:
        time.sleep(5)
        while True:
            try:
                now = time.monotonic()
                if now - state.work_session_report_last_run >= state.work_session_report_interval_seconds:
                    api_generate_event_work_session_report({"session": "current"})
                    state.work_session_report_last_run = now
            except Exception as exc:
                print(f"Auto work-session report failed: {exc}", flush=True)
            time.sleep(60)

    threading.Thread(target=worker, name="work-session-report-scheduler", daemon=True).start()


def api_event_listener_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    for key in (
        "WORKERS",
        "EVENT_BATCH_SIZE",
        "EVENT_DATE",
        "EVENT_DATE_FROM",
        "EVENT_DATE_TO",
        "STABILITY_INTERVAL",
        "STABILITY_TIMEOUT",
        "MIN_FREE_MEM_GB",
        "MAX_LOAD_RATIO",
        "RESOURCE_MAX_WAIT_SECONDS",
        "RETENTION_DAYS",
        "RETENTION_MAX_RUNS",
        "RETENTION_MAX_GB",
        "PHASES",
        "QUALITY_LABEL",
        "DISABLE_QUALITY_LABEL_FILTER",
        "DCS_CONFIG_FILE",
        "QA_DCS_NOTIFY_ENABLED",
        "QA_DCS_NOTIFY_DRY_RUN",
        "QA_DCS_NOTIFY_WAIT",
        "QA_DCS_NOTIFY_EVENT",
        "QA_DCS_NOTIFY_STATUSES",
        "QA_DCS_NOTIFY_ACTIONABLE_STATUSES",
        "QA_DCS_NOTIFY_ACTIONABLE_CHECKS",
        "QA_DCS_NOTIFY_EXCLUDE_CHECKS",
    ):
        if key.lower() in payload:
            env[key] = str(payload[key.lower()])
        elif key in payload:
            env[key] = str(payload[key])
    completed = subprocess.run(
        [str(EVENT_CONTROL_SCRIPT), action],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": completed.stdout[-4000:],
        "status": event_listener_status(),
    }


def api_event_listener_status() -> dict[str, Any]:
    return {"event_listener": event_listener_status()}


def event_listener_status() -> dict[str, Any]:
    status = {
        "tmux_running": tmux_session_running("qa_event_listener"),
        "job_db": str(EVENT_JOB_DB),
        "counts": {},
        "recent": [],
        "output_size": directory_size(PROJECT_ROOT / "outputs" / "event_listener", max_files=2000),
        "settings": event_listener_settings(),
    }
    if not EVENT_JOB_DB.exists():
        return status
    try:
        with sqlite3.connect(EVENT_JOB_DB) as conn:
            conn.row_factory = sqlite3.Row
            status["counts"] = {
                row["status"]: row["count"]
                for row in conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")
            }
            rows = conn.execute(
                """
                SELECT id, status, attempts, mounted_path, run_id, error, updated_at
                FROM jobs
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
            status["recent"] = [dict(row) for row in rows]
    except sqlite3.Error as exc:
        status["error"] = str(exc)
    return status


def event_listener_settings() -> dict[str, Any]:
    args = current_process_args("Werkzeuge/listen_episode_verified.py")
    if not args:
        return {}
    return {
        "phases": option_value(args, "--phases", ""),
        "workers": option_value(args, "--workers", ""),
        "batch_size": option_value(args, "--batch-size", ""),
        "event_date": option_value(args, "--event-date", ""),
        "event_date_from": option_value(args, "--event-date-from", ""),
        "event_date_to": option_value(args, "--event-date-to", ""),
        "max_load_ratio": option_value(args, "--max-load-ratio", ""),
        "min_free_mem_gb": option_value(args, "--min-free-mem-gb", ""),
        "stability_interval": option_value(args, "--stability-interval", ""),
        "stability_timeout": option_value(args, "--stability-timeout", ""),
        "resource_max_wait_seconds": option_value(args, "--resource-max-wait-seconds", ""),
        "retention_days": option_value(args, "--retention-days", ""),
        "retention_max_runs": option_value(args, "--retention-max-runs", ""),
        "retention_max_gb": option_value(args, "--retention-max-gb", ""),
    }


def current_process_args(marker: str) -> list[str]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw = (path / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if any(marker in arg for arg in args) and "serve" in args:
            return args
    return []


def option_value(args: list[str], flag: str, default: str = "") -> str:
    try:
        index = args.index(flag)
    except ValueError:
        return default
    if index + 1 >= len(args):
        return default
    return args[index + 1]


def event_listener_jobs(limit: int = 100, issues_only: bool = False) -> list[dict[str, Any]]:
    if not EVENT_JOB_DB.exists():
        return []
    limit = max(1, min(500, int(limit)))
    query_limit = 3000 if issues_only else limit
    jobs: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(EVENT_JOB_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, status, attempts, mounted_path, run_id, output_dir,
                       db_path, error, received_at, started_at, finished_at, updated_at
                FROM jobs
                ORDER BY id DESC
                LIMIT ?
                """,
                (query_limit,),
            ).fetchall()
    except sqlite3.Error:
        return []
    for row in rows:
        job = dict(row)
        job["episode_name"] = Path(str(job.get("mounted_path") or "")).name
        job["task"] = task_from_episode_path(str(job.get("mounted_path") or ""))
        job["robot"] = robot_from_episode_path(str(job.get("mounted_path") or ""))
        job.update(job_episode_result(job))
        if issues_only and int(job.get("issue_count") or 0) <= 0:
            continue
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def event_listener_job_detail(job_id: int, verified_root: Path) -> dict[str, Any]:
    if not EVENT_JOB_DB.exists():
        raise ValueError("event listener job DB does not exist")
    with sqlite3.connect(EVENT_JOB_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, status, attempts, mounted_path, run_id, output_dir,
                   db_path, error, received_at, started_at, finished_at, updated_at
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown event job id: {job_id}")
    job = dict(row)
    job["episode_name"] = Path(str(job.get("mounted_path") or "")).name
    job["nas_internal_path"] = nas_internal_verified_path(str(job.get("mounted_path") or ""), verified_root)
    job["task"] = task_from_episode_path(str(job.get("mounted_path") or ""))
    job["robot"] = robot_from_episode_path(str(job.get("mounted_path") or ""))
    job.update(job_episode_result(job, include_findings=True))
    output_dir = Path(str(job.get("output_dir") or ""))
    if output_dir:
        job["log_tail"] = tail_file(output_dir / "pipeline.log", 120)
    return job


def consecutive_failure_warnings(state: DashboardState) -> dict[str, Any]:
    signature = event_job_warning_signature()
    if signature is None:
        return {"count": 0, "warnings": [], "checked_jobs": 0}
    with state.lock:
        if state.failure_warning_signature == signature:
            return state.failure_warning_cache
    warnings = compute_consecutive_failure_warnings(state.issue_history_db)
    with state.lock:
        state.failure_warning_signature = signature
        state.failure_warning_cache = warnings
    return warnings


def event_job_warning_signature() -> tuple[int, int, str, str] | None:
    if not EVENT_JOB_DB.exists():
        return None
    try:
        with sqlite3.connect(f"file:{EVENT_JOB_DB}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id, "
                "COALESCE(MAX(updated_at), '') AS max_updated_at FROM jobs"
            ).fetchone()
            db_paths = [
                str(item[0] or "")
                for item in conn.execute("SELECT DISTINCT db_path FROM jobs WHERE db_path IS NOT NULL")
            ]
    except sqlite3.Error:
        return None
    db_mtimes = []
    for db_path in db_paths:
        try:
            db_mtimes.append(f"{db_path}:{Path(db_path).stat().st_mtime_ns}")
        except OSError:
            db_mtimes.append(f"{db_path}:missing")
    return (int(row[0] or 0), int(row[1] or 0), str(row[2] or ""), "|".join(sorted(db_mtimes)))


def compute_consecutive_failure_warnings(issue_history_db: Path = DEFAULT_ISSUE_HISTORY_DB) -> dict[str, Any]:
    latest_by_path: dict[str, dict[str, Any]] = {}
    try:
        with sqlite3.connect(f"file:{EVENT_JOB_DB}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, status, mounted_path, db_path, updated_at
                FROM jobs
                ORDER BY id
                """
            ).fetchall()
    except sqlite3.Error:
        return {"count": 0, "warnings": [], "checked_jobs": 0}

    for row in rows:
        job = dict(row)
        mounted_path = str(job.get("mounted_path") or "")
        if not mounted_path:
            continue
        latest_by_path[mounted_path] = job

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for job in latest_by_path.values():
        mounted_path = str(job.get("mounted_path") or "")
        episode_number = parse_episode_number(mounted_path)
        if episode_number is None:
            continue
        result = job_episode_result(job)
        task = str(result.get("task") or task_from_episode_path(mounted_path) or "")
        robot = str(result.get("robot") or robot_from_episode_path(mounted_path) or "")
        operator = str(result.get("operator") or operator_from_episode_path(mounted_path) or "")
        if not task or not robot:
            continue
        groups.setdefault((task, robot, operator), []).append(
            {
                "episode_number": episode_number,
                "episode_name": Path(mounted_path).name,
                "mounted_path": mounted_path,
                "db_path": str(job.get("db_path") or ""),
                "job_status": str(job.get("status") or ""),
                "final_status": str(result.get("final_status") or ""),
                "updated_at": str(job.get("updated_at") or ""),
            }
        )

    detected = []
    for (task, robot, operator), episodes in groups.items():
        for streak in consecutive_bad_segments(episodes, CONSECUTIVE_FAILURE_STREAK_LENGTH):
            detected.append(
                {
                    "task": task,
                    "robot": robot,
                    "operator": operator,
                    "start_episode_number": streak[0]["episode_number"],
                    "end_episode_number": streak[-1]["episode_number"],
                    "start_episode_name": streak[0]["episode_name"],
                    "end_episode_name": streak[-1]["episode_name"],
                    "streak_length": len(streak),
                    "issue_types": issue_types_for_streak(streak),
                    "_episodes": streak,
                    "message": (
                        f"episode_{streak[0]['episode_number']:04d} ~ "
                        f"episode_{streak[-1]['episode_number']:04d} 连续{len(streak)}次失败"
                    ),
                }
            )
    record_consecutive_failure_detections(issue_history_db, detected)
    active = active_consecutive_failure_warnings(issue_history_db, limit=20)
    active["checked_jobs"] = len(latest_by_path)
    return active


def issue_types_for_streak(streak: list[dict[str, Any]]) -> list[str]:
    issue_types: set[str] = set()
    for episode in streak:
        db_path = Path(str(episode.get("db_path") or ""))
        mounted_path = str(episode.get("mounted_path") or "")
        if not db_path.is_file() or not mounted_path:
            continue
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT check_name
                    FROM findings
                    WHERE episode_path = ? AND status != ?
                    ORDER BY check_name
                    """,
                    (mounted_path, "pass"),
                ).fetchall()
        except sqlite3.Error:
            continue
        issue_types.update(str(row[0]) for row in rows if row[0])
    return sorted(issue_types)


def record_consecutive_failure_detections(db_path: Path, detections: list[dict[str, Any]]) -> None:
    init_issue_history(db_path)
    detected_at = now_iso()
    with sqlite3.connect(db_path) as conn:
        for item in detections:
            effective = effective_detection_for_history(conn, item)
            if effective is None:
                continue
            exists = conn.execute(
                """
                SELECT 1
                FROM consecutive_failure_streaks
                WHERE task = ? AND robot = ? AND operator = ?
                  AND episode_start = ? AND episode_end = ?
                LIMIT 1
                """,
                (
                    effective["task"],
                    effective["robot"],
                    effective["operator"],
                    int(effective["start_episode_number"]),
                    int(effective["end_episode_number"]),
                ),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO consecutive_failure_streaks (
                    task, robot, operator, episode_start, episode_end,
                    streak_length, issue_types, detected_at, resolved_at,
                    resolution_time_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    effective["task"],
                    effective["robot"],
                    effective["operator"],
                    int(effective["start_episode_number"]),
                    int(effective["end_episode_number"]),
                    int(effective["streak_length"]),
                    json.dumps(effective.get("issue_types") or [], ensure_ascii=False),
                    detected_at,
                ),
            )


def effective_detection_for_history(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any] | None:
    task = str(item["task"])
    robot = str(item["robot"])
    operator = str(item["operator"])
    segment_start = int(item["start_episode_number"])
    segment_end = int(item["end_episode_number"])
    rows = conn.execute(
        """
        SELECT id, episode_start, episode_end, issue_types, resolved_at
        FROM consecutive_failure_streaks
        WHERE task = ? AND robot = ? AND operator = ?
          AND episode_start <= ? AND episode_end >= ?
        ORDER BY episode_start, episode_end, id
        """,
        (task, robot, operator, segment_end, segment_start),
    ).fetchall()

    for row in rows:
        row_start = int(row[1])
        row_end = int(row[2])
        if row[4] is None and row_start == segment_start:
            if segment_end > row_end:
                issue_types = sorted(set(json_value(row[3], [])) | set(item.get("issue_types") or []))
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET episode_end = ?, streak_length = ?, issue_types = ?
                    WHERE id = ?
                    """,
                    (
                        segment_end,
                        segment_end - row_start + 1,
                        json.dumps(issue_types, ensure_ascii=False),
                        int(row[0]),
                    ),
                )
            return None

    effective_start = segment_start
    changed = True
    while changed:
        changed = False
        for row in rows:
            row_start = int(row[1])
            row_end = int(row[2])
            if row[4] is not None and row_start <= effective_start <= row_end:
                effective_start = row_end + 1
                changed = True
                break

    if segment_end - effective_start + 1 < CONSECUTIVE_FAILURE_STREAK_LENGTH:
        return None

    for row in rows:
        row_start = int(row[1])
        row_end = int(row[2])
        if row[4] is None and row_start == effective_start:
            if segment_end > row_end:
                effective_episodes = _episodes_in_range(item.get("_episodes") or [], effective_start, segment_end)
                issue_types = sorted(set(json_value(row[3], [])) | set(issue_types_for_streak(effective_episodes)))
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET episode_end = ?, streak_length = ?, issue_types = ?
                    WHERE id = ?
                    """,
                    (
                        segment_end,
                        segment_end - row_start + 1,
                        json.dumps(issue_types, ensure_ascii=False),
                        int(row[0]),
                    ),
                )
            return None

    effective = dict(item)
    effective["start_episode_number"] = effective_start
    effective["end_episode_number"] = segment_end
    effective["streak_length"] = segment_end - effective_start + 1
    effective_episodes = _episodes_in_range(item.get("_episodes") or [], effective_start, segment_end)
    effective["issue_types"] = issue_types_for_streak(effective_episodes) if effective_episodes else item.get("issue_types", [])
    return effective


def _episodes_in_range(episodes: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    return [
        episode
        for episode in episodes
        if start <= int(episode.get("episode_number", -1)) <= end
    ]


def active_consecutive_failure_warnings(db_path: Path, limit: int = 20) -> dict[str, Any]:
    init_issue_history(db_path)
    rows: list[sqlite3.Row]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, task, robot, operator, episode_start, episode_end,
                   streak_length, issue_types, detected_at
            FROM consecutive_failure_streaks
            WHERE resolved_at IS NULL
            ORDER BY detected_at DESC, id DESC
            """
        ).fetchall()

    by_combo: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["task"], row["robot"], row["operator"])
        if key in by_combo:
            continue
        item = {
            "id": int(row["id"]),
            "task": row["task"],
            "robot": row["robot"],
            "operator": row["operator"],
            "start_episode_number": int(row["episode_start"]),
            "end_episode_number": int(row["episode_end"]),
            "streak_length": int(row["streak_length"]),
            "issue_types": json_value(row["issue_types"], []),
            "detected_at": row["detected_at"],
            "message": (
                f"episode_{int(row['episode_start']):04d} ~ "
                f"episode_{int(row['episode_end']):04d} 连续{int(row['streak_length'])}次失败"
            ),
        }
        by_combo[key] = item

    warnings = list(by_combo.values())
    total = len(warnings)
    warnings = warnings[:limit]
    return {
        "count": total,
        "warnings": warnings,
        "hidden_count": max(0, total - len(warnings)),
        "limit": limit,
    }


def api_resolve_consecutive_failure(state: DashboardState, payload: dict[str, Any]) -> dict[str, Any]:
    task = str(payload.get("task") or "")
    robot = str(payload.get("robot") or "")
    operator = str(payload.get("operator") or "")
    episode_start = int(payload.get("episode_start"))
    episode_end = int(payload.get("episode_end"))
    resolved = resolve_consecutive_failure(
        state.issue_history_db,
        task,
        robot,
        operator,
        episode_start,
        episode_end,
    )
    with state.lock:
        state.failure_warning_signature = None
        state.failure_warning_cache = {"count": 0, "warnings": [], "checked_jobs": 0}
    return {"ok": resolved, "consecutive_failures": consecutive_failure_warnings(state)}


def resolve_consecutive_failure(
    db_path: Path,
    task: str,
    robot: str,
    operator: str,
    episode_start: int,
    episode_end: int,
) -> bool:
    init_issue_history(db_path)
    resolved_at = now_iso()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, detected_at
            FROM consecutive_failure_streaks
            WHERE task = ? AND robot = ? AND operator = ?
              AND episode_start = ? AND episode_end = ?
              AND resolved_at IS NULL
            ORDER BY detected_at DESC, id DESC
            LIMIT 1
            """,
            (task, robot, operator, episode_start, episode_end),
        ).fetchone()
        if row is None:
            return False
        resolution_seconds = max(0, int(datetime.fromisoformat(resolved_at).timestamp() - datetime.fromisoformat(row[1]).timestamp()))
        conn.execute(
            """
            UPDATE consecutive_failure_streaks
            SET resolved_at = ?, resolution_time_seconds = ?
            WHERE id = ?
            """,
            (resolved_at, resolution_seconds, int(row[0])),
        )
    return True


def first_consecutive_bad_streak(episodes: list[dict[str, Any]], length: int) -> list[dict[str, Any]] | None:
    streaks = consecutive_bad_segments(episodes, length)
    return streaks[0] if streaks else None


def consecutive_bad_segments(episodes: list[dict[str, Any]], length: int) -> list[list[dict[str, Any]]]:
    streaks = []
    streak: list[dict[str, Any]] = []
    previous_number: int | None = None
    for episode in sorted(episodes, key=lambda item: item["episode_number"]):
        number = int(episode["episode_number"])
        if previous_number is None or number != previous_number + 1:
            if len(streak) >= length:
                streaks.append(streak)
            streak = []
        if is_bad_event_episode(episode):
            streak.append(episode)
        else:
            if len(streak) >= length:
                streaks.append(streak)
            streak = []
        previous_number = number
    if len(streak) >= length:
        streaks.append(streak)
    return streaks


def is_bad_event_episode(episode: dict[str, Any]) -> bool:
    job_status = str(episode.get("job_status") or "").strip().lower().replace("-", "_")
    final_status = str(episode.get("final_status") or "").strip().lower().replace("-", "_")
    return job_status == "done" and final_status in BAD_QA_FINAL_STATUSES


def parse_episode_number(path_or_name: str) -> int | None:
    match = re.match(r"episode_(\d+)(?:_|$)", Path(str(path_or_name)).name)
    return int(match.group(1)) if match else None


def event_issue_summary(limit: int = 500) -> dict[str, Any]:
    limit = max(1, min(2000, int(limit)))
    jobs = event_listener_jobs(limit=limit, issues_only=True)
    by_db: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        db_path = str(job.get("db_path") or "")
        episode_path = str(job.get("mounted_path") or "")
        if db_path and episode_path:
            by_db.setdefault(db_path, []).append(job)

    severity_counts: dict[str, int] = {}
    issue_groups: dict[tuple[str, str], dict[str, Any]] = {}
    context_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    total_findings = 0
    issue_episode_paths: set[str] = set()

    for db_path_raw, db_jobs in by_db.items():
        db_path = Path(db_path_raw)
        if not db_path.is_file():
            continue
        episode_to_job = {str(job.get("mounted_path") or ""): job for job in db_jobs}
        episode_paths = list(episode_to_job)
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                for chunk in chunks(episode_paths, 400):
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"""
                        SELECT f.episode_path, f.check_name, f.severity, f.status,
                               e.task, e.robot, e.operator
                        FROM findings f
                        LEFT JOIN episodes e ON e.episode_path = f.episode_path
                        WHERE f.status != ? AND f.episode_path IN ({placeholders})
                        """,
                        ("pass", *chunk),
                    ).fetchall()
                    for row in rows:
                        episode_path = str(row["episode_path"] or "")
                        job = episode_to_job.get(episode_path, {})
                        task = row["task"] or job.get("task") or task_from_episode_path(episode_path)
                        robot = row["robot"] or job.get("robot") or robot_from_episode_path(episode_path)
                        operator = row["operator"] or job.get("operator") or operator_from_episode_path(episode_path)
                        check_name = row["check_name"] or "unknown"
                        severity = normalize_severity(row["severity"] or row["status"] or "unknown")

                        total_findings += 1
                        issue_episode_paths.add(episode_path)
                        severity_counts[severity] = severity_counts.get(severity, 0) + 1

                        issue_key = (str(check_name), severity)
                        issue_group = issue_groups.setdefault(
                            issue_key,
                            {
                                "check_name": str(check_name),
                                "severity": severity,
                                "finding_count": 0,
                                "episodes": set(),
                                "contexts": set(),
                            },
                        )
                        issue_group["finding_count"] += 1
                        issue_group["episodes"].add(episode_path)
                        issue_group["contexts"].add((str(task), str(robot), str(operator)))

                        context_key = (str(task), str(robot), str(operator))
                        context_group = context_groups.setdefault(
                            context_key,
                            {
                                "task": str(task),
                                "robot": str(robot),
                                "operator": str(operator),
                                "finding_count": 0,
                                "episodes": set(),
                                "severity_counts": {},
                                "issues": {},
                            },
                        )
                        context_group["finding_count"] += 1
                        context_group["episodes"].add(episode_path)
                        context_group["severity_counts"][severity] = (
                            context_group["severity_counts"].get(severity, 0) + 1
                        )
                        context_group["issues"][str(check_name)] = context_group["issues"].get(str(check_name), 0) + 1
        except sqlite3.Error:
            continue

    issue_rows = []
    for group in issue_groups.values():
        issue_rows.append(
            {
                "check_name": group["check_name"],
                "severity": group["severity"],
                "finding_count": group["finding_count"],
                "episode_count": len(group["episodes"]),
                "context_count": len(group["contexts"]),
            }
        )
    issue_rows.sort(key=lambda item: (-severity_rank(item["severity"]), -item["finding_count"], item["check_name"]))

    context_rows = []
    for group in context_groups.values():
        top_issues = sorted(group["issues"].items(), key=lambda item: (-item[1], item[0]))[:5]
        context_rows.append(
            {
                "task": group["task"],
                "robot": group["robot"],
                "operator": group["operator"],
                "finding_count": group["finding_count"],
                "episode_count": len(group["episodes"]),
                "severity_counts": group["severity_counts"],
                "top_issues": [{"check_name": name, "count": count} for name, count in top_issues],
            }
        )
    context_rows.sort(
        key=lambda item: (
            -max((severity_rank(sev) for sev in item["severity_counts"]), default=0),
            -item["finding_count"],
            item["task"],
            item["robot"],
            item["operator"],
        )
    )

    ordered_severity_counts = {
        severity: severity_counts[severity]
        for severity in sorted(severity_counts, key=lambda name: (-severity_rank(name), name))
    }
    return {
        "job_count": len(jobs),
        "issue_episode_count": len(issue_episode_paths),
        "finding_count": total_findings,
        "severity_counts": ordered_severity_counts,
        "by_issue": issue_rows[:50],
        "by_context": context_rows[:80],
    }


def job_episode_result(job: dict[str, Any], include_findings: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "final_status": "",
        "phase_status": {},
        "issue_count": 0,
        "top_issues": [],
    }
    db_path = Path(str(job.get("db_path") or ""))
    episode_path = str(job.get("mounted_path") or "")
    if not db_path.is_file() or not episode_path:
        return result
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            episode = conn.execute(
                """
                SELECT final_status, phase_status, task, robot, operator, date, controller, last_updated
                FROM episodes
                WHERE episode_path = ?
                """,
                (episode_path,),
            ).fetchone()
            if episode is not None:
                result["final_status"] = episode["final_status"] or ""
                result["phase_status"] = json_value(episode["phase_status"], {})
                result["task"] = episode["task"] or result.get("task") or ""
                result["robot"] = episode["robot"] or result.get("robot") or ""
                result["operator"] = episode["operator"] or ""
                result["date"] = episode["date"] or ""
                result["controller"] = episode["controller"] or ""
                result["episode_last_updated"] = episode["last_updated"] or ""
            result["issue_count"] = scalar(
                conn,
                "SELECT COUNT(*) FROM findings WHERE episode_path = ? AND status != ?",
                (episode_path, "pass"),
            )
            top_issue_groups: dict[str, dict[str, Any]] = {}
            for check_name, details_raw in conn.execute(
                """
                SELECT check_name, details
                FROM findings
                WHERE episode_path = ? AND status != ?
                ORDER BY id
                """,
                (episode_path, "pass"),
            ):
                name = str(check_name)
                group = top_issue_groups.setdefault(name, {"check_name": name, "count": 0, "fields": []})
                group["count"] += 1
                for field in finding_detail_fields(json_value(details_raw, {})):
                    if field not in group["fields"]:
                        group["fields"].append(field)
            result["top_issues"] = sorted(
                top_issue_groups.values(),
                key=lambda item: (-int(item["count"]), str(item["check_name"])),
            )[:10]
            if include_findings:
                findings = conn.execute(
                    """
                    SELECT phase, check_name, severity, status, message, details
                    FROM findings
                    WHERE episode_path = ? AND status != ?
                    ORDER BY phase, id
                    """,
                    (episode_path, "pass"),
                ).fetchall()
                result["findings"] = [
                    {
                        "phase": row["phase"],
                        "check_name": row["check_name"],
                        "severity": row["severity"],
                        "status": row["status"],
                        "message": row["message"],
                        "details": json_value(row["details"], {}),
                    }
                    for row in findings
                ]
    except sqlite3.Error as exc:
        result["db_error"] = str(exc)
    return result


def task_from_episode_path(path: str) -> str:
    parts = Path(path).parts
    try:
        index = parts.index("verified")
        return parts[index + 1] if len(parts) > index + 1 else ""
    except ValueError:
        return ""


def robot_from_episode_path(path: str) -> str:
    parts = Path(path).parts
    try:
        index = parts.index("verified")
        return parts[index + 2] if len(parts) > index + 2 else ""
    except ValueError:
        return ""


def operator_from_episode_path(path: str) -> str:
    parts = Path(path).parts
    try:
        index = parts.index("verified")
        return parts[index + 5] if len(parts) > index + 5 else ""
    except ValueError:
        return ""


def normalize_severity(value: str) -> str:
    severity = str(value or "unknown").strip().lower()
    aliases = {
        "failed": "fail",
        "failure": "fail",
        "error": "fail",
        "err": "fail",
        "warn": "warning",
        "needs_review": "warning",
        "review": "warning",
    }
    return aliases.get(severity, severity or "unknown")


def severity_rank(severity: str) -> int:
    ranks = {
        "critical": 6,
        "fatal": 6,
        "fail": 5,
        "high": 5,
        "warning": 4,
        "medium": 4,
        "low": 3,
        "info": 2,
        "unknown": 1,
    }
    return ranks.get(normalize_severity(severity), 1)


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def sync_registered_run_statuses(state: DashboardState) -> None:
    with state.lock:
        runs = registered_runs(state.registry_db)
        for run in runs:
            if run.get("status") != "running":
                continue
            session = run.get("tmux_session") or ""
            if session and tmux_session_running(session):
                continue
            run_status = latest_run_status(Path(run["output_dir"]))
            status = str(run_status.get("status") or "complete")
            if status == "running":
                status = "complete"
            update_run_status(state.registry_db, run["run_id"], status, finished=True)


def record_run(registry_db: Path, row: dict[str, Any]) -> None:
    with sqlite3.connect(registry_db) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, mode, output_dir, db_path, command, tmux_session,
                status, phases, workers, batch_size, root_path, date_from,
                date_to, task_filter, started_at, finished_at, updated_at, notes
            )
            VALUES (
                :run_id, :mode, :output_dir, :db_path, :command, :tmux_session,
                :status, :phases, :workers, :batch_size, :root_path, :date_from,
                :date_to, :task_filter, :started_at, :finished_at, :updated_at, :notes
            )
            """,
            row,
        )


def update_run_status(registry_db: Path, run_id: str, status: str, finished: bool = False) -> None:
    with sqlite3.connect(registry_db) as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = CASE WHEN ? THEN ? ELSE finished_at END, updated_at = ?
            WHERE run_id = ?
            """,
            (status, int(finished), now_iso(), now_iso(), run_id),
        )


def append_run_event(registry_db: Path, run_id: str, event_type: str, message: str) -> None:
    with sqlite3.connect(registry_db) as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, recorded_at, event_type, message) VALUES (?, ?, ?, ?)",
            (run_id, now_iso(), event_type, message),
        )


def run_summary(db_path: Path, output_dir: Path, include_top_issues: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "episode_count": 0,
        "finding_count": 0,
        "status_counts": {},
        "top_issues": [],
        "db_exists": db_path.exists(),
        "output_exists": output_dir.exists(),
    }
    if not db_path.exists():
        return result
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            result["episode_count"] = scalar(conn, "SELECT COUNT(*) FROM episodes")
            result["finding_count"] = scalar(conn, "SELECT COUNT(*) FROM findings WHERE status != ?", ("pass",))
            result["status_counts"] = {
                str(status or "pending"): int(count)
                for status, count in conn.execute(
                    "SELECT final_status, COUNT(*) FROM episodes GROUP BY final_status"
                )
            }
            if include_top_issues:
                result["top_issues"] = [
                    {"check_name": str(name), "count": int(count)}
                    for name, count in conn.execute(
                        """
                        SELECT check_name, COUNT(*) AS count
                        FROM findings
                        WHERE status != ?
                        GROUP BY check_name
                        ORDER BY count DESC
                        LIMIT 20
                        """,
                        ("pass",),
                    )
                ]
    except sqlite3.Error as exc:
        result["db_error"] = str(exc)
    status = latest_run_status(output_dir)
    if status:
        result["live_status"] = status
    return result


def recent_findings(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []


def issue_episodes(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT e.episode_path, e.task, e.robot, e.operator, e.final_status,
                       COUNT(f.id) AS issue_count,
                       GROUP_CONCAT(DISTINCT f.check_name) AS issue_names
                FROM episodes e
                JOIN findings f ON f.episode_path = e.episode_path AND f.status != ?
                GROUP BY e.episode_path, e.task, e.robot, e.operator, e.final_status
                ORDER BY issue_count DESC, e.episode_path
                LIMIT ?
                """,
                ("pass", limit),
            ).fetchall()
            return [
                {
                    "episode_path": row["episode_path"],
                    "episode_name": Path(row["episode_path"]).name,
                    "task": row["task"] or "",
                    "robot": row["robot"] or "",
                    "operator": row["operator"] or "",
                    "final_status": row["final_status"] or "",
                    "issue_count": int(row["issue_count"] or 0),
                    "issue_names": [name for name in str(row["issue_names"] or "").split(",") if name],
                }
                for row in rows
            ]
    except sqlite3.Error:
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT f.id, f.episode_path, f.phase, f.check_name, f.severity,
                       f.status, f.message, e.task, e.robot, e.operator
                FROM findings f
                LEFT JOIN episodes e ON e.episode_path = f.episode_path
                WHERE f.status != ?
                ORDER BY f.id DESC
                LIMIT ?
                """,
                ("pass", limit),
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def latest_run_status(output_dir: Path) -> dict[str, Any]:
    pointer = output_dir / "latest_run.txt"
    candidates = []
    try:
        run_dir = Path(pointer.read_text(encoding="utf-8").strip())
        candidates.append(run_dir / "run_status.json")
    except OSError:
        pass
    runs_dir = output_dir / "runs"
    if runs_dir.is_dir():
        run_dirs = [path for path in runs_dir.iterdir() if path.is_dir()]
        run_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        candidates.extend(path / "run_status.json" for path in run_dirs[:3])
    for status_path in candidates:
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def latest_work_session_report(output_dir: Path) -> dict[str, Any]:
    reports_root = output_dir / "reports" / "work_sessions"
    if not reports_root.is_dir():
        return {}
    report_dirs = [path for path in reports_root.iterdir() if path.is_dir()]
    report_dirs.sort(key=lambda path: safe_stat(path / "半日质检报告.md")[0], reverse=True)
    for report_dir in report_dirs:
        payload = work_session_report_payload(report_dir)
        if payload:
            return payload
    return {}


def work_session_report_payload(report_dir: Path) -> dict[str, Any]:
    markdown_path = report_dir / "半日质检报告.md"
    json_path = report_dir / "report.json"
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    metadata: dict[str, Any] = {}
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            metadata = {
                "generated_at": raw.get("generated_at", ""),
                "window": raw.get("window", {}),
                "summary": raw.get("summary", {}),
                "core_issue_count": len(raw.get("core_issues") or []),
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "report_dir": str(report_dir),
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "core_issues_csv": str(report_dir / "核心问题汇总.csv"),
        "rule_explanations_csv": str(report_dir / "检测规则说明.csv"),
        "operator_issues_csv": str(report_dir / "采集人员问题占比.csv"),
        "operator_issue_episodes_csv": str(report_dir / "采集人员问题episode索引.csv"),
        "affected_episodes_csv": str(report_dir / "问题episode清单.csv"),
        "actions_csv": str(report_dir / "处理建议.csv"),
        "markdown": markdown,
        **metadata,
    }


def server_load() -> dict[str, Any]:
    load1, load5, load15 = os.getloadavg()
    mem = meminfo()
    disk_outputs = shutil.disk_usage(PROJECT_ROOT / "outputs") if (PROJECT_ROOT / "outputs").exists() else None
    return {
        "generated_at": now_iso(),
        "cpu_count": os.cpu_count() or 1,
        "load": {"1m": load1, "5m": load5, "15m": load15},
        "load_ratio_1m": load1 / max(1, os.cpu_count() or 1),
        "memory": mem,
        "disk_outputs": disk_payload(disk_outputs),
        "top_processes": top_processes(),
    }


def meminfo() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            number = int(raw.strip().split()[0]) * 1024
            values[key] = number
    except OSError:
        return {}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "total_gb": bytes_to_gb(total),
        "available_gb": bytes_to_gb(available),
        "used_gb": bytes_to_gb(max(0, total - available)),
        "swap_total_gb": bytes_to_gb(swap_total),
        "swap_used_gb": bytes_to_gb(max(0, swap_total - swap_free)),
    }


def top_processes() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid,ppid,%cpu,%mem,rss,cmd", "--sort=-%cpu"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return []
    rows = []
    for line in completed.stdout.splitlines()[1:11]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        rows.append(
            {
                "pid": parts[0],
                "ppid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "rss_mb": round(int(parts[4]) / 1024, 1),
                "cmd": parts[5][:160],
            }
        )
    return rows


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def json_value(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def finding_detail_fields(details: Any) -> list[str]:
    if not isinstance(details, dict):
        return []
    fields: list[str] = []
    for key in ("modality", "field", "sensor", "channel"):
        value = details.get(key)
        if isinstance(value, str) and value:
            fields.append(value)
    for key in ("modalities", "fields", "sensors", "channels"):
        value = details.get(key)
        if isinstance(value, list):
            fields.extend(str(item) for item in value if str(item))
    return fields


def write_run_script(path: Path, command: list[str], log_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(PROJECT_ROOT))}",
        f"mkdir -p {shlex.quote(str(path.parent))}",
        f"exec > >(tee -a {shlex.quote(str(log_path))}) 2>&1",
        "source datapipeline-env/bin/activate",
        "if [[ -f \"$HOME/.qa_task_db_env\" ]]; then source \"$HOME/.qa_task_db_env\"; fi",
        "echo \"Run started at $(date '+%F %T')\"",
        " ".join(shlex.quote(part) for part in command),
        "echo \"Run finished at $(date '+%F %T')\"",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def start_tmux(session: str, command: str) -> None:
    if tmux_session_running(session):
        raise ValueError(f"tmux session already exists: {session}")
    completed = subprocess.run(["tmux", "new-session", "-d", "-s", session, command], cwd=PROJECT_ROOT)
    if completed.returncode != 0:
        raise RuntimeError(f"tmux failed to start session: {session}")


def tmux_session_running(session: str) -> bool:
    if not session:
        return False
    try:
        completed = subprocess.run(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0
    except FileNotFoundError:
        return False


def normalize_verified_path(raw: str, verified_root: Path) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("root_path is required")
    prefixes = ["/volume1/database/verified", "/database/verified", str(verified_root)]
    for prefix in prefixes:
        if value == prefix or value.startswith(prefix.rstrip("/") + "/"):
            rel = value[len(prefix.rstrip("/")) :].lstrip("/")
            return str((verified_root / rel).resolve(strict=False))
    if value.startswith("/mnt/nas/database/verified"):
        return str(Path(value).resolve(strict=False))
    raise ValueError("root_path must be under /mnt/nas/database/verified or /database/verified")


def nas_internal_verified_path(raw: str, verified_root: Path) -> str:
    value = raw.strip()
    if not value:
        return value
    prefixes = {
        "/volume1/database/verified",
        "/database/verified",
        "/mnt/nas/database/verified",
        str(verified_root),
    }
    for prefix in sorted((item.rstrip("/") for item in prefixes if item), key=len, reverse=True):
        if value == prefix or value.startswith(prefix + "/"):
            rel = value[len(prefix) :].lstrip("/")
            return "/database/verified" + (f"/{rel}" if rel else "")
    return value


def validate_phases(value: str) -> str:
    stripped = value.strip()
    if not re.fullmatch(r"[1-7](,[1-7])*", stripped):
        raise ValueError("phases must look like 1,2,3,7")
    return ",".join(sorted(set(stripped.split(",")), key=int))


def validate_date(value: str, name: str) -> str:
    stripped = value.strip()
    if not re.fullmatch(r"\d{8}", stripped):
        raise ValueError(f"{name} must be YYYYMMDD")
    return stripped


def sanitize_id(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    if not sanitized:
        sanitized = "run"
    return sanitized[:120]


def make_run_id(mode: str) -> str:
    return sanitize_id(f"{mode}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")


def clamp_int(value: Any, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def clamp_float(value: Any, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def log_tail(state: DashboardState, target: str) -> list[str]:
    if target == "event_listener":
        return tail_file(PROJECT_ROOT / "outputs" / "event_listener" / "listener.log", 120)
    run = find_run(state, target)
    if not run:
        return []
    output_dir = Path(run["output_dir"])
    return tail_file(output_dir / f"{run['run_id']}_pipeline.log", 120) or tail_file(output_dir / "pipeline.log", 120)


def tail_file(path: Path, limit: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def directory_size(path: Path, max_files: int | None = None) -> str:
    if not path.exists():
        return "0B"
    total = 0
    scanned = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for item in entries:
            if item.is_dir():
                stack.append(item)
                continue
            if not item.is_file():
                continue
            try:
                total += item.stat().st_size
            except OSError:
                pass
            scanned += 1
            if max_files is not None and scanned >= max_files:
                return human_bytes(total) + "+"
    return human_bytes(total)


def issue_translations() -> dict[str, str]:
    global _ISSUE_TRANSLATIONS_CACHE
    if _ISSUE_TRANSLATIONS_CACHE is not None:
        return _ISSUE_TRANSLATIONS_CACHE
    try:
        raw = json.loads(ISSUE_TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not load issue translations from {ISSUE_TRANSLATIONS_PATH}: {exc}", flush=True)
        _ISSUE_TRANSLATIONS_CACHE = {}
        return _ISSUE_TRANSLATIONS_CACHE
    if not isinstance(raw, dict):
        print(f"Warning: issue translations file must contain a JSON object: {ISSUE_TRANSLATIONS_PATH}", flush=True)
        _ISSUE_TRANSLATIONS_CACHE = {}
        return _ISSUE_TRANSLATIONS_CACHE
    _ISSUE_TRANSLATIONS_CACHE = {str(key): str(value) for key, value in raw.items()}
    return _ISSUE_TRANSLATIONS_CACHE


def disk_payload(usage: shutil._ntuple_diskusage | None) -> dict[str, Any]:
    if usage is None:
        return {}
    return {
        "total_gb": bytes_to_gb(usage.total),
        "used_gb": bytes_to_gb(usage.used),
        "free_gb": bytes_to_gb(usage.free),
        "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
    }


def safe_stat(path: Path) -> tuple[float, int]:
    try:
        stat = path.stat()
        return stat.st_mtime, stat.st_size
    except OSError:
        return 0.0, 0


def mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


def discovered_run_id(output_dir: Path) -> str:
    pointer = output_dir / "latest_run.txt"
    try:
        run_dir = Path(pointer.read_text(encoding="utf-8").strip())
        if run_dir.name:
            return run_dir.name
    except OSError:
        pass
    for root in (PROJECT_ROOT / "outputs", REPO_ROOT / "outputs"):
        try:
            return sanitize_id(str(output_dir.relative_to(root)).replace("/", "-"))
        except ValueError:
            continue
    return sanitize_id(output_dir.name)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def bytes_to_gb(value: int) -> float:
    return round(value / (1024**3), 2)


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
        amount /= 1024
    return f"{amount:.1f}TB"


def render_index_html(state: DashboardState) -> str:
    refresh = int(state.refresh_seconds * 1000)
    issue_translations_json = json.dumps(issue_translations(), ensure_ascii=False, sort_keys=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QA Control Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde6;
      --text: #1b1f24;
      --muted: #667085;
      --accent: #1769aa;
      --danger: #b42318;
      --ok: #067647;
      --warn: #b54708;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, sans-serif; background: var(--bg); color: var(--text); }}
    header {{ height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; background: #1f2933; color: white; }}
    main {{ display: grid; grid-template-columns: 380px 1fr; min-height: calc(100vh - 56px); }}
    aside {{ border-right: 1px solid var(--line); background: var(--panel); padding: 14px; overflow: auto; }}
    section {{ padding: 16px; overflow: auto; }}
    h1 {{ font-size: 18px; margin: 0; }}
    h2 {{ font-size: 15px; margin: 0 0 10px; }}
    h3 {{ font-size: 14px; margin: 14px 0 8px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 12px; margin-bottom: 12px; }}
    .metric {{ font-size: 24px; font-weight: 700; }}
    .muted {{ color: var(--muted); font-size: 12px; }}
    .row {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .summary-card {{ display: grid; grid-template-columns: minmax(180px, 260px) 1fr auto; gap: 14px; align-items: center; border-left: 4px solid var(--accent); }}
    .summary-count {{ font-size: 30px; line-height: 1; font-weight: 800; }}
    .summary-subtitle {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .severity-strip {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; min-height: 32px; }}
    .severity-chip {{ border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; background: #fff; font-size: 12px; font-weight: 700; }}
    .severity-chip.status-critical, .severity-chip.status-fatal {{ border-color: #fecdca; background: #fff1f0; color: var(--danger); }}
    .severity-chip.status-major, .severity-chip.status-fail, .severity-chip.status-high {{ border-color: #fedf89; background: #fffaeb; color: var(--warn); }}
    .severity-chip.status-minor, .severity-chip.status-warning, .severity-chip.status-medium {{ border-color: #b9e6fe; background: #f0f9ff; color: var(--accent); }}
    .activity-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--ok); display: inline-block; box-shadow: 0 0 0 0 rgba(6, 118, 71, 0.45); animation: pulse 1.8s infinite; }}
    @keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(6, 118, 71, 0.45); }} 70% {{ box-shadow: 0 0 0 8px rgba(6, 118, 71, 0); }} 100% {{ box-shadow: 0 0 0 0 rgba(6, 118, 71, 0); }} }}
    input, select, button {{ font: inherit; height: 34px; border: 1px solid var(--line); border-radius: 5px; padding: 0 8px; background: white; }}
    input, select {{ width: 100%; }}
    button {{ cursor: pointer; background: #eef4fb; color: #103b5f; }}
    button.primary {{ background: var(--accent); color: white; border-color: var(--accent); }}
    button.danger {{ background: #fee4e2; color: var(--danger); }}
    label {{ display: block; font-size: 12px; color: var(--muted); margin: 8px 0 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 7px 6px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .table-scroll {{ max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    .table-scroll table {{ min-width: 760px; }}
    .table-scroll.wide table {{ min-width: 1040px; }}
    .task-cell {{ max-width: 150px; width: 150px; white-space: normal; overflow-wrap: normal; word-break: normal; }}
    .numeric-cell {{ text-align: right; font-variant-numeric: tabular-nums; }}
    tr.run-row {{ cursor: pointer; }}
    tr.run-row:hover {{ background: #f1f5f9; }}
    .link-btn {{ height: 28px; padding: 0 7px; font-size: 12px; }}
    .status-pass, .status-complete {{ color: var(--ok); font-weight: 700; }}
    .status-fail, .status-failed {{ color: var(--danger); font-weight: 700; }}
    .status-running {{ color: var(--accent); font-weight: 700; }}
    .status-warning, .status-pending {{ color: var(--warn); font-weight: 700; }}
    .issue-summary {{ min-width: 260px; max-width: 720px; white-space: normal; }}
    .issue-summary-line {{ line-height: 1.35; overflow-wrap: anywhere; word-break: break-word; }}
    .issue-summary-head {{ white-space: nowrap; }}
    .issue-summary-empty {{ color: var(--muted); }}
    .issue-check-name {{ cursor: help; text-decoration: underline dotted var(--muted); text-underline-offset: 2px; }}
    .path-field {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .copy-path-btn {{ height: 26px; padding: 0 8px; font-size: 12px; }}
    .issues-section {{ padding: 12px; }}
    .issues-section h2 {{ margin-bottom: 10px; }}
    .failure-warning-panel {{ border: 1px solid #f59e0b; border-radius: 6px; background: #fffbeb; padding: 10px; }}
    .failure-warning-title {{ font-weight: 700; color: #92400e; margin-bottom: 8px; }}
    .failure-warning-list {{ max-height: 260px; overflow-y: auto; padding-right: 4px; }}
    .failure-warning-empty {{ color: var(--muted); }}
    .failure-warning-item {{ padding: 8px 0; border-top: 1px solid #fde68a; }}
    .failure-warning-item:first-child {{ border-top: 0; padding-top: 0; }}
    .failure-warning-task {{ font-weight: 700; overflow-wrap: anywhere; }}
    .failure-warning-meta {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
    .failure-warning-range {{ color: #92400e; font-size: 12px; margin-top: 3px; }}
    .failure-warning-issues {{ color: var(--muted); font-size: 12px; margin-top: 3px; overflow-wrap: anywhere; }}
    .resolve-streak-btn {{ height: 26px; padding: 0 8px; font-size: 12px; margin-top: 6px; }}
    .resolve-streak-btn.confirm {{ background: #f59e0b; border-color: #d97706; color: #fff; }}
    .report-preview {{ max-height: 460px; overflow: auto; white-space: pre-wrap; }}
    .failure-warning-more {{ color: var(--muted); font-size: 12px; padding-top: 8px; border-top: 1px solid #fde68a; }}
    .event-summary-section {{ margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--line); }}
    .event-summary-overview {{ margin-top: 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: 6px; background: #f8fafc; }}
    .severity-text {{ font-weight: 700; }}
    .severity-text.status-critical, .severity-text.status-fatal {{ color: var(--danger); }}
    .severity-text.status-major, .severity-text.status-fail, .severity-text.status-high {{ color: var(--warn); }}
    .severity-text.status-minor, .severity-text.status-warning, .severity-text.status-medium {{ color: var(--accent); }}
    #checkNameTooltip {{ position: fixed; z-index: 50; display: none; max-width: 340px; padding: 7px 9px; border-radius: 5px; background: #101828; color: #fff; font-size: 12px; line-height: 1.4; box-shadow: 0 8px 20px rgba(15, 23, 42, 0.25); pointer-events: none; }}
    th.sortable {{ cursor: pointer; user-select: none; white-space: nowrap; }}
    th.sortable:hover {{ color: var(--text); }}
    .sort-arrow {{ display: inline-block; width: 1em; margin-left: 3px; color: var(--accent); }}
    pre {{ background: #101828; color: #e5e7eb; padding: 12px; border-radius: 6px; overflow: auto; max-height: 360px; }}
    .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .modal-backdrop {{ position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: none; align-items: center; justify-content: center; padding: 24px; z-index: 20; }}
    .modal-backdrop.open {{ display: flex; }}
    .modal {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; width: min(1100px, 96vw); max-height: 88vh; overflow: auto; box-shadow: 0 20px 50px rgba(15, 23, 42, 0.25); }}
    .modal.wide {{ width: min(1280px, 96vw); }}
    .modal-header {{ position: sticky; top: 0; background: var(--panel); border-bottom: 1px solid var(--line); padding: 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .modal-body {{ padding: 12px; }}
    @media (max-width: 1000px) {{ main {{ grid-template-columns: 1fr; }} aside {{ border-right: 0; border-bottom: 1px solid var(--line); }} .grid, .two, .summary-card {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
<header>
  <h1>QA Control Dashboard</h1>
  <div id="clock" class="muted"></div>
</header>
<main>
  <aside>
    <div class="panel issues-section">
      <h2>Issues</h2>
      <div class="failure-warning-panel">
        <div id="consecutiveFailureTitle" class="failure-warning-title">⚠️ 0 个组合连续失败</div>
        <div id="consecutiveFailureBody" class="failure-warning-list failure-warning-empty">暂无问题</div>
      </div>
    </div>
    <div class="panel">
      <h2>Start Run</h2>
      <label>Mode</label>
      <select id="mode"><option value="task_folder">Task folder</option><option value="date_range">Date range</option></select>
      <label>Task/root path</label>
      <input id="rootPath" placeholder="/database/verified/...">
      <div class="two">
        <div><label>Date from</label><input id="dateFrom" placeholder="20260601"></div>
        <div><label>Date to</label><input id="dateTo" placeholder="20260623"></div>
      </div>
      <label>Task filter</label>
      <input id="taskFilter" placeholder="optional">
      <div class="two">
        <div><label>Phases</label><input id="phases" value="1,2,3"></div>
        <div><label>Workers</label><input id="workers" type="number" value="2" min="1" max="8"></div>
      </div>
      <div class="two">
        <div><label>Batch size</label><input id="batchSize" type="number" value="100"></div>
        <div><label>Min free mem GB</label><input id="minMem" type="number" value="6"></div>
      </div>
      <label>Quality label filter</label>
      <input id="qualityLabel" value="完全正常">
      <label class="row" style="margin-top:8px">
        <input id="disableQualityLabelFilter" type="checkbox" style="width:auto;height:auto">
        <span>Full audit: ignore quality labels</span>
      </label>
      <label>Run ID</label>
      <input id="runId" placeholder="optional">
      <div class="row" style="margin-top:10px"><button class="primary" onclick="startRun()">Start</button></div>
      <div id="startMsg" class="muted"></div>
    </div>
    <div class="panel">
      <h2>Event Listener</h2>
      <div class="two">
        <div><label>Phases</label><input id="eventPhases" value="1,2,3,7"></div>
        <div><label>Workers</label><input id="eventWorkers" type="number" value="1" min="1" max="8"></div>
      </div>
      <div class="two">
        <div><label>Event batch size</label><input id="eventBatchSize" type="number" value="16" min="1" max="256"></div>
        <div><label>Max load ratio</label><input id="eventMaxLoadRatio" type="number" value="0.75" min="0.1" max="1.5" step="0.05"></div>
      </div>
      <label>Event exact date</label>
      <input id="eventDate" placeholder="YYYYMMDD/today/yesterday, optional">
      <div class="two">
        <div><label>Event date from</label><input id="eventDateFrom" placeholder="YYYYMMDD or today"></div>
        <div><label>Event date to</label><input id="eventDateTo" placeholder="YYYYMMDD or today"></div>
      </div>
      <div class="two">
        <div><label>Min free mem GB</label><input id="eventMinMem" type="number" value="6" min="1" max="64" step="0.1"></div>
        <div><label>Resource wait sec</label><input id="eventResourceWait" type="number" value="300" min="0" max="3600"></div>
      </div>
      <div class="two">
        <div><label>Stability interval sec</label><input id="eventStabilityInterval" type="number" value="3" min="1" max="120"></div>
        <div><label>Stability timeout sec</label><input id="eventStabilityTimeout" type="number" value="90" min="10" max="3600"></div>
      </div>
      <label>Quality label filter</label>
      <input id="eventQualityLabel" value="完全正常">
      <label class="row" style="margin-top:8px">
        <input id="eventDisableQualityLabelFilter" type="checkbox" style="width:auto;height:auto">
        <span>Full audit: ignore quality labels</span>
      </label>
      <div class="row">
        <button onclick="eventAction('start')">Start</button>
        <button onclick="eventAction('restart')">Restart</button>
        <button class="danger" onclick="eventAction('stop')">Stop</button>
      </div>
      <div id="eventBox" style="margin-top:10px"></div>
    </div>
  </aside>
  <section>
    <div class="grid">
      <div class="panel"><div class="muted">Load 1m</div><div id="load1" class="metric">-</div></div>
      <div class="panel">
        <div class="muted">Memory</div>
        <div id="memUsed" class="metric">-</div>
        <div id="memAvail" class="muted">-</div>
      </div>
      <div class="panel"><div class="muted">Event Pending</div><div id="eventPending" class="metric">-</div></div>
      <div class="panel"><div class="muted">Event Done</div><div id="eventDone" class="metric">-</div></div>
    </div>
    <div class="panel summary-card">
      <div>
        <div class="row"><span class="activity-dot"></span><span class="muted">Event issue summary</span></div>
        <div id="eventSummaryCount" class="summary-count">-</div>
        <div id="eventIssueSummaryMeta" class="summary-subtitle">recent event-listener issue jobs</div>
      </div>
      <div>
        <div id="eventSeverityStrip" class="severity-strip"></div>
        <div id="eventSummaryLead" class="summary-subtitle"></div>
        <div id="eventWorkSessionReportBox" class="summary-subtitle" style="margin-top:8px"></div>
      </div>
      <div class="row">
        <button class="primary" onclick="generateEventWorkSessionReport()">生成半日报告</button>
        <button onclick="openEventWorkSessionReport()">打开报告</button>
        <button onclick="openEventSummaryModal()">Summary</button>
      </div>
    </div>
    <div class="panel">
      <div class="row" style="justify-content:space-between">
        <h2>Latest Event Episodes</h2>
        <span class="muted">latest detected episodes from event listener</span>
      </div>
      <div class="table-scroll wide">
        <table>
          <thead><tr>
            <th class="sortable" onclick="setEventJobsSort('id')">ID<span id="eventSort-id" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventJobsSort('status')">Status<span id="eventSort-status" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventJobsSort('qa')">QA<span id="eventSort-qa" class="sort-arrow"></span></th>
            <th class="sortable task-cell" onclick="setEventJobsSort('task')">Task<span id="eventSort-task" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventJobsSort('robot')">Robot<span id="eventSort-robot" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventJobsSort('episode')">Episode<span id="eventSort-episode" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventJobsSort('issues')">Issues<span id="eventSort-issues" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventJobsSort('updated')">Updated<span id="eventSort-updated" class="sort-arrow"></span></th>
            <th></th>
          </tr></thead>
          <tbody id="eventJobsBody"></tbody>
        </table>
      </div>
    </div>
    <div class="panel">
      <h2>Runs</h2>
      <div class="table-scroll"><table><thead><tr><th>Run</th><th>Mode</th><th>Status</th><th>Episodes</th><th>Issues</th><th>Updated</th><th></th></tr></thead><tbody id="runsBody"></tbody></table></div>
    </div>
    <div class="panel">
      <h2>Run Detail</h2>
      <div id="runDetail" class="muted">Select a run.</div>
    </div>
    <div class="panel">
      <h2>Server Processes</h2>
      <div class="table-scroll"><table><thead><tr><th>PID</th><th>CPU</th><th>Mem</th><th>RSS MB</th><th>Command</th></tr></thead><tbody id="procBody"></tbody></table></div>
    </div>
    <div class="panel">
      <h2>Log</h2>
      <pre id="logBox"></pre>
    </div>
  </section>
</main>
<div id="eventJobModal" class="modal-backdrop" onclick="closeEventJobModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h2>Event Episode Detail</h2>
      <button onclick="closeEventJobModal()" class="link-btn">Close</button>
    </div>
    <div id="eventJobDetail" class="modal-body muted">Select an event issue episode.</div>
  </div>
</div>
<div id="eventSummaryModal" class="modal-backdrop" onclick="closeEventSummaryModal(event)">
  <div class="modal wide" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div>
        <h2>Event Issue Summary</h2>
        <div id="eventSummaryModalMeta" class="muted">recent event-listener issue jobs</div>
      </div>
      <button onclick="closeEventSummaryModal()" class="link-btn">Close</button>
    </div>
    <div class="modal-body">
      <div id="eventSummaryModalSeverity" class="severity-strip event-summary-overview"></div>
      <div class="event-summary-section">
        <h3>By Task, Device, Collector</h3>
        <div class="table-scroll wide"><table>
          <thead><tr>
            <th class="sortable task-cell" onclick="setEventContextSummarySort('task')">Task<span id="eventContextSort-task" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventContextSummarySort('robot')">Device<span id="eventContextSort-robot" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventContextSummarySort('operator')">Collector<span id="eventContextSort-operator" class="sort-arrow"></span></th>
            <th class="sortable numeric-cell" onclick="setEventContextSummarySort('episodes')">Episodes<span id="eventContextSort-episodes" class="sort-arrow"></span></th>
            <th class="sortable numeric-cell" onclick="setEventContextSummarySort('findings')">Findings<span id="eventContextSort-findings" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventContextSummarySort('severity')">Severity<span id="eventContextSort-severity" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventContextSummarySort('issues')">Top Issues<span id="eventContextSort-issues" class="sort-arrow"></span></th>
          </tr></thead>
          <tbody id="eventContextSummaryBody"></tbody>
        </table></div>
      </div>
      <div class="event-summary-section">
        <h3>By Issue Type</h3>
        <div class="table-scroll"><table>
          <thead><tr>
            <th class="sortable" onclick="setEventIssueSummarySort('issue')">Issue<span id="eventIssueSort-issue" class="sort-arrow"></span></th>
            <th class="sortable" onclick="setEventIssueSummarySort('severity')">Severity<span id="eventIssueSort-severity" class="sort-arrow"></span></th>
            <th class="sortable numeric-cell" onclick="setEventIssueSummarySort('findings')">Findings<span id="eventIssueSort-findings" class="sort-arrow"></span></th>
            <th class="sortable numeric-cell" onclick="setEventIssueSummarySort('episodes')">Episodes<span id="eventIssueSort-episodes" class="sort-arrow"></span></th>
            <th class="sortable numeric-cell" onclick="setEventIssueSummarySort('contexts')">Task/device/collector groups<span id="eventIssueSort-contexts" class="sort-arrow"></span></th>
          </tr></thead>
          <tbody id="eventIssueSummaryBody"></tbody>
        </table></div>
      </div>
    </div>
  </div>
</div>
<div id="checkNameTooltip" role="tooltip"></div>
<script>
const refreshMs = {refresh};
let selectedRun = null;
let selectedEventJob = null;
let eventJobsSort = {{key: 'id', direction: 'asc'}};
let eventJobsCache = [];
let eventIssueSummaryCache = {{}};
let eventContextSummarySort = {{key: null, direction: 'asc'}};
let eventIssueSummarySort = {{key: null, direction: 'asc'}};
let refreshInFlight = false;
let lastHeavyRefresh = 0;
let eventSettingsInitialized = false;
let pendingResolveButton = null;
let pendingResolveTimer = null;

async function getJson(url) {{
  const r = await fetch(url, {{cache: 'no-store'}});
  return await r.json();
}}

async function postJson(url, body) {{
  const r = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body || {{}})}});
  return await r.json();
}}

function cls(status) {{ return 'status-' + String(status || '').replace(/_/g, '-'); }}
function val(obj, path, fallback='-') {{
  try {{ return path.split('.').reduce((o,k)=>o[k], obj) ?? fallback; }} catch(e) {{ return fallback; }}
}}
function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

async function copyEventPath(button) {{
  const text = button.dataset.copyPath || '';
  const original = button.textContent;
  try {{
    await navigator.clipboard.writeText(text);
    button.textContent = '已复制';
  }} catch (error) {{
    button.textContent = '复制失败';
  }}
  window.setTimeout(() => {{
    button.textContent = original;
  }}, 1500);
}}

function localTimestamp(value) {{
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = number => String(number).padStart(2, '0');
  return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ` +
    `${{pad(date.getHours())}}:${{pad(date.getMinutes())}}:${{pad(date.getSeconds())}}`;
}}

async function refresh() {{
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {{
    const data = await getJson('/api/status');
    document.getElementById('clock').textContent = localTimestamp(data.generated_at);
    renderConsecutiveFailures(data.consecutive_failures || {{}});
    document.getElementById('load1').textContent = Number(val(data,'server.load.1m',0)).toFixed(2);
    document.getElementById('memUsed').textContent =
      val(data,'server.memory.used_gb','-') + ' / ' + val(data,'server.memory.total_gb','-') + ' GB';
    document.getElementById('memAvail').textContent =
      'remaining ' + val(data,'server.memory.available_gb','-') + ' GB, swap used ' + val(data,'server.memory.swap_used_gb','-') + ' GB';
    document.getElementById('eventPending').textContent = val(data,'event_listener.counts.pending',0);
    document.getElementById('eventDone').textContent = val(data,'event_listener.counts.done',0);
    const settings = val(data, 'event_listener.settings', {{}});
    applyEventSettings(settings);
    document.getElementById('eventBox').innerHTML =
      '<div>tmux: <b>' + (val(data,'event_listener.tmux_running',false) ? 'running' : 'stopped') + '</b></div>' +
      '<div>running: ' + val(data,'event_listener.counts.running',0) + '</div>' +
      '<div>pending: ' + val(data,'event_listener.counts.pending',0) + '</div>' +
      '<div>workers: ' + esc(settings.workers || '-') + ', batch: ' + esc(settings.batch_size || '-') + ', phases: ' + esc(settings.phases || '-') + '</div>' +
      '<div>date filter: exact ' + esc(settings.event_date || '-') + ', from ' + esc(settings.event_date_from || '-') + ', to ' + esc(settings.event_date_to || '-') + '</div>' +
      '<div>guard: max load ratio ' + esc(settings.max_load_ratio || '-') + ', min free mem ' + esc(settings.min_free_mem_gb || '-') + ' GB</div>' +
      '<div>output: ' + val(data,'event_listener.output_size','-') + '</div>' +
      '<div class="muted">Changing these values requires Restart if the listener is already running.</div>';
    renderRuns(data.runs || []);
    const now = Date.now();
    if (now - lastHeavyRefresh > Math.max(30000, refreshMs * 4)) {{
      lastHeavyRefresh = now;
      await refreshEventIssueSummary();
    }}
    await refreshEventJobs();
    renderProcesses(val(data,'server.top_processes',[]));
    if (selectedRun) await loadLog(selectedRun);
    else await loadLog('event_listener');
  }} catch (err) {{
    document.getElementById('clock').textContent = 'dashboard refresh failed: ' + err;
  }} finally {{
    refreshInFlight = false;
  }}
}}

function applyEventSettings(settings) {{
  if (eventSettingsInitialized || !settings || !settings.workers) return;
  const mapping = {{
    eventPhases: settings.phases,
    eventWorkers: settings.workers,
    eventBatchSize: settings.batch_size,
    eventDate: settings.event_date,
    eventDateFrom: settings.event_date_from,
    eventDateTo: settings.event_date_to,
    eventMaxLoadRatio: settings.max_load_ratio,
    eventMinMem: settings.min_free_mem_gb,
    eventResourceWait: settings.resource_max_wait_seconds,
    eventStabilityInterval: settings.stability_interval,
    eventStabilityTimeout: settings.stability_timeout
  }};
  for (const [id, value] of Object.entries(mapping)) {{
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== null && value !== '') el.value = value;
  }}
  eventSettingsInitialized = true;
}}

function renderConsecutiveFailures(summary) {{
  const warnings = summary.warnings || [];
  document.getElementById('consecutiveFailureTitle').textContent =
    `⚠️ ${{summary.count || warnings.length || 0}} 个组合连续失败`;
  const body = document.getElementById('consecutiveFailureBody');
  if (!warnings.length) {{
    body.className = 'failure-warning-list failure-warning-empty';
    body.textContent = '暂无连续失败组合';
    return;
  }}
  body.className = 'failure-warning-list';
  body.innerHTML = warnings.map(item => {{
    const operator = item.operator ? ` · ${{esc(item.operator)}}` : '';
    const issueTypes = (item.issue_types || []).join(', ');
    return `<div class="failure-warning-item">
      <div class="failure-warning-task">${{esc(item.task || '-')}}</div>
      <div class="failure-warning-meta">${{esc(item.robot || '-')}}${{operator}}</div>
      <div class="failure-warning-range">${{esc(item.message || '')}}</div>
      <div class="failure-warning-issues">${{esc(issueTypes || 'issue types unavailable')}}</div>
      <button class="resolve-streak-btn"
        data-task="${{esc(item.task || '')}}"
        data-robot="${{esc(item.robot || '')}}"
        data-operator="${{esc(item.operator || '')}}"
        data-episode-start="${{item.start_episode_number}}"
        data-episode-end="${{item.end_episode_number}}"
        onclick="resolveConsecutiveFailureClick(event, this)">标记已解决</button>
    </div>`;
  }}).join('') + (summary.hidden_count ? `<div class="failure-warning-more">+${{summary.hidden_count}} more</div>` : '');
}}

function resetPendingResolveButton() {{
  if (pendingResolveTimer) window.clearTimeout(pendingResolveTimer);
  pendingResolveTimer = null;
  if (pendingResolveButton) {{
    pendingResolveButton.classList.remove('confirm');
    pendingResolveButton.textContent = '标记已解决';
  }}
  pendingResolveButton = null;
}}

async function resolveConsecutiveFailureClick(event, button) {{
  event.stopPropagation();
  if (pendingResolveButton !== button) {{
    resetPendingResolveButton();
    pendingResolveButton = button;
    button.classList.add('confirm');
    button.textContent = '确认解决？';
    pendingResolveTimer = window.setTimeout(resetPendingResolveButton, 3000);
    return;
  }}
  const payload = {{
    task: button.dataset.task || '',
    robot: button.dataset.robot || '',
    operator: button.dataset.operator || '',
    episode_start: Number(button.dataset.episodeStart || 0),
    episode_end: Number(button.dataset.episodeEnd || 0),
  }};
  resetPendingResolveButton();
  const data = await postJson('/api/consecutive-failures/resolve', payload);
  renderConsecutiveFailures(data.consecutive_failures || {{}});
}}

document.addEventListener('click', event => {{
  if (!event.target.closest || !event.target.closest('.resolve-streak-btn')) resetPendingResolveButton();
}});

function renderRuns(runs) {{
  const body = document.getElementById('runsBody');
  body.innerHTML = runs.map(run => {{
    const counts = run.status_counts || {{}};
    return `<tr class="run-row" onclick="loadRun('${{run.run_id}}', true)">
      <td>${{run.run_id}}</td><td>${{run.mode || ''}}</td>
      <td class="${{cls(run.status || val(run,'live_status.status',''))}}">${{run.status || val(run,'live_status.status','')}}</td>
      <td>${{run.episode_count || 0}}</td><td>${{run.finding_count || 0}}</td>
      <td>${{localTimestamp(run.updated_at)}}</td>
      <td><button onclick="event.stopPropagation(); stopRun('${{run.run_id}}')">Stop</button></td>
    </tr>`;
  }}).join('');
}}

async function refreshEventJobs() {{
  const data = await getJson('/api/event-listener/jobs?limit=120');
  eventJobsCache = data.jobs || [];
  renderEventJobs(eventJobsCache);
}}

async function refreshEventIssueSummary() {{
  const data = await getJson('/api/event-listener/issue-summary?limit=500');
  renderEventIssueSummary(data.summary || {{}});
  const reportData = await getJson('/api/event-listener/work-session-report');
  renderEventWorkSessionReport(reportData.report || {{}});
}}

function severityText(counts) {{
  const entries = Object.entries(counts || {{}});
  return entries.length ? entries.map(([name,count]) => `${{esc(name)}}:${{count}}`).join(', ') : '-';
}}

function severityBadge(name, count) {{
  return `<span class="severity-chip ${{cls(name)}}">${{esc(name)}} ${{count}}</span>`;
}}

const CHECK_NAME_TOOLTIPS = {issue_translations_json};
let checkNameTooltipTimer = null;

function checkNameTooltipHtml(checkName) {{
  const name = String(checkName || '');
  const tooltip = CHECK_NAME_TOOLTIPS[name] || '暂无说明';
  return `<span class="issue-check-name" data-tooltip="${{esc(tooltip)}}">${{esc(name)}}</span>`;
}}

function wrapTaskTextHtml(value) {{
  return esc(value || '').replace(/([_/-])/g, '$1<wbr>');
}}

function hideCheckNameTooltip() {{
  if (checkNameTooltipTimer) {{
    clearTimeout(checkNameTooltipTimer);
    checkNameTooltipTimer = null;
  }}
  const tooltip = document.getElementById('checkNameTooltip');
  if (tooltip) tooltip.style.display = 'none';
}}

function positionCheckNameTooltip(target, tooltip) {{
  const rect = target.getBoundingClientRect();
  tooltip.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - tooltip.offsetWidth - 12)) + 'px';
  tooltip.style.top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - tooltip.offsetHeight - 12)) + 'px';
}}

function showCheckNameTooltip(target) {{
  hideCheckNameTooltip();
  checkNameTooltipTimer = setTimeout(() => {{
    const tooltip = document.getElementById('checkNameTooltip');
    if (!tooltip) return;
    tooltip.textContent = target.dataset.tooltip || '暂无说明';
    tooltip.style.display = 'block';
    positionCheckNameTooltip(target, tooltip);
  }}, 120);
}}

function eventIssueSummaryHtml(job) {{
  const issues = job.top_issues || [];
  if (!issues.length) {{
    return `<div class="issue-summary issue-summary-empty">${{job.issue_count || 0}}</div>`;
  }}
  const issueText = issues.map(issue =>
    `<span class="issue-summary-head">${{checkNameTooltipHtml(issue.check_name)}} (${{issue.count || 0}})</span>`
  ).join(', ');
  return `<div class="issue-summary">${{job.issue_count || 0}}: ${{issueText}}</div>`;
}}

function findingDetailFields(details) {{
  if (!details || typeof details !== 'object') return [];
  const fields = [];
  ['modality', 'field', 'sensor', 'channel'].forEach(key => {{
    const value = details[key];
    if (typeof value === 'string' && value) fields.push(value);
  }});
  ['modalities', 'fields', 'sensors', 'channels'].forEach(key => {{
    const value = details[key];
    if (Array.isArray(value)) value.forEach(item => {{
      if (item !== null && item !== undefined && String(item)) fields.push(String(item));
    }});
  }});
  return [...new Set(fields)];
}}

function findingMessageHtml(finding) {{
  const fields = findingDetailFields(finding.details);
  const prefix = fields.length ? `[${{fields.map(field => esc(field)).join(', ')}}] ` : '';
  return prefix + esc(finding.message || '');
}}

function eventJobSortValue(job, key) {{
  if (key === 'id') return Number(job.id || 0);
  if (key === 'issues') return Number(job.issue_count || 0);
  if (key === 'updated') {{
    const timestamp = Date.parse(job.updated_at || '');
    return Number.isNaN(timestamp) ? 0 : timestamp;
  }}
  if (key === 'qa') return String(job.final_status || (job.status === 'done' ? 'complete' : '') || '').toLowerCase();
  if (key === 'episode') return String(job.episode_name || '').toLowerCase();
  return String(job[key] || '').toLowerCase();
}}

function compareEventJobs(left, right) {{
  const leftValue = eventJobSortValue(left, eventJobsSort.key);
  const rightValue = eventJobSortValue(right, eventJobsSort.key);
  let result = 0;
  if (typeof leftValue === 'number' && typeof rightValue === 'number') {{
    result = leftValue - rightValue;
  }} else {{
    result = String(leftValue).localeCompare(String(rightValue), undefined, {{numeric: true, sensitivity: 'base'}});
  }}
  if (result === 0) result = Number(left.id || 0) - Number(right.id || 0);
  return eventJobsSort.direction === 'asc' ? result : -result;
}}

function setEventJobsSort(key) {{
  if (eventJobsSort.key === key) {{
    eventJobsSort.direction = eventJobsSort.direction === 'asc' ? 'desc' : 'asc';
  }} else {{
    eventJobsSort = {{key, direction: 'asc'}};
  }}
  renderEventJobs(eventJobsCache);
}}

function updateEventJobsSortIndicators() {{
  ['id', 'status', 'qa', 'task', 'robot', 'episode', 'issues', 'updated'].forEach(key => {{
    const marker = document.getElementById('eventSort-' + key);
    if (marker) marker.textContent = eventJobsSort.key === key ? (eventJobsSort.direction === 'asc' ? '▲' : '▼') : '';
  }});
}}

function severitySortRank(severity) {{
  const ranks = {{
    critical: 6,
    fatal: 6,
    fail: 5,
    major: 5,
    high: 5,
    warning: 4,
    minor: 4,
    medium: 4,
    low: 3,
    info: 2,
    pass: 2,
    unknown: 1
  }};
  return ranks[String(severity || '').toLowerCase()] || 1;
}}

function maxSeveritySortRank(counts) {{
  return Math.max(0, ...Object.keys(counts || {{}}).map(severitySortRank));
}}

function topIssuesSortText(issues) {{
  return (issues || []).map(issue => `${{issue.check_name || ''}}(${{issue.count || 0}})`).join(', ').toLowerCase();
}}

function compareSummaryValues(leftValue, rightValue) {{
  if (typeof leftValue === 'number' && typeof rightValue === 'number') {{
    return leftValue - rightValue;
  }}
  return String(leftValue).localeCompare(String(rightValue), undefined, {{numeric: true, sensitivity: 'base'}});
}}

function eventContextSummarySortValue(row, key) {{
  if (key === 'episodes') return Number(row.episode_count || 0);
  if (key === 'findings') return Number(row.finding_count || 0);
  if (key === 'severity') return maxSeveritySortRank(row.severity_counts);
  if (key === 'issues') return topIssuesSortText(row.top_issues);
  return String(row[key] || '').toLowerCase();
}}

function eventIssueSummarySortValue(row, key) {{
  if (key === 'findings') return Number(row.finding_count || 0);
  if (key === 'episodes') return Number(row.episode_count || 0);
  if (key === 'contexts') return Number(row.context_count || 0);
  if (key === 'severity') return severitySortRank(row.severity);
  if (key === 'issue') return String(row.check_name || '').toLowerCase();
  return String(row[key] || '').toLowerCase();
}}

function compareEventContextSummaryRows(left, right) {{
  if (!eventContextSummarySort.key) return 0;
  const result = compareSummaryValues(
    eventContextSummarySortValue(left, eventContextSummarySort.key),
    eventContextSummarySortValue(right, eventContextSummarySort.key)
  );
  return eventContextSummarySort.direction === 'asc' ? result : -result;
}}

function compareEventIssueSummaryRows(left, right) {{
  if (!eventIssueSummarySort.key) return 0;
  const result = compareSummaryValues(
    eventIssueSummarySortValue(left, eventIssueSummarySort.key),
    eventIssueSummarySortValue(right, eventIssueSummarySort.key)
  );
  return eventIssueSummarySort.direction === 'asc' ? result : -result;
}}

function setEventContextSummarySort(key) {{
  if (eventContextSummarySort.key === key) {{
    eventContextSummarySort.direction = eventContextSummarySort.direction === 'asc' ? 'desc' : 'asc';
  }} else {{
    eventContextSummarySort = {{key, direction: 'asc'}};
  }}
  renderEventSummaryTables(eventIssueSummaryCache);
}}

function setEventIssueSummarySort(key) {{
  if (eventIssueSummarySort.key === key) {{
    eventIssueSummarySort.direction = eventIssueSummarySort.direction === 'asc' ? 'desc' : 'asc';
  }} else {{
    eventIssueSummarySort = {{key, direction: 'asc'}};
  }}
  renderEventSummaryTables(eventIssueSummaryCache);
}}

function updateEventSummarySortIndicators() {{
  ['task', 'robot', 'operator', 'episodes', 'findings', 'severity', 'issues'].forEach(key => {{
    const marker = document.getElementById('eventContextSort-' + key);
    if (marker) marker.textContent = eventContextSummarySort.key === key ? (eventContextSummarySort.direction === 'asc' ? '▲' : '▼') : '';
  }});
  ['issue', 'severity', 'findings', 'episodes', 'contexts'].forEach(key => {{
    const marker = document.getElementById('eventIssueSort-' + key);
    if (marker) marker.textContent = eventIssueSummarySort.key === key ? (eventIssueSummarySort.direction === 'asc' ? '▲' : '▼') : '';
  }});
}}

function openEventSummaryModal() {{
  document.getElementById('eventSummaryModal').classList.add('open');
}}

function closeEventSummaryModal(event) {{
  if (event && event.target && event.target.id !== 'eventSummaryModal') return;
  document.getElementById('eventSummaryModal').classList.remove('open');
}}

function renderEventIssueSummary(summary) {{
  eventIssueSummaryCache = summary || {{}};
  const meta = `${{summary.issue_episode_count || 0}} issue episodes, ${{summary.finding_count || 0}} findings from ${{summary.job_count || 0}} recent jobs`;
  document.getElementById('eventSummaryCount').textContent = summary.issue_episode_count || 0;
  document.getElementById('eventIssueSummaryMeta').textContent = meta;
  document.getElementById('eventSummaryModalMeta').textContent = meta;
  const sevEntries = Object.entries(summary.severity_counts || {{}});
  const severityHtml =
    sevEntries.length ? sevEntries.map(([name,count]) => severityBadge(name, count)).join('') : '<span class="muted">No event-listener issues found.</span>';
  document.getElementById('eventSeverityStrip').innerHTML = severityHtml;
  document.getElementById('eventSummaryModalSeverity').innerHTML = severityHtml;
  const lead = (summary.by_context || [])[0];
  document.getElementById('eventSummaryLead').textContent = lead
    ? `Top group: ${{lead.task || '-'}} / ${{lead.robot || '-'}} / ${{lead.operator || '-'}} with ${{lead.finding_count || 0}} findings`
    : 'No grouped issue context available yet.';
  renderEventSummaryTables(summary);
}}

function renderEventSummaryTables(summary) {{
  updateEventSummarySortIndicators();
  document.getElementById('eventContextSummaryBody').innerHTML = [...(summary.by_context || [])].sort(compareEventContextSummaryRows).map(row => {{
    const issues = (row.top_issues || []).map(i => `${{esc(i.check_name)}} (${{i.count}})`).join(', ');
    return `<tr>
      <td class="task-cell">${{wrapTaskTextHtml(row.task || '')}}</td>
      <td>${{esc(row.robot || '')}}</td>
      <td>${{esc(row.operator || '')}}</td>
      <td class="numeric-cell">${{row.episode_count || 0}}</td>
      <td class="numeric-cell">${{row.finding_count || 0}}</td>
      <td>${{severityText(row.severity_counts)}}</td>
      <td>${{issues || '-'}}</td>
    </tr>`;
  }}).join('');
  document.getElementById('eventIssueSummaryBody').innerHTML = [...(summary.by_issue || [])].sort(compareEventIssueSummaryRows).map(row =>
    `<tr>
      <td>${{esc(row.check_name || '')}}</td>
      <td class="severity-text ${{cls(row.severity)}}">${{esc(row.severity || '')}}</td>
      <td class="numeric-cell">${{row.finding_count || 0}}</td>
      <td class="numeric-cell">${{row.episode_count || 0}}</td>
      <td class="numeric-cell">${{row.context_count || 0}}</td>
    </tr>`
  ).join('');
}}

function renderEventWorkSessionReport(report) {{
  const box = document.getElementById('eventWorkSessionReportBox');
  if (!box) return;
  if (!report || !report.markdown_path) {{
    box.innerHTML = '半日报告：尚未生成。可点击“生成半日报告”。';
    return;
  }}
  const summary = report.summary || {{}};
  const window = report.window || {{}};
  box.innerHTML =
    '半日报告：' + esc(window.label || '') +
    '，episode ' + (summary.episode_count || 0) +
    '，问题 episode ' + (summary.issue_episode_count || 0) +
    '，<a href="/event-listener/work-session-report.html" target="_blank">查看内容</a>' +
    '，文件：' + esc(report.markdown_path || '');
}}

function openEventWorkSessionReport() {{
  window.open('/event-listener/work-session-report.html', '_blank');
}}

async function generateEventWorkSessionReport() {{
  const box = document.getElementById('eventWorkSessionReportBox');
  if (box) box.textContent = '半日报告：正在生成...';
  const data = await postJson('/api/event-listener/work-session-report', {{
    session: 'current'
  }});
  if (!data.ok) {{
    if (box) box.textContent = '半日报告：生成失败 ' + (data.error || '');
    return;
  }}
  renderEventWorkSessionReport(data.report || {{}});
  openEventWorkSessionReport();
}}

function renderEventJobs(jobs) {{
  const body = document.getElementById('eventJobsBody');
  updateEventJobsSortIndicators();
  body.innerHTML = [...jobs].sort(compareEventJobs).map(job => {{
    const qa = job.final_status || (job.status === 'done' ? 'complete' : '');
    return `<tr>
      <td>${{job.id}}</td>
      <td class="${{cls(job.status)}}">${{esc(job.status)}}</td>
      <td class="${{cls(qa)}}">${{esc(qa || '-')}}</td>
      <td class="task-cell">${{wrapTaskTextHtml(job.task || '')}}</td>
      <td>${{esc(job.robot || '')}}</td>
      <td title="${{esc(job.mounted_path || '')}}">${{esc(job.episode_name || '')}}</td>
      <td>${{eventIssueSummaryHtml(job)}}</td>
      <td>${{localTimestamp(job.updated_at)}}</td>
      <td><button class="link-btn" onclick="loadEventJob(${{job.id}}, true)">Details</button></td>
    </tr>`;
  }}).join('');
}}

function closeEventJobModal(event) {{
  if (event && event.target && event.target.id !== 'eventJobModal') return;
  document.getElementById('eventJobModal').classList.remove('open');
}}

async function loadEventJob(jobId, openModal) {{
  if (openModal) selectedEventJob = jobId;
  const data = await getJson('/api/event-listener/jobs/' + encodeURIComponent(jobId));
  const job = data.job || {{}};
  const findings = job.findings || [];
  const issueRows = findings.length ? findings.map(f =>
    `<tr><td>${{f.phase}}</td><td>${{esc(f.check_name)}}</td><td>${{esc(f.severity)}}</td><td class="${{cls(f.status)}}">${{esc(f.status)}}</td><td>${{findingMessageHtml(f)}}</td></tr>`
  ).join('') : '<tr><td colspan="5" class="muted">No non-pass findings.</td></tr>';
  const phaseStatus = Object.entries(job.phase_status || {{}}).map(([phase,status]) => `P${{phase}}=${{status}}`).join(', ');
  const topIssues = (job.top_issues || []).map(i => `${{esc(i.check_name)}}(${{i.count}})`).join(', ') || 'None';
  const issueSummary = findings.map(f => `${{esc(f.check_name)}}: ${{findingMessageHtml(f)}}`).join('\\n');
  const mountedPath = job.mounted_path || '';
  const copyPath = job.nas_internal_path || mountedPath;
  document.getElementById('eventJobDetail').innerHTML =
    `<div class="row"><b>Job ${{job.id}}</b><span class="${{cls(job.status)}}">${{esc(job.status)}}</span><span class="${{cls(job.final_status)}}">${{esc(job.final_status || '-')}}</span></div>` +
    `<div><b>Episode:</b> ${{esc(job.episode_name || '')}}</div>` +
    `<div class="path-field"><b>Path:</b><span>${{esc(mountedPath)}}</span><button class="copy-path-btn" data-copy-path="${{esc(copyPath)}}" onclick="copyEventPath(this)">复制路径</button></div>` +
    `<div><b>Task:</b> ${{esc(job.task || '')}} | <b>Robot:</b> ${{esc(job.robot || '')}} | <b>Operator:</b> ${{esc(job.operator || '')}}</div>` +
    `<div><b>Phase status:</b> ${{esc(phaseStatus || '-')}}</div>` +
    `<div><b>Top issues:</b> ${{topIssues}}</div>` +
    `<div><b>Output:</b> ${{esc(job.output_dir || '')}}</div>` +
    `<h3>Issue Summary</h3><pre>${{issueSummary || 'No non-pass findings.'}}</pre>` +
    `<h3>Episode Issues</h3>` +
    `<div class="table-scroll"><table><thead><tr><th>Phase</th><th>Check</th><th>Severity</th><th>Status</th><th>Message</th></tr></thead><tbody>${{issueRows}}</tbody></table></div>`;
  if (openModal) {{
    selectedEventJob = jobId;
    document.getElementById('eventJobModal').classList.add('open');
  }}
  await loadLog('event_listener');
}}

function renderProcesses(rows) {{
  document.getElementById('procBody').innerHTML = (rows || []).map(p =>
    `<tr><td>${{p.pid}}</td><td>${{p.cpu}}</td><td>${{p.mem}}</td><td>${{p.rss_mb}}</td><td>${{p.cmd}}</td></tr>`
  ).join('');
}}

async function loadRun(runId, select) {{
  if (select) selectedRun = runId;
  const data = await getJson('/api/runs/' + encodeURIComponent(runId));
  const run = data.run || {{}};
  const counts = run.status_counts || {{}};
  const live = run.live_status || run.run_status || {{}};
  const issueEpisodes = run.issue_episodes || [];
  const report = run.latest_work_session_report || {{}};
  const issueEpisodeRows = issueEpisodes.length ? issueEpisodes.map(ep =>
    `<tr class="run-row"><td>${{esc(ep.episode_name)}}</td><td class="${{cls(ep.final_status)}}">${{esc(ep.final_status)}}</td><td class="task-cell">${{wrapTaskTextHtml(ep.task)}}</td><td>${{esc(ep.robot)}}</td><td>${{ep.issue_count}}</td><td>${{esc((ep.issue_names || []).join(', '))}}</td></tr>`
  ).join('') : '<tr><td colspan="6" class="muted">No issue episodes in this run.</td></tr>';
  document.getElementById('runDetail').innerHTML =
    `<div class="row"><b>${{run.run_id}}</b><span class="${{cls(run.status)}}">${{run.status}}</span></div>` +
    `<div>Output: ${{run.output_dir}}</div>` +
    `<div>DB: ${{run.db_path}}</div>` +
    `<div>Episodes: ${{run.episode_count || 0}} | Issues: ${{run.finding_count || 0}}</div>` +
    `<div>Pass: ${{counts.pass || 0}} Warning: ${{counts.warning || 0}} Fail: ${{counts.fail || 0}} Review: ${{counts.needs_review || 0}}</div>` +
    `<div>Phase: ${{live.current_phase || '-'}} ${{live.current_phase_processed || 0}}/${{live.current_phase_total || 0}}</div>` +
    `<h3>中文质检报告</h3>` +
    `<div class="row">
      <select id="workSessionSelect" style="width:160px">
        <option value="run_all">当前运行累计报告</option>
        <option value="previous">最近结束的工作半日</option>
        <option value="current">当前工作半日</option>
        <option value="forenoon">今天上午</option>
        <option value="afternoon">今天下午</option>
      </select>
      <button class="primary" onclick="generateWorkSessionReport('${{run.run_id}}')">生成/刷新报告</button>
      <span id="workSessionReportMsg" class="muted"></span>
    </div>` +
    renderWorkSessionReport(report) +
    `<h3>Top Issues</h3>` +
    `<div class="table-scroll"><table><tbody>${{(run.top_issues || []).map(i=>`<tr><td>${{i.check_name}}</td><td>${{i.count}}</td></tr>`).join('')}}</tbody></table></div>` +
    `<h3>Episodes With Issues</h3>` +
    `<div class="table-scroll wide"><table><thead><tr><th>Episode</th><th>Status</th><th class="task-cell">Task</th><th>Robot</th><th>Issues</th><th>Issue Names</th></tr></thead><tbody>${{issueEpisodeRows}}</tbody></table></div>`;
  await loadLog(runId);
}}

function renderWorkSessionReport(report) {{
  if (!report || !report.markdown_path) {{
    return '<div class="muted" style="margin-top:8px">还没有生成中文半日报告。</div>';
  }}
  const summary = report.summary || {{}};
  const window = report.window || {{}};
  return `<div style="margin-top:8px">
    <div><b>最新报告：</b>${{esc(window.label || '')}}，${{esc(window.start || '')}} 至 ${{esc(window.end || '')}}</div>
    <div>episode: ${{summary.episode_count || 0}} | 问题 episode: ${{summary.issue_episode_count || 0}} | 影响训练: ${{summary.training_blocking_episode_count || 0}} | 核心问题: ${{report.core_issue_count || 0}}</div>
    <div class="muted">文件：${{esc(report.markdown_path || '')}}</div>
    <pre class="report-preview">${{esc(report.markdown || '')}}</pre>
  </div>`;
}}

async function generateWorkSessionReport(runId) {{
  const msg = document.getElementById('workSessionReportMsg');
  if (msg) msg.textContent = '正在生成...';
  const sessionEl = document.getElementById('workSessionSelect');
  const data = await postJson('/api/runs/' + encodeURIComponent(runId) + '/work-session-report', {{
    session: sessionEl ? sessionEl.value : 'run_all',
    include_all_when_empty: true
  }});
  if (!data.ok) {{
    if (msg) msg.textContent = data.error || '生成失败';
    return;
  }}
  if (msg) msg.textContent = '已生成';
  await loadRun(runId, false);
}}

async function loadLog(target) {{
  const data = await getJson('/api/log-tail?target=' + encodeURIComponent(target));
  document.getElementById('logBox').textContent = (data.lines || []).join('\\n');
}}

async function startRun() {{
  const body = {{
    mode: document.getElementById('mode').value,
    root_path: document.getElementById('rootPath').value,
    date_from: document.getElementById('dateFrom').value,
    date_to: document.getElementById('dateTo').value,
    task_filter: document.getElementById('taskFilter').value,
    phases: document.getElementById('phases').value,
    workers: Number(document.getElementById('workers').value),
    batch_size: Number(document.getElementById('batchSize').value),
    min_free_mem_gb: Number(document.getElementById('minMem').value),
    quality_label: document.getElementById('qualityLabel').value,
    disable_quality_label_filter: document.getElementById('disableQualityLabelFilter').checked,
    run_id: document.getElementById('runId').value
  }};
  const data = await postJson('/api/start', body);
  document.getElementById('startMsg').textContent = data.ok ? ('started ' + data.run_id) : (data.error || 'failed');
  await refresh();
}}

async function stopRun(runId) {{
  await postJson('/api/stop/' + encodeURIComponent(runId), {{}});
  await refresh();
}}

async function eventAction(action) {{
  await postJson('/api/event-listener/' + action, {{
    phases: document.getElementById('eventPhases').value,
    workers: Number(document.getElementById('eventWorkers').value),
    event_batch_size: Number(document.getElementById('eventBatchSize').value),
    event_date: document.getElementById('eventDate').value,
    event_date_from: document.getElementById('eventDateFrom').value,
    event_date_to: document.getElementById('eventDateTo').value,
    max_load_ratio: Number(document.getElementById('eventMaxLoadRatio').value),
    min_free_mem_gb: Number(document.getElementById('eventMinMem').value),
    resource_max_wait_seconds: Number(document.getElementById('eventResourceWait').value),
    stability_interval: Number(document.getElementById('eventStabilityInterval').value),
    stability_timeout: Number(document.getElementById('eventStabilityTimeout').value),
    quality_label: document.getElementById('eventQualityLabel').value,
    disable_quality_label_filter: document.getElementById('eventDisableQualityLabelFilter').checked ? '1' : '0'
  }});
  await refresh();
}}

refresh();
setInterval(refresh, refreshMs);
document.addEventListener('mouseover', event => {{
  const target = event.target.closest?.('.issue-check-name');
  if (target) showCheckNameTooltip(target);
}});
document.addEventListener('mouseout', event => {{
  const target = event.target.closest?.('.issue-check-name');
  if (target && !target.contains(event.relatedTarget)) hideCheckNameTooltip();
}});
document.addEventListener('scroll', hideCheckNameTooltip, true);
</script>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
