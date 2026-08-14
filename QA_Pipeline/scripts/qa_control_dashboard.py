"""Central QA pipeline control dashboard.

This service is intentionally small and self-contained:
- serves a browser UI on one port
- tracks dashboard-started runs in a local SQLite registry
- starts/stops approved pipeline modes through systemd or validated detached processes
- reports server load, event listener queue state, and per-run summaries
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

SCRIPT_DIR_FOR_IMPORT = Path(__file__).resolve().parent
if str(SCRIPT_DIR_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR_FOR_IMPORT))

from generate_work_session_report import (
    DEFAULT_CONFIG as WORK_SESSION_REPORT_CONFIG,
    SEVERITY_ORDER,
    STATUS_ORDER,
    build_cumulative_report as build_cumulative_work_session_report,
    build_report as build_work_session_report,
    collector_id_for_episode,
    core_issue_rows,
    device_failure_summary_rows,
    action_rows,
    affected_episode_rows,
    enrich_rule_fields,
    operator_issue_breakdown_rows,
    operator_issue_detail_rows,
    operator_issue_episode_rows,
    operator_issue_rows,
    load_config as load_work_session_report_config,
    load_rule_explanations,
    rule_explanation_rows,
    issue_title_zh,
    ordered_counter,
    query_all_rows,
    report_directory_name,
    render_markdown_html,
    resolve_window as resolve_work_session_window,
    write_report as write_work_session_report,
)
from scripts.pipeline.issue_messages_zh import finding_presentation_zh
from generate_operator_severe_report import (
    generate_operator_severe_report,
    load_report_config as load_operator_severe_report_config,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT.parent
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANAGER_DIR = PROJECT_ROOT / "outputs" / "dashboard_manager"
DEFAULT_REGISTRY_DB = DEFAULT_MANAGER_DIR / "runs.db"
DEFAULT_ISSUE_HISTORY_DB = DEFAULT_MANAGER_DIR / "issue_history.db"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "dashboard_runs"
DEFAULT_VERIFIED_ROOT = Path("/mnt/nas/database/verified")
DEFAULT_RUN_BACKEND = os.environ.get("DASHBOARD_RUN_BACKEND", "auto")
EVENT_CONTROL_SCRIPT = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "event_listener_control.sh"
EVENT_JOB_DB = PROJECT_ROOT / "outputs" / "event_listener" / "jobs.db"
EVENT_CONTROL_DIR = PROJECT_ROOT / "outputs" / "event_listener" / "control"
EVENT_LISTENER_ENABLED_FILE = EVENT_CONTROL_DIR / "listener.enabled"
EVENT_LISTENER_ERROR_FILE = EVENT_CONTROL_DIR / "listener-last-error.txt"
EVENT_LISTENER_SYSTEMD_SERVICE = os.environ.get(
    "EVENT_LISTENER_SYSTEMD_SERVICE", "datapipeline-event-listener.service"
)
RETENTION_MANIFEST = "retention_cleanup.jsonl"
ISSUE_TRANSLATIONS_PATH = SCRIPT_DIR / "issue_translations.json"
_ISSUE_TRANSLATIONS_CACHE: dict[str, str] | None = None
CONSECUTIVE_FAILURE_STREAK_LENGTH = 5
BAD_QA_FINAL_STATUSES = {"fail", "needs_review"}
WARNING_SIGNATURE_DB_SAMPLE_LIMIT = 500
OPERATOR_SEVERE_REPORT_JSON = "operator_severe_report.json"
OPERATOR_SEVERE_RANKING_CSV = "operator_severe_ranking.csv"
OPERATOR_SEVERE_EVIDENCE_CSV = "operator_severe_evidence.csv"
OPERATOR_SEVERE_FREQUENCY_CSV = "criteria_frequency.csv"
OPERATOR_SEVERE_MARKDOWN = "operator_severe_report.md"


@dataclass(frozen=True)
class PipelineRunRequest:
    argv: list[str]
    settings: dict[str, Any]


@dataclass(frozen=True)
class RunManagerHandle:
    manager: str
    manager_id: str = ""
    manager_pid: int | None = None
    manager_start_ticks: str = ""


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class DashboardState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.port = args.port
        self.registry_db = Path(args.registry_db)
        self.issue_history_db = DEFAULT_ISSUE_HISTORY_DB
        self.output_root = _project_path(args.output_root)
        self.verified_root = _project_path(args.verified_root)
        self.qa_python = _absolute_path(args.qa_python)
        self.run_backend = str(args.run_backend)
        self.refresh_seconds = max(1.0, float(args.refresh_seconds))
        self.max_discovered_runs = max(0, int(args.max_discovered_runs))
        self.auto_work_session_reports = not args.disable_auto_work_session_reports
        self.work_session_report_interval_seconds = max(60, int(args.work_session_report_interval_seconds))
        self.work_session_report_last_run = 0.0
        self.lock = threading.Lock()
        self.cache_lock = threading.RLock()
        self.cache: dict[str, tuple[float, Any]] = {}
        self.failure_warning_signature: tuple[int, int, str, int, int] | None = None
        self.failure_warning_cache: dict[str, Any] = {"count": 0, "warnings": [], "checked_jobs": 0}
        self.failure_warning_last_checked = 0.0
        self.failure_warning_refreshing = False
        init_registry(self.registry_db)
        init_issue_history(self.issue_history_db)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = DashboardState(args)
    start_work_session_report_scheduler(state)
    start_event_listener_supervisor()
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
    parser.add_argument(
        "--run-backend",
        choices=("auto", "systemd", "process"),
        default=DEFAULT_RUN_BACKEND,
        help=(
            "Manager for dashboard-started pipeline runs. auto uses user-systemd "
            "when available and otherwise a validated detached process."
        ),
    )
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
                    query = parse_qs(parsed.query)
                    self._send_html(render_event_work_session_report_html(query.get("report", [""])[0]))
                elif path == "/event-listener/work-session-report-issue.html":
                    self._send_html(render_event_work_session_issue_html(parse_qs(parsed.query)))
                elif path == "/event-listener/device-failure-report.html":
                    query = parse_qs(parsed.query)
                    self._send_html(render_event_device_failure_report_html(query.get("report", [""])[0]))
                elif path == "/api/status":
                    self._send_json(api_status(state))
                elif path == "/api/runs":
                    self._send_json({"runs": list_runs(state)})
                elif path.startswith("/api/runs/") and path.endswith("/issue-drilldown"):
                    run_id = path.split("/")[-2]
                    self._send_json(api_run_issue_drilldown(state, run_id, parse_qs(parsed.query)))
                elif path.startswith("/api/runs/"):
                    run_id = path.rsplit("/", 1)[-1]
                    self._send_json(api_run_detail(state, run_id))
                elif path == "/api/event-listener/status":
                    self._send_json(api_event_listener_status())
                elif path == "/api/event-listener/issue-summary":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["500"])[0] or "500")
                    self._send_json({"summary": event_issue_summary(limit=limit)})
                elif path == "/api/event-listener/work-session-report/issue-detail":
                    self._send_json(api_event_work_session_issue_detail(parse_qs(parsed.query)))
                elif path == "/api/event-listener/work-session-report":
                    self._send_json({"report": latest_event_work_session_report()})
                elif path == "/api/event-listener/jobs":
                    query = parse_qs(parsed.query)
                    issues_only = query.get("issues_only", ["0"])[0] in {"1", "true", "yes"}
                    if "page" in query or "page_size" in query:
                        page = _positive_query_int(query, "page", 1, 1, 10_000_000)
                        page_size = _positive_query_int(query, "page_size", 20, 1, 100)
                        self._send_json(event_listener_jobs_page(page, page_size, issues_only))
                    else:
                        limit = int(query.get("limit", ["100"])[0] or "100")
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
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                try:
                    self._send_json({"error": str(exc)}, status=500)
                except (BrokenPipeError, ConnectionResetError):
                    return

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
                elif path.startswith("/api/runs/") and path.endswith("/operator-severe-report/regenerate"):
                    run_id = path.split("/")[-3]
                    clear_cache(state)
                    self._send_json(api_regenerate_operator_severe_report(state, run_id))
                elif path.startswith("/api/stop/"):
                    run_id = path.rsplit("/", 1)[-1]
                    clear_cache(state)
                    self._send_json(api_stop_run(state, run_id))
                elif path.startswith("/api/resume/"):
                    run_id = path.rsplit("/", 1)[-1]
                    clear_cache(state)
                    self._send_json(api_resume_run(state, run_id))
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
                elif path == "/api/consecutive-failures/recheck":
                    clear_cache(state)
                    self._send_json(api_recheck_consecutive_failure(state, payload))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                try:
                    self._send_json({"error": str(exc)}, status=500)
                except (BrokenPipeError, ConnectionResetError):
                    return

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
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

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
                manager TEXT NOT NULL DEFAULT '',
                manager_id TEXT NOT NULL DEFAULT '',
                manager_pid INTEGER,
                manager_start_ticks TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                phases TEXT,
                workers INTEGER,
                batch_size INTEGER,
                root_path TEXT,
                date_from TEXT,
                date_to TEXT,
                task_filter TEXT,
                operator_filter TEXT NOT NULL DEFAULT '',
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                notes TEXT,
                severe_report_requested INTEGER NOT NULL DEFAULT 0,
                severe_report_config TEXT NOT NULL DEFAULT '',
                continue_after_fail INTEGER NOT NULL DEFAULT 0
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        migrations = {
            "severe_report_requested": "INTEGER NOT NULL DEFAULT 0",
            "severe_report_config": "TEXT NOT NULL DEFAULT ''",
            "continue_after_fail": "INTEGER NOT NULL DEFAULT 0",
            "operator_filter": "TEXT NOT NULL DEFAULT ''",
            "manager": "TEXT NOT NULL DEFAULT ''",
            "manager_id": "TEXT NOT NULL DEFAULT ''",
            "manager_pid": "INTEGER",
            "manager_start_ticks": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, declaration in migrations.items():
            if column_name not in columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column_name} {declaration}")


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
                context_date TEXT NOT NULL DEFAULT '',
                episode_start INTEGER NOT NULL,
                episode_end INTEGER NOT NULL,
                streak_length INTEGER NOT NULL,
                issue_types TEXT NOT NULL,
                collector_ids TEXT NOT NULL DEFAULT '[]',
                issue_locations TEXT NOT NULL DEFAULT '[]',
                episode_status_counts TEXT NOT NULL DEFAULT '{}',
                recheck_requested_at TEXT NOT NULL DEFAULT '',
                recheck_job_ids TEXT NOT NULL DEFAULT '[]',
                detected_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_time_seconds INTEGER,
                resolution_source TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:
            conn.execute("ALTER TABLE consecutive_failure_streaks ADD COLUMN context_date TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        for column_name in ("collector_ids", "issue_locations", "recheck_job_ids"):
            try:
                conn.execute(
                    f"ALTER TABLE consecutive_failure_streaks ADD COLUMN {column_name} TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        try:
            conn.execute(
                "ALTER TABLE consecutive_failure_streaks "
                "ADD COLUMN episode_status_counts TEXT NOT NULL DEFAULT '{}'"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        try:
            conn.execute("ALTER TABLE consecutive_failure_streaks ADD COLUMN recheck_requested_at TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        try:
            conn.execute("ALTER TABLE consecutive_failure_streaks ADD COLUMN resolution_source TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        conn.execute("DROP INDEX IF EXISTS idx_consecutive_failure_unresolved_identity")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_consecutive_failure_unresolved_identity
            ON consecutive_failure_streaks (
                task, robot, operator, context_date, episode_start, episode_end
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
            "run_launcher": run_launcher_status(state),
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
        run["manager"] = run_manager_name(run)
        run["manager_running"] = run_manager_running(run)
        run["tmux_running"] = run["manager"] == "tmux" and run["manager_running"]
        by_key[run["run_id"]] = run
    runs = list(by_key.values())
    runs.sort(key=lambda item: item.get("updated_at") or item.get("mtime") or "", reverse=True)
    return runs[:limit] if limit else runs


def registered_runs(registry_db: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(registry_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT run_id, mode, output_dir, db_path, tmux_session,
                   manager, manager_id, manager_pid, manager_start_ticks, status,
                   phases, workers, batch_size, root_path, date_from, date_to,
                   task_filter, operator_filter, started_at, finished_at, updated_at, notes,
                   severe_report_requested, severe_report_config,
                   continue_after_fail, command
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
            "manager": "",
            "manager_id": "",
            "manager_pid": None,
            "manager_start_ticks": "",
            "manager_running": False,
            "status": "complete",
            "updated_at": mtime_iso(output_dir),
            "mtime": mtime_iso(output_dir),
            "registered": False,
            "severe_report_requested": 0,
            "severe_report_config": "",
            "continue_after_fail": 0,
            "operator_filter": "",
            "command": "",
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
    detail["operator_severe_report"] = load_operator_severe_report(
        output_dir,
        bool(run.get("severe_report_requested", False)),
        str(run.get("status") or ""),
    )
    detail["logs"] = tail_file(output_dir / f"{run_id}_pipeline.log", 80)
    if not detail["logs"]:
        detail["logs"] = tail_file(output_dir / "pipeline.log", 80)
    return {"run": detail}


def api_regenerate_operator_severe_report(
    state: DashboardState,
    run_id: str,
) -> dict[str, Any]:
    """Regenerate a completed run's severe report without changing its QA DB."""
    sync_registered_run_statuses(state)
    run = find_run(state, run_id)
    if not run:
        raise ValueError(f"unknown run_id: {run_id}")
    if str(run.get("status") or "") != "complete":
        raise ValueError("Severe report regeneration is allowed only for completed runs.")
    if not bool(run.get("severe_report_requested", False)):
        raise ValueError("This run did not request an operator severe report.")
    db_path = Path(str(run.get("db_path") or ""))
    output_dir = Path(str(run.get("output_dir") or ""))
    if not db_path.is_file():
        raise ValueError(f"Run database does not exist: {db_path}")
    if not output_dir.is_dir():
        raise ValueError(f"Run output directory does not exist: {output_dir}")
    config_raw = str(run.get("severe_report_config") or "").strip()
    config_path = _project_path(
        config_raw or "QA_Pipeline/configs/operator_severe_report.json"
    )
    load_operator_severe_report_config(config_path)
    paths = generate_operator_severe_report(db_path, output_dir, config_path)
    report = load_operator_severe_report(output_dir, True, "complete")
    if report.get("state") != "generated":
        raise RuntimeError("Regenerated severe report could not be loaded.")
    return {
        "ok": True,
        "run_id": run_id,
        "paths": [str(path) for path in paths],
        "operator_severe_report": report,
    }


def api_run_issue_drilldown(
    state: DashboardState,
    run_id: str,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    """Return one run issue grouped by operator with paginated episode evidence."""
    run = find_run(state, run_id)
    if not run:
        raise ValueError(f"unknown run_id: {run_id}")
    check_name = str(query.get("check_name", [""])[0]).strip()
    if not check_name:
        raise ValueError("check_name is required")
    if len(check_name) > 300:
        raise ValueError("check_name is too long")
    selected_operator = str(query.get("operator", [""])[0]).strip()
    page = _positive_query_int(query, "page", 1, 1, 1_000_000)
    page_size = _positive_query_int(query, "page_size", 20, 1, 100)
    db_path = Path(str(run.get("db_path") or ""))
    if not db_path.is_file():
        raise ValueError(f"Run database does not exist: {db_path}")

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        grouped_rows = conn.execute(
            """
            SELECT e.episode_path, e.date, e.task, e.robot, e.controller,
                   e.operator, e.final_status, COUNT(f.id) AS finding_count
            FROM findings f
            JOIN episodes e ON e.episode_path = f.episode_path
            WHERE f.status != ? AND f.check_name = ?
            GROUP BY e.episode_path, e.date, e.task, e.robot, e.controller,
                     e.operator, e.final_status
            ORDER BY e.episode_path
            """,
            ("pass", check_name),
        ).fetchall()
        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) FROM findings
            WHERE status != ? AND check_name = ? GROUP BY status
            """,
            ("pass", check_name),
        ).fetchall()
        severity_rows = conn.execute(
            """
            SELECT severity, COUNT(*) FROM findings
            WHERE status != ? AND check_name = ? GROUP BY severity
            """,
            ("pass", check_name),
        ).fetchall()

        episodes = []
        operator_groups: dict[str, dict[str, Any]] = {}
        for row in grouped_rows:
            episode_path = str(row["episode_path"] or "")
            operator = _display_operator(row["operator"], episode_path)
            item = {
                "episode_path": episode_path,
                "episode_name": Path(episode_path).name,
                "date": str(row["date"] or _date_from_path(episode_path)),
                "task": str(row["task"] or ""),
                "robot": str(row["robot"] or ""),
                "controller": str(row["controller"] or ""),
                "operator": operator,
                "final_status": str(row["final_status"] or ""),
                "source_finding_count": int(row["finding_count"] or 0),
            }
            episodes.append(item)
            group = operator_groups.setdefault(
                operator,
                {"operator": operator, "finding_count": 0, "affected_episode_count": 0},
            )
            group["finding_count"] += item["source_finding_count"]
            group["affected_episode_count"] += 1

        operators = sorted(
            operator_groups.values(),
            key=lambda item: (
                -int(item["affected_episode_count"]),
                -int(item["finding_count"]),
                str(item["operator"]),
            ),
        )
        filtered = [
            item for item in episodes
            if not selected_operator or item["operator"] == selected_operator
        ]
        filtered.sort(
            key=lambda item: (-int(item["source_finding_count"]), item["episode_path"])
        )
        total = len(filtered)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_rows = filtered[start : start + page_size]
        if page_rows:
            paths = [item["episode_path"] for item in page_rows]
            placeholders = ",".join("?" for _ in paths)
            finding_rows = conn.execute(
                f"""
                SELECT episode_path, phase, severity, status, message, details
                FROM findings
                WHERE status != ? AND check_name = ?
                  AND episode_path IN ({placeholders})
                ORDER BY episode_path, phase, id
                """,
                ("pass", check_name, *paths),
            ).fetchall()
            findings_by_path: dict[str, list[dict[str, Any]]] = {}
            for finding in finding_rows:
                details = json_value(finding["details"], {})
                localized = localized_finding(
                    check_name,
                    str(finding["message"] or ""),
                    details,
                )
                findings_by_path.setdefault(str(finding["episode_path"]), []).append(
                    {
                        "phase": int(finding["phase"] or 0),
                        "severity": str(finding["severity"] or ""),
                        "status": str(finding["status"] or ""),
                        **localized,
                    }
                )
            for item in page_rows:
                item["findings"] = findings_by_path.get(item["episode_path"], [])

    explanation_rows = rule_explanation_rows([check_name], load_rule_explanations())
    return {
        "run_id": run_id,
        "check_name": check_name,
        "selected_operator": selected_operator,
        "summary": {
            "finding_count": sum(int(row["finding_count"] or 0) for row in grouped_rows),
            "affected_episode_count": len(grouped_rows),
            "affected_operator_count": len(operator_groups),
            "status_counts": {str(name): int(count) for name, count in status_rows},
            "severity_counts": {str(name): int(count) for name, count in severity_rows},
        },
        "rule": explanation_rows[0] if explanation_rows else {},
        "operators": operators,
        "episodes": page_rows,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


def _positive_query_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _display_operator(value: Any, episode_path: str) -> str:
    operator = str(value or "").strip()
    if operator and operator.casefold() not in {"unknown", "未填写"}:
        return operator
    parts = Path(episode_path).parts
    for index, part in enumerate(parts[:-1]):
        if len(part) == 8 and part.isdigit() and index + 1 < len(parts):
            candidate = parts[index + 1].strip()
            if candidate and not candidate.startswith("episode_"):
                return candidate
    try:
        metadata = json.loads((Path(episode_path) / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        metadata = {}
    if isinstance(metadata, dict):
        for field in ("operator", "username"):
            candidate = str(metadata.get(field) or "").strip()
            if candidate and candidate.casefold() not in {"unknown", "未填写"}:
                return candidate
    return "(unknown)"


def _date_from_path(episode_path: str) -> str:
    for part in Path(episode_path).parts:
        if len(part) == 8 and part.isdigit():
            return part
    return ""


def find_run(state: DashboardState, run_id: str) -> dict[str, Any] | None:
    for run in list_runs(state):
        if run.get("run_id") == run_id:
            return run
    return None


def registered_run(registry_db: Path, run_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(registry_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def build_pipeline_command(
    state: DashboardState,
    payload: dict[str, Any],
    run_id: str,
    output_dir: Path,
    db_path: Path,
) -> PipelineRunRequest:
    """Validate and normalize a start request without changing external state."""
    mode = str(payload.get("mode") or "").strip()
    if mode not in {"task_folder", "date_range"}:
        raise ValueError("mode must be task_folder or date_range")
    phases = validate_phases(str(payload.get("phases") or "1,2,7"))
    selected_phases = {int(value) for value in phases.split(",")}
    workers = clamp_int(payload.get("workers", 2), 1, 8)
    batch_size = clamp_int(payload.get("batch_size", 100), 1, 10000)
    max_load_ratio = clamp_float(payload.get("max_load_ratio", 0.75), 0.1, 2.0)
    min_free_mem_gb = clamp_float(payload.get("min_free_mem_gb", 6.0), 0.0, 128.0)
    resource_wait = clamp_int(payload.get("resource_max_wait_seconds", 0), 0, 86400)
    overload_action = str(payload.get("overload_action") or "pause").strip()
    if overload_action not in {"pause", "stop"}:
        raise ValueError("overload_action must be pause or stop")
    resource_error_retries = clamp_int(payload.get("resource_error_retries", 3), 0, 100)
    resource_retry_delay = clamp_int(
        payload.get("resource_retry_delay_seconds", 30),
        0,
        86400,
    )
    task_filter = str(payload.get("task_filter") or "").strip()
    operator_filter = str(payload.get("operator_filter") or "").strip()
    date_from = ""
    date_to = ""
    if mode == "task_folder":
        root_path = normalize_verified_path(str(payload.get("root_path") or ""), state.verified_root)
    else:
        root_path = normalize_verified_path(str(state.verified_root), state.verified_root)
        date_from = validate_date(str(payload.get("date_from") or ""), "date_from")
        date_to = validate_date(str(payload.get("date_to") or ""), "date_to")
        if date_from > date_to:
            raise ValueError("date_from must be earlier than or equal to date_to")

    severe_requested = payload_bool(payload.get("generate_operator_severe_report", False))
    continue_after_fail = payload_bool(payload.get("continue_after_fail", False)) or severe_requested
    severe_config = ""
    if severe_requested:
        raw_config = str(
            payload.get("operator_severe_config")
            or "QA_Pipeline/configs/operator_severe_report.json"
        )
        severe_config_path, required_phases = validate_operator_severe_config(raw_config)
        missing_phases = sorted(required_phases - selected_phases)
        if missing_phases:
            raise ValueError(
                "Severe report config requires phases "
                f"{','.join(str(value) for value in sorted(required_phases))}. "
                f"Selected phases: {phases}. "
                f"Missing phases: {','.join(str(value) for value in missing_phases)}."
            )
        severe_config = str(severe_config_path)

    qa_python = _absolute_path(state.qa_python)
    pipeline_script = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "run_pipeline.py"
    argv = [
        str(qa_python),
        str(pipeline_script),
        "--roots",
        root_path,
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
        overload_action,
        "--resource-error-retries",
        str(resource_error_retries),
        "--resource-retry-delay-seconds",
        str(resource_retry_delay),
    ]
    if mode == "date_range":
        argv.extend(["--date-from", date_from, "--date-to", date_to, "--streaming-discovery"])
    if task_filter:
        argv.extend(["--task", task_filter])
    if operator_filter:
        argv.extend(["--operator", operator_filter])
    if "quality_label" in payload:
        argv.extend(["--quality-label", str(payload.get("quality_label") or "完全正常")])
    if payload_bool(payload.get("disable_quality_label_filter", False)):
        argv.append("--disable-quality-label-filter")
    if payload_bool(payload.get("force_rerun", False)):
        argv.append("--force-rerun")
    if severe_requested:
        argv.extend(["--generate-operator-severe-report", "--operator-severe-config", severe_config])
    if continue_after_fail:
        argv.append("--continue-after-fail")

    return PipelineRunRequest(
        argv=argv,
        settings={
            "mode": mode,
            "phases": phases,
            "workers": workers,
            "batch_size": batch_size,
            "root_path": root_path,
            "date_from": date_from,
            "date_to": date_to,
            "task_filter": task_filter,
            "operator_filter": operator_filter,
            "resource_max_wait_seconds": resource_wait,
            "overload_action": overload_action,
            "resource_error_retries": resource_error_retries,
            "resource_retry_delay_seconds": resource_retry_delay,
            "generate_operator_severe_report": severe_requested,
            "operator_severe_config": severe_config,
            "continue_after_fail": continue_after_fail,
        },
    )


def validate_operator_severe_config(raw_path: str) -> tuple[Path, set[int]]:
    value = raw_path.strip()
    if not value:
        raise ValueError("Severe report config path is required")
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve(strict=False)
    if not path.exists():
        raise ValueError(f"Severe report config does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Severe report config must be a regular file: {path}")
    if path.suffix.lower() != ".json":
        raise ValueError(f"Severe report config must have a .json suffix: {path}")
    try:
        config = load_operator_severe_report_config(path)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    enabled = [criterion for criterion in config["criteria"] if criterion.get("enabled", True)]
    if not enabled:
        raise ValueError("Severe report config must contain at least one enabled criterion")
    required_phases = {
        int(phase)
        for criterion in enabled
        for phase in criterion.get("phases", [])
    }
    return path, required_phases


def validate_execution_requirements(
    state: DashboardState,
    selected_root: Path,
    output_dir: Path,
) -> None:
    qa_python = _absolute_path(state.qa_python)
    if not qa_python.exists():
        raise ValueError(f"QA Python does not exist: {qa_python}")
    if not qa_python.is_file():
        raise ValueError(f"QA Python is not a file: {qa_python}")
    if not os.access(qa_python, os.X_OK):
        raise ValueError(f"QA Python is not executable: {qa_python}")
    pipeline_script = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "run_pipeline.py"
    if not pipeline_script.is_file():
        raise ValueError(f"run_pipeline.py does not exist: {pipeline_script}")
    configured_root = Path(state.verified_root).resolve(strict=False)
    if not configured_root.is_dir():
        raise ValueError(f"Configured data root does not exist or is not a directory: {configured_root}")
    if not selected_root.is_dir():
        raise ValueError(f"Selected data root does not exist or is not a directory: {selected_root}")
    parent = output_dir.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError(f"Output root parent is not writable: {parent}")


def payload_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def shell_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def _project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _absolute_path(value: str | Path) -> Path:
    """Return an absolute path without replacing a supplied symlink target."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.absolute()


