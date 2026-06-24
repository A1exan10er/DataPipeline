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
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT.parent
DEFAULT_MANAGER_DIR = PROJECT_ROOT / "outputs" / "dashboard_manager"
DEFAULT_REGISTRY_DB = DEFAULT_MANAGER_DIR / "runs.db"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "dashboard_runs"
DEFAULT_VERIFIED_ROOT = Path("/mnt/nas/database/verified")
EVENT_CONTROL_SCRIPT = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "event_listener_control.sh"
EVENT_JOB_DB = PROJECT_ROOT / "outputs" / "event_listener" / "jobs.db"


class DashboardState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.port = args.port
        self.registry_db = Path(args.registry_db)
        self.output_root = Path(args.output_root)
        self.verified_root = Path(args.verified_root)
        self.qa_python = Path(args.qa_python)
        self.refresh_seconds = max(1.0, float(args.refresh_seconds))
        self.max_discovered_runs = max(0, int(args.max_discovered_runs))
        self.lock = threading.Lock()
        init_registry(self.registry_db)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = DashboardState(args)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
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
                elif path == "/api/event-listener/jobs":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["100"])[0] or "100")
                    issues_only = query.get("issues_only", ["0"])[0] in {"1", "true", "yes"}
                    self._send_json({"jobs": event_listener_jobs(limit=limit, issues_only=issues_only)})
                elif path.startswith("/api/event-listener/jobs/"):
                    job_id = int(path.rsplit("/", 1)[-1])
                    self._send_json({"job": event_listener_job_detail(job_id)})
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
                    self._send_json(api_start_run(state, payload))
                elif path.startswith("/api/stop/"):
                    run_id = path.rsplit("/", 1)[-1]
                    self._send_json(api_stop_run(state, run_id))
                elif path == "/api/event-listener/start":
                    self._send_json(api_event_listener_action("start", payload))
                elif path == "/api/event-listener/stop":
                    self._send_json(api_event_listener_action("stop", payload))
                elif path == "/api/event-listener/restart":
                    self._send_json(api_event_listener_action("restart", payload))
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


def api_status(state: DashboardState) -> dict[str, Any]:
    sync_registered_run_statuses(state)
    return {
        "generated_at": now_iso(),
        "server": server_load(),
        "event_listener": event_listener_status(),
        "runs": list_runs(state, limit=30),
    }


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
    outputs = PROJECT_ROOT / "outputs"
    if not outputs.is_dir() or state.max_discovered_runs <= 0:
        return []
    candidates = []
    for db_path in outputs.rglob("qa.db"):
        rel = db_path.relative_to(outputs)
        if len(rel.parts) > 5:
            continue
        output_dir = db_path.parent
        stat = safe_stat(db_path)
        candidates.append((stat[0], db_path, output_dir))
    candidates.sort(reverse=True)
    runs = []
    for _mtime, db_path, output_dir in candidates[: state.max_discovered_runs]:
        run_id = discovered_run_id(output_dir)
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


