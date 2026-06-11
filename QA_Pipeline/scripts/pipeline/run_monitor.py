"""Live run monitoring and issue-event reporting for the QA pipeline."""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.pipeline.qa_core import EpisodeState


ISSUE_FIELDNAMES = [
    "run_id",
    "recorded_at",
    "finding_id",
    "episode_path",
    "task",
    "date",
    "operator",
    "robot",
    "controller",
    "phase",
    "check_name",
    "severity",
    "status",
    "message",
    "details_json",
]


class RunMonitor:
    """Write live progress and exact issue records for one pipeline run."""

    def __init__(
        self,
        db_path: Path,
        output_root: Path,
        run_id: str,
        roots: list[Path],
        phases: list[int],
        workers: int,
        refresh_interval_seconds: float,
    ) -> None:
        self.db_path = Path(db_path)
        self.output_root = Path(output_root)
        self.run_id = run_id
        self.run_dir = self.output_root / "runs" / run_id
        self.roots = [str(root) for root in roots]
        self.phases = phases
        self.workers = workers
        self.refresh_interval_seconds = max(0.1, refresh_interval_seconds)
        self.started_at = _now()
        self.started_perf = time.perf_counter()
        self.current_phase: int | None = None
        self.current_phase_started_perf = 0.0
        self.current_phase_total = 0
        self.current_phase_processed = 0
        self.last_refresh_perf = 0.0
        self.last_finding_id = 0
        self.issue_counts: Counter[str] = Counter()
        self.severity_counts: Counter[str] = Counter()
        self.status_counts: Counter[str] = Counter()
        self.latest_issue: dict[str, Any] | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_files()

    def start(self, states: list[EpisodeState]) -> None:
        self.last_finding_id = self._max_finding_id()
        self._write_run_status(states, "running")
        self._write_live_summary(states)

    def start_phase(self, phase_number: int, total: int, skipped: int, states: list[EpisodeState]) -> None:
        self.current_phase = phase_number
        self.current_phase_total = total
        self.current_phase_processed = 0
        self.current_phase_started_perf = time.perf_counter()
        self._append_phase_event(
            {
                "event": "phase_start",
                "phase": phase_number,
                "total_episodes": total,
                "skipped_episodes": skipped,
            }
        )
        self.refresh(states, force=True)

    def progress(self, phase_number: int, current: int, total: int, states: list[EpisodeState]) -> None:
        self.current_phase = phase_number
        self.current_phase_processed = current
        self.current_phase_total = total
        self.refresh(states)

    def finish_phase(self, phase_number: int, states: list[EpisodeState]) -> None:
        self.current_phase = phase_number
        self.refresh(states, force=True)
        self._append_phase_event(
            {
                "event": "phase_end",
                "phase": phase_number,
                "total_episodes": self.current_phase_total,
                "processed_episodes": self.current_phase_processed,
                "elapsed_seconds": self._phase_elapsed_seconds(),
                "status_counts": _phase_counts(states, phase_number),
                "issue_counts": dict(self.issue_counts),
            }
        )
        self._write_run_status(states, "running")
        self._write_live_summary(states)

    def finish_run(self, states: list[EpisodeState]) -> None:
        self.refresh(states, force=True)
        self._write_run_status(states, "complete")
        self._write_live_summary(states)

    def refresh(self, states: list[EpisodeState], force: bool = False) -> None:
        now_perf = time.perf_counter()
        if not force and now_perf - self.last_refresh_perf < self.refresh_interval_seconds:
            return
        new_events = self._poll_issue_events()
        if new_events:
            self._append_issue_events(new_events)
        self._append_phase_event(
            {
                "event": "progress",
                "phase": self.current_phase,
                "processed_episodes": self.current_phase_processed,
                "total_episodes": self.current_phase_total,
                "elapsed_seconds": self._phase_elapsed_seconds(),
                "status_counts": _phase_counts(states, self.current_phase),
                "issue_counts": dict(self.issue_counts),
            }
        )
        self._write_run_status(states, "running")
        self._write_live_summary(states)
        self.last_refresh_perf = now_perf

    def _initialize_files(self) -> None:
        (self.run_dir / "issue_events.jsonl").write_text("", encoding="utf-8")
        (self.run_dir / "phase_status.jsonl").write_text("", encoding="utf-8")
        with (self.run_dir / "episode_issues.csv").open("w", encoding="utf-8", newline="") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=ISSUE_FIELDNAMES)
            writer.writeheader()

    def _poll_issue_events(self) -> list[dict[str, Any]]:
        rows = self._new_finding_rows()
        events = []
        for row in rows:
            finding_id = int(row["id"])
            self.last_finding_id = max(self.last_finding_id, finding_id)
            details = _json_dict(row["details"])
            event = {
                "run_id": self.run_id,
                "recorded_at": _now(),
                "finding_id": finding_id,
                "episode_path": row["episode_path"],
                "task": row["task"] or "",
                "date": row["date"] or "",
                "operator": row["operator"] or "",
                "robot": row["robot"] or "",
                "controller": row["controller"] or "",
                "phase": row["phase"],
                "check_name": row["check_name"],
                "severity": row["severity"],
                "status": row["status"],
                "message": row["message"],
                "details": details,
            }
            self.issue_counts[event["check_name"]] += 1
            self.severity_counts[event["severity"]] += 1
            self.status_counts[event["status"]] += 1
            self.latest_issue = event
            events.append(event)
        return events

    def _new_finding_rows(self) -> list[sqlite3.Row]:
        if not self.db_path.exists():
            return []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT f.id, f.episode_path, f.phase, f.check_name, f.severity,
                       f.status, f.message, f.details,
                       e.task, e.date, e.operator, e.robot, e.controller
                FROM findings f
                LEFT JOIN episodes e ON e.episode_path = f.episode_path
                WHERE f.id > ? AND f.status != ?
                ORDER BY f.id
                """,
                (self.last_finding_id, "pass"),
            ).fetchall()

    def _append_issue_events(self, events: list[dict[str, Any]]) -> None:
        with (self.run_dir / "issue_events.jsonl").open("a", encoding="utf-8") as jsonl_obj, (
            self.run_dir / "episode_issues.csv"
        ).open("a", encoding="utf-8", newline="") as csv_obj:
            writer = csv.DictWriter(csv_obj, fieldnames=ISSUE_FIELDNAMES)
            for event in events:
                jsonl_obj.write(json.dumps(event, ensure_ascii=False) + "\n")
                writer.writerow(_issue_csv_row(event))

    def _append_phase_event(self, event: dict[str, Any]) -> None:
        event = {
            "run_id": self.run_id,
            "recorded_at": _now(),
            **event,
        }
        with (self.run_dir / "phase_status.jsonl").open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_run_status(self, states: list[EpisodeState], status: str) -> None:
        elapsed = time.perf_counter() - self.started_perf
        payload = {
            "run_id": self.run_id,
            "status": status,
            "started_at": self.started_at,
            "updated_at": _now(),
            "elapsed_seconds": elapsed,
            "roots": self.roots,
            "phases": self.phases,
            "workers": self.workers,
            "current_phase": self.current_phase,
            "current_phase_processed": self.current_phase_processed,
            "current_phase_total": self.current_phase_total,
            "current_phase_elapsed_seconds": self._phase_elapsed_seconds(),
            "final_status_counts": _final_counts(states),
            "issue_counts": dict(self.issue_counts),
            "severity_counts": dict(self.severity_counts),
            "finding_status_counts": dict(self.status_counts),
            "latest_issue": self.latest_issue,
            "run_dir": str(self.run_dir),
        }
        _write_json_atomic(self.run_dir / "run_status.json", payload)

    def _write_live_summary(self, states: list[EpisodeState]) -> None:
        lines = [
            "# Live QA Run Summary",
            "",
            f"Run ID: `{self.run_id}`",
            f"Status updated: `{_now()}`",
            f"Current phase: `{self.current_phase}`",
            f"Progress: `{self.current_phase_processed}/{self.current_phase_total}`",
            f"Run directory: `{self.run_dir}`",
            "",
            "## Final Status Counts",
            "",
        ]
        for status, count in sorted(_final_counts(states).items()):
            lines.append(f"- `{status}`: {count}")
        lines.extend(["", "## Issue Counts", ""])
        if self.issue_counts:
            for check_name, count in self.issue_counts.most_common(20):
                lines.append(f"- `{check_name}`: {count}")
        else:
            lines.append("- None recorded yet.")
        lines.extend(["", "## Latest Issue", ""])
        if self.latest_issue:
            issue = self.latest_issue
            lines.append(
                f"- p{issue['phase']} `{issue['severity']}` `{issue['status']}` "
                f"**{issue['check_name']}** in `{issue['episode_path']}`"
            )
            lines.append(f"- {issue['message']}")
        else:
            lines.append("- None recorded yet.")
        _write_text_atomic(self.run_dir / "live_summary.md", "\n".join(lines) + "\n")

    def _max_finding_id(self) -> int:
        if not self.db_path.exists():
            return 0
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(id) FROM findings").fetchone()
        return int(row[0] or 0)

    def _phase_elapsed_seconds(self) -> float:
        if not self.current_phase_started_perf:
            return 0.0
        return time.perf_counter() - self.current_phase_started_perf


def _issue_csv_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": event["run_id"],
        "recorded_at": event["recorded_at"],
        "finding_id": event["finding_id"],
        "episode_path": event["episode_path"],
        "task": event["task"],
        "date": event["date"],
        "operator": event["operator"],
        "robot": event["robot"],
        "controller": event["controller"],
        "phase": event["phase"],
        "check_name": event["check_name"],
        "severity": event["severity"],
        "status": event["status"],
        "message": event["message"],
        "details_json": json.dumps(event["details"], ensure_ascii=False),
    }


def _phase_counts(states: list[EpisodeState], phase_number: int | None) -> dict[str, int]:
    if phase_number is None:
        return {}
    counts: Counter[str] = Counter()
    for state in states:
        status = state.phase_status.get(phase_number)
        if status:
            counts[status] += 1
    return dict(counts)


def _final_counts(states: list[EpisodeState]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for state in states:
        status = state.final_status or "pending"
        counts[status] += 1
    return dict(counts)


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text_atomic(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
