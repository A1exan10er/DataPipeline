"""Live run monitoring and issue-event reporting for the QA pipeline."""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.pipeline.qa_dcs_notifier import DcsIssueNotifier
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
        self.overall_label = ""
        self.overall_total = 0
        self.overall_processed = 0
        self.overall_started_perf = self.started_perf
        self.last_refresh_perf = 0.0
        self.last_finding_id = 0
        self.issue_counts: Counter[str] = Counter()
        self.severity_counts: Counter[str] = Counter()
        self.status_counts: Counter[str] = Counter()
        self.latest_issue: dict[str, Any] | None = None
        self.episode_stats: dict[str, tuple[str, str, float]] = {}
        self.final_status_counts: Counter[str] = Counter()
        self.duration_by_robot: dict[str, list[float]] = {}
        self.throughput_samples: deque[tuple[float, int]] = deque(maxlen=10_000)
        self.throughput_samples.append((self.started_perf, 0))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.dcs_notifier = DcsIssueNotifier(self.run_dir, self.run_id)
        self._initialize_files()

    def start(self, states: list[EpisodeState]) -> None:
        self.last_finding_id = self._max_finding_id()
        self._bootstrap_aggregate_cache()
        self._sync_state_aggregates(states)
        self._write_latest_run_pointer()
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

    def overall_progress(self, processed: int, total: int, label: str = "") -> None:
        self.overall_processed = max(0, int(processed))
        self.overall_total = max(0, int(total))
        self.overall_label = label
        now = time.perf_counter()
        if not self.throughput_samples or self.throughput_samples[-1][1] != self.overall_processed:
            self.throughput_samples.append((now, self.overall_processed))
        while self.throughput_samples and now - self.throughput_samples[0][0] > 1800:
            self.throughput_samples.popleft()

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

    def finish_run(self, states: list[EpisodeState], status: str = "complete") -> None:
        self.refresh(states, force=True)
        self._write_run_status(states, status)
        self._write_live_summary(states)

    def refresh(self, states: list[EpisodeState], force: bool = False) -> None:
        now_perf = time.perf_counter()
        if not force and now_perf - self.last_refresh_perf < self.refresh_interval_seconds:
            return
        self._sync_state_aggregates(states)
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

    def _write_latest_run_pointer(self) -> None:
        _write_text_atomic(self.output_root / "latest_run.txt", str(self.run_dir) + "\n")

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
        self.dcs_notifier.notify_many(events)

    def _append_phase_event(self, event: dict[str, Any]) -> None:
        event = {
            "run_id": self.run_id,
            "recorded_at": _now(),
            **event,
        }
        with (self.run_dir / "phase_status.jsonl").open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_run_status(self, states: list[EpisodeState], status: str) -> None:
        self._sync_state_aggregates(states)
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
            "current_phase_eta_seconds": self._phase_eta_seconds(),
            "overall_label": self.overall_label,
            "overall_processed": self.overall_processed,
            "overall_total": self.overall_total,
            "overall_elapsed_seconds": self._overall_elapsed_seconds(),
            "overall_eta_seconds": self._overall_eta_seconds(),
            "throughput": {
                "current_phase_per_second": self._current_phase_rate(),
                "overall_1m_per_second": self._rolling_overall_rate(60.0),
                "overall_5m_per_second": self._rolling_overall_rate(300.0),
                "overall_15m_per_second": self._rolling_overall_rate(900.0),
            },
            "final_status_counts": dict(self.final_status_counts),
            "issue_counts": dict(self.issue_counts),
            "severity_counts": dict(self.severity_counts),
            "finding_status_counts": dict(self.status_counts),
            "robot_duration": self._robot_duration_rows(),
            "latest_issue": self.latest_issue,
            "run_dir": str(self.run_dir),
        }
        _write_json_atomic(self.run_dir / "run_status.json", payload)

    def _write_live_summary(self, states: list[EpisodeState]) -> None:
        self._sync_state_aggregates(states)
        lines = [
            "# Live QA Run Summary",
            "",
            f"Run ID: `{self.run_id}`",
            f"Status updated: `{_now()}`",
            f"Current phase: `{self.current_phase}`",
            f"Progress: `{self.current_phase_processed}/{self.current_phase_total}`",
            f"Current phase ETA: `{_format_duration_seconds(self._phase_eta_seconds())}`",
            f"Overall progress: `{self.overall_processed}/{self.overall_total}`",
            f"Overall ETA: `{_format_duration_seconds(self._overall_eta_seconds())}`",
            f"Run directory: `{self.run_dir}`",
            "",
            "## Final Status Counts",
            "",
        ]
        for status, count in sorted(self.final_status_counts.items()):
            lines.append(f"- `{status}`: {count}")
        lines.extend(["", "## Duration By Robot", ""])
        robot_duration = self._robot_duration_rows()
        if robot_duration:
            for row in robot_duration:
                lines.append(
                    f"- `{row['robot']}`: {row['episodes']} episode(s), "
                    f"{_format_duration_seconds(row['duration_seconds'])}"
                )
        else:
            lines.append("- Duration appears after Phase 2 records p2_duration_seconds.")
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

    def _bootstrap_aggregate_cache(self) -> None:
        """Scan historical state once; later refreshes apply only batch deltas."""
        if not self.db_path.exists():
            return
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT episode_path, final_status, robot, metrics FROM episodes"
                ).fetchall()
            except sqlite3.OperationalError:
                return
        for episode_path, final_status, robot, metrics_json in rows:
            metrics = _json_dict(metrics_json)
            try:
                duration = float(metrics.get("p2_duration_seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            self._replace_episode_stat(
                str(episode_path),
                str(final_status or "pending"),
                str(robot or "(unknown)"),
                max(0.0, duration),
            )

    def _sync_state_aggregates(self, states: list[EpisodeState]) -> None:
        for state in states:
            try:
                duration = float(state.metrics.get("p2_duration_seconds") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            self._replace_episode_stat(
                str(state.episode_path),
                str(state.final_status or "pending"),
                str(state.robot or "(unknown)"),
                max(0.0, duration),
            )

    def _replace_episode_stat(
        self,
        episode_path: str,
        status: str,
        robot: str,
        duration: float,
    ) -> None:
        previous = self.episode_stats.get(episode_path)
        current = (status, robot, duration)
        if previous == current:
            return
        if previous is not None:
            previous_status, previous_robot, previous_duration = previous
            self.final_status_counts[previous_status] -= 1
            if self.final_status_counts[previous_status] <= 0:
                self.final_status_counts.pop(previous_status, None)
            if previous_duration > 0:
                values = self.duration_by_robot.get(previous_robot)
                if values is not None:
                    values[0] -= 1
                    values[1] -= previous_duration
                    if values[0] <= 0:
                        self.duration_by_robot.pop(previous_robot, None)
        self.episode_stats[episode_path] = current
        self.final_status_counts[status] += 1
        if duration > 0:
            values = self.duration_by_robot.setdefault(robot, [0.0, 0.0])
            values[0] += 1
            values[1] += duration

    def _robot_duration_rows(self) -> list[dict[str, Any]]:
        rows = [
            {
                "robot": robot,
                "episodes": int(values[0]),
                "duration_seconds": round(float(values[1]), 3),
                "duration_hours": round(float(values[1]) / 3600.0, 3),
            }
            for robot, values in self.duration_by_robot.items()
            if values[0] > 0
        ]
        rows.sort(key=lambda row: (-float(row["duration_seconds"]), str(row["robot"])))
        return rows

    def _phase_elapsed_seconds(self) -> float:
        if not self.current_phase_started_perf:
            return 0.0
        return time.perf_counter() - self.current_phase_started_perf

    def _phase_eta_seconds(self) -> float:
        return _eta_seconds(self.current_phase_processed, self.current_phase_total, self._phase_elapsed_seconds())

    def _overall_elapsed_seconds(self) -> float:
        return time.perf_counter() - self.overall_started_perf

    def _overall_eta_seconds(self) -> float:
        return _eta_seconds(self.overall_processed, self.overall_total, self._overall_elapsed_seconds())

    def _current_phase_rate(self) -> float:
        elapsed = self._phase_elapsed_seconds()
        return round(self.current_phase_processed / elapsed, 4) if elapsed > 0 else 0.0

    def _rolling_overall_rate(self, window_seconds: float) -> float:
        if len(self.throughput_samples) < 2:
            return 0.0
        latest_time, latest_count = self.throughput_samples[-1]
        earliest_time, earliest_count = self.throughput_samples[0]
        threshold = latest_time - window_seconds
        for sample_time, sample_count in self.throughput_samples:
            if sample_time >= threshold:
                earliest_time, earliest_count = sample_time, sample_count
                break
        elapsed = latest_time - earliest_time
        return round((latest_count - earliest_count) / elapsed, 4) if elapsed > 0 else 0.0


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


def _final_counts(states: list[EpisodeState], db_path: Path | None = None) -> dict[str, int]:
    if not states and db_path is not None and db_path.exists():
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(final_status, ''), 'pending') AS status, COUNT(*)
                FROM episodes
                GROUP BY status
                """
            ).fetchall()
        return {str(status): int(count) for status, count in rows}
    counts: Counter[str] = Counter()
    for state in states:
        status = state.final_status or "pending"
        counts[status] += 1
    return dict(counts)


def _duration_by_robot(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    totals: dict[str, dict[str, float | int | str]] = {}
    with sqlite3.connect(db_path) as conn:
        try:
            rows = conn.execute("SELECT robot, metrics FROM episodes").fetchall()
        except sqlite3.OperationalError:
            return []
    for robot, metrics_json in rows:
        metrics = _json_dict(metrics_json)
        try:
            duration = float(metrics.get("p2_duration_seconds"))
        except (TypeError, ValueError):
            continue
        if duration <= 0:
            continue
        key = robot or "(unknown)"
        item = totals.setdefault(key, {"robot": key, "episodes": 0, "duration_seconds": 0.0})
        item["episodes"] = int(item["episodes"]) + 1
        item["duration_seconds"] = float(item["duration_seconds"]) + duration
    return [
        {
            "robot": str(item["robot"]),
            "episodes": int(item["episodes"]),
            "duration_seconds": round(float(item["duration_seconds"]), 3),
            "duration_hours": round(float(item["duration_seconds"]) / 3600.0, 3),
        }
        for item in sorted(totals.values(), key=lambda row: (-float(row["duration_seconds"]), str(row["robot"])))
    ]


def _format_duration_seconds(seconds: float | int) -> str:
    value = float(seconds)
    if value <= 0:
        return "unknown"
    if value >= 3600:
        return f"{value / 3600:.2f} h"
    if value >= 60:
        return f"{value / 60:.1f} min"
    return f"{value:.1f} s"


def _eta_seconds(processed: int, total: int, elapsed: float) -> float:
    if processed <= 0 or total <= 0 or processed >= total or elapsed <= 0:
        return 0.0
    rate = processed / elapsed
    return (total - processed) / rate if rate > 0 else 0.0


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