def api_event_listener_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    for key in (
        "WORKERS",
        "EVENT_BATCH_SIZE",
        "STABILITY_INTERVAL",
        "STABILITY_TIMEOUT",
        "MIN_FREE_MEM_GB",
        "PHASES",
        "QUALITY_LABEL",
        "DISABLE_QUALITY_LABEL_FILTER",
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
        "output_size": directory_size(PROJECT_ROOT / "outputs" / "event_listener"),
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


def event_listener_job_detail(job_id: int) -> dict[str, Any]:
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
    job["task"] = task_from_episode_path(str(job.get("mounted_path") or ""))
    job["robot"] = robot_from_episode_path(str(job.get("mounted_path") or ""))
    job.update(job_episode_result(job, include_findings=True))
    output_dir = Path(str(job.get("output_dir") or ""))
    if output_dir:
        job["log_tail"] = tail_file(output_dir / "pipeline.log", 120)
    return job


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
            result["top_issues"] = [
                {"check_name": str(name), "count": int(count)}
                for name, count in conn.execute(
                    """
                    SELECT check_name, COUNT(*) AS count
                    FROM findings
                    WHERE episode_path = ? AND status != ?
                    GROUP BY check_name
                    ORDER BY count DESC, check_name
                    LIMIT 10
                    """,
                    (episode_path, "pass"),
                )
            ]
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
    completed = subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


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


def directory_size(path: Path) -> str:
    if not path.exists():
        return "0B"
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return human_bytes(total)


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
    return sanitize_id(str(output_dir.relative_to(PROJECT_ROOT / "outputs")).replace("/", "-"))


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
    tr.run-row {{ cursor: pointer; }}
    tr.run-row:hover {{ background: #f1f5f9; }}
    .link-btn {{ height: 28px; padding: 0 7px; font-size: 12px; }}
    .status-pass, .status-complete {{ color: var(--ok); font-weight: 700; }}
    .status-fail, .status-failed {{ color: var(--danger); font-weight: 700; }}
    .status-running {{ color: var(--accent); font-weight: 700; }}
    .status-warning, .status-pending {{ color: var(--warn); font-weight: 700; }}
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
      </div>
      <div><button class="primary" onclick="openEventSummaryModal()">Summary</button></div>
    </div>
    <div class="panel">
      <div class="row" style="justify-content:space-between">
        <h2>Event Issue Episodes</h2>
        <span class="muted">episodes with non-pass findings from event listener</span>
      </div>
      <table>
        <thead><tr><th>ID</th><th>Status</th><th>QA</th><th>Task</th><th>Robot</th><th>Episode</th><th>Issues</th><th>Updated</th><th></th></tr></thead>
        <tbody id="eventJobsBody"></tbody>
      </table>
    </div>
    <div class="panel">
      <h2>Runs</h2>
      <table><thead><tr><th>Run</th><th>Mode</th><th>Status</th><th>Episodes</th><th>Issues</th><th>Updated</th><th></th></tr></thead><tbody id="runsBody"></tbody></table>
    </div>
    <div class="panel">
      <h2>Run Detail</h2>
      <div id="runDetail" class="muted">Select a run.</div>
    </div>
    <div class="panel">
      <h2>Server Processes</h2>
      <table><thead><tr><th>PID</th><th>CPU</th><th>Mem</th><th>RSS MB</th><th>Command</th></tr></thead><tbody id="procBody"></tbody></table>
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
      <div id="eventSummaryModalSeverity" class="severity-strip" style="margin-bottom:12px"></div>
      <h3>By Task, Device, Collector</h3>
      <table>
        <thead><tr><th>Task</th><th>Device</th><th>Collector</th><th>Episodes</th><th>Findings</th><th>Severity</th><th>Top Issues</th></tr></thead>
        <tbody id="eventContextSummaryBody"></tbody>
      </table>
      <h3>By Issue Type</h3>
      <table>
        <thead><tr><th>Issue</th><th>Severity</th><th>Findings</th><th>Episodes</th><th>Task/device/collector groups</th></tr></thead>
        <tbody id="eventIssueSummaryBody"></tbody>
      </table>
    </div>
  </div>
</div>
<script>
const refreshMs = {refresh};
let selectedRun = null;
let selectedEventJob = null;

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

async function refresh() {{
  const data = await getJson('/api/status');
  document.getElementById('clock').textContent = data.generated_at || '';
  document.getElementById('load1').textContent = Number(val(data,'server.load.1m',0)).toFixed(2);
  document.getElementById('memUsed').textContent =
    val(data,'server.memory.used_gb','-') + ' / ' + val(data,'server.memory.total_gb','-') + ' GB';
  document.getElementById('memAvail').textContent =
    'remaining ' + val(data,'server.memory.available_gb','-') + ' GB, swap used ' + val(data,'server.memory.swap_used_gb','-') + ' GB';
  document.getElementById('eventPending').textContent = val(data,'event_listener.counts.pending',0);
  document.getElementById('eventDone').textContent = val(data,'event_listener.counts.done',0);
  document.getElementById('eventBox').innerHTML =
    '<div>tmux: <b>' + (val(data,'event_listener.tmux_running',false) ? 'running' : 'stopped') + '</b></div>' +
    '<div>running: ' + val(data,'event_listener.counts.running',0) + '</div>' +
    '<div>pending: ' + val(data,'event_listener.counts.pending',0) + '</div>' +
    '<div>output: ' + val(data,'event_listener.output_size','-') + '</div>';
  renderRuns(data.runs || []);
  await refreshEventIssueSummary();
  await refreshEventJobs();
  renderProcesses(val(data,'server.top_processes',[]));
  if (selectedRun) await loadRun(selectedRun, false);
  else await loadLog('event_listener');
}}

function renderRuns(runs) {{
  const body = document.getElementById('runsBody');
  body.innerHTML = runs.map(run => {{
    const counts = run.status_counts || {{}};
    return `<tr class="run-row" onclick="loadRun('${{run.run_id}}', true)">
      <td>${{run.run_id}}</td><td>${{run.mode || ''}}</td>
      <td class="${{cls(run.status || val(run,'live_status.status',''))}}">${{run.status || val(run,'live_status.status','')}}</td>
      <td>${{run.episode_count || 0}}</td><td>${{run.finding_count || 0}}</td>
      <td>${{run.updated_at || ''}}</td>
      <td><button onclick="event.stopPropagation(); stopRun('${{run.run_id}}')">Stop</button></td>
    </tr>`;
  }}).join('');
}}

async function refreshEventJobs() {{
  const data = await getJson('/api/event-listener/jobs?limit=120&issues_only=1');
  renderEventJobs(data.jobs || []);
}}

async function refreshEventIssueSummary() {{
  const data = await getJson('/api/event-listener/issue-summary?limit=500');
  renderEventIssueSummary(data.summary || {{}});
}}

function severityText(counts) {{
  const entries = Object.entries(counts || {{}});
  return entries.length ? entries.map(([name,count]) => `${{esc(name)}}:${{count}}`).join(', ') : '-';
}}

function severityBadge(name, count) {{
  return `<span class="severity-chip ${{cls(name)}}">${{esc(name)}} ${{count}}</span>`;
}}

function openEventSummaryModal() {{
  document.getElementById('eventSummaryModal').classList.add('open');
}}

function closeEventSummaryModal(event) {{
  if (event && event.target && event.target.id !== 'eventSummaryModal') return;
  document.getElementById('eventSummaryModal').classList.remove('open');
}}

function renderEventIssueSummary(summary) {{
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
  document.getElementById('eventContextSummaryBody').innerHTML = (summary.by_context || []).map(row => {{
    const issues = (row.top_issues || []).map(i => `${{esc(i.check_name)}}(${{i.count}})`).join(', ');
    return `<tr>
      <td>${{esc(row.task || '')}}</td>
      <td>${{esc(row.robot || '')}}</td>
      <td>${{esc(row.operator || '')}}</td>
      <td>${{row.episode_count || 0}}</td>
      <td>${{row.finding_count || 0}}</td>
      <td>${{severityText(row.severity_counts)}}</td>
      <td>${{issues || '-'}}</td>
    </tr>`;
  }}).join('');
  document.getElementById('eventIssueSummaryBody').innerHTML = (summary.by_issue || []).map(row =>
    `<tr>
      <td>${{esc(row.check_name || '')}}</td>
      <td class="${{cls(row.severity)}}">${{esc(row.severity || '')}}</td>
      <td>${{row.finding_count || 0}}</td>
      <td>${{row.episode_count || 0}}</td>
      <td>${{row.context_count || 0}}</td>
    </tr>`
  ).join('');
}}

function renderEventJobs(jobs) {{
  const body = document.getElementById('eventJobsBody');
  body.innerHTML = jobs.map(job => {{
    const qa = job.final_status || (job.status === 'done' ? 'complete' : '');
    const issues = (job.top_issues || []).map(i => `${{esc(i.check_name)}}(${{i.count}})`).join(', ');
    return `<tr>
      <td>${{job.id}}</td>
      <td class="${{cls(job.status)}}">${{esc(job.status)}}</td>
      <td class="${{cls(qa)}}">${{esc(qa || '-')}}</td>
      <td>${{esc(job.task || '')}}</td>
      <td>${{esc(job.robot || '')}}</td>
      <td title="${{esc(job.mounted_path || '')}}">${{esc(job.episode_name || '')}}</td>
      <td>${{job.issue_count || 0}} ${{issues ? '- ' + issues : ''}}</td>
      <td>${{esc(job.updated_at || '')}}</td>
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
    `<tr><td>${{f.phase}}</td><td>${{esc(f.check_name)}}</td><td>${{esc(f.severity)}}</td><td class="${{cls(f.status)}}">${{esc(f.status)}}</td><td>${{esc(f.message)}}</td></tr>`
  ).join('') : '<tr><td colspan="5" class="muted">No non-pass findings.</td></tr>';
  const phaseStatus = Object.entries(job.phase_status || {{}}).map(([phase,status]) => `P${{phase}}=${{status}}`).join(', ');
  const topIssues = (job.top_issues || []).map(i => `${{esc(i.check_name)}}(${{i.count}})`).join(', ') || 'None';
  const issueSummary = findings.map(f => `${{esc(f.check_name)}}: ${{esc(f.message)}}`).join('\\n');
  document.getElementById('eventJobDetail').innerHTML =
    `<div class="row"><b>Job ${{job.id}}</b><span class="${{cls(job.status)}}">${{esc(job.status)}}</span><span class="${{cls(job.final_status)}}">${{esc(job.final_status || '-')}}</span></div>` +
    `<div><b>Episode:</b> ${{esc(job.episode_name || '')}}</div>` +
    `<div><b>Path:</b> ${{esc(job.mounted_path || '')}}</div>` +
    `<div><b>Task:</b> ${{esc(job.task || '')}} | <b>Robot:</b> ${{esc(job.robot || '')}} | <b>Operator:</b> ${{esc(job.operator || '')}}</div>` +
    `<div><b>Phase status:</b> ${{esc(phaseStatus || '-')}}</div>` +
    `<div><b>Top issues:</b> ${{topIssues}}</div>` +
    `<div><b>Output:</b> ${{esc(job.output_dir || '')}}</div>` +
    `<h3>Issue Summary</h3><pre>${{issueSummary || 'No non-pass findings.'}}</pre>` +
    `<h3>Episode Issues</h3>` +
    `<table><thead><tr><th>Phase</th><th>Check</th><th>Severity</th><th>Status</th><th>Message</th></tr></thead><tbody>${{issueRows}}</tbody></table>`;
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
  const issueEpisodeRows = issueEpisodes.length ? issueEpisodes.map(ep =>
    `<tr class="run-row"><td>${{esc(ep.episode_name)}}</td><td class="${{cls(ep.final_status)}}">${{esc(ep.final_status)}}</td><td>${{esc(ep.task)}}</td><td>${{esc(ep.robot)}}</td><td>${{ep.issue_count}}</td><td>${{esc((ep.issue_names || []).join(', '))}}</td></tr>`
  ).join('') : '<tr><td colspan="6" class="muted">No issue episodes in this run.</td></tr>';
  document.getElementById('runDetail').innerHTML =
    `<div class="row"><b>${{run.run_id}}</b><span class="${{cls(run.status)}}">${{run.status}}</span></div>` +
    `<div>Output: ${{run.output_dir}}</div>` +
    `<div>DB: ${{run.db_path}}</div>` +
    `<div>Episodes: ${{run.episode_count || 0}} | Issues: ${{run.finding_count || 0}}</div>` +
    `<div>Pass: ${{counts.pass || 0}} Warning: ${{counts.warning || 0}} Fail: ${{counts.fail || 0}} Review: ${{counts.needs_review || 0}}</div>` +
    `<div>Phase: ${{live.current_phase || '-'}} ${{live.current_phase_processed || 0}}/${{live.current_phase_total || 0}}</div>` +
    `<h3>Top Issues</h3>` +
    `<table><tbody>${{(run.top_issues || []).map(i=>`<tr><td>${{i.check_name}}</td><td>${{i.count}}</td></tr>`).join('')}}</tbody></table>` +
    `<h3>Episodes With Issues</h3>` +
    `<table><thead><tr><th>Episode</th><th>Status</th><th>Task</th><th>Robot</th><th>Issues</th><th>Issue Names</th></tr></thead><tbody>${{issueEpisodeRows}}</tbody></table>`;
  await loadLog(runId);
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
    quality_label: document.getElementById('eventQualityLabel').value,
    disable_quality_label_filter: document.getElementById('eventDisableQualityLabelFilter').checked ? '1' : '0'
  }});
  await refresh();
}}

refresh();
setInterval(refresh, refreshMs);
</script>
</body>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