def api_start_run(state: DashboardState, payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "").strip()
    run_id = sanitize_id(str(payload.get("run_id") or make_run_id(mode)))
    output_dir = state.output_root / run_id
    db_path = output_dir / "qa.db"
    if registered_run(state.registry_db, run_id) is not None:
        raise ValueError(f"run_id already exists: {run_id}")
    request = build_pipeline_command(state, payload, run_id, output_dir, db_path)
    validate_execution_requirements(state, Path(request.settings["root_path"]), output_dir)
    command = request.argv
    settings = request.settings

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{run_id}_pipeline.log"
    command_path = output_dir / "dashboard_command.sh"
    row = {
            "run_id": run_id,
            "mode": settings["mode"],
            "output_dir": str(output_dir),
            "db_path": str(db_path),
            "command": shell_command(command),
            "tmux_session": "",
            "manager": "",
            "manager_id": "",
            "manager_pid": None,
            "manager_start_ticks": "",
            "status": "running",
            "phases": settings["phases"],
            "workers": settings["workers"],
            "batch_size": settings["batch_size"],
            "root_path": settings["root_path"],
            "date_from": settings["date_from"],
            "date_to": settings["date_to"],
            "task_filter": settings["task_filter"],
            "operator_filter": settings["operator_filter"],
            "started_at": now_iso(),
            "finished_at": "",
            "updated_at": now_iso(),
            "notes": "",
            "severe_report_requested": int(settings["generate_operator_severe_report"]),
            "severe_report_config": settings["operator_severe_config"],
            "continue_after_fail": int(settings["continue_after_fail"]),
        }
    handle: RunManagerHandle | None = None
    try:
        write_run_script(command_path, command, log_path)
        handle = launch_dashboard_run(state, run_id, command_path)
        apply_run_manager_handle(row, handle)
    except Exception as exc:
        row["status"] = "failed"
        row["finished_at"] = now_iso()
        row["updated_at"] = now_iso()
        row["notes"] = str(exc)
        record_run(state.registry_db, row)
        raise
    try:
        record_run(state.registry_db, row)
    except Exception:
        if handle is not None:
            stop_run_manager(row)
        raise
    append_run_event(
        state.registry_db,
        run_id,
        "start",
        f"Started with {handle.manager} manager {handle.manager_id or handle.manager_pid}",
    )
    return {
        "ok": True,
        "run_id": run_id,
        "manager": handle.manager,
        "manager_id": handle.manager_id,
        "manager_pid": handle.manager_pid,
        "output_dir": str(output_dir),
        "command": shell_command(command),
    }


def api_stop_run(state: DashboardState, run_id: str) -> dict[str, Any]:
    run = find_run(state, run_id)
    if not run:
        raise ValueError(f"unknown run_id: {run_id}")
    manager = run_manager_name(run)
    stop_run_manager(run)
    update_run_status(state.registry_db, run_id, "stopped", finished=True)
    append_run_event(
        state.registry_db,
        run_id,
        "stop",
        f"Stopped by dashboard using {manager or 'inactive'} manager",
    )
    return {"ok": True, "run_id": run_id, "status": "stopped"}


def api_resume_run(state: DashboardState, run_id: str) -> dict[str, Any]:
    """Resume a dashboard run in place without rerunning completed phases."""
    run = registered_run(state.registry_db, run_id)
    if not run:
        raise ValueError(f"unknown registered run_id: {run_id}")
    if run_manager_running(run):
        raise ValueError(f"run is already active: {run_id}")
    db_path = Path(str(run.get("db_path") or ""))
    output_dir = Path(str(run.get("output_dir") or ""))
    if not db_path.is_file():
        raise ValueError(f"Cannot resume without an existing QA database: {db_path}")
    if not output_dir.is_dir():
        raise ValueError(f"Cannot resume without the existing output directory: {output_dir}")

    command = _resume_pipeline_command(run)
    validate_execution_requirements(state, Path(str(run.get("root_path") or "")), output_dir)
    command_path = output_dir / "dashboard_command.sh"
    log_path = output_dir / f"{run_id}_pipeline.log"
    exit_status_path = output_dir / "dashboard_exit_status.txt"
    exit_status_path.unlink(missing_ok=True)
    write_run_script(command_path, command, log_path)
    handle = launch_dashboard_run(state, run_id, command_path)
    try:
        with sqlite3.connect(state.registry_db) as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = 'running', command = ?, tmux_session = '',
                    manager = ?, manager_id = ?, manager_pid = ?, manager_start_ticks = ?,
                    finished_at = '', updated_at = ?, notes = ''
                WHERE run_id = ?
                """,
                (
                    shell_command(command),
                    handle.manager,
                    handle.manager_id,
                    handle.manager_pid,
                    handle.manager_start_ticks,
                    now_iso(),
                    run_id,
                ),
            )
    except Exception:
        cleanup_row = dict(run)
        apply_run_manager_handle(cleanup_row, handle)
        stop_run_manager(cleanup_row)
        raise
    append_run_event(
        state.registry_db,
        run_id,
        "resume",
        "Resumed existing DB/output; removed --force-rerun so completed phases are skipped",
    )
    return {
        "ok": True,
        "run_id": run_id,
        "status": "running",
        "resumed": True,
        "db_path": str(db_path),
        "output_dir": str(output_dir),
        "command": shell_command(command),
        "manager": handle.manager,
        "manager_id": handle.manager_id,
        "manager_pid": handle.manager_pid,
    }


def _resume_pipeline_command(run: dict[str, Any]) -> list[str]:
    raw_command = str(run.get("command") or "").strip()
    if not raw_command:
        raise ValueError("Run has no saved pipeline command")
    command = shlex.split(raw_command)
    if not command:
        raise ValueError("Run has an empty saved pipeline command")
    expected_script = (PROJECT_ROOT / "QA_Pipeline" / "scripts" / "run_pipeline.py").resolve(
        strict=False
    )
    if len(command) < 2 or Path(command[1]).resolve(strict=False) != expected_script:
        raise ValueError("Saved command is not the configured QA pipeline entry point")
    command = [part for part in command if part != "--force-rerun"]
    _replace_command_option(command, "--db-path", str(run.get("db_path") or ""))
    _replace_command_option(command, "--output-dir", str(run.get("output_dir") or ""))
    _replace_command_option(command, "--run-id", str(run.get("run_id") or ""))
    return command


def _replace_command_option(command: list[str], option: str, value: str) -> None:
    if not value:
        raise ValueError(f"Run is missing the value required for {option}")
    try:
        index = command.index(option)
    except ValueError:
        command.extend([option, value])
        return
    if index + 1 >= len(command):
        raise ValueError(f"Saved command has no value for {option}")
    command[index + 1] = value


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
    report_key = report_directory_name(report)
    report["device_failure_report_url"] = event_report_url(
        "/event-listener/device-failure-report.html",
        report_key,
    )
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
    rule_rows = rule_explanation_rows(
        sorted({str(finding.get("check_name") or "unknown") for finding in finding_rows}),
        load_rule_explanations(),
    )
    rule_by_check = {str(row.get("check_name") or ""): row for row in rule_rows}
    core_issues = core_issue_rows(
        finding_rows,
        config,
        total_episodes=len(episode_rows),
        total_issue_episodes=len(findings_by_episode),
        total_findings=len(finding_rows),
    )
    enrich_rule_fields(core_issues, rule_by_check)
    affected_episodes = affected_episode_rows(episode_rows, findings_by_episode, config)
    operator_issues = operator_issue_rows(
        episode_rows,
        findings_by_episode,
        total_issue_episodes=len(findings_by_episode),
        total_findings=len(finding_rows),
        config=config,
    )
    operator_issue_episodes = operator_issue_episode_rows(episode_rows, findings_by_episode, config)
    enrich_rule_fields(operator_issue_episodes, rule_by_check)
    operator_issue_breakdown = operator_issue_breakdown_rows(episode_rows, findings_by_episode)
    operator_issue_details = operator_issue_detail_rows(episode_rows, findings_by_episode)
    device_failures = device_failure_summary_rows(episode_rows, finding_rows)
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
        "rule_explanations": rule_rows,
        "operator_issues": operator_issues,
        "operator_issue_breakdown": operator_issue_breakdown,
        "operator_issue_episodes": operator_issue_episodes,
        "operator_issue_details": operator_issue_details,
        "device_failure_summary": device_failures,
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


def event_work_session_report_dir(report_key: str = "") -> Path | None:
    reports_root = event_work_session_report_root()
    if not reports_root.is_dir():
        return None
    if report_key:
        if Path(report_key).name != report_key:
            return None
        report_dir = reports_root / report_key
        return report_dir if report_dir.is_dir() else None
    report_dirs = [path for path in reports_root.iterdir() if path.is_dir()]
    report_dirs.sort(key=lambda path: safe_stat(path / "半日质检报告.md")[0], reverse=True)
    return next((path for path in report_dirs if (path / "report.json").is_file()), None)


def latest_event_work_session_report() -> dict[str, Any]:
    report_dir = event_work_session_report_dir()
    return work_session_report_payload(report_dir) if report_dir else {}


def event_work_session_report(report_key: str = "") -> dict[str, Any]:
    report_dir = event_work_session_report_dir(report_key)
    return work_session_report_payload(report_dir) if report_dir else {}


def load_event_work_session_report_snapshot(report_key: str = "") -> tuple[Path, dict[str, Any]]:
    report_dir = event_work_session_report_dir(report_key)
    if not report_dir:
        raise ValueError("找不到指定的半日报告。")
    try:
        report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("半日报告详情文件无法读取。") from exc
    if not isinstance(report, dict):
        raise ValueError("半日报告详情格式无效。")
    return report_dir, report


def api_event_work_session_issue_detail(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return one operator/check issue from the immutable report snapshot."""
    report_key = str(query.get("report", [""])[0]).strip()
    operator = str(query.get("operator", [""])[0]).strip()
    check_name = str(query.get("check_name", [""])[0]).strip()
    if not operator or not check_name:
        raise ValueError("operator 和 check_name 为必填参数。")
    if len(operator) > 300 or len(check_name) > 300:
        raise ValueError("查询参数过长。")
    page = _positive_query_int(query, "page", 1, 1, 1_000_000)
    page_size = _positive_query_int(query, "page_size", 20, 1, 100)
    report_dir, report = load_event_work_session_report_snapshot(report_key)

    detail_level = "完整快照"
    all_details = report.get("operator_issue_details") or []
    matching = [
        row for row in all_details
        if str(row.get("operator") or "未填写") == operator
        and str(row.get("check_name") or "unknown") == check_name
    ]
    if not all_details:
        detail_level = "历史汇总"
        matching = [
            _legacy_operator_issue_detail(row)
            for row in report.get("operator_issue_episodes") or []
            if str(row.get("operator") or "未填写") == operator
            and str(row.get("check_name") or "unknown") == check_name
        ]
    if not matching:
        raise ValueError("该报告中没有找到对应采集人员和问题类型的记录。")
    matching.sort(key=lambda row: str(row.get("episode_path") or ""))

    rule = next(
        (
            row for row in report.get("rule_explanations") or []
            if str(row.get("check_name") or "") == check_name
        ),
        None,
    )
    rule_source = "报告快照"
    if not rule:
        current_rules = rule_explanation_rows([check_name], load_rule_explanations())
        rule = current_rules[0] if current_rules else {}
        rule_source = "当前规则配置（历史报告未保存规则）"
    action = next(
        (
            row for row in report.get("suggested_actions") or []
            if str(row.get("check_name") or "") == check_name
        ),
        {},
    )
    total = len(matching)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_rows = matching[start : start + page_size]
    finding_count = sum(int(row.get("finding_count") or len(row.get("findings") or [])) for row in matching)
    return {
        "report_key": report_dir.name,
        "operator": operator,
        "check_name": check_name,
        "issue_title_zh": str((rule or {}).get("issue_title_zh") or issue_title_zh(check_name)),
        "detail_level": detail_level,
        "rule_source": rule_source,
        "summary": {
            "finding_count": finding_count,
            "affected_episode_count": total,
        },
        "rule": rule or {},
        "action": action,
        "episodes": page_rows,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


def _legacy_operator_issue_detail(row: dict[str, Any]) -> dict[str, Any]:
    """Adapt reports generated before complete per-finding snapshots were added."""
    return {
        "operator": str(row.get("operator") or "未填写"),
        "check_name": str(row.get("check_name") or "unknown"),
        "issue_title_zh": str(row.get("issue_title_zh") or row.get("check_name") or "未知问题"),
        "episode_path": str(row.get("episode_path") or ""),
        "episode_name": str(row.get("episode_name") or Path(str(row.get("episode_path") or "")).name),
        "task": str(row.get("task") or ""),
        "date": str(row.get("date") or ""),
        "robot": str(row.get("robot") or ""),
        "controller": str(row.get("controller") or ""),
        "final_status": str(row.get("final_status") or "pending"),
        "source_db": str(row.get("source_db") or ""),
        "finding_count": int(row.get("finding_count") or 0),
        "findings": [
            {
                "phase": 0,
                "severity": str(row.get("severity_summary") or "unknown"),
                "status": str(row.get("final_status") or "unknown"),
                "occurrence_zh": str(row.get("occurrence_zh") or row.get("messages_zh") or ""),
                "trigger_reason_zh": str(row.get("trigger_reason_zh") or ""),
                "location_zh": str(row.get("locations_zh") or ""),
                "time_range_zh": str(row.get("time_ranges_zh") or ""),
                "evidence_summary_zh": str(row.get("evidence_summary_zh") or row.get("details_summary_zh") or ""),
                "recommended_action_zh": str(row.get("recommended_action_zh") or ""),
                "original_message": str(row.get("messages") or ""),
                "details": {},
            }
        ],
    }


def event_report_url(path: str, report_key: str) -> str:
    return f"{path}?{urlencode({'report': report_key})}"


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


def render_event_work_session_report_html(report_key: str = "") -> str:
    report = event_work_session_report(report_key)
    selected_report_key = str(report.get("report_key") or report_key or "")
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
        detail_url = event_report_url(
            "/event-listener/device-failure-report.html",
            str(report.get("report_key") or ""),
        )
        device_failure_html = render_device_failure_summary_html(
            report.get("device_failure_summary") or [],
            detail_url,
        )
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
            <div><b>{int(summary.get("finding_count") or 0)}</b><span>问题记录</span></div>
            <div><b>{int(summary.get("training_blocking_episode_count") or 0)}</b><span>影响训练</span></div>
          </div>
          {device_failure_html}
          <p class="muted">报告文件：{esc_html(report.get("markdown_path", ""))}</p>
          <p class="muted">附件：{esc_html(report.get("core_issues_csv", ""))} | {esc_html(report.get("rule_explanations_csv", ""))} | {esc_html(report.get("operator_issues_csv", ""))} | {esc_html(report.get("operator_issue_episodes_csv", ""))} | {esc_html(report.get("affected_episodes_csv", ""))} | {esc_html(report.get("actions_csv", ""))}</p>
          {report.get("rendered_html") or render_markdown_html(str(report.get("markdown") or ""))}
        </main>
        """
    report_key_json = json.dumps(selected_report_key, ensure_ascii=False)
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
    .device-summary {{ margin: 18px 0; }}
    .device-summary h2 {{ font-size: 18px; margin: 0 0 10px; }}
    .device-summary-list {{ margin: 0; padding-left: 22px; columns: 2; }}
    .device-summary-list li {{ margin: 6px 18px 6px 0; break-inside: avoid; }}
    .device-summary-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
    .device-summary-head code {{ overflow-wrap: anywhere; }}
    .device-summary-total {{ font-weight: 700; white-space: nowrap; }}
    .device-report-link {{ display: inline-block; margin-top: 10px; font-weight: 700; }}
    .work-session-report {{ background: white; border: 1px solid #d8dde6; border-radius: 6px; padding: 22px; line-height: 1.65; overflow-wrap: anywhere; }}
    .work-session-report h1 {{ font-size: 26px; margin: 0 0 18px; }}
    .work-session-report h2 {{ font-size: 21px; margin: 28px 0 12px; padding-bottom: 7px; border-bottom: 1px solid #e5e7eb; }}
    .work-session-report h3 {{ font-size: 18px; margin: 22px 0 10px; }}
    .work-session-report h4 {{ font-size: 16px; margin: 18px 0 8px; }}
    .work-session-report p {{ margin: 8px 0; }}
    .work-session-report li {{ margin: 5px 0; }}
    .work-session-report a[href^="/event-listener/work-session-report-issue.html"] {{ display: inline-block; margin: 2px 3px; padding: 3px 8px; border: 1px solid #b8d4ea; border-radius: 999px; background: #eef7fd; font-weight: 600; }}
    .work-session-report code {{ background: #f2f4f7; padding: 2px 5px; border-radius: 4px; }}
    .work-session-report blockquote {{ margin: 12px 0; padding: 8px 12px; border-left: 4px solid #1769aa; background: #f5f9fd; }}
    .modal-backdrop {{ position: fixed; inset: 0; z-index: 50; display: none; align-items: center; justify-content: center; padding: 22px; background: rgba(15, 23, 42, .55); }}
    .modal-backdrop.open {{ display: flex; }}
    .issue-modal {{ width: min(1120px, 96vw); max-height: 92vh; overflow: auto; border-radius: 10px; background: white; box-shadow: 0 24px 70px rgba(15,23,42,.3); }}
    .issue-modal-head {{ position: sticky; top: 0; z-index: 1; display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 16px 20px; border-bottom: 1px solid #e5e7eb; background: white; }}
    .issue-modal-head h2 {{ margin: 0 0 4px; font-size: 22px; }}
    .issue-modal-body {{ padding: 18px 20px 24px; }}
    .close-button, .page-button, .copy-button {{ border: 1px solid #cbd5e1; border-radius: 6px; background: white; color: #1b1f24; padding: 7px 11px; cursor: pointer; }}
    .close-button:hover, .page-button:hover, .copy-button:hover {{ background: #f1f5f9; }}
    .close-button:focus, .page-button:focus, .copy-button:focus, .work-session-report a:focus {{ outline: 3px solid #93c5fd; outline-offset: 2px; }}
    .summary-strip {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }}
    .summary-strip span {{ padding: 5px 9px; border-radius: 999px; background: #eef2f7; font-size: 13px; }}
    .explanation-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 18px; }}
    .explanation-card {{ border: 1px solid #d8dde6; border-radius: 8px; padding: 12px; background: #f8fafc; }}
    .explanation-card h3 {{ margin: 0 0 7px; font-size: 16px; }}
    .explanation-card p {{ margin: 5px 0; }}
    .notice {{ margin: 10px 0; padding: 9px 11px; border-left: 4px solid #d97706; background: #fff7ed; }}
    .episode-card {{ margin-top: 12px; border: 1px solid #d8dde6; border-radius: 8px; overflow: hidden; }}
    .episode-card > summary {{ cursor: pointer; padding: 11px 13px; background: #f8fafc; }}
    .episode-body {{ padding: 12px 13px; }}
    .path-row {{ display: flex; align-items: flex-start; gap: 8px; }}
    .path-row code {{ flex: 1; overflow-wrap: anywhere; background: #f2f4f7; padding: 5px; border-radius: 4px; }}
    .finding-card {{ margin-top: 10px; padding: 11px; border-left: 4px solid #1769aa; background: #f7fafc; }}
    .finding-card p {{ margin: 5px 0; }}
    .finding-card pre {{ max-height: 260px; overflow: auto; padding: 10px; border-radius: 5px; background: #111827; color: #e5e7eb; }}
    .pagination {{ display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 16px; flex-wrap: wrap; }}
    .page-button:disabled {{ opacity: .45; cursor: default; }}
    @media (max-width: 760px) {{ .topbar, .metrics {{ display: block; }} .metrics div {{ margin-bottom: 10px; }} .device-summary-list {{ columns: 1; }} .modal-backdrop {{ padding: 0; }} .issue-modal {{ width: 100vw; max-height: 100vh; min-height: 100vh; border-radius: 0; }} .explanation-grid {{ grid-template-columns: 1fr; }} .path-row {{ display: block; }} .copy-button {{ margin-top: 8px; }} }}
  </style>
</head>
<body>{body}
<div id="operatorIssueModal" class="modal-backdrop" aria-hidden="true">
  <section class="issue-modal" role="dialog" aria-modal="true" aria-labelledby="operatorIssueTitle">
    <header class="issue-modal-head">
      <div><h2 id="operatorIssueTitle">问题详情</h2><div id="operatorIssueMeta" class="muted"></div></div>
      <button id="operatorIssueClose" class="close-button" type="button">关闭</button>
    </header>
    <div id="operatorIssueBody" class="issue-modal-body"><p class="muted">正在加载问题详情……</p></div>
  </section>
</div>
<script>
const selectedReportKey = {report_key_json};
const issueState = {{report: selectedReportKey, operator: '', checkName: '', page: 1, pageSize: 20}};
let issueReturnFocus = null;
const issueModal = document.getElementById('operatorIssueModal');
const issueBody = document.getElementById('operatorIssueBody');
const issueTitle = document.getElementById('operatorIssueTitle');
const issueMeta = document.getElementById('operatorIssueMeta');
const esc = value => String(value == null ? '' : value)
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const qaLabel = value => ({{
  critical: '严重', major: '主要', minor: '次要', info: '提示',
  fail: '不通过', needs_review: '需要复查', warning: '警告',
  pass: '通过', pending: '待处理', unknown: '未知'
}}[String(value || '').toLowerCase()] || String(value || '未知'));

document.querySelectorAll('.work-session-report a[href^="/event-listener/work-session-report-issue.html"]').forEach(link => {{
  link.addEventListener('click', event => {{
    event.preventDefault();
    const url = new URL(link.href, window.location.href);
    openOperatorIssue(
      url.searchParams.get('operator') || '',
      url.searchParams.get('check_name') || '',
      url.searchParams.get('report') || selectedReportKey,
      link
    );
  }});
}});
document.getElementById('operatorIssueClose').addEventListener('click', closeOperatorIssue);
issueModal.addEventListener('click', event => {{ if (event.target === issueModal) closeOperatorIssue(); }});
document.addEventListener('keydown', event => {{
  if (!issueModal.classList.contains('open')) return;
  if (event.key === 'Escape') {{ event.preventDefault(); closeOperatorIssue(); return; }}
  if (event.key !== 'Tab') return;
  const focusable = [...issueModal.querySelectorAll('button:not([disabled]), a[href], summary')];
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {{ event.preventDefault(); last.focus(); }}
  else if (!event.shiftKey && document.activeElement === last) {{ event.preventDefault(); first.focus(); }}
}});

function openOperatorIssue(operator, checkName, report, trigger) {{
  issueState.report = report || selectedReportKey;
  issueState.operator = operator;
  issueState.checkName = checkName;
  issueState.page = 1;
  issueReturnFocus = trigger || document.activeElement;
  issueModal.classList.add('open');
  issueModal.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  document.getElementById('operatorIssueClose').focus();
  loadOperatorIssue();
}}
function closeOperatorIssue() {{
  issueModal.classList.remove('open');
  issueModal.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (issueReturnFocus && issueReturnFocus.focus) issueReturnFocus.focus();
}}
async function changeIssuePage(delta) {{
  issueState.page = Math.max(1, issueState.page + Number(delta || 0));
  await loadOperatorIssue();
  issueModal.querySelector('.issue-modal').scrollTop = 0;
}}
async function loadOperatorIssue() {{
  issueBody.innerHTML = '<p class="muted">正在加载问题说明、判定规则和 episode 证据……</p>';
  const query = new URLSearchParams({{
    report: issueState.report, operator: issueState.operator, check_name: issueState.checkName,
    page: String(issueState.page), page_size: String(issueState.pageSize)
  }});
  try {{
    const response = await fetch('/api/event-listener/work-session-report/issue-detail?' + query.toString(), {{cache: 'no-store'}});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '问题详情请求失败。');
    renderOperatorIssue(data);
  }} catch (error) {{
    issueBody.innerHTML = `<div class="notice">无法加载问题详情：${{esc(error.message || error)}}。请稍后重试，或点击原链接进入独立详情页。</div>`;
  }}
}}
function renderOperatorIssue(data) {{
  const rule = data.rule || {{}}, action = data.action || {{}}, summary = data.summary || {{}};
  const pagination = data.pagination || {{page: 1, total_pages: 1, total: 0}};
  issueState.page = Number(pagination.page || 1);
  issueTitle.textContent = data.issue_title_zh || data.check_name || '问题详情';
  issueMeta.textContent = `${{data.operator}} · ${{summary.finding_count || 0}} 条问题记录 · ${{summary.affected_episode_count || 0}} 个问题 episode`;
  const fallbackNotice = data.detail_level === '历史汇总'
    ? '<div class="notice">这是旧版历史报告，仅保存了汇总证据；重新生成报告后可查看逐条检测记录的完整快照。</div>' : '';
  const ruleSourceNotice = String(data.rule_source || '').startsWith('当前规则')
    ? `<div class="notice">${{esc(data.rule_source)}}，可能与该历史报告生成时的配置不同。</div>` : '';
  const ruleHtml = `<div class="explanation-grid">
    <section class="explanation-card"><h3>这个问题是什么意思</h3><p>${{esc(rule.description_zh || '该检测项返回了非通过结果，请结合下方实际证据确认。')}}</p></section>
    <section class="explanation-card"><h3>系统如何判定</h3><p>${{esc(rule.standard_zh || '系统根据检测结果和配置阈值判断。')}}</p><p><b>检测阈值：</b>${{esc(rule.threshold_summary || '该规则没有单独的数值阈值，请以下方检测证据为准。')}}</p><p><b>等级判定：</b>${{esc(rule.severity_rule_zh || '见每条检测记录的状态和严重程度。')}}</p></section>
    <section class="explanation-card"><h3>为什么需要关注</h3><p>${{esc(action.impact || '该问题可能影响数据质量或训练可用性，需要复查。')}}</p></section>
    <section class="explanation-card"><h3>建议如何处理</h3><p>${{esc(action.suggested_action || firstRecommendedAction(data.episodes) || '请根据下方证据复查对应 episode。')}}</p></section>
  </div>`;
  const episodesHtml = (data.episodes || []).map((episode, index) => episodeHtml(episode, index === 0)).join('')
    || '<p class="muted">没有找到符合条件的 episode。</p>';
  issueBody.innerHTML = fallbackNotice + ruleSourceNotice +
    `<div class="summary-strip"><span>采集人员：${{esc(data.operator)}}</span><span>问题记录：${{summary.finding_count || 0}} 条</span><span>问题 episode：${{summary.affected_episode_count || 0}} 个</span></div>` +
    ruleHtml + '<h3>受影响的 episode 与检测证据</h3>' + episodesHtml +
    `<div class="pagination"><span class="muted">第 ${{pagination.page || 1}} / ${{pagination.total_pages || 1}} 页，共 ${{pagination.total || 0}} 个 episode</span>` +
    `<button class="page-button" type="button" data-page-delta="-1" ${{Number(pagination.page || 1) <= 1 ? 'disabled' : ''}}>上一页</button>` +
    `<button class="page-button" type="button" data-page-delta="1" ${{Number(pagination.page || 1) >= Number(pagination.total_pages || 1) ? 'disabled' : ''}}>下一页</button></div>`;
  issueBody.querySelectorAll('[data-page-delta]').forEach(button => button.addEventListener('click', () => changeIssuePage(button.dataset.pageDelta)));
  issueBody.querySelectorAll('[data-copy-path]').forEach(button => button.addEventListener('click', async () => {{
    try {{ await navigator.clipboard.writeText(button.dataset.copyPath || ''); button.textContent = '已复制'; }}
    catch (error) {{ button.textContent = '复制失败，请手动选择路径'; }}
  }}));
}}
function firstRecommendedAction(episodes) {{
  for (const episode of episodes || []) for (const finding of episode.findings || [])
    if (finding.recommended_action_zh) return finding.recommended_action_zh;
  return '';
}}
function episodeHtml(episode, open) {{
  const findings = (episode.findings || []).map((finding, index) => `<section class="finding-card">
    <p><b>检测记录 ${{index + 1}}</b> · 阶段 ${{Number(finding.phase || 0)}} · ${{esc(qaLabel(finding.severity))}} / ${{esc(qaLabel(finding.status))}}</p>
    <p><b>发生情况：</b>${{esc(finding.occurrence_zh || '该检测项返回非通过状态。')}}</p>
    <p><b>为什么报告：</b>${{esc(finding.trigger_reason_zh || '检测结果达到该规则的报告条件。')}}</p>
    <p><b>问题位置：</b>${{esc(finding.location_zh || '整个 episode 或未记录更细位置')}}${{finding.time_range_zh ? `；<b>发生区间：</b>${{esc(finding.time_range_zh)}}` : ''}}</p>
    <p><b>检测证据：</b>${{esc(finding.evidence_summary_zh || '该检测记录未保存额外证据摘要。')}}</p>
    <details><summary>查看原始技术信息</summary><p><b>原始消息：</b>${{esc(finding.original_message || '无')}}</p><pre>${{esc(JSON.stringify(finding.details || {{}}, null, 2))}}</pre></details>
  </section>`).join('');
  return `<details class="episode-card" ${{open ? 'open' : ''}}><summary><b>${{esc(episode.episode_name || episode.episode_path)}}</b> · ${{Number(episode.finding_count || (episode.findings || []).length)}} 条检测记录 · 最终状态：${{esc(qaLabel(episode.final_status))}}</summary>
    <div class="episode-body"><div class="path-row"><code>${{esc(episode.episode_path)}}</code><button class="copy-button" type="button" data-copy-path="${{esc(episode.episode_path)}}">复制路径</button></div>
    <p class="muted">日期：${{esc(episode.date || '未填写')}} · 任务：${{esc(episode.task || '未填写')}} · 机器人：${{esc(episode.robot || '未填写')}} · 控制器：${{esc(episode.controller || '未填写')}}</p>${{findings}}</div>
  </details>`;
}}
</script>
</body>
</html>"""


def render_event_work_session_issue_html(query: dict[str, list[str]]) -> str:
    """Server-render the same issue evidence as a robust link fallback."""
    try:
        data = api_event_work_session_issue_detail(query)
        rule = data.get("rule") or {}
        action = data.get("action") or {}
        summary = data.get("summary") or {}
        pagination = data.get("pagination") or {}
        notices = []
        if data.get("detail_level") == "历史汇总":
            notices.append("这是旧版历史报告，仅保存了汇总证据；重新生成后可查看逐条检测记录的完整快照。")
        if str(data.get("rule_source") or "").startswith("当前规则"):
            notices.append(f"{data.get('rule_source')}，可能与该历史报告生成时的配置不同。")
        notices_html = "".join(f'<div class="notice">{esc_html(text)}</div>' for text in notices)
        episode_html = "".join(_render_issue_episode_html(row) for row in data.get("episodes") or [])
        if not episode_html:
            episode_html = '<p class="muted">没有找到符合条件的 episode。</p>'
        page = int(pagination.get("page") or 1)
        total_pages = int(pagination.get("total_pages") or 1)
        base_params = {
            "report": str(data.get("report_key") or ""),
            "operator": str(data.get("operator") or ""),
            "check_name": str(data.get("check_name") or ""),
            "page_size": str(pagination.get("page_size") or 20),
        }
        previous = ""
        following = ""
        if page > 1:
            previous = (
                '<a class="button" href="/event-listener/work-session-report-issue.html?'
                + urlencode({**base_params, "page": page - 1})
                + '">上一页</a>'
            )
        if page < total_pages:
            following = (
                '<a class="button" href="/event-listener/work-session-report-issue.html?'
                + urlencode({**base_params, "page": page + 1})
                + '">下一页</a>'
            )
        back_url = event_report_url(
            "/event-listener/work-session-report.html",
            str(data.get("report_key") or ""),
        )
        content = f"""
        <div class="top"><div><h1>{esc_html(data.get("issue_title_zh"))}</h1>
          <p class="muted">{esc_html(data.get("operator"))} · {int(summary.get("finding_count") or 0)} 条问题记录 · {int(summary.get("affected_episode_count") or 0)} 个问题 episode</p>
        </div><a href="{esc_html(back_url)}">返回半日报告</a></div>
        {notices_html}
        <div class="grid">
          <section><h2>这个问题是什么意思</h2><p>{esc_html(rule.get("description_zh") or "该检测项返回了非通过结果，请结合下方证据确认。")}</p></section>
          <section><h2>系统如何判定</h2><p>{esc_html(rule.get("standard_zh") or "系统根据检测结果和配置阈值判断。")}</p><p><b>检测阈值：</b>{esc_html(rule.get("threshold_summary") or "无单独数值阈值，请以下方证据为准。")}</p></section>
          <section><h2>为什么需要关注</h2><p>{esc_html(action.get("impact") or "该问题可能影响数据质量或训练可用性，需要复查。")}</p></section>
          <section><h2>建议如何处理</h2><p>{esc_html(action.get("suggested_action") or "请根据下方证据复查对应 episode。")}</p></section>
        </div>
        <h2>受影响的 episode 与检测证据</h2>
        {episode_html}
        <nav class="pagination"><span>第 {page} / {total_pages} 页，共 {int(pagination.get("total") or 0)} 个 episode</span>{previous}{following}</nav>
        """
    except Exception as exc:
        content = f'<h1>问题详情无法显示</h1><div class="notice">{esc_html(exc)}</div><p><a href="/event-listener/work-session-report.html">返回半日报告</a></p>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>采集人员问题详情</title><style>
body{{margin:0;background:#f6f7f9;color:#1b1f24;font-family:Arial,"Microsoft YaHei",sans-serif;line-height:1.65}}
main{{max-width:1120px;margin:auto;padding:24px}}a{{color:#1769aa;text-decoration:none}}h1{{margin:0}}h2{{font-size:17px}}
.top{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}}.muted{{color:#667085;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0}}.grid section,.episode{{background:white;border:1px solid #d8dde6;border-radius:8px;padding:12px}}
.notice{{margin:12px 0;padding:9px 11px;border-left:4px solid #d97706;background:#fff7ed}}.episode{{margin-top:12px}}.finding{{margin-top:9px;padding:10px;border-left:4px solid #1769aa;background:#f7fafc}}
code{{overflow-wrap:anywhere}}pre{{overflow:auto;max-height:280px;padding:10px;background:#111827;color:#e5e7eb;border-radius:5px}}.pagination{{display:flex;justify-content:flex-end;align-items:center;gap:9px;margin-top:16px;flex-wrap:wrap}}
.button{{padding:6px 10px;border:1px solid #cbd5e1;border-radius:6px;background:white}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.top{{display:block}}}}
</style></head><body><main>{content}</main></body></html>"""


def _render_issue_episode_html(episode: dict[str, Any]) -> str:
    finding_html = []
    for index, finding in enumerate(episode.get("findings") or [], start=1):
        raw_json = json.dumps(finding.get("details") or {}, ensure_ascii=False, indent=2)
        location = str(finding.get("location_zh") or "整个 episode 或未记录更细位置")
        time_range = str(finding.get("time_range_zh") or "")
        if time_range:
            location += f"；发生区间：{time_range}"
        finding_html.append(
            f"""<section class="finding">
            <b>检测记录 {index}</b> · 阶段 {int(finding.get("phase") or 0)} · {esc_html(_qa_label_zh(finding.get("severity")))} / {esc_html(_qa_label_zh(finding.get("status")))}
            <p><b>发生情况：</b>{esc_html(finding.get("occurrence_zh") or "该检测项返回非通过状态。")}</p>
            <p><b>为什么报告：</b>{esc_html(finding.get("trigger_reason_zh") or "检测结果达到该规则的报告条件。")}</p>
            <p><b>问题位置：</b>{esc_html(location)}</p>
            <p><b>检测证据：</b>{esc_html(finding.get("evidence_summary_zh") or "该检测记录未保存额外证据摘要。")}</p>
            <details><summary>查看原始技术信息</summary><p>{esc_html(finding.get("original_message") or "无")}</p><pre>{esc_html(raw_json)}</pre></details>
            </section>"""
        )
    return f"""<details class="episode"><summary><b>{esc_html(episode.get("episode_name") or episode.get("episode_path"))}</b> · {int(episode.get("finding_count") or len(finding_html))} 条检测记录 · 最终状态：{esc_html(_qa_label_zh(episode.get("final_status")))}</summary>
    <p><code>{esc_html(episode.get("episode_path"))}</code></p>
    <p class="muted">日期：{esc_html(episode.get("date") or "未填写")} · 任务：{esc_html(episode.get("task") or "未填写")} · 机器人：{esc_html(episode.get("robot") or "未填写")} · 控制器：{esc_html(episode.get("controller") or "未填写")}</p>
    {''.join(finding_html)}</details>"""


def _qa_label_zh(value: Any) -> str:
    key = str(value or "").strip().lower()
    return {
        "critical": "严重",
        "major": "主要",
        "minor": "次要",
        "info": "提示",
        "fail": "不通过",
        "needs_review": "需要复查",
        "warning": "警告",
        "pass": "通过",
        "pending": "待处理",
        "unknown": "未知",
    }.get(key, str(value or "未知"))


def render_device_failure_summary_html(rows: list[dict[str, Any]], detail_url: str) -> str:
    if not rows:
        content = '<p class="muted">本时段暂无设备级失败或待复查问题记录。</p>'
    else:
        items = "".join(
            f"<li><code>{esc_html(row.get('collector_id', 'unknown_collector'))}</code>："
            f"<b>{int(row.get('finding_count') or 0)}</b> 条</li>"
            for row in rows
        )
        content = f'<ol class="device-summary-list">{items}</ol>'
    return (
        '<section class="device-summary"><h2>设备故障统计</h2>'
        f"{content}"
        f'<a class="device-report-link" href="{esc_html(detail_url)}">设备故障统计</a></section>'
    )


def render_device_failure_detail_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="muted">本时段暂无设备级失败或待复查 finding。</p>'
    items = []
    for row in rows:
        check_text = "，".join(
            f"{esc_html(item.get('issue_title_zh') or item.get('check_name', 'unknown'))} "
            f"<code>{esc_html(item.get('check_name', 'unknown'))}</code> "
            f"{int(item.get('finding_count') or 0)} 条 ({float(item.get('percent') or 0):.1f}%)"
            for item in (row.get("check_name_counts_zh") or row.get("check_name_counts") or [])
        )
        risk_html = ""
        risk_class = " risk" if row.get("hardware_issue_signal") else ""
        if row.get("hardware_issue_signal"):
            risk_html = (
                '<div class="device-risk">重点设备风险：'
                f"{esc_html(row.get('dominant_issue_title_zh') or row.get('dominant_check_name', 'unknown'))} 占 "
                f"{float(row.get('dominant_percent') or 0):.1f}%，建议优先检查硬件或连接状态。</div>"
            )
        items.append(
            f'<div class="device-summary-item{risk_class}">'
            '<div class="device-summary-head">'
            f"<code>{esc_html(row.get('collector_id', 'unknown_collector'))}</code>"
            f'<span class="device-summary-total">{int(row.get("finding_count") or 0)} 条</span>'
            "</div>"
            f'<div class="device-summary-checks">{check_text}</div>'
            f"{risk_html}</div>"
        )
    return (
        '<div class="device-summary-list">'
        f'{"".join(items)}</div>'
    )


def render_event_device_failure_report_html(report_key: str = "") -> str:
    report = event_work_session_report(report_key)
    if not report:
        body = """
        <main>
          <h1>设备故障统计</h1>
          <p class="muted">未找到指定报告。</p>
          <p><a href="/event-listener/work-session-report.html">返回半日报告</a></p>
        </main>
        """
    else:
        window = report.get("window") or {}
        selected_key = str(report.get("report_key") or "")
        back_url = event_report_url("/event-listener/work-session-report.html", selected_key)
        detail_html = render_device_failure_detail_html(report.get("device_failure_summary") or [])
        body = f"""
        <main>
          <div class="topbar">
            <div>
              <h1>设备故障统计</h1>
              <p class="muted">{esc_html(window.get("start", ""))} 至 {esc_html(window.get("end", ""))}</p>
            </div>
            <a href="{esc_html(back_url)}">返回半日报告</a>
          </div>
          <p class="muted">仅统计 fail / needs_review finding，全部设备按问题总数降序排列。</p>
          {detail_html}
        </main>
        """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>设备故障统计</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: #f6f7f9; color: #1b1f24; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    a {{ color: #1769aa; text-decoration: none; }}
    code {{ overflow-wrap: anywhere; }}
    .muted {{ color: #667085; font-size: 13px; }}
    .topbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 16px; }}
    .device-summary-list {{ display: grid; gap: 8px; margin-top: 14px; }}
    .device-summary-item {{ background: white; border: 1px solid #d8dde6; border-radius: 6px; padding: 12px; }}
    .device-summary-item.risk {{ border-color: #d92d20; background: #fff7f6; }}
    .device-summary-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
    .device-summary-total {{ font-weight: 700; white-space: nowrap; }}
    .device-summary-checks {{ margin-top: 7px; color: #475467; font-size: 13px; line-height: 1.6; }}
    .device-risk {{ margin-top: 7px; color: #b42318; font-size: 13px; font-weight: 700; }}
    @media (max-width: 760px) {{ .topbar {{ display: block; }} }}
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


def event_listener_process_info() -> tuple[int | None, list[str]]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None, []
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
        if any("Werkzeuge/listen_episode_verified.py" in arg for arg in args) and "serve" in args:
            return int(path.name), args
    return None, []


def event_listener_is_running() -> bool:
    pid, _ = event_listener_process_info()
    return (
        pid is not None
        or tmux_session_running("qa_event_listener")
        or event_listener_systemd_running()
    )


def event_listener_systemd_running() -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", EVENT_LISTENER_SYSTEMD_SERVICE):
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", EVENT_LISTENER_SYSTEMD_SERVICE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _record_event_listener_start_result(completed: subprocess.CompletedProcess[str]) -> None:
    try:
        if completed.returncode == 0:
            EVENT_LISTENER_ERROR_FILE.unlink(missing_ok=True)
            return
        EVENT_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        message = (completed.stdout or "实时检测服务启动失败，命令未返回详细信息。")[-4000:]
        EVENT_LISTENER_ERROR_FILE.write_text(message, encoding="utf-8")
    except OSError as exc:
        print(f"无法更新实时检测服务的错误状态：{exc}", flush=True)


def start_event_listener_supervisor() -> None:
    """Restart an intentionally enabled listener after crashes or host reboots."""

    def worker() -> None:
        time.sleep(5)
        retry_seconds = 15
        while True:
            try:
                if EVENT_LISTENER_ENABLED_FILE.is_file() and not event_listener_is_running():
                    env = os.environ.copy()
                    env["EVENT_LISTENER_USE_SAVED_SETTINGS"] = "1"
                    completed = subprocess.run(
                        [str(EVENT_CONTROL_SCRIPT), "start"],
                        cwd=PROJECT_ROOT,
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    _record_event_listener_start_result(completed)
                    if completed.returncode == 0:
                        print("实时检测服务托管器已恢复 listener。", flush=True)
                        retry_seconds = 15
                    else:
                        print(
                            f"实时检测服务启动失败（{retry_seconds} 秒后重试）："
                            f"{(completed.stdout or '').strip()[-1000:]}",
                            flush=True,
                        )
                        time.sleep(retry_seconds)
                        retry_seconds = min(300, retry_seconds * 2)
                        continue
                else:
                    retry_seconds = 15
            except Exception as exc:
                print(f"实时检测服务托管器发生异常：{exc}", flush=True)
            time.sleep(15)

    threading.Thread(target=worker, name="event-listener-supervisor", daemon=True).start()


def api_event_listener_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if "phases" in payload:
        payload["phases"] = validate_phases(str(payload["phases"]))
    profile = str(payload.get("phase6_profile", "video_only"))
    if profile not in {"video_only", "full"}:
        raise ValueError("phase6_profile must be video_only or full")
    output_mode = str(payload.get("phase6_video_output_mode", "sidecar"))
    if output_mode not in {"sidecar", "derived_copy"}:
        raise ValueError("phase6_video_output_mode must be sidecar or derived_copy")
    sidecar_filename = str(payload.get("phase6_sidecar_filename", "video_rotated_180.mp4"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.mp4", sidecar_filename):
        raise ValueError("phase6_sidecar_filename must be a plain .mp4 filename")
    if "mount_prefix" in payload:
        payload["mount_prefix"] = validate_absolute_path(
            str(payload["mount_prefix"]),
            "mount_prefix",
        )
    if "nas_prefix" in payload:
        prefixes = [item.strip() for item in str(payload["nas_prefix"]).split(",") if item.strip()]
        if not prefixes:
            raise ValueError("nas_prefix must contain at least one absolute path")
        payload["nas_prefix"] = ",".join(
            validate_absolute_path(item, "nas_prefix") for item in prefixes
        )
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
        "PHASE6_PROFILE",
        "PHASE6_VIDEO_OUTPUT_MODE",
        "PHASE6_SIDECAR_FILENAME",
        "DISABLE_QUALITY_LABEL_FILTER",
        "MOUNT_PREFIX",
        "NAS_PREFIX",
        "RETENTION_DAYS",
        "RETENTION_MAX_RUNS",
        "RETENTION_MAX_GB",
        "PHASES",
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
    if action in {"start", "restart"}:
        _record_event_listener_start_result(completed)
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": completed.stdout[-4000:],
        "status": event_listener_status(),
    }


def api_event_listener_status() -> dict[str, Any]:
    return {"event_listener": event_listener_status()}


def event_listener_status() -> dict[str, Any]:
    listener_pid, _ = event_listener_process_info()
    tmux_running = tmux_session_running("qa_event_listener")
    systemd_running = event_listener_systemd_running()
    running = listener_pid is not None or tmux_running or systemd_running
    manager = (
        "systemd"
        if systemd_running
        else ("tmux" if tmux_running else ("process" if listener_pid is not None else "none"))
    )
    status = {
        "running": running,
        "manager": manager,
        "pid": listener_pid,
        "desired_running": EVENT_LISTENER_ENABLED_FILE.is_file(),
        "tmux_running": tmux_running,
        "systemd_running": systemd_running,
        "systemd_service": EVENT_LISTENER_SYSTEMD_SERVICE,
        "job_db": str(EVENT_JOB_DB),
        "counts": {},
        "recent": [],
        "output_size": directory_size(PROJECT_ROOT / "outputs" / "event_listener", max_files=2000),
        "settings": event_listener_settings(),
    }
    if EVENT_LISTENER_ERROR_FILE.is_file():
        try:
            status["last_start_error"] = EVENT_LISTENER_ERROR_FILE.read_text(
                encoding="utf-8", errors="replace"
            )[-4000:]
        except OSError:
            pass
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
    env = current_process_env("Werkzeuge/listen_episode_verified.py")
    return {
        "phases": option_value(args, "--phases", ""),
        "mount_prefix": option_value(args, "--mount-prefix", ""),
        "nas_prefix": option_value(args, "--nas-prefix", ""),
        "phase6_profile": option_value(args, "--phase6-profile", ""),
        "phase6_video_output_mode": option_value(args, "--phase6-video-output-mode", ""),
        "phase6_sidecar_filename": option_value(args, "--phase6-sidecar-filename", ""),
        "quality_label_filter_disabled": "--disable-quality-label-filter" in args,
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
        "dcs_notify_enabled": env.get("QA_DCS_NOTIFY_ENABLED", ""),
        "dcs_notify_dry_run": env.get("QA_DCS_NOTIFY_DRY_RUN", ""),
        "dcs_notify_wait": env.get("QA_DCS_NOTIFY_WAIT", ""),
        "dcs_notify_event": env.get("QA_DCS_NOTIFY_EVENT", ""),
        "retention_days": option_value(args, "--retention-days", ""),
        "retention_max_runs": option_value(args, "--retention-max-runs", ""),
        "retention_max_gb": option_value(args, "--retention-max-gb", ""),
    }


def current_process_args(marker: str) -> list[str]:
    if marker == "Werkzeuge/listen_episode_verified.py":
        return event_listener_process_info()[1]
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


def current_process_env(marker: str) -> dict[str, str]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return {}
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw_cmd = (path / "cmdline").read_bytes()
        except OSError:
            continue
        args = [part.decode("utf-8", "replace") for part in raw_cmd.split(b"\0") if part]
        if not (any(marker in arg for arg in args) and "serve" in args):
            continue
        try:
            raw_env = (path / "environ").read_bytes()
        except OSError:
            return {}
        env: dict[str, str] = {}
        for item in raw_env.split(b"\0"):
            if not item or b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        return env
    return {}


def option_value(args: list[str], flag: str, default: str = "") -> str:
    try:
        index = args.index(flag)
    except ValueError:
        return default
    if index + 1 >= len(args):
        return default
    return args[index + 1]


def event_listener_jobs(
    limit: int = 100,
    issues_only: bool = False,
    offset: int = 0,
) -> list[dict[str, Any]]:
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
                LIMIT ? OFFSET ?
                """,
                (query_limit, max(0, int(offset))),
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


def event_listener_jobs_page(
    page: int,
    page_size: int,
    issues_only: bool = False,
) -> dict[str, Any]:
    """Return one latest-first event-job page and pagination metadata."""
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    if issues_only:
        jobs = event_listener_jobs(limit=page_size, issues_only=True, offset=(page - 1) * page_size)
        total = len(jobs)
        total_pages = page if len(jobs) == page_size else max(1, page)
    else:
        try:
            with sqlite3.connect(f"file:{EVENT_JOB_DB}?mode=ro", uri=True) as conn:
                total = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        except (sqlite3.Error, TypeError):
            total = 0
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        jobs = event_listener_jobs(
            limit=page_size,
            offset=(page - 1) * page_size,
        )
    return {
        "jobs": jobs,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


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
    now = time.monotonic()
    signature = event_job_warning_signature()
    if signature is None:
        return {"count": 0, "warnings": [], "checked_jobs": 0}
    with state.lock:
        if state.failure_warning_signature == signature:
            state.failure_warning_last_checked = now
            return state.failure_warning_cache
        if not state.failure_warning_refreshing:
            state.failure_warning_refreshing = True
            threading.Thread(
                target=refresh_consecutive_failure_warnings,
                args=(state, signature),
                name="consecutive-failure-refresh",
                daemon=True,
            ).start()
        cached = dict(state.failure_warning_cache)
    if not cached.get("warnings") and int(cached.get("count") or 0) == 0:
        cached = active_consecutive_failure_warnings(state.issue_history_db, limit=20)
    cached["checked_jobs"] = int(signature[0] or 0)
    cached["refreshing"] = True
    return cached


def refresh_consecutive_failure_warnings(
    state: DashboardState, signature: tuple[int, int, str, int, int]
) -> None:
    try:
        warnings = compute_consecutive_failure_warnings(state.issue_history_db)
    except Exception as exc:
        print(f"Warning: consecutive failure refresh failed: {exc}", flush=True)
        warnings = active_consecutive_failure_warnings(state.issue_history_db, limit=20)
        warnings["refresh_error"] = str(exc)
    warnings["refreshing"] = False
    with state.lock:
        state.failure_warning_signature = signature
        state.failure_warning_cache = warnings
        state.failure_warning_last_checked = time.monotonic()
        state.failure_warning_refreshing = False
    with state.cache_lock:
        state.cache.pop("api_status", None)


def event_job_warning_signature() -> tuple[int, int, str, int, int] | None:
    if not EVENT_JOB_DB.exists():
        return None
    try:
        with sqlite3.connect(f"file:{EVENT_JOB_DB}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id, "
                "COALESCE(MAX(updated_at), '') AS max_updated_at FROM jobs"
            ).fetchone()
            db_rows = conn.execute(
                """
                SELECT db_path
                FROM jobs
                WHERE db_path IS NOT NULL AND db_path != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (WARNING_SIGNATURE_DB_SAMPLE_LIMIT,),
            ).fetchall()
    except sqlite3.Error:
        return None
    latest_result_mtime_ns = 0
    sampled_result_dbs = 0
    for db_row in db_rows:
        db_path = Path(str(db_row[0] or ""))
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        try:
            stat = db_path.stat()
        except OSError:
            continue
        sampled_result_dbs += 1
        latest_result_mtime_ns = max(latest_result_mtime_ns, int(stat.st_mtime_ns))
    return (
        int(row[0] or 0),
        int(row[1] or 0),
        str(row[2] or ""),
        sampled_result_dbs,
        latest_result_mtime_ns,
    )


def compute_consecutive_failure_warnings(issue_history_db: Path = DEFAULT_ISSUE_HISTORY_DB) -> dict[str, Any]:
    latest_by_path: dict[str, dict[str, Any]] = {}
    collector_cache: dict[str, str] = {}
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

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for job in latest_by_path.values():
        mounted_path = str(job.get("mounted_path") or "")
        episode_number = parse_episode_number(mounted_path)
        if episode_number is None:
            continue
        result = job_episode_result(job)
        task = str(result.get("task") or task_from_episode_path(mounted_path) or "")
        robot = str(result.get("robot") or robot_from_episode_path(mounted_path) or "")
        operator = str(result.get("operator") or operator_from_episode_path(mounted_path) or "")
        context_date = str(result.get("date") or date_from_episode_path(mounted_path) or "")
        if not task or not robot:
            continue
        groups.setdefault((task, robot, operator, context_date), []).append(
            {
                "episode_number": episode_number,
                "episode_name": Path(mounted_path).name,
                "mounted_path": mounted_path,
                "db_path": str(job.get("db_path") or ""),
                "job_status": str(job.get("status") or ""),
                "final_status": str(result.get("final_status") or ""),
                "date": context_date,
                "updated_at": str(job.get("updated_at") or ""),
            }
        )

    detected = []
    for (task, robot, operator, context_date), episodes in groups.items():
        for streak in consecutive_bad_segments(episodes, CONSECUTIVE_FAILURE_STREAK_LENGTH):
            issue_locations = issue_location_summary_for_streak(streak)
            detected.append(
                {
                    "task": task,
                    "robot": robot,
                    "operator": operator,
                    "context_date": context_date,
                    "start_episode_number": streak[0]["episode_number"],
                    "end_episode_number": streak[-1]["episode_number"],
                    "start_episode_name": streak[0]["episode_name"],
                    "end_episode_name": streak[-1]["episode_name"],
                    "streak_length": len(streak),
                    "issue_types": issue_types_for_streak(streak),
                    "collector_ids": collector_ids_for_streak(streak, collector_cache),
                    "issue_locations": issue_locations,
                    "episode_status_counts": episode_status_counts_for_streak(streak),
                    "_episodes": streak,
                    "message": consecutive_streak_message(streak),
                }
            )
    record_consecutive_failure_detections(issue_history_db, detected)
    finalize_rechecked_consecutive_failures(issue_history_db, detected)
    active = active_consecutive_failure_warnings(issue_history_db, limit=20)
    active["checked_jobs"] = len(latest_by_path)
    return active


def collector_ids_for_streak(streak: list[dict[str, Any]], cache: dict[str, str] | None = None) -> list[str]:
    collector_ids: set[str] = set()
    for episode in streak:
        mounted_path = str(episode.get("mounted_path") or "")
        if mounted_path:
            collector_ids.add(collector_id_for_episode(mounted_path, cache))
    return sorted(collector_id for collector_id in collector_ids if collector_id)


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


def episode_status_counts_for_streak(streak: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(episode.get("final_status") or "unknown") for episode in streak)
    return ordered_counter(counts, STATUS_ORDER)


def consecutive_streak_message(streak: list[dict[str, Any]]) -> str:
    if not streak:
        return ""
    counts = episode_status_counts_for_streak(streak)
    fail_count = int(counts.get("fail") or 0)
    review_count = int(counts.get("needs_review") or 0)
    if fail_count == len(streak):
        result_text = "不合格"
    elif review_count == len(streak):
        result_text = "待复核"
    else:
        result_text = "异常"
    return (
        f"episode_{int(streak[0]['episode_number']):04d} ~ "
        f"episode_{int(streak[-1]['episode_number']):04d} "
        f"连续{len(streak)}个{result_text} episode"
    )


def issue_location_summary_for_streak(streak: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_check: dict[str, dict[str, Any]] = {}
    for episode in streak:
        db_path = Path(str(episode.get("db_path") or ""))
        mounted_path = str(episode.get("mounted_path") or "")
        episode_number = int(episode.get("episode_number") or 0)
        if not db_path.is_file() or not mounted_path:
            continue
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    """
                    SELECT check_name, severity, status, details
                    FROM findings
                    WHERE episode_path = ? AND status != ?
                    ORDER BY check_name
                    """,
                    (mounted_path, "pass"),
                ).fetchall()
        except sqlite3.Error:
            continue
        for check_name_raw, severity_raw, status_raw, details_raw in rows:
            check_name = str(check_name_raw or "")
            if not check_name:
                continue
            details = json_value(str(details_raw or ""), {}) if isinstance(details_raw, str) else {}
            location = issue_location_from_details(details)
            item = by_check.setdefault(
                check_name,
                {
                    "locations": Counter(),
                    "episode_numbers": set(),
                    "severity_counts": Counter(),
                    "status_counts": Counter(),
                },
            )
            item["locations"][location] += 1
            item["episode_numbers"].add(episode_number)
            item["severity_counts"][str(severity_raw or "unknown")] += 1
            item["status_counts"][str(status_raw or "unknown")] += 1

    summary: list[dict[str, Any]] = []
    for check_name, item in by_check.items():
        counter = item["locations"]
        locations = [
            {"location": location, "count": count}
            for location, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        ]
        summary.append(
            {
                "check_name": check_name,
                "title_zh": issue_title_zh(check_name),
                "count": sum(counter.values()),
                "affected_episode_count": len(item["episode_numbers"]),
                "episode_ranges": compact_episode_ranges(item["episode_numbers"]),
                "severity_counts": ordered_counter(item["severity_counts"], SEVERITY_ORDER),
                "status_counts": ordered_counter(item["status_counts"], STATUS_ORDER),
                "category": consecutive_issue_category(check_name, item["status_counts"]),
                "locations": locations,
                "display": issue_location_display(check_name, locations),
            }
        )
    category_order = {"failure": 0, "review": 1, "operator_label": 2, "warning": 3}
    severity_order = {name: index for index, name in enumerate(SEVERITY_ORDER)}
    return sorted(
        summary,
        key=lambda item: (
            category_order.get(str(item.get("category") or ""), 9),
            min(
                (severity_order.get(name, 99) for name in (item.get("severity_counts") or {})),
                default=99,
            ),
            -int(item.get("affected_episode_count") or 0),
            str(item.get("check_name") or ""),
        ),
    )


def consecutive_issue_category(check_name: str, status_counts: Counter[str]) -> str:
    if check_name == "quality_label_unexpected":
        return "operator_label"
    if status_counts.get("fail"):
        return "failure"
    if status_counts.get("needs_review"):
        return "review"
    return "warning"


def compact_episode_ranges(numbers: Any) -> list[str]:
    ordered = sorted({int(number) for number in numbers})
    if not ordered:
        return []
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(format_episode_range(start, previous))
        start = previous = number
    ranges.append(format_episode_range(start, previous))
    return ranges


def format_episode_range(start: int, end: int) -> str:
    if start == end:
        return f"episode_{start:04d}"
    return f"episode_{start:04d} ~ episode_{end:04d}"


def issue_location_from_details(details: Any) -> str:
    if not isinstance(details, dict):
        return "episode 级检查"
    fields = finding_detail_fields(details)
    if not fields:
        for key in ("source_modality", "earliest_modality", "latest_modality", "file", "missing_file"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                fields.append(value.strip())
    if not fields:
        return "episode 级检查"
    return " / ".join(dict.fromkeys(fields))


def issue_location_display(check_name: str, locations: list[dict[str, Any]]) -> str:
    if not locations:
        return f"{check_name}: episode 级检查"
    parts = [
        f"{item.get('location') or 'episode 级检查'}({int(item.get('count') or 0)})"
        for item in locations[:4]
    ]
    remaining = len(locations) - len(parts)
    suffix = f", +{remaining}" if remaining > 0 else ""
    return f"{check_name}: {', '.join(parts)}{suffix}"


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
                SELECT id, issue_types, collector_ids, issue_locations, episode_status_counts
                FROM consecutive_failure_streaks
                WHERE task = ? AND robot = ? AND operator = ? AND context_date = ?
                  AND episode_start = ? AND episode_end = ?
                LIMIT 1
                """,
                (
                    effective["task"],
                    effective["robot"],
                    effective["operator"],
                    str(effective.get("context_date") or ""),
                    int(effective["start_episode_number"]),
                    int(effective["end_episode_number"]),
                ),
            ).fetchone()
            if exists:
                issue_types = sorted(effective.get("issue_types") or json_value(exists[1], []))
                collector_ids = sorted(effective.get("collector_ids") or json_value(exists[2], []))
                issue_locations = effective.get("issue_locations") or json_value(exists[3], [])
                episode_status_counts = effective.get("episode_status_counts") or json_value(exists[4], {})
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET issue_types = ?, collector_ids = ?, issue_locations = ?, episode_status_counts = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(issue_types, ensure_ascii=False),
                        json.dumps(collector_ids, ensure_ascii=False),
                        json.dumps(issue_locations, ensure_ascii=False),
                        json.dumps(episode_status_counts, ensure_ascii=False),
                        int(exists[0]),
                    ),
                )
                continue
            conn.execute(
                """
                INSERT INTO consecutive_failure_streaks (
                    task, robot, operator, context_date, episode_start, episode_end,
                    streak_length, issue_types, collector_ids, issue_locations,
                    episode_status_counts, detected_at, resolved_at, resolution_time_seconds, resolution_source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '')
                """,
                (
                    effective["task"],
                    effective["robot"],
                    effective["operator"],
                    str(effective.get("context_date") or ""),
                    int(effective["start_episode_number"]),
                    int(effective["end_episode_number"]),
                    int(effective["streak_length"]),
                    json.dumps(effective.get("issue_types") or [], ensure_ascii=False),
                    json.dumps(effective.get("collector_ids") or [], ensure_ascii=False),
                    json.dumps(effective.get("issue_locations") or [], ensure_ascii=False),
                    json.dumps(effective.get("episode_status_counts") or {}, ensure_ascii=False),
                    detected_at,
                ),
            )


def consecutive_detection_key(item: dict[str, Any]) -> tuple[str, str, str, str, int, int]:
    return (
        str(item.get("task") or ""),
        str(item.get("robot") or ""),
        str(item.get("operator") or ""),
        str(item.get("context_date") or item.get("date") or ""),
        int(item.get("start_episode_number") or item.get("episode_start") or 0),
        int(item.get("end_episode_number") or item.get("episode_end") or 0),
    )


def finalize_rechecked_consecutive_failures(db_path: Path, detections: list[dict[str, Any]]) -> None:
    init_issue_history(db_path)
    resolved_at = now_iso()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, task, robot, operator, context_date, episode_start, episode_end,
                   detected_at, recheck_job_ids
            FROM consecutive_failure_streaks
            WHERE resolved_at IS NULL AND recheck_requested_at != ''
            """
        ).fetchall()
        for row in rows:
            key = (
                row["task"],
                row["robot"],
                row["operator"],
                row["context_date"],
                int(row["episode_start"]),
                int(row["episode_end"]),
            )
            status = recheck_job_status(json_value(row["recheck_job_ids"], []))
            if not status.get("terminal"):
                continue
            if any(detection_covers_rechecked_key(item, key) for item in detections):
                conn.execute(
                    """
                UPDATE consecutive_failure_streaks
                SET recheck_requested_at = '', recheck_job_ids = '[]'
                WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
                continue
            resolution_seconds = max(
                0,
                int(
                    datetime.fromisoformat(resolved_at).timestamp()
                    - datetime.fromisoformat(row["detected_at"]).timestamp()
                ),
            )
            conn.execute(
                """
                UPDATE consecutive_failure_streaks
                SET resolved_at = ?, resolution_time_seconds = ?,
                    resolution_source = 'recheck_clear',
                    recheck_requested_at = '', recheck_job_ids = '[]'
                WHERE id = ?
                """,
                (resolved_at, resolution_seconds, int(row["id"])),
            )


def detection_covers_rechecked_key(
    item: dict[str, Any], key: tuple[str, str, str, str, int, int]
) -> bool:
    task, robot, operator, context_date, episode_start, episode_end = key
    return (
        str(item.get("task") or "") == task
        and str(item.get("robot") or "") == robot
        and str(item.get("operator") or "") == operator
        and str(item.get("context_date") or item.get("date") or "") == context_date
        and int(item.get("start_episode_number") or item.get("episode_start") or 0) <= episode_start
        and int(item.get("end_episode_number") or item.get("episode_end") or 0) >= episode_end
    )


def effective_detection_for_history(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any] | None:
    task = str(item["task"])
    robot = str(item["robot"])
    operator = str(item["operator"])
    context_date = str(item.get("context_date") or "")
    segment_start = int(item["start_episode_number"])
    segment_end = int(item["end_episode_number"])
    rows = conn.execute(
        """
        SELECT id, episode_start, episode_end, issue_types, resolved_at, collector_ids, issue_locations,
               resolution_source, episode_status_counts
        FROM consecutive_failure_streaks
        WHERE task = ? AND robot = ? AND operator = ? AND context_date = ?
          AND episode_start <= ? AND episode_end >= ?
        ORDER BY episode_start, episode_end, id
        """,
        (task, robot, operator, context_date, segment_end, segment_start),
    ).fetchall()

    for row in rows:
        row_start = int(row[1])
        row_end = int(row[2])
        resolution_source = str(row[7] or "")
        if row[4] is not None and resolution_source == "recheck_clear":
            effective_episodes = _episodes_in_range(item.get("_episodes") or [], segment_start, segment_end)
            issue_types = sorted(item.get("issue_types") or json_value(row[3], []))
            collector_ids = sorted(item.get("collector_ids") or json_value(row[5], []))
            issue_locations = item.get("issue_locations") or json_value(row[6], [])
            episode_status_counts = item.get("episode_status_counts") or json_value(row[8], {})
            conn.execute(
                """
                UPDATE consecutive_failure_streaks
                SET episode_start = ?, episode_end = ?, streak_length = ?,
                    issue_types = ?, collector_ids = ?, issue_locations = ?, episode_status_counts = ?,
                    resolved_at = NULL, resolution_time_seconds = NULL, resolution_source = '',
                    recheck_requested_at = '', recheck_job_ids = '[]'
                WHERE id = ?
                """,
                (
                    segment_start,
                    segment_end,
                    segment_end - segment_start + 1,
                    json.dumps(issue_types, ensure_ascii=False),
                    json.dumps(collector_ids, ensure_ascii=False),
                    json.dumps(issue_locations, ensure_ascii=False),
                    json.dumps(episode_status_counts, ensure_ascii=False),
                    int(row[0]),
                ),
            )
            return None
        if row[4] is None and row_start == segment_start:
            issue_types = sorted(item.get("issue_types") or json_value(row[3], []))
            collector_ids = sorted(item.get("collector_ids") or json_value(row[5], []))
            issue_locations = item.get("issue_locations") or json_value(row[6], [])
            episode_status_counts = item.get("episode_status_counts") or json_value(row[8], {})
            if segment_end > row_end:
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET episode_end = ?, streak_length = ?, issue_types = ?,
                        collector_ids = ?, issue_locations = ?, episode_status_counts = ?
                    WHERE id = ?
                    """,
                    (
                        segment_end,
                        segment_end - row_start + 1,
                        json.dumps(issue_types, ensure_ascii=False),
                        json.dumps(collector_ids, ensure_ascii=False),
                        json.dumps(issue_locations, ensure_ascii=False),
                        json.dumps(episode_status_counts, ensure_ascii=False),
                        int(row[0]),
                    ),
                )
            elif (
                issue_types != json_value(row[3], [])
                or collector_ids != json_value(row[5], [])
                or issue_locations != json_value(row[6], [])
                or episode_status_counts != json_value(row[8], {})
            ):
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET issue_types = ?, collector_ids = ?, issue_locations = ?, episode_status_counts = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(issue_types, ensure_ascii=False),
                        json.dumps(collector_ids, ensure_ascii=False),
                        json.dumps(issue_locations, ensure_ascii=False),
                        json.dumps(episode_status_counts, ensure_ascii=False),
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
            if row[4] is not None and str(row[7] or "") != "recheck_clear" and row_start <= effective_start <= row_end:
                effective_start = row_end + 1
                changed = True
                break

    if segment_end - effective_start + 1 < CONSECUTIVE_FAILURE_STREAK_LENGTH:
        return None

    for row in rows:
        row_start = int(row[1])
        row_end = int(row[2])
        if row[4] is None and row_start == effective_start:
            effective_episodes = _episodes_in_range(item.get("_episodes") or [], effective_start, segment_end)
            issue_types = issue_types_for_streak(effective_episodes) or json_value(row[3], [])
            collector_ids = collector_ids_for_streak(effective_episodes) or json_value(row[5], [])
            issue_locations = issue_location_summary_for_streak(effective_episodes) or json_value(row[6], [])
            episode_status_counts = (
                episode_status_counts_for_streak(effective_episodes) or json_value(row[8], {})
            )
            if segment_end > row_end:
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET episode_end = ?, streak_length = ?, issue_types = ?,
                        collector_ids = ?, issue_locations = ?, episode_status_counts = ?
                    WHERE id = ?
                    """,
                    (
                        segment_end,
                        segment_end - row_start + 1,
                        json.dumps(issue_types, ensure_ascii=False),
                        json.dumps(collector_ids, ensure_ascii=False),
                        json.dumps(issue_locations, ensure_ascii=False),
                        json.dumps(episode_status_counts, ensure_ascii=False),
                        int(row[0]),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE consecutive_failure_streaks
                    SET issue_types = ?, collector_ids = ?, issue_locations = ?, episode_status_counts = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(issue_types, ensure_ascii=False),
                        json.dumps(collector_ids, ensure_ascii=False),
                        json.dumps(issue_locations, ensure_ascii=False),
                        json.dumps(episode_status_counts, ensure_ascii=False),
                        int(row[0]),
                    ),
                )
            return None

    effective = dict(item)
    effective["start_episode_number"] = effective_start
    effective["end_episode_number"] = segment_end
    effective["streak_length"] = segment_end - effective_start + 1
    effective_episodes = _episodes_in_range(item.get("_episodes") or [], effective_start, segment_end)
    effective["issue_types"] = (
        issue_types_for_streak(effective_episodes) or item.get("issue_types", [])
        if effective_episodes
        else item.get("issue_types", [])
    )
    effective["collector_ids"] = item.get("collector_ids") or collector_ids_for_streak(effective_episodes)
    effective["episode_status_counts"] = (
        episode_status_counts_for_streak(effective_episodes) or item.get("episode_status_counts", {})
        if effective_episodes
        else item.get("episode_status_counts", {})
    )
    effective["issue_locations"] = (
        issue_location_summary_for_streak(effective_episodes) or item.get("issue_locations", [])
        if effective_episodes
        else item.get("issue_locations", [])
    )
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
            SELECT id, task, robot, operator, context_date, episode_start, episode_end,
                   streak_length, issue_types, collector_ids, issue_locations,
                   recheck_requested_at, recheck_job_ids, detected_at, episode_status_counts
            FROM consecutive_failure_streaks
            WHERE resolved_at IS NULL
            ORDER BY detected_at DESC, id DESC
            """
        ).fetchall()

    by_combo: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["task"], row["robot"], row["operator"], row["context_date"])
        if key in by_combo:
            continue
        item = {
            "id": int(row["id"]),
            "task": row["task"],
            "robot": row["robot"],
            "operator": row["operator"],
            "context_date": row["context_date"],
            "start_episode_number": int(row["episode_start"]),
            "end_episode_number": int(row["episode_end"]),
            "streak_length": int(row["streak_length"]),
            "issue_types": json_value(row["issue_types"], []),
            "collector_ids": json_value(row["collector_ids"], []),
            "issue_locations": json_value(row["issue_locations"], []),
            "episode_status_counts": json_value(row["episode_status_counts"], {}),
            "recheck_requested_at": row["recheck_requested_at"],
            "recheck_job_ids": json_value(row["recheck_job_ids"], []),
            "recheck_status": recheck_job_status(json_value(row["recheck_job_ids"], [])),
            "detected_at": row["detected_at"],
            "message": stored_consecutive_streak_message(row),
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


def stored_consecutive_streak_message(row: sqlite3.Row) -> str:
    length = int(row["streak_length"])
    counts = json_value(row["episode_status_counts"], {})
    fail_count = int(counts.get("fail") or 0) if isinstance(counts, dict) else 0
    review_count = int(counts.get("needs_review") or 0) if isinstance(counts, dict) else 0
    if fail_count == length:
        result_text = "不合格"
    elif review_count == length:
        result_text = "待复核"
    else:
        result_text = "异常"
    return (
        f"episode_{int(row['episode_start']):04d} ~ "
        f"episode_{int(row['episode_end']):04d} 连续{length}个{result_text} episode"
    )


def api_resolve_consecutive_failure(state: DashboardState, payload: dict[str, Any]) -> dict[str, Any]:
    task = str(payload.get("task") or "")
    robot = str(payload.get("robot") or "")
    operator = str(payload.get("operator") or "")
    context_date = str(payload.get("context_date") or payload.get("date") or "")
    episode_start = int(payload.get("episode_start"))
    episode_end = int(payload.get("episode_end"))
    resolved = resolve_consecutive_failure(
        state.issue_history_db,
        task,
        robot,
        operator,
        context_date,
        episode_start,
        episode_end,
    )
    active = active_consecutive_failure_warnings(state.issue_history_db, limit=20)
    signature = event_job_warning_signature()
    with state.lock:
        previous_checked_jobs = state.failure_warning_cache.get("checked_jobs", 0)
        active["checked_jobs"] = previous_checked_jobs
        state.failure_warning_signature = signature
        state.failure_warning_cache = active
        state.failure_warning_last_checked = time.monotonic()
    return {"ok": resolved, "consecutive_failures": active}


def api_recheck_consecutive_failure(state: DashboardState, payload: dict[str, Any]) -> dict[str, Any]:
    task = str(payload.get("task") or "")
    robot = str(payload.get("robot") or "")
    operator = str(payload.get("operator") or "")
    context_date = str(payload.get("context_date") or payload.get("date") or "")
    episode_start = int(payload.get("episode_start"))
    episode_end = int(payload.get("episode_end"))
    collector_ids = [str(item) for item in payload.get("collector_ids") or [] if str(item)]
    jobs = matching_event_jobs_for_streak(task, robot, operator, context_date, episode_start, episode_end, collector_ids)
    if not jobs:
        raise ValueError("No event-listener jobs matched this consecutive-failure range.")
    running = [job for job in jobs if str(job.get("status") or "").lower() == "running"]
    if running:
        raise ValueError(f"Cannot recheck while {len(running)} matching job(s) are running.")
    job_ids = requeue_event_jobs_for_recheck(jobs)
    mark_consecutive_recheck_requested(
        state.issue_history_db,
        task,
        robot,
        operator,
        context_date,
        episode_start,
        episode_end,
        job_ids,
    )
    active = active_consecutive_failure_warnings(state.issue_history_db, limit=20)
    signature = event_job_warning_signature()
    with state.lock:
        active["checked_jobs"] = state.failure_warning_cache.get("checked_jobs", 0)
        state.failure_warning_signature = signature
        state.failure_warning_cache = active
        state.failure_warning_last_checked = time.monotonic()
    return {
        "ok": True,
        "requeued_job_ids": job_ids,
        "requeued_count": len(job_ids),
        "consecutive_failures": active,
        "event_listener": event_listener_status(),
    }


def matching_event_jobs_for_streak(
    task: str,
    robot: str,
    operator: str,
    context_date: str,
    episode_start: int,
    episode_end: int,
    collector_ids: list[str],
) -> list[dict[str, Any]]:
    if not EVENT_JOB_DB.exists():
        return []
    collector_set = set(collector_ids)
    rows: list[sqlite3.Row]
    with sqlite3.connect(EVENT_JOB_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, status, attempts, mounted_path, run_id, output_dir,
                   db_path, error, received_at, started_at, finished_at, updated_at
            FROM jobs
            WHERE mounted_path LIKE ?
            ORDER BY id
            """,
            (f"%/{task}/%",),
        ).fetchall()
    matches: list[dict[str, Any]] = []
    for row in rows:
        job = dict(row)
        mounted_path = str(job.get("mounted_path") or "")
        episode_number = parse_episode_number(mounted_path)
        if episode_number is None or not (episode_start <= episode_number <= episode_end):
            continue
        if task_from_episode_path(mounted_path) != task:
            continue
        if operator_from_episode_path(mounted_path) != operator:
            continue
        if context_date and date_from_episode_path(mounted_path) != context_date:
            continue
        if collector_set and collector_id_for_episode(mounted_path) not in collector_set:
            continue
        path_robot = robot_from_episode_path(mounted_path)
        if not collector_set and robot and path_robot and path_robot != robot:
            continue
        matches.append(job)
    return matches


def requeue_event_jobs_for_recheck(jobs: list[dict[str, Any]]) -> list[int]:
    if not jobs:
        return []
    now = now_iso()
    job_ids = [int(job["id"]) for job in jobs]
    placeholders = ",".join("?" for _ in job_ids)
    with sqlite3.connect(EVENT_JOB_DB) as conn:
        conn.execute(
            f"""
            UPDATE jobs
            SET status = 'pending',
                attempts = 0,
                run_id = NULL,
                output_dir = NULL,
                db_path = NULL,
                error = 'Recheck requested from dashboard.',
                started_at = NULL,
                finished_at = NULL,
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (now, *job_ids),
        )
    return job_ids


def mark_consecutive_recheck_requested(
    db_path: Path,
    task: str,
    robot: str,
    operator: str,
    context_date: str,
    episode_start: int,
    episode_end: int,
    job_ids: list[int],
) -> None:
    init_issue_history(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE consecutive_failure_streaks
            SET recheck_requested_at = ?, recheck_job_ids = ?
            WHERE task = ? AND robot = ? AND operator = ? AND context_date = ?
              AND episode_start = ? AND episode_end = ?
              AND resolved_at IS NULL
            """,
            (
                now_iso(),
                json.dumps(job_ids, ensure_ascii=False),
                task,
                robot,
                operator,
                context_date,
                episode_start,
                episode_end,
            ),
        )


def recheck_job_status(job_ids: list[Any]) -> dict[str, Any]:
    ids = [int(job_id) for job_id in job_ids if str(job_id).strip().isdigit()]
    if not ids or not EVENT_JOB_DB.exists():
        return {"state": "", "counts": {}, "terminal": False}
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(EVENT_JOB_DB) as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) FROM jobs WHERE id IN ({placeholders}) GROUP BY status",
            ids,
        ).fetchall()
    counts = {str(status): int(count) for status, count in rows}
    total = sum(counts.values())
    terminal_count = sum(counts.get(status, 0) for status in ("done", "failed", "skipped"))
    if counts.get("running", 0):
        state = "running"
    elif counts.get("pending", 0) or counts.get("retry", 0):
        state = "pending"
    elif total and terminal_count == total:
        state = "done" if counts.get("done", 0) == total else "finished_with_errors"
    else:
        state = "unknown"
    return {"state": state, "counts": counts, "terminal": bool(total and terminal_count == total)}


def resolve_consecutive_failure(
    db_path: Path,
    task: str,
    robot: str,
    operator: str,
    context_date: str,
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
            WHERE task = ? AND robot = ? AND operator = ? AND context_date = ?
              AND episode_start = ? AND episode_end = ?
              AND resolved_at IS NULL
            ORDER BY detected_at DESC, id DESC
            LIMIT 1
            """,
            (task, robot, operator, context_date, episode_start, episode_end),
        ).fetchone()
        if row is None:
            return False
        unresolved_rows = conn.execute(
            """
            SELECT id, detected_at
            FROM consecutive_failure_streaks
            WHERE task = ? AND robot = ? AND operator = ? AND context_date = ?
              AND resolved_at IS NULL
            """,
            (task, robot, operator, context_date),
        ).fetchall()
        for unresolved in unresolved_rows:
            resolution_seconds = max(
                0,
                int(
                    datetime.fromisoformat(resolved_at).timestamp()
                    - datetime.fromisoformat(unresolved[1]).timestamp()
                ),
            )
            conn.execute(
                """
                UPDATE consecutive_failure_streaks
                SET resolved_at = ?, resolution_time_seconds = ?, resolution_source = 'manual'
                WHERE id = ?
                """,
                (resolved_at, resolution_seconds, int(unresolved[0])),
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
                                "issue_title_zh": issue_title_zh(str(check_name)),
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
                "issue_title_zh": group["issue_title_zh"],
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
                "top_issues": [
                    {"check_name": name, "issue_title_zh": issue_title_zh(name), "count": count}
                    for name, count in top_issues
                ],
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
        if str(job.get("status") or "").lower() == "failed":
            result["pipeline_error"] = str(job.get("error") or "QA pipeline failed before a result database was created.")
            result["result_missing"] = True
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
                if str(job.get("status") or "").lower() == "failed" and not result["phase_status"]:
                    result["final_status"] = ""
                    result["qa_result_missing"] = True
                    result["qa_missing_reason"] = (
                        "QA pipeline failed before any phase completed. "
                        f"{str(job.get('error') or '').strip() or 'No pipeline error was recorded.'}"
                    )
                    result["pipeline_error"] = str(job.get("error") or "")
            elif str(job.get("status") or "").lower() == "done":
                result["qa_result_missing"] = True
                result["qa_missing_reason"] = (
                    "Listener job finished, but this episode is not in the QA result DB. "
                    "It was likely skipped before QA checks, for example because it was already complete."
                )
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
                group = top_issue_groups.setdefault(
                    name,
                    {
                        "check_name": name,
                        "issue_title_zh": issue_title_zh(name),
                        "count": 0,
                        "fields": [],
                    },
                )
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
                result["findings"] = []
                for row in findings:
                    localized = localized_finding(
                        str(row["check_name"] or "unknown"),
                        str(row["message"] or ""),
                        json_value(row["details"], {}),
                    )
                    result["findings"].append(
                        {
                            "phase": row["phase"],
                            "severity": row["severity"],
                            "status": row["status"],
                            **localized,
                        }
                    )
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


def date_from_episode_path(path: str) -> str:
    parts = Path(path).parts
    try:
        index = parts.index("verified")
        return parts[index + 4] if len(parts) > index + 4 else ""
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
            if run_manager_running(run):
                continue
            run_status = latest_run_status(Path(run["output_dir"]))
            status = str(run_status.get("status") or "")
            if status in {"", "running"}:
                exit_status_path = Path(run["output_dir"]) / "dashboard_exit_status.txt"
                try:
                    exit_code = int(exit_status_path.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    status = "failed"
                else:
                    status = "complete" if exit_code == 0 else "failed"
            update_run_status(state.registry_db, run["run_id"], status, finished=True)


def record_run(registry_db: Path, row: dict[str, Any]) -> None:
    row = dict(row)
    row.setdefault("tmux_session", "")
    row.setdefault("manager", "")
    row.setdefault("manager_id", "")
    row.setdefault("manager_pid", None)
    row.setdefault("manager_start_ticks", "")
    with sqlite3.connect(registry_db) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, mode, output_dir, db_path, command, tmux_session,
                manager, manager_id, manager_pid, manager_start_ticks,
                status, phases, workers, batch_size, root_path, date_from,
                date_to, task_filter, operator_filter, started_at, finished_at, updated_at, notes,
                severe_report_requested, severe_report_config, continue_after_fail
            )
            VALUES (
                :run_id, :mode, :output_dir, :db_path, :command, :tmux_session,
                :manager, :manager_id, :manager_pid, :manager_start_ticks,
                :status, :phases, :workers, :batch_size, :root_path, :date_from,
                :date_to, :task_filter, :operator_filter, :started_at, :finished_at, :updated_at, :notes,
                :severe_report_requested, :severe_report_config, :continue_after_fail
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
                    {
                        "check_name": str(name),
                        "issue_title_zh": issue_title_zh(str(name)),
                        "count": int(count),
                        "episode_count": int(episode_count),
                    }
                    for name, count, episode_count in conn.execute(
                        """
                        SELECT check_name, COUNT(*) AS count,
                               COUNT(DISTINCT episode_path) AS episode_count
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
            result = []
            for row in rows:
                item = dict(row)
                item.update(
                    localized_finding(
                        str(item.get("check_name") or "unknown"),
                        str(item.get("message") or ""),
                        {},
                    )
                )
                result.append(item)
            return result
    except sqlite3.Error:
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
                    "operator": _display_operator(row["operator"], str(row["episode_path"] or "")),
                    "final_status": row["final_status"] or "",
                    "issue_count": int(row["issue_count"] or 0),
                    "issue_names": [name for name in str(row["issue_names"] or "").split(",") if name],
                    "issue_titles_zh": [
                        issue_title_zh(name)
                        for name in str(row["issue_names"] or "").split(",")
                        if name
                    ],
                }
                for row in rows
            ]
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


def load_operator_severe_report(
    output_dir: Path,
    requested: bool,
    run_status: str,
) -> dict[str, Any]:
    """Load severe-report summary data without breaking the full run detail."""
    output_dir = Path(output_dir)
    json_path = output_dir / OPERATOR_SEVERE_REPORT_JSON
    paths = {
        "report_json_path": str(json_path),
        "ranking_csv_path": _existing_path(output_dir / OPERATOR_SEVERE_RANKING_CSV),
        "evidence_csv_path": _existing_path(output_dir / OPERATOR_SEVERE_EVIDENCE_CSV),
        "criterion_frequency_csv_path": _existing_path(output_dir / OPERATOR_SEVERE_FREQUENCY_CSV),
        "markdown_path": _existing_path(output_dir / OPERATOR_SEVERE_MARKDOWN),
    }
    if not requested:
        return {"state": "not_requested", **paths}
    if not json_path.is_file():
        if run_status in {"running", "starting", "pending"}:
            return {"state": "pending", **paths}
        return {"state": "missing", **paths}
    try:
        report = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"state": "invalid", "error": str(exc), **paths}
    if not isinstance(report, dict):
        return {"state": "invalid", "error": "Report JSON root must be an object", **paths}
    summary = report.get("summary", {})
    ranking = report.get("operator_ranking", [])
    frequency = report.get("criterion_frequency", [])
    if not isinstance(summary, dict):
        return {"state": "invalid", "error": "Report summary must be an object", **paths}
    if not isinstance(ranking, list):
        return {"state": "invalid", "error": "Report operator_ranking must be a list", **paths}
    if not isinstance(frequency, list):
        return {"state": "invalid", "error": "Report criterion_frequency must be a list", **paths}
    evidence = report.get("evidence", [])
    severe_episode_count = None
    named_severe_episode_count = None
    unknown_operator_severe_episode_count = None
    if isinstance(evidence, list):
        severe_paths: set[str] = set()
        named_paths: set[str] = set()
        unknown_paths: set[str] = set()
        for row in evidence:
            if not isinstance(row, dict) or not row.get("episode_path"):
                continue
            episode_path = str(row["episode_path"])
            severe_paths.add(episode_path)
            operator = str(row.get("operator") or "").strip()
            is_unknown = bool(row.get("operator_is_unknown")) or operator.casefold() in {
                "",
                "unknown",
                "(unknown)",
                "未填写",
            }
            (unknown_paths if is_unknown else named_paths).add(episode_path)
        severe_episode_count = len(severe_paths)
        named_severe_episode_count = len(named_paths)
        unknown_operator_severe_episode_count = len(unknown_paths)
    summary.setdefault("named_severe_episode_count", named_severe_episode_count)
    summary.setdefault(
        "unknown_operator_severe_episode_count",
        unknown_operator_severe_episode_count,
    )
    return {
        "state": "generated",
        "generation_time": str(report.get("generation_time") or ""),
        "summary": summary,
        "severe_episode_count": severe_episode_count,
        "criteria": report.get("criteria", []) if isinstance(report.get("criteria", []), list) else [],
        "criterion_frequency": [row for row in frequency if isinstance(row, dict)],
        "operator_ranking": [row for row in ranking if isinstance(row, dict)],
        **paths,
    }


def _existing_path(path: Path) -> str:
    return str(path) if path.is_file() else ""


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
                "device_failure_summary": raw.get("device_failure_summary") or [],
                "rule_explanations": raw.get("rule_explanations") or [],
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "report_key": report_dir.name,
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
        "rendered_html": render_markdown_html(markdown),
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


def localized_finding(check_name: str, original_message: str, details: Any) -> dict[str, Any]:
    details_object = details if isinstance(details, dict) else {}
    presentation = finding_presentation_zh(
        {
            "check_name": str(check_name or "unknown"),
            "message": str(original_message or ""),
            "details": details_object,
        }
    )
    return {
        "check_name": str(check_name or "unknown"),
        "message": presentation["occurrence_zh"],
        "message_zh": presentation["occurrence_zh"],
        "original_message": str(original_message or ""),
        "details": details_object,
        **presentation,
    }


def write_run_script(path: Path, command: list[str], log_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        f"cd {shlex.quote(str(PROJECT_ROOT))}",
        f"mkdir -p {shlex.quote(str(path.parent))}",
        f"exec > >(tee -a {shlex.quote(str(log_path))}) 2>&1",
        "if [[ -f datapipeline-env/bin/activate ]]; then source datapipeline-env/bin/activate; fi",
        "if [[ -f \"$HOME/.qa_task_db_env\" ]]; then source \"$HOME/.qa_task_db_env\"; fi",
        "echo \"Run started at $(date '+%F %T')\"",
        " ".join(shlex.quote(part) for part in command),
        "exit_code=$?",
        f"printf '%s\\n' \"$exit_code\" > {shlex.quote(str(path.parent / 'dashboard_exit_status.txt'))}",
        "echo \"Run finished at $(date '+%F %T') with exit $exit_code\"",
        "exit \"$exit_code\"",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def configured_run_backend(state: DashboardState) -> str:
    backend = str(getattr(state, "run_backend", DEFAULT_RUN_BACKEND) or "auto").strip().lower()
    if backend not in {"auto", "systemd", "process"}:
        raise ValueError("run backend must be auto, systemd, or process")
    return backend


def systemd_user_available() -> bool:
    if shutil.which("systemctl") is None or shutil.which("systemd-run") is None:
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def effective_run_backend(state: DashboardState) -> str:
    configured = configured_run_backend(state)
    if configured == "process":
        return "process"
    available = systemd_user_available()
    if configured == "systemd":
        if not available:
            raise RuntimeError("user-systemd is required by the configured dashboard run backend")
        return "systemd"
    return "systemd" if available else "process"


def run_launcher_status(state: DashboardState) -> dict[str, Any]:
    configured = configured_run_backend(state)
    available = systemd_user_available()
    if configured == "systemd":
        effective = "systemd"
    elif configured == "process":
        effective = "process"
    else:
        effective = "systemd" if available else "process"
    return {
        "configured_backend": configured,
        "effective_backend": effective,
        "systemd_available": available,
    }


def launch_dashboard_run(
    state: DashboardState,
    run_id: str,
    command_path: Path,
) -> RunManagerHandle:
    backend = effective_run_backend(state)
    if backend == "systemd":
        return launch_systemd_run(run_id, command_path)
    return launch_process_run(command_path)


def launch_systemd_run(run_id: str, command_path: Path) -> RunManagerHandle:
    safe_run_id = sanitize_id(run_id)[:120]
    unit = f"datapipeline-qa-run-{safe_run_id}-{uuid.uuid4().hex[:10]}.service"
    try:
        completed = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--service-type=exec",
                "--unit",
                unit,
                "--property=TimeoutStopSec=30s",
                "bash",
                str(command_path),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        raise RuntimeError("user-systemd timed out while starting dashboard run") from exc
    if completed.returncode != 0:
        message = completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"user-systemd failed to start dashboard run: {message}")
    return RunManagerHandle(manager="systemd", manager_id=unit)


def launch_process_run(command_path: Path) -> RunManagerHandle:
    try:
        process = subprocess.Popen(
            ["bash", str(command_path)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(f"detached process failed to start dashboard run: {exc}") from exc
    start_ticks = process_start_ticks(process.pid)
    if not start_ticks:
        return_code = process.poll()
        if return_code is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise RuntimeError(
            "detached dashboard run exited during startup"
            + (f" with code {return_code}" if return_code is not None else "")
        )
    return RunManagerHandle(
        manager="process",
        manager_pid=process.pid,
        manager_start_ticks=start_ticks,
    )


def apply_run_manager_handle(row: dict[str, Any], handle: RunManagerHandle) -> None:
    row["tmux_session"] = ""
    row["manager"] = handle.manager
    row["manager_id"] = handle.manager_id
    row["manager_pid"] = handle.manager_pid
    row["manager_start_ticks"] = handle.manager_start_ticks


def run_manager_name(run: dict[str, Any]) -> str:
    manager = str(run.get("manager") or "").strip().lower()
    if manager in {"systemd", "process", "tmux"}:
        return manager
    if str(run.get("tmux_session") or "").strip():
        return "tmux"
    return ""


def run_manager_running(run: dict[str, Any]) -> bool:
    manager = run_manager_name(run)
    if manager == "systemd":
        return systemd_run_unit_running(str(run.get("manager_id") or ""))
    if manager == "process":
        return managed_run_process_pid(run) is not None
    if manager == "tmux":
        return tmux_session_running(str(run.get("tmux_session") or run.get("manager_id") or ""))
    return False


def _valid_run_unit(unit: str) -> bool:
    return bool(re.fullmatch(r"datapipeline-qa-run-[A-Za-z0-9_.-]+\.service", unit))


def systemd_run_unit_running(unit: str) -> bool:
    if not _valid_run_unit(unit):
        return False
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def process_start_ticks(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].strip().split()
        return fields[19]
    except (OSError, IndexError):
        return ""


def process_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def managed_run_process_pid(run: dict[str, Any]) -> int | None:
    try:
        pid = int(run.get("manager_pid") or 0)
    except (TypeError, ValueError):
        return None
    expected_ticks = str(run.get("manager_start_ticks") or "")
    if pid <= 1 or not expected_ticks or process_start_ticks(pid) != expected_ticks:
        return None
    expected_script = str(Path(str(run.get("output_dir") or "")) / "dashboard_command.sh")
    if expected_script not in process_cmdline(pid):
        return None
    return pid


def stop_run_manager(run: dict[str, Any]) -> bool:
    manager = run_manager_name(run)
    if manager == "systemd":
        unit = str(run.get("manager_id") or "")
        if not _valid_run_unit(unit) or not systemd_run_unit_running(unit):
            return False
        completed = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=35,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"user-systemd failed to stop dashboard run unit: {unit}")
        return True
    if manager == "process":
        pid = managed_run_process_pid(run)
        if pid is None:
            return False
        try:
            pgid = os.getpgid(pid)
            if pgid == pid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + 10
        while managed_run_process_pid(run) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        if managed_run_process_pid(run) is not None:
            if pgid == pid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        return True
    if manager == "tmux":
        session = str(run.get("tmux_session") or run.get("manager_id") or "")
        if session and tmux_session_running(session):
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                cwd=PROJECT_ROOT,
                check=False,
            )
            return True
    return False


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
    configured = Path(verified_root).expanduser().resolve(strict=False)
    requested_value = value
    if configured == Path("/mnt/nas/database/verified"):
        for alias in ("/volume1/database/verified", "/database/verified"):
            if value == alias or value.startswith(alias + "/"):
                relative = value[len(alias) :].lstrip("/")
                requested_value = str(configured / relative)
                break
    requested = Path(requested_value).expanduser().resolve(strict=False)
    error = (
        "root_path must be the configured data root or a directory below it: "
        f"{configured}"
    )
    try:
        requested.relative_to(configured)
    except ValueError as exc:
        raise ValueError(error) from exc
    if not requested.exists() or not requested.is_dir():
        raise ValueError(error + f"; selected path does not exist or is not a directory: {requested}")
    return str(requested)


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


def validate_absolute_path(value: str, name: str) -> str:
    stripped = value.strip().rstrip("/") or "/"
    if not stripped.startswith("/") or "'" in stripped or "\n" in stripped or "\r" in stripped:
        raise ValueError(f"{name} must be an absolute path without quotes or newlines")
    return stripped


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
    configured_root = str(getattr(state, "verified_root", DEFAULT_VERIFIED_ROOT))
    configured_root_html = html.escape(configured_root, quote=True)
    configured_root_json = json.dumps(configured_root, ensure_ascii=False).replace("<", "\\u003c")
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
    .check-row {{ display: flex; align-items: center; gap: 8px; color: var(--text); margin: 10px 0; }}
    .check-row input[type="checkbox"] {{ width: 16px; height: 16px; margin: 0; padding: 0; flex: 0 0 auto; }}
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
    .failure-warning-list {{ max-height: 520px; overflow-y: auto; padding-right: 4px; user-select: text; -webkit-user-select: text; }}
    .failure-warning-empty {{ color: var(--muted); }}
    .failure-warning-item {{ padding: 8px 0; border-top: 1px solid #fde68a; }}
    .failure-warning-item:first-child {{ border-top: 0; padding-top: 0; }}
    .failure-warning-task {{ font-weight: 700; overflow-wrap: anywhere; }}
    .failure-warning-meta {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
    .failure-warning-range {{ color: #92400e; font-size: 12px; margin-top: 3px; }}
    .failure-warning-counts {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }}
    .failure-status-count {{ border: 1px solid var(--line); border-radius: 4px; background: #fff; padding: 2px 6px; font-size: 11px; font-weight: 700; }}
    .failure-status-count.fail {{ border-color: #fca5a5; color: #b91c1c; }}
    .failure-status-count.review {{ border-color: #fcd34d; color: #92400e; }}
    .failure-issue-groups {{ margin-top: 8px; }}
    .failure-issue-group {{ border-top: 1px solid #fde68a; padding-top: 6px; margin-top: 6px; }}
    .failure-issue-group-title {{ color: #6b7280; font-size: 11px; font-weight: 700; }}
    .failure-issue {{ margin-top: 4px; font-size: 12px; }}
    .failure-issue summary {{ cursor: pointer; color: #1f2937; overflow-wrap: anywhere; }}
    .failure-issue-count {{ color: #6b7280; font-variant-numeric: tabular-nums; }}
    .failure-issue-code {{ color: #6b7280; font-size: 11px; margin-top: 3px; overflow-wrap: anywhere; }}
    .failure-issue-detail {{ color: #4b5563; font-size: 11px; margin: 3px 0 0 14px; overflow-wrap: anywhere; }}
    .failure-warning-detail {{ color: #374151; font-size: 12px; margin-top: 3px; overflow-wrap: anywhere; }}
    .resolve-streak-btn {{ height: 26px; padding: 0 8px; font-size: 12px; margin-top: 6px; }}
    .resolve-streak-btn.confirm {{ background: #f59e0b; border-color: #d97706; color: #fff; }}
    .report-preview {{ max-height: 460px; overflow: auto; background: #fff; border: 1px solid var(--line); border-radius: 6px; padding: 14px; line-height: 1.55; }}
    .report-preview h1 {{ font-size: 21px; }}
    .report-preview h2 {{ font-size: 18px; margin-top: 22px; border-bottom: 1px solid var(--line); padding-bottom: 5px; }}
    .report-preview h3 {{ font-size: 16px; }}
    .report-preview code {{ background: #f2f4f7; padding: 1px 4px; border-radius: 3px; }}
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
    .rule-card {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px; margin-bottom: 10px; background: #fff; }}
    .rule-title {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; }}
    .rule-title code {{ color: var(--muted); }}
    .rule-meta {{ display: grid; grid-template-columns: 110px 1fr; gap: 6px 10px; font-size: 13px; }}
    .rule-meta b {{ color: var(--muted); font-weight: 600; }}
    .pagination {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
    .pagination select {{ width: auto; min-width: 72px; }}
    .criterion-card {{ border: 1px solid var(--line); border-radius: 6px; padding: 9px; margin-bottom: 8px; background: #f8fafc; }}
    .criterion-thresholds {{ margin: 5px 0 0 18px; padding: 0; font-size: 12px; color: var(--muted); }}
    .compact-details > summary {{ cursor: pointer; font-weight: 600; padding: 8px 0; }}
    .bounded-issues {{ max-height: 420px; overflow: auto; }}
    .operator-link, .issue-link {{ background: transparent; border: 0; padding: 0; height: auto; color: var(--accent); text-align: left; text-decoration: underline; text-underline-offset: 2px; }}
    .evidence-card {{ border: 1px solid var(--line); border-radius: 6px; margin-top: 8px; padding: 8px; }}
    .evidence-card summary {{ cursor: pointer; }}
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
        <div id="consecutiveFailureTitle" class="failure-warning-title">⚠️ 0 个连续异常组合</div>
        <div id="consecutiveFailureBody" class="failure-warning-list failure-warning-empty">暂无问题</div>
      </div>
    </div>
    <div class="panel">
      <h2>Start Run</h2>
      <div class="muted">Configured data root: <span id="configuredRootDisplay">{configured_root_html}</span></div>
      <label>Mode</label>
      <select id="mode"><option value="task_folder">Task folder</option><option value="date_range">Date range</option></select>
      <label>Task/root path</label>
      <input id="rootPath" value="{configured_root_html}">
      <div class="row" style="margin-top:6px"><button type="button" onclick="useConfiguredDataRoot()">Use configured data root</button></div>
      <div class="two">
        <div><label>Date from</label><input id="dateFrom" placeholder="20260601"></div>
        <div><label>Date to</label><input id="dateTo" placeholder="20260623"></div>
      </div>
      <div class="two">
        <div><label>Task filter</label><input id="taskFilter" placeholder="optional"></div>
        <div><label>Operator filter (exact)</label><input id="operatorFilter" placeholder="optional"></div>
      </div>
      <div class="two">
        <div><label>Phases</label><input id="phases" value="1,2,7"></div>
        <div><label>Workers</label><input id="workers" type="number" value="2" min="1" max="8"></div>
      </div>
      <div class="two">
        <div><label>Batch size</label><input id="batchSize" type="number" value="100"></div>
        <div><label>Min free mem GB</label><input id="minMem" type="number" value="6"></div>
      </div>
      <div class="two">
        <div><label>Max load ratio</label><input id="maxLoadRatio" type="number" value="0.75" min="0.1" max="2" step="0.05"></div>
        <div><label>Overload action</label><select id="overloadAction"><option value="pause" selected>Pause and resume</option><option value="stop">Stop phase and retry</option></select></div>
      </div>
      <div class="two">
        <div><label>Resource wait seconds (0 = indefinitely)</label><input id="resourceWait" type="number" value="0" min="0" max="86400"></div>
        <div><label>Resource error retries</label><input id="resourceErrorRetries" type="number" value="3" min="0" max="100"></div>
      </div>
      <div class="two">
        <div><label>Retry delay seconds</label><input id="resourceRetryDelay" type="number" value="30" min="0" max="86400"></div>
        <div class="muted" style="padding-top:24px">Default: wait until resources recover, then resume automatically.</div>
      </div>
      <label>Quality label</label>
      <input id="qualityLabel" value="完全正常">
      <label class="check-row"><input id="disableQualityLabelFilter" type="checkbox"> Full audit: include all quality labels</label>
      <label class="check-row"><input id="forceRerun" type="checkbox"> Force rerun selected phases</label>
      <label class="check-row"><input id="generateOperatorSevereReport" type="checkbox" onchange="syncSevereReportControls()"> Generate severe operator QA report</label>
      <label>Severe report config</label>
      <input id="operatorSevereConfig" value="QA_Pipeline/configs/operator_severe_report.json" disabled>
      <label class="check-row"><input id="continueAfterFail" type="checkbox" checked> Continue selected phases after an earlier failure</label>
      <div class="row"><button type="button" onclick="applySevereReportPreset()">Severe report preset</button></div>
      <div class="muted" style="margin-top:8px">The severe report lists selected QA findings linked to operator episodes. It does not make an employment decision.</div>
      <label>Run ID</label>
      <input id="runId" placeholder="optional">
      <div class="row" style="margin-top:10px"><button id="startRunButton" class="primary" type="button" onclick="startRun()">Start</button></div>
      <div id="startMsg" class="muted"></div>
    </div>
    <div class="panel">
      <h2>Event Listener</h2>
      <label>Server verified mount</label>
      <input id="eventMountPrefix" value="/mnt/nas/database/verified">
      <label>Event/NAS path prefixes</label>
      <input id="eventNasPrefix" value="/volume1/database/verified,/database/verified">
      <div class="two">
        <div><label>Phases</label><input id="eventPhases" value="1,2,3,6,7"></div>
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
      <div class="two">
        <div><label>UMI Phase 6 profile</label><select id="eventPhase6Profile"><option value="video_only">Video only</option><option value="full">Full</option></select></div>
        <div><label>UMI video output</label><select id="eventPhase6OutputMode"><option value="sidecar">Sidecar</option><option value="derived_copy">Derived copy</option></select></div>
      </div>
      <label>UMI rotated video filename</label>
      <input id="eventPhase6SidecarFilename" value="video_rotated_180.mp4">
      <label class="check-row"><input id="eventDisableQualityFilter" type="checkbox" checked> Process exact verified events regardless of quality label</label>
      <div class="row">
        <button onclick="eventAction('start')">Start</button>
        <button onclick="eventAction('restart')">Restart</button>
        <button class="danger" onclick="eventAction('stop')">Stop</button>
      </div>
      <div id="eventActionMsg" class="muted"></div>
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
      <div class="pagination">
        <span id="eventJobsPageMeta" class="muted"></span>
        <label style="margin:0">Rows <select id="eventJobsPageSize" onchange="changeEventJobsPageSize(this.value)"><option>10</option><option selected>20</option><option>50</option><option>100</option></select></label>
        <button id="eventJobsPrev" onclick="changeEventJobsPage(-1)">Previous</button>
        <button id="eventJobsNext" onclick="changeEventJobsPage(1)">Next</button>
      </div>
    </div>
    <div class="panel">
      <h2>Runs</h2>
      <div class="table-scroll"><table><thead><tr><th>Run</th><th>Mode</th><th>Operator</th><th>Status</th><th>Episodes</th><th>Issues</th><th>Updated</th><th></th></tr></thead><tbody id="runsBody"></tbody></table></div>
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
<div id="issueDrilldownModal" class="modal-backdrop" onclick="closeIssueDrilldownModal(event)">
  <div class="modal wide" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div><h2 id="issueDrilldownTitle">Issue traceback</h2><div id="issueDrilldownMeta" class="muted"></div></div>
      <button onclick="closeIssueDrilldownModal()" class="link-btn">Close</button>
    </div>
    <div id="issueDrilldownBody" class="modal-body">Loading...</div>
  </div>
</div>
<div id="ruleExplanationModal" class="modal-backdrop" onclick="closeRuleExplanationModal(event)">
  <div class="modal wide" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div>
        <h2>检测规则说明</h2>
        <div id="ruleExplanationMeta" class="muted">当前报告命中的非通过规则</div>
      </div>
      <button onclick="closeRuleExplanationModal()" class="link-btn">Close</button>
    </div>
    <div class="modal-body">
      <input id="ruleExplanationFilter" placeholder="搜索问题类型、中文标题或检测阶段" oninput="renderRuleExplanationRows()" style="margin-bottom:10px">
      <div id="ruleExplanationBody"></div>
    </div>
  </div>
</div>
<div id="checkNameTooltip" role="tooltip"></div>
<script>
const refreshMs = {refresh};
const configuredDataRoot = {configured_root_json};
let selectedRun = null;
let selectedEventJob = null;
let latestRuleExplanations = [];
let eventJobsSort = {{key: 'id', direction: 'desc'}};
let eventJobsCache = [];
let eventJobsPage = 1;
let eventJobsPageSize = 20;
let eventJobsTotalPages = 1;
let eventJobsTotal = 0;
let issueDrilldownState = {{runId: '', checkName: '', operator: '', page: 1, pageSize: 20}};
let severeRankingPage = 1;
let severeRankingPageSize = 20;
let selectedSevereReport = null;
let selectedSevereRun = null;
let eventIssueSummaryCache = {{}};
let eventContextSummarySort = {{key: null, direction: 'asc'}};
let eventIssueSummarySort = {{key: null, direction: 'asc'}};
let refreshInFlight = false;
let lastHeavyRefresh = 0;
let eventSettingsInitialized = false;
let pendingResolveKey = null;
let pendingResolveTimer = null;
let pendingRecheckKey = null;
let pendingRecheckTimer = null;
let consecutiveFailuresState = {{count: 0, warnings: [], hidden_count: 0}};
let consecutiveFailureRenderFingerprint = '';
let consecutiveFailureRenderDeferred = false;
let consecutiveFailurePointerDown = false;
let consecutiveFailureSelectionTimer = null;
const locallyResolvedFailureKeys = new Set();

async function getJson(url) {{
  const r = await fetch(url, {{cache: 'no-store'}});
  return await r.json();
}}

async function postJson(url, body) {{
  const r = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body || {{}})}});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${{r.status}}`);
  return data;
}}

function cls(status) {{ return 'status-' + String(status || '').replace(/_/g, '-'); }}
function val(obj, path, fallback='-') {{
  try {{ return path.split('.').reduce((o,k)=>o[k], obj) ?? fallback; }} catch(e) {{ return fallback; }}
}}
function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function jsArg(value) {{
  return esc(JSON.stringify(String(value ?? '')));
}}

function closeRuleExplanationModal(event) {{
  if (event && event.target && event.target.id !== 'ruleExplanationModal') return;
  document.getElementById('ruleExplanationModal').classList.remove('open');
}}

function openRuleExplanationModal() {{
  const modal = document.getElementById('ruleExplanationModal');
  const filter = document.getElementById('ruleExplanationFilter');
  if (filter) filter.value = '';
  renderRuleExplanationRows();
  modal.classList.add('open');
}}

function renderRuleExplanationRows() {{
  const body = document.getElementById('ruleExplanationBody');
  const meta = document.getElementById('ruleExplanationMeta');
  const filter = String((document.getElementById('ruleExplanationFilter') || {{}}).value || '').trim().toLowerCase();
  const rows = (latestRuleExplanations || []).filter(row => {{
    if (!filter) return true;
    return [
      row.check_name,
      row.rule_title_zh,
      row.phase,
      row.description_zh,
      row.standard_zh,
      row.threshold_summary,
      row.evidence_fields
    ].some(value => String(value || '').toLowerCase().includes(filter));
  }});
  if (meta) meta.textContent = `${{rows.length}} / ${{(latestRuleExplanations || []).length}} 条规则`;
  if (!rows.length) {{
    body.innerHTML = '<div class="muted">当前报告暂无可显示的检测规则说明。</div>';
    return;
  }}
  body.innerHTML = rows.map(row => `
    <div class="rule-card">
      <div class="rule-title">
        <b>${{esc(row.rule_title_zh || row.check_name || '未命名规则')}}</b>
        <code>${{esc(row.check_name || '')}}</code>
      </div>
      <div class="rule-meta">
        <b>检测阶段</b><div>${{esc(row.phase || '未填写')}}</div>
        <b>检测说明</b><div>${{esc(row.description_zh || '未填写')}}</div>
        <b>判定标准</b><div>${{esc(row.standard_zh || '未填写')}}</div>
        <b>阈值</b><div>${{esc(row.threshold_summary || '未填写')}}</div>
        <b>严重程度</b><div>${{esc(row.severity_rule_zh || '未填写')}}</div>
        <b>证据字段</b><div>${{esc(row.evidence_fields || '未填写')}}</div>
      </div>
    </div>
  `).join('');
}}

async function copyEventPath(button) {{
  const text = button.dataset.copyPath || '';
  const original = button.textContent;
  try {{
    if (navigator.clipboard && window.isSecureContext) {{
      await navigator.clipboard.writeText(text);
    }} else {{
      fallbackCopyText(text);
    }}
    button.textContent = '已复制';
  }} catch (error) {{
    button.textContent = '复制失败';
  }}
  window.setTimeout(() => {{
    button.textContent = original;
  }}, 1500);
}}

function fallbackCopyText(text) {{
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(textarea);
  if (!ok) throw new Error('copy command failed');
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
    const listenerRunning = val(data,'event_listener.running',val(data,'event_listener.tmux_running',false));
    const listenerManager = val(data,'event_listener.manager','none');
    const listenerError = val(data,'event_listener.last_start_error','');
    document.getElementById('eventBox').innerHTML =
      '<div>实时检测服务：<b>' + (listenerRunning ? '运行中' : '已停止') + '</b>（托管方式：' + esc(listenerManager) + '）</div>' +
      '<div>正在处理：' + val(data,'event_listener.counts.running',0) + '</div>' +
      '<div>等待处理：' + val(data,'event_listener.counts.pending',0) + '</div>' +
      '<div>并发数：' + esc(settings.workers || '-') + '，批大小：' + esc(settings.batch_size || '-') + '，检测阶段：' + esc(settings.phases || '-') + '</div>' +
      '<div>路径映射：' + esc(settings.nas_prefix || '-') + ' → ' + esc(settings.mount_prefix || '-') + '</div>' +
      '<div>UMI 第 6 阶段：配置 ' + esc(settings.phase6_profile || '-') + '，输出方式 ' + esc(settings.phase6_video_output_mode || '-') + '，文件名 ' + esc(settings.phase6_sidecar_filename || '-') + '</div>' +
      '<div>日期筛选：指定日期 ' + esc(settings.event_date || '-') + '，开始 ' + esc(settings.event_date_from || '-') + '，结束 ' + esc(settings.event_date_to || '-') + '</div>' +
      '<div>通知：启用 ' + esc(settings.dcs_notify_enabled || '-') + '，试运行 ' + esc(settings.dcs_notify_dry_run || '-') + '，等待结果 ' + esc(settings.dcs_notify_wait || '-') + '</div>' +
      '<div>资源保护：最大负载比例 ' + esc(settings.max_load_ratio || '-') + '，最少可用内存 ' + esc(settings.min_free_mem_gb || '-') + ' GB</div>' +
      '<div>输出目录大小：' + val(data,'event_listener.output_size','-') + '</div>' +
      (listenerError ? '<div class="status-fail">最近一次启动错误：' + esc(listenerError) + '</div>' : '') +
      '<div class="muted">服务运行中时，修改以上参数后需要点击“重启”才能生效。</div>';
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
    eventMountPrefix: settings.mount_prefix,
    eventNasPrefix: settings.nas_prefix,
    eventWorkers: settings.workers,
    eventBatchSize: settings.batch_size,
    eventDate: settings.event_date,
    eventDateFrom: settings.event_date_from,
    eventDateTo: settings.event_date_to,
    eventMaxLoadRatio: settings.max_load_ratio,
    eventMinMem: settings.min_free_mem_gb,
    eventResourceWait: settings.resource_max_wait_seconds,
    eventStabilityInterval: settings.stability_interval,
    eventStabilityTimeout: settings.stability_timeout,
    eventPhase6Profile: settings.phase6_profile,
    eventPhase6OutputMode: settings.phase6_video_output_mode,
    eventPhase6SidecarFilename: settings.phase6_sidecar_filename
  }};
  for (const [id, value] of Object.entries(mapping)) {{
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== null && value !== '') el.value = value;
  }}
  const qualityFilter = document.getElementById('eventDisableQualityFilter');
  if (qualityFilter && settings.quality_label_filter_disabled !== undefined) {{
    qualityFilter.checked = Boolean(settings.quality_label_filter_disabled);
  }}
  eventSettingsInitialized = true;
}}

function renderConsecutiveFailures(summary) {{
  const rawWarnings = summary.warnings || [];
  const warnings = rawWarnings.filter(item => !locallyResolvedFailureKeys.has(consecutiveFailureKey(item)));
  const removedCount = rawWarnings.length - warnings.length;
  const count = Math.max(0, Number(summary.count ?? rawWarnings.length) - removedCount);
  const normalized = {{
    ...summary,
    count,
    warnings,
    hidden_count: Math.max(0, count - warnings.length),
  }};
  consecutiveFailuresState = normalized;
  document.getElementById('consecutiveFailureTitle').textContent =
    `⚠️ ${{count}} 个连续异常组合`;
  const body = document.getElementById('consecutiveFailureBody');
  const fingerprint = JSON.stringify({{
    count: normalized.count,
    hidden_count: normalized.hidden_count,
    warnings: normalized.warnings,
    pending_resolve: pendingResolveKey,
    pending_recheck: pendingRecheckKey,
  }});
  if (consecutiveFailurePointerDown || consecutiveFailureHasSelection(body)) {{
    consecutiveFailureRenderDeferred = fingerprint !== consecutiveFailureRenderFingerprint;
    return;
  }}
  if (fingerprint === consecutiveFailureRenderFingerprint) return;
  const previousScrollTop = body.scrollTop;
  if (!warnings.length) {{
    body.className = 'failure-warning-list failure-warning-empty';
    body.textContent = '暂无连续异常组合';
    consecutiveFailureRenderFingerprint = fingerprint;
    consecutiveFailureRenderDeferred = false;
    return;
  }}
  body.className = 'failure-warning-list';
  body.innerHTML = warnings.map(item => {{
    const operator = item.operator ? ` · ${{esc(item.operator)}}` : '';
    const contextDate = item.context_date ? ` · ${{esc(item.context_date)}}` : '';
    const collectorText = consecutiveCollectorText(item.collector_ids || []);
    const statusCounts = consecutiveStatusCountsHtml(item.episode_status_counts || {{}}, item.streak_length || 0);
    const issueGroups = consecutiveIssueGroupsHtml(item.issue_locations || [], item.streak_length || 0);
    const recheckText = consecutiveFailureRecheckText(item.recheck_status || {{}}, item.recheck_requested_at || '');
    const resolveKey = consecutiveFailureKey(item);
    const confirming = pendingResolveKey === resolveKey;
    const confirmingRecheck = pendingRecheckKey === resolveKey;
    const collectorData = JSON.stringify(item.collector_ids || []);
    return `<div class="failure-warning-item">
      <div class="failure-warning-task">${{esc(item.task || '-')}}</div>
      <div class="failure-warning-meta">${{esc(item.robot || '-')}}${{operator}}${{contextDate}}</div>
      <div class="failure-warning-detail">采集设备：${{esc(collectorText)}}</div>
      <div class="failure-warning-range">${{esc(item.message || '')}}</div>
      ${{statusCounts}}
      ${{issueGroups}}
      ${{recheckText ? `<div class="failure-warning-detail">重检：${{esc(recheckText)}}</div>` : ''}}
      <button class="resolve-streak-btn${{confirmingRecheck ? ' confirm' : ''}}"
        data-task="${{esc(item.task || '')}}"
        data-robot="${{esc(item.robot || '')}}"
        data-operator="${{esc(item.operator || '')}}"
        data-context-date="${{esc(item.context_date || '')}}"
        data-episode-start="${{item.start_episode_number}}"
        data-episode-end="${{item.end_episode_number}}"
        data-collector-ids="${{esc(collectorData)}}"
        onclick="recheckConsecutiveFailureClick(event, this)">${{confirmingRecheck ? '确认重检？' : '重新检查'}}</button>
      <button class="resolve-streak-btn${{confirming ? ' confirm' : ''}}"
        data-task="${{esc(item.task || '')}}"
        data-robot="${{esc(item.robot || '')}}"
        data-operator="${{esc(item.operator || '')}}"
        data-context-date="${{esc(item.context_date || '')}}"
        data-episode-start="${{item.start_episode_number}}"
        data-episode-end="${{item.end_episode_number}}"
        onclick="resolveConsecutiveFailureClick(event, this)">${{confirming ? '确认解决？' : '标记已解决'}}</button>
    </div>`;
  }}).join('') + (normalized.hidden_count ? `<div class="failure-warning-more">+${{normalized.hidden_count}} more</div>` : '');
  body.scrollTop = Math.min(previousScrollTop, Math.max(0, body.scrollHeight - body.clientHeight));
  consecutiveFailureRenderFingerprint = fingerprint;
  consecutiveFailureRenderDeferred = false;
}}

function consecutiveFailureHasSelection(body) {{
  const selection = window.getSelection?.();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
  return [selection.anchorNode, selection.focusNode].some(node => node && body.contains(node));
}}

function flushDeferredConsecutiveFailureRender() {{
  const body = document.getElementById('consecutiveFailureBody');
  if (!body || consecutiveFailurePointerDown || consecutiveFailureHasSelection(body)) return;
  if (consecutiveFailureRenderDeferred) renderConsecutiveFailures(consecutiveFailuresState);
}}

function consecutiveCollectorText(collectorIds) {{
  const known = collectorIds.filter(value => value && value !== 'unknown_collector');
  return known.length ? known.join(', ') : '无法识别';
}}

function consecutiveStatusCountsHtml(counts, streakLength) {{
  const fail = Number(counts.fail || 0);
  const review = Number(counts.needs_review || 0);
  const known = fail + review;
  const fallback = Math.max(0, Number(streakLength || 0) - known);
  const parts = [];
  if (fail) parts.push(`<span class="failure-status-count fail">不合格 ${{fail}}</span>`);
  if (review) parts.push(`<span class="failure-status-count review">待复核 ${{review}}</span>`);
  if (fallback) parts.push(`<span class="failure-status-count">异常 ${{fallback}}</span>`);
  return `<div class="failure-warning-counts">${{parts.join('')}}</div>`;
}}

function consecutiveIssueGroupsHtml(items, streakLength) {{
  const labels = {{
    failure: '不合格项',
    review: '待复核项',
    operator_label: '操作员质量标记',
    warning: '警告项',
  }};
  const order = ['failure', 'review', 'operator_label', 'warning'];
  const groups = {{}};
  for (const item of items) {{
    const category = order.includes(item.category) ? item.category : 'warning';
    (groups[category] ||= []).push(item);
  }}
  return `<div class="failure-issue-groups">${{order.map(category => {{
    const rows = groups[category] || [];
    if (!rows.length) return '';
    return `<div class="failure-issue-group">
      <div class="failure-issue-group-title">${{labels[category]}}</div>
      ${{rows.map(item => consecutiveIssueHtml(item, streakLength)).join('')}}
    </div>`;
  }}).join('')}}</div>`;
}}

function consecutiveIssueHtml(item, streakLength) {{
  const title = item.title_zh || item.check_name || '未知问题';
  const affected = Number(item.affected_episode_count || 0);
  const findingCount = Number(item.count || 0);
  const countText = affected
    ? `${{affected}} / ${{Number(streakLength || affected)}} 个 episode`
    : `${{findingCount}} 条发现`;
  const ranges = (item.episode_ranges || []).join('，');
  const locations = (item.locations || []).map(location =>
    `${{location.location || 'episode 级检查'}} (${{Number(location.count || 0)}} 条)`
  ).join('；');
  return `<details class="failure-issue">
    <summary>${{esc(title)}} <span class="failure-issue-count">${{esc(countText)}}</span></summary>
    <div class="failure-issue-code">${{esc(item.check_name || '')}}</div>
    ${{ranges ? `<div class="failure-issue-detail">涉及 episode：${{esc(ranges)}}</div>` : ''}}
    ${{locations ? `<div class="failure-issue-detail">涉及位置：${{esc(locations)}}</div>` : ''}}
    ${{findingCount > affected && affected ? `<div class="failure-issue-detail">共 ${{findingCount}} 条模态或分段发现</div>` : ''}}
  </details>`;
}}

function consecutiveFailureRecheckText(status, requestedAt) {{
  if (!requestedAt) return '';
  const state = status.state || 'pending';
  const counts = status.counts || {{}};
  const parts = Object.entries(counts).map(([key, value]) => `${{key}}=${{value}}`);
  return `${{state}}${{parts.length ? ' (' + parts.join(', ') + ')' : ''}}`;
}}

function consecutiveFailureKey(item) {{
  return JSON.stringify([
    item.task || '',
    item.robot || '',
    item.operator || '',
    item.context_date || item.date || '',
    Number(item.start_episode_number ?? item.episode_start ?? 0),
    Number(item.end_episode_number ?? item.episode_end ?? 0),
  ]);
}}

function consecutiveFailureButtonKey(button) {{
  return consecutiveFailureKey({{
    task: button.dataset.task,
    robot: button.dataset.robot,
    operator: button.dataset.operator,
    context_date: button.dataset.contextDate,
    episode_start: button.dataset.episodeStart,
    episode_end: button.dataset.episodeEnd,
  }});
}}

function resetPendingResolveButton() {{
  if (pendingResolveTimer) window.clearTimeout(pendingResolveTimer);
  pendingResolveTimer = null;
  pendingResolveKey = null;
  document.querySelectorAll('.resolve-streak-btn.confirm').forEach(button => {{
    if (button.textContent === '确认解决？') {{
      button.classList.remove('confirm');
      button.textContent = '标记已解决';
    }}
  }});
}}

function resetPendingRecheckButton() {{
  if (pendingRecheckTimer) window.clearTimeout(pendingRecheckTimer);
  pendingRecheckTimer = null;
  pendingRecheckKey = null;
  document.querySelectorAll('.resolve-streak-btn.confirm').forEach(button => {{
    if (button.textContent === '确认重检？') {{
      button.classList.remove('confirm');
      button.textContent = '重新检查';
    }}
  }});
}}

async function resolveConsecutiveFailureClick(event, button) {{
  event.stopPropagation();
  const resolveKey = consecutiveFailureButtonKey(button);
  if (pendingResolveKey !== resolveKey) {{
    resetPendingRecheckButton();
    resetPendingResolveButton();
    pendingResolveKey = resolveKey;
    button.classList.add('confirm');
    button.textContent = '确认解决？';
    pendingResolveTimer = window.setTimeout(resetPendingResolveButton, 3000);
    return;
  }}
  const payload = {{
    task: button.dataset.task || '',
    robot: button.dataset.robot || '',
    operator: button.dataset.operator || '',
    context_date: button.dataset.contextDate || '',
    episode_start: Number(button.dataset.episodeStart || 0),
    episode_end: Number(button.dataset.episodeEnd || 0),
  }};
  resetPendingResolveButton();
  locallyResolvedFailureKeys.add(resolveKey);
  renderConsecutiveFailures(consecutiveFailuresState);
  try {{
    const data = await postJson('/api/consecutive-failures/resolve', payload);
    if (!data.ok) throw new Error('No matching unresolved streak was found');
    renderConsecutiveFailures(data.consecutive_failures || consecutiveFailuresState);
  }} catch (error) {{
    locallyResolvedFailureKeys.delete(resolveKey);
    renderConsecutiveFailures(consecutiveFailuresState);
    console.error('Failed to resolve consecutive-failure streak', error, payload);
    button.textContent = '解决失败';
    window.setTimeout(() => {{ button.textContent = '标记已解决'; }}, 2000);
  }}
}}

async function recheckConsecutiveFailureClick(event, button) {{
  event.stopPropagation();
  const resolveKey = consecutiveFailureButtonKey(button);
  if (pendingRecheckKey !== resolveKey) {{
    resetPendingResolveButton();
    resetPendingRecheckButton();
    pendingRecheckKey = resolveKey;
    button.classList.add('confirm');
    button.textContent = '确认重检？';
    pendingRecheckTimer = window.setTimeout(resetPendingRecheckButton, 3000);
    return;
  }}
  let collectorIds = [];
  try {{ collectorIds = JSON.parse(button.dataset.collectorIds || '[]'); }} catch (error) {{ collectorIds = []; }}
  const payload = {{
    task: button.dataset.task || '',
    robot: button.dataset.robot || '',
    operator: button.dataset.operator || '',
    context_date: button.dataset.contextDate || '',
    episode_start: Number(button.dataset.episodeStart || 0),
    episode_end: Number(button.dataset.episodeEnd || 0),
    collector_ids: collectorIds,
  }};
  resetPendingRecheckButton();
  button.textContent = '重检入队中';
  button.disabled = true;
  try {{
    const data = await postJson('/api/consecutive-failures/recheck', payload);
    if (!data.ok) throw new Error('Recheck request failed');
    renderConsecutiveFailures(data.consecutive_failures || consecutiveFailuresState);
    await refreshEventJobs();
  }} catch (error) {{
    console.error('Failed to recheck consecutive-failure streak', error, payload);
    button.disabled = false;
    button.textContent = '重检失败';
    window.setTimeout(() => {{ button.textContent = '重新检查'; }}, 2000);
  }}
}}

document.addEventListener('click', event => {{
  if (!event.target.closest || !event.target.closest('.resolve-streak-btn')) {{
    resetPendingResolveButton();
    resetPendingRecheckButton();
  }}
}});

document.addEventListener('pointerdown', event => {{
  consecutiveFailurePointerDown = Boolean(event.target.closest?.('#consecutiveFailureBody'));
}});

document.addEventListener('pointerup', () => {{
  if (!consecutiveFailurePointerDown) return;
  consecutiveFailurePointerDown = false;
  window.setTimeout(flushDeferredConsecutiveFailureRender, 0);
}});

document.addEventListener('selectionchange', () => {{
  if (consecutiveFailureSelectionTimer) window.clearTimeout(consecutiveFailureSelectionTimer);
  consecutiveFailureSelectionTimer = window.setTimeout(flushDeferredConsecutiveFailureRender, 150);
}});

window.addEventListener('blur', () => {{
  consecutiveFailurePointerDown = false;
}});

function renderRuns(runs) {{
  const body = document.getElementById('runsBody');
  body.innerHTML = runs.map(run => {{
    const counts = run.status_counts || {{}};
    const status = run.status || val(run,'live_status.status','');
    const active = Boolean(run.manager_running) || status === 'running';
    const canResume = !active && Boolean(run.command) && ['stopped', 'failed'].includes(status);
    const actions = active
      ? `<button onclick="event.stopPropagation(); stopRun('${{run.run_id}}')">Stop</button>`
      : (canResume ? `<button onclick="event.stopPropagation(); resumeRun('${{run.run_id}}')">Resume</button>` : '');
    return `<tr class="run-row" onclick="loadRun('${{run.run_id}}', true)">
      <td>${{run.run_id}}</td><td>${{run.mode || ''}}</td>
      <td>${{esc(run.operator_filter || '-')}}</td>
      <td class="${{cls(status)}}">${{status}}</td>
      <td>${{run.episode_count || 0}}</td><td>${{run.finding_count || 0}}</td>
      <td>${{localTimestamp(run.updated_at)}}</td>
      <td>${{actions}}</td>
    </tr>`;
  }}).join('');
}}

async function refreshEventJobs() {{
  const data = await getJson('/api/event-listener/jobs?page=' + eventJobsPage + '&page_size=' + eventJobsPageSize);
  eventJobsCache = data.jobs || [];
  eventJobsPage = Number(data.page || 1);
  eventJobsPageSize = Number(data.page_size || eventJobsPageSize);
  eventJobsTotalPages = Number(data.total_pages || 1);
  eventJobsTotal = Number(data.total || 0);
  renderEventJobs(eventJobsCache);
}}

async function changeEventJobsPage(delta) {{
  const target = Math.max(1, Math.min(eventJobsTotalPages, eventJobsPage + Number(delta || 0)));
  if (target === eventJobsPage) return;
  eventJobsPage = target;
  await refreshEventJobs();
}}

async function changeEventJobsPageSize(value) {{
  eventJobsPageSize = Math.max(1, Math.min(100, Number(value || 20)));
  eventJobsPage = 1;
  await refreshEventJobs();
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
  const labels = {{critical:'致命', major:'严重', minor:'一般', info:'提示', fail:'失败', needs_review:'需要复查', warning:'警告', pass:'通过'}};
  return `<span class="severity-chip ${{cls(name)}}">${{esc(labels[name] || name)}} ${{count}}</span>`;
}}

function qaLabel(name) {{
  const labels = {{critical:'致命', major:'严重', minor:'一般', info:'提示', fail:'失败', needs_review:'需要复查', warning:'警告', pass:'通过', pending:'待处理', running:'运行中', done:'完成', failed:'执行失败'}};
  return labels[String(name || '')] || String(name || '');
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
  if (job.pipeline_error) {{
    return `<div class="issue-summary status-failed" title="${{esc(job.pipeline_error)}}">质检流程执行失败</div>`;
  }}
  if (job.qa_result_missing) {{
    return `<div class="issue-summary status-warning" title="${{esc(job.qa_missing_reason || '')}}">未生成质检结果</div>`;
  }}
  const issues = job.top_issues || [];
  if (!issues.length) {{
    return `<div class="issue-summary issue-summary-empty">${{job.issue_count || 0}}</div>`;
  }}
  const issueText = issues.map(issue =>
    `<span class="issue-summary-head">${{esc(issue.issue_title_zh || issue.check_name)}} (${{issue.count || 0}})</span>`
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
  const location = finding.location_zh ? `<div><b>问题位置：</b>${{esc(finding.location_zh)}}</div>` : '';
  const timeRange = finding.time_range_zh ? `<div><b>发生区间：</b>${{esc(finding.time_range_zh)}}</div>` : '';
  const evidence = finding.evidence_summary_zh ? `<div><b>检测证据：</b>${{esc(finding.evidence_summary_zh)}}</div>` : '';
  return `<div><b>发生情况：</b>${{esc(finding.occurrence_zh || finding.message_zh || finding.message || '')}}</div>` +
    `<div><b>报告原因：</b>${{esc(finding.trigger_reason_zh || '')}}</div>` + location + timeRange + evidence +
    `<div><b>处理建议：</b>${{esc(finding.recommended_action_zh || '')}}</div>`;
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
  const meta = `最近 ${{summary.job_count || 0}} 个任务中有 ${{summary.issue_episode_count || 0}} 个问题 episode，共 ${{summary.finding_count || 0}} 条问题记录`;
  document.getElementById('eventSummaryCount').textContent = summary.issue_episode_count || 0;
  document.getElementById('eventIssueSummaryMeta').textContent = meta;
  document.getElementById('eventSummaryModalMeta').textContent = meta;
  const sevEntries = Object.entries(summary.severity_counts || {{}});
  const severityHtml =
    sevEntries.length ? sevEntries.map(([name,count]) => severityBadge(name, count)).join('') : '<span class="muted">近期事件任务中未发现问题。</span>';
  document.getElementById('eventSeverityStrip').innerHTML = severityHtml;
  document.getElementById('eventSummaryModalSeverity').innerHTML = severityHtml;
  const lead = (summary.by_context || [])[0];
  document.getElementById('eventSummaryLead').textContent = lead
    ? `问题最多的组合：${{lead.task || '-'}} / ${{lead.robot || '-'}} / ${{lead.operator || '-'}}，共 ${{lead.finding_count || 0}} 条`
    : '暂无可汇总的问题组合。';
  renderEventSummaryTables(summary);
}}

function renderEventSummaryTables(summary) {{
  updateEventSummarySortIndicators();
  document.getElementById('eventContextSummaryBody').innerHTML = [...(summary.by_context || [])].sort(compareEventContextSummaryRows).map(row => {{
    const issues = (row.top_issues || []).map(i => `${{esc(i.issue_title_zh || i.check_name)}} (${{i.count}})`).join(', ');
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
      <td><b>${{esc(row.issue_title_zh || row.check_name || '')}}</b><div class="muted">${{esc(row.check_name || '')}}</div></td>
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
    const qa = job.final_status || (job.qa_result_missing ? 'skipped' : (job.status === 'done' ? 'complete' : ''));
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
  const meta = document.getElementById('eventJobsPageMeta');
  if (meta) meta.textContent = `Page ${{eventJobsPage}}/${{eventJobsTotalPages}} · ${{eventJobsTotal}} episodes · sorting applies to this page`;
  const pageSize = document.getElementById('eventJobsPageSize');
  if (pageSize) pageSize.value = String(eventJobsPageSize);
  const previous = document.getElementById('eventJobsPrev');
  const next = document.getElementById('eventJobsNext');
  if (previous) previous.disabled = eventJobsPage <= 1;
  if (next) next.disabled = eventJobsPage >= eventJobsTotalPages;
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
    `<tr><td>${{f.phase}}</td><td><b>${{esc(f.issue_title_zh || f.check_name)}}</b><div class="muted">${{esc(f.check_name)}}</div></td><td>${{esc(qaLabel(f.severity))}}</td><td class="${{cls(f.status)}}">${{esc(qaLabel(f.status))}}</td><td>${{findingMessageHtml(f)}}</td></tr>`
  ).join('') : '<tr><td colspan="5" class="muted">没有非通过问题。</td></tr>';
  const phaseStatus = Object.entries(job.phase_status || {{}}).map(([phase,status]) => `P${{phase}}=${{status}}`).join(', ');
  const topIssues = (job.top_issues || []).map(i => `${{esc(i.issue_title_zh || i.check_name)}}(${{i.count}})`).join(', ') || '无';
  const pipelineError = job.pipeline_error ? `<div><b>流程错误：</b> <span class="status-failed">${{esc(job.pipeline_error)}}</span></div>` : '';
  const qaMissing = job.qa_result_missing ? `<div><b>质检结果：</b> <span class="status-warning">${{esc(job.qa_missing_reason || '未生成质检结果。')}}</span></div>` : '';
  const mountedPath = job.mounted_path || '';
  const copyPath = job.nas_internal_path || mountedPath;
  document.getElementById('eventJobDetail').innerHTML =
    `<div class="row"><b>任务 ${{job.id}}</b><span class="${{cls(job.status)}}">${{esc(qaLabel(job.status))}}</span><span class="${{cls(job.final_status)}}">${{esc(qaLabel(job.final_status) || '-')}}</span></div>` +
    `<div><b>Episode：</b>${{esc(job.episode_name || '')}}</div>` +
    `<div class="path-field"><b>路径：</b><span>${{esc(mountedPath)}}</span><button class="copy-path-btn" data-copy-path="${{esc(copyPath)}}" onclick="copyEventPath(this)">复制路径</button></div>` +
    `<div><b>任务：</b>${{esc(job.task || '')}} | <b>机器人：</b>${{esc(job.robot || '')}} | <b>操作员：</b>${{esc(job.operator || '')}}</div>` +
    pipelineError +
    qaMissing +
    `<div><b>阶段状态：</b>${{esc(phaseStatus || '-')}}</div>` +
    `<div><b>主要问题：</b>${{topIssues}}</div>` +
    `<div><b>输出目录：</b>${{esc(job.output_dir || '')}}</div>` +
    `<h3>Episode 问题明细</h3>` +
    `<div class="table-scroll"><table><thead><tr><th>阶段</th><th>问题</th><th>严重程度</th><th>状态</th><th>发生情况与原因</th></tr></thead><tbody>${{issueRows}}</tbody></table></div>`;
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
  if (select) {{
    selectedRun = runId;
    severeRankingPage = 1;
  }}
  const data = await getJson('/api/runs/' + encodeURIComponent(runId));
  const run = data.run || {{}};
  const counts = run.status_counts || {{}};
  const live = run.live_status || run.run_status || {{}};
  const issueEpisodes = run.issue_episodes || [];
  const report = run.latest_work_session_report || {{}};
  const severeReport = run.operator_severe_report || {{state: 'not_requested'}};
  const issueEpisodeRows = issueEpisodes.length ? issueEpisodes.map(ep =>
    `<tr class="run-row"><td>${{esc(ep.episode_name)}}</td><td class="${{cls(ep.final_status)}}">${{esc(qaLabel(ep.final_status))}}</td><td class="task-cell">${{wrapTaskTextHtml(ep.task)}}</td><td>${{esc(ep.robot)}}</td><td>${{ep.issue_count}}</td><td>${{esc((ep.issue_titles_zh || ep.issue_names || []).join('，'))}}</td></tr>`
  ).join('') : '<tr><td colspan="6" class="muted">本次运行没有问题 episode。</td></tr>';
  document.getElementById('runDetail').innerHTML =
    `<div class="row"><b>${{run.run_id}}</b><span class="${{cls(run.status)}}">${{run.status}}</span></div>` +
    `<div>Scope: ${{esc(run.date_from || '-')}} to ${{esc(run.date_to || '-')}} | Task: ${{esc(run.task_filter || 'all')}} | Operator: ${{esc(run.operator_filter || 'all')}}</div>` +
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
    `<h3>Severe operator QA report</h3>` +
    `<div id="operatorSevereReportContent">${{renderOperatorSevereReport(severeReport, run)}}</div>` +
    `<h3>Top Issues</h3>` +
    `<div class="table-scroll"><table><thead><tr><th>问题</th><th>问题记录数</th><th>影响 episode 数</th></tr></thead><tbody>${{(run.top_issues || []).map(i=>`<tr><td><button class="issue-link" onclick="openIssueDrilldown(${{jsArg(run.run_id)}}, ${{jsArg(i.check_name)}})">${{esc(i.issue_title_zh || i.check_name)}}</button><div class="muted">${{esc(i.check_name)}}</div></td><td>${{i.count}}</td><td>${{i.episode_count || 0}}</td></tr>`).join('')}}</tbody></table></div>` +
    `<details class="compact-details"><summary>Episodes With Issues (${{issueEpisodes.length}} shown)</summary>` +
    `<div class="table-scroll wide bounded-issues"><table><thead><tr><th>Episode</th><th>Status</th><th class="task-cell">Task</th><th>Robot</th><th>Issues</th><th>Issue Names</th></tr></thead><tbody>${{issueEpisodeRows}}</tbody></table></div></details>`;
  await loadLog(runId);
}}

function renderOperatorSevereReport(report, run) {{
  selectedSevereReport = report;
  selectedSevereRun = run;
  const state = report.state || 'not_requested';
  if (state === 'not_requested') return '<div class="muted">Report state: not_requested</div>';
  if (state === 'pending') return '<div class="status-pending">The pipeline is still running. The severe report will be generated after successful completion.</div>';
  const regenerateButton = run && run.status === 'complete'
    ? `<button onclick="regenerateOperatorSevereReport(${{jsArg(run.run_id)}})">Regenerate report / 重新生成报告</button><span id="severeReportActionMsg" class="muted"></span>`
    : '';
  if (state === 'missing') return `<div class="status-warning">The run completed, but the requested severe report is missing.</div><div class="row" style="margin-top:8px">${{regenerateButton}}</div>`;
  if (state === 'invalid') return `<div class="status-fail">Invalid severe report: ${{esc(report.error || 'unknown parsing error')}}</div><div class="row" style="margin-top:8px">${{regenerateButton}}</div>`;
  const summary = report.summary || {{}};
  const frequency = report.criterion_frequency || [];
  const ranking = report.operator_ranking || [];
  const criteria = (report.criteria || []).filter(item => item && item.id);
  const criterionColumns = criteria.map(item => ({{
    id: item.id,
    label: item.label || item.id,
    field: item.id + '_episode_count'
  }}));
  const criterionById = Object.fromEntries(criteria.map(item => [item.id, item]));
  const criterionCards = criteria.map(item => `<div class="criterion-card">
    <div><b>${{esc(item.label || item.id)}}</b> / <b>${{esc(item.label_zh || '')}}</b> <code>${{esc(item.id)}}</code></div>
    <div>${{esc(item.description || '')}}</div><div>${{esc(item.description_zh || '')}}</div>
    <div class="thresholds"><b>Thresholds / 阈值：</b><br>${{(item.thresholds || []).map(value => esc(value)).join('<br>')}}${{(item.thresholds_zh || []).length ? '<br>' + item.thresholds_zh.map(value => esc(value)).join('<br>') : ''}}</div>
    <div class="muted">Checks: ${{esc([...(item.check_names || []), ...(item.check_name_patterns || [])].join(', '))}} · Status: ${{esc((item.statuses || []).join(', ') || 'any')}}</div>
  </div>`).join('');
  const frequencyRows = frequency.length ? frequency.map(row => {{
    const criterion = criterionById[row.criterion_id] || {{}};
    return `<tr>
    <td><code>${{esc(row.criterion_id || '')}}</code></td><td>${{esc(row.criterion_label || criterion.label || '')}}<br><span class="muted">${{esc(criterion.label_zh || '')}}</span></td>
    <td class="numeric-cell">${{row.affected_episode_count || 0}}</td>
    <td class="numeric-cell">${{row.affected_named_operator_count ?? row.affected_operator_count ?? 0}}</td>
    <td class="numeric-cell">${{row.affected_unknown_operator_episode_count ?? 0}}</td>
  </tr>`;
  }}).join('') : '<tr><td colspan="5" class="muted">No criterion hits.</td></tr>';
  const rankingPages = Math.max(1, Math.ceil(ranking.length / severeRankingPageSize));
  severeRankingPage = Math.max(1, Math.min(rankingPages, severeRankingPage));
  const rankingStart = (severeRankingPage - 1) * severeRankingPageSize;
  const rankingRows = ranking.length ? ranking.slice(rankingStart, rankingStart + severeRankingPageSize).map(row => `<tr>
    <td>${{esc(row.operator || '')}}</td>
    <td class="numeric-cell">${{row.total_episode_count || 0}}</td>
    <td class="numeric-cell">${{row.severe_episode_count || 0}}</td>
    <td class="numeric-cell">${{row.severe_episode_rate_percent || 0}}%</td>
    <td class="numeric-cell">${{row.criterion_hit_count || 0}}</td>
    ${{criterionColumns.map(column => `<td class="numeric-cell">${{row[column.field] || 0}}</td>`).join('')}}
  </tr>`).join('') : `<tr><td colspan="${{5 + criterionColumns.length}}" class="muted">${{Number(report.severe_episode_count || 0) > 0 ? 'Selected severe findings exist, but no named operator is eligible for ranking. Unknown operators are excluded by configuration.' : 'No selected severe findings were found in this run.'}}</td></tr>`;
  const files = [
    ['JSON', report.report_json_path],
    ['Ranking CSV', report.ranking_csv_path],
    ['Evidence CSV', report.evidence_csv_path],
    ['Criterion frequency CSV', report.criterion_frequency_csv_path],
    ['Markdown', report.markdown_path]
  ].filter(item => item[1]).map(item => `<div class="muted">${{esc(item[0])}}: ${{esc(item[1])}}</div>`).join('');
  return `<div>
    <div class="row"><span class="status-complete">generated</span><span class="muted">${{esc(report.generation_time || '')}}</span>${{regenerateButton}}</div>
    <div>Episodes: ${{summary.episode_count || 0}} | Named operators: ${{summary.named_operator_count || 0}} | Unknown-operator episodes: ${{summary.unknown_operator_episode_count || 0}} | Severe episodes: ${{report.severe_episode_count ?? '-'}} | Named severe episodes: ${{summary.named_severe_episode_count ?? '-'}} | Unknown severe episodes: ${{summary.unknown_operator_severe_episode_count ?? '-'}}</div>
    <div class="muted" style="margin-top:6px">Ranking includes only configured severe criteria and only their selected statuses (normally fail). Raw Top Issues below use a broader non-pass definition.</div>
    <h3>Criterion definitions / 判定标准</h3>${{criterionCards || '<div class="muted">No criterion definitions found.</div>'}}
    <h3>Criterion frequency</h3>
    <div class="table-scroll"><table><thead><tr><th>Criterion</th><th>Meaning / 含义</th><th>Affected episodes</th><th>Named operators</th><th>Unknown-operator episodes</th></tr></thead><tbody>${{frequencyRows}}</tbody></table></div>
    <h3>Operator ranking</h3>
    <div class="table-scroll wide"><table><thead><tr><th>Operator</th><th>Total episodes</th><th>Severe episodes</th><th>Severe rate</th><th>Criterion hits</th>${{criterionColumns.map(column => `<th>${{esc(column.label)}}</th>`).join('')}}</tr></thead><tbody>${{rankingRows}}</tbody></table></div>
    <div class="pagination"><span class="muted">Page ${{severeRankingPage}}/${{rankingPages}} · ${{ranking.length}} operators</span><button onclick="changeSevereRankingPage(-1)" ${{severeRankingPage <= 1 ? 'disabled' : ''}}>Previous</button><button onclick="changeSevereRankingPage(1)" ${{severeRankingPage >= rankingPages ? 'disabled' : ''}}>Next</button></div>
    <div style="margin-top:8px">${{files}}</div>
    <div class="muted" style="margin-top:8px">This report lists QA evidence and does not make an employment decision.</div>
  </div>`;
}}

function changeSevereRankingPage(delta) {{
  severeRankingPage += Number(delta || 0);
  const target = document.getElementById('operatorSevereReportContent');
  if (target && selectedSevereReport && selectedSevereRun) target.innerHTML = renderOperatorSevereReport(selectedSevereReport, selectedSevereRun);
}}

async function regenerateOperatorSevereReport(runId) {{
  const msg = document.getElementById('severeReportActionMsg');
  if (msg) msg.textContent = 'Regenerating...';
  try {{
    await postJson('/api/runs/' + encodeURIComponent(runId) + '/operator-severe-report/regenerate', {{}});
    await loadRun(runId, false);
  }} catch (error) {{
    if (msg) msg.textContent = error.message || String(error);
  }}
}}

function closeIssueDrilldownModal(event) {{
  if (event && event.target && event.target.id !== 'issueDrilldownModal') return;
  document.getElementById('issueDrilldownModal').classList.remove('open');
}}

async function openIssueDrilldown(runId, checkName) {{
  issueDrilldownState = {{runId, checkName, operator: '', page: 1, pageSize: 20}};
  document.getElementById('issueDrilldownModal').classList.add('open');
  await loadIssueDrilldown();
}}

async function selectIssueOperator(operator) {{
  issueDrilldownState.operator = operator || '';
  issueDrilldownState.page = 1;
  await loadIssueDrilldown();
}}

async function changeIssueDrilldownPage(delta) {{
  issueDrilldownState.page = Math.max(1, issueDrilldownState.page + Number(delta || 0));
  await loadIssueDrilldown();
}}

async function copyEvidencePath(path) {{
  try {{ await navigator.clipboard.writeText(path); }} catch (error) {{ /* path remains selectable */ }}
}}

async function loadIssueDrilldown() {{
  const state = issueDrilldownState;
  const body = document.getElementById('issueDrilldownBody');
  body.innerHTML = '<div class="muted">Loading issue evidence...</div>';
  const query = new URLSearchParams({{
    check_name: state.checkName,
    operator: state.operator,
    page: String(state.page),
    page_size: String(state.pageSize)
  }});
  try {{
    const data = await getJson('/api/runs/' + encodeURIComponent(state.runId) + '/issue-drilldown?' + query.toString());
    if (data.error) throw new Error(data.error);
    const summary = data.summary || {{}};
    const rule = data.rule || {{}};
    const pagination = data.pagination || {{page: 1, total_pages: 1, total: 0}};
    issueDrilldownState.page = Number(pagination.page || 1);
    document.getElementById('issueDrilldownTitle').textContent = rule.issue_title_zh || data.check_name || 'Issue traceback';
    document.getElementById('issueDrilldownMeta').textContent = `${{data.check_name}} · ${{summary.finding_count || 0}} findings · ${{summary.affected_episode_count || 0}} episodes`;
    const ruleHtml = `<div class="criterion-card">
      <div><b>${{esc(rule.rule_title_zh || rule.issue_title_zh || data.check_name)}}</b> <code>${{esc(data.check_name)}}</code></div>
      <div>${{esc(rule.description_zh || 'See the measured evidence and source message for each episode below.')}}</div>
      <div>${{esc(rule.standard_zh || '')}}</div>
      <div class="thresholds">${{esc(rule.threshold_summary || 'Thresholds are recorded in each finding details object.')}}</div>
      <div class="muted">Severity/status: ${{esc(JSON.stringify(summary.severity_counts || {{}}))}} / ${{esc(JSON.stringify(summary.status_counts || {{}}))}}</div>
    </div>`;
    const operatorRows = (data.operators || []).map(item => `<tr>
      <td><button class="operator-link" onclick="selectIssueOperator(${{jsArg(item.operator)}})">${{esc(item.operator || '(unknown)')}}</button></td>
      <td class="numeric-cell">${{item.affected_episode_count || 0}}</td><td class="numeric-cell">${{item.finding_count || 0}}</td>
    </tr>`).join('') || '<tr><td colspan="3" class="muted">No affected operators.</td></tr>';
    const operatorFilter = state.operator ? `<div class="row"><b>Operator: ${{esc(state.operator)}}</b><button onclick="selectIssueOperator('')">Show all operators</button></div>` : '';
    const episodeCards = (data.episodes || []).map(item => `<details class="evidence-card">
      <summary><b>${{esc(item.episode_name || item.episode_path)}}</b> · ${{esc(item.operator || '(unknown)')}} · ${{item.source_finding_count || 0}} finding(s)</summary>
      <div class="row"><code>${{esc(item.episode_path)}}</code><button onclick="copyEvidencePath(${{jsArg(item.episode_path)}})">Copy path</button></div>
      <div class="muted">Date: ${{esc(item.date)}} · Task: ${{esc(item.task)}} · Robot: ${{esc(item.robot)}} · Controller: ${{esc(item.controller)}}</div>
      ${{(item.findings || []).map(finding => `<div class="criterion-card"><div><b>阶段 ${{finding.phase}}</b> · ${{esc(qaLabel(finding.severity))}} / ${{esc(qaLabel(finding.status))}}</div>${{findingMessageHtml(finding)}}<details class="compact-details"><summary>查看原始检测数据</summary><pre>${{esc(JSON.stringify(finding.details, null, 2))}}</pre></details></div>`).join('')}}
    </details>`).join('') || '<div class="muted">No matching episode evidence.</div>';
    body.innerHTML = ruleHtml + `<h3>Operators / 操作员</h3><div class="table-scroll"><table><thead><tr><th>Operator</th><th>Affected episodes</th><th>Findings</th></tr></thead><tbody>${{operatorRows}}</tbody></table></div>` +
      `<h3>Episode evidence / Episode 证据</h3>${{operatorFilter}}${{episodeCards}}` +
      `<div class="pagination"><span class="muted">Page ${{pagination.page || 1}}/${{pagination.total_pages || 1}} · ${{pagination.total || 0}} episodes</span><button onclick="changeIssueDrilldownPage(-1)" ${{Number(pagination.page || 1) <= 1 ? 'disabled' : ''}}>Previous</button><button onclick="changeIssueDrilldownPage(1)" ${{Number(pagination.page || 1) >= Number(pagination.total_pages || 1) ? 'disabled' : ''}}>Next</button></div>`;
  }} catch (error) {{
    body.innerHTML = `<div class="status-fail">${{esc(error.message || String(error))}}</div>`;
  }}
}}

function renderWorkSessionReport(report) {{
  if (!report || !report.markdown_path) {{
    latestRuleExplanations = [];
    return '<div class="muted" style="margin-top:8px">还没有生成中文半日报告。</div>';
  }}
  latestRuleExplanations = report.rule_explanations || [];
  const summary = report.summary || {{}};
  const window = report.window || {{}};
  const ruleButton = latestRuleExplanations.length
    ? `<button class="link-btn" onclick="openRuleExplanationModal()">查看检测规则说明</button>`
    : `<span class="muted">当前报告暂无检测规则说明</span>`;
  return `<div style="margin-top:8px">
    <div><b>最新报告：</b>${{esc(window.label || '')}}，${{esc(window.start || '')}} 至 ${{esc(window.end || '')}}</div>
    <div>episode: ${{summary.episode_count || 0}} | 问题 episode: ${{summary.issue_episode_count || 0}} | 影响训练: ${{summary.training_blocking_episode_count || 0}} | 核心问题: ${{report.core_issue_count || 0}}</div>
    <div class="row" style="margin-top:6px">${{ruleButton}}<span class="muted">括号内统计数字默认表示 finding 条数；去重 episode 数见报告里的 episode 字段。</span></div>
    <div class="muted">文件：${{esc(report.markdown_path || '')}}</div>
    <div class="report-preview">${{report.rendered_html || '<span class="muted">报告正文不可用。</span>'}}</div>
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

function useConfiguredDataRoot() {{
  document.getElementById('rootPath').value = configuredDataRoot;
}}

function applySevereReportPreset() {{
  document.getElementById('phases').value = '1,2,7';
  document.getElementById('generateOperatorSevereReport').checked = true;
  document.getElementById('continueAfterFail').checked = true;
  document.getElementById('disableQualityLabelFilter').checked = true;
  syncSevereReportControls();
}}

function syncSevereReportControls() {{
  const requested = document.getElementById('generateOperatorSevereReport').checked;
  const continueBox = document.getElementById('continueAfterFail');
  const configInput = document.getElementById('operatorSevereConfig');
  if (requested) continueBox.checked = true;
  continueBox.disabled = requested;
  configInput.disabled = !requested;
}}

async function startRun() {{
  const button = document.getElementById('startRunButton');
  if (button.disabled) return;
  const msg = document.getElementById('startMsg');
  button.disabled = true;
  button.textContent = 'Starting...';
  const body = {{
    mode: document.getElementById('mode').value,
    root_path: document.getElementById('rootPath').value,
    date_from: document.getElementById('dateFrom').value,
    date_to: document.getElementById('dateTo').value,
    task_filter: document.getElementById('taskFilter').value,
    operator_filter: document.getElementById('operatorFilter').value,
    phases: document.getElementById('phases').value,
    workers: Number(document.getElementById('workers').value),
    batch_size: Number(document.getElementById('batchSize').value),
    min_free_mem_gb: Number(document.getElementById('minMem').value),
    max_load_ratio: Number(document.getElementById('maxLoadRatio').value),
    resource_max_wait_seconds: Number(document.getElementById('resourceWait').value),
    overload_action: document.getElementById('overloadAction').value,
    resource_error_retries: Number(document.getElementById('resourceErrorRetries').value),
    resource_retry_delay_seconds: Number(document.getElementById('resourceRetryDelay').value),
    quality_label: document.getElementById('qualityLabel').value,
    disable_quality_label_filter: document.getElementById('disableQualityLabelFilter').checked,
    force_rerun: document.getElementById('forceRerun').checked,
    generate_operator_severe_report: document.getElementById('generateOperatorSevereReport').checked,
    operator_severe_config: document.getElementById('operatorSevereConfig').value,
    continue_after_fail: document.getElementById('continueAfterFail').checked,
    run_id: document.getElementById('runId').value
  }};
  try {{
    const data = await postJson('/api/start', body);
    if (!data.ok) throw new Error(data.error || 'failed');
    msg.textContent = 'Started: ' + data.run_id;
    await refresh();
  }} catch (error) {{
    msg.textContent = error.message || String(error);
  }} finally {{
    button.textContent = 'Start';
    button.disabled = false;
  }}
}}

async function stopRun(runId) {{
  await postJson('/api/stop/' + encodeURIComponent(runId), {{}});
  await refresh();
}}

async function resumeRun(runId) {{
  await postJson('/api/resume/' + encodeURIComponent(runId), {{}});
  await refresh();
}}

async function eventAction(action) {{
  const msg = document.getElementById('eventActionMsg');
  if (msg) msg.textContent = action === 'stop' ? 'Stopping...' : 'Applying settings...';
  try {{
    const data = await postJson('/api/event-listener/' + action, {{
      phases: document.getElementById('eventPhases').value,
      mount_prefix: document.getElementById('eventMountPrefix').value,
      nas_prefix: document.getElementById('eventNasPrefix').value,
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
      phase6_profile: document.getElementById('eventPhase6Profile').value,
      phase6_video_output_mode: document.getElementById('eventPhase6OutputMode').value,
      phase6_sidecar_filename: document.getElementById('eventPhase6SidecarFilename').value,
      disable_quality_label_filter: document.getElementById('eventDisableQualityFilter').checked ? 1 : 0
    }});
    if (msg) msg.textContent = data.ok ? (action + ' succeeded') : (data.output || data.error || (action + ' failed'));
  }} catch (error) {{
    if (msg) msg.textContent = error.message || String(error);
  }} finally {{
    await refresh();
  }}
}}

syncSevereReportControls();
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
