#!/usr/bin/env python3
"""Standalone, read-only unusual episode-duration detector.

This tool is intentionally independent from the QA event listener, pipeline
phases, and dashboards. It reads finalized episode metadata and writes only to
its configured local SQLite/report paths.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import html
import json
import math
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


NORMAL_LABEL = "完全正常"
ALGORITHM_VERSION = "robust-log-mad-v1"
MIN_BASELINE_SIZE = 30
DEFAULT_DB = Path("outputs/duration_anomalies/duration_anomalies.sqlite")
HISTORY_DAYS = 90


def percentile(values: list[float], percent: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)


def robust_profile(durations: Iterable[float]) -> dict[str, float] | None:
    values = sorted(
        float(value)
        for value in durations
        if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0
    )
    if not values:
        return None
    logs = [math.log(value) for value in values]
    median_seconds = percentile(values, 50)
    median_log = percentile(logs, 50)
    mad = percentile([abs(value - median_log) for value in logs], 50)
    mad_sigma = 1.4826 * mad
    iqr_sigma = max(0.0, (percentile(logs, 75) - percentile(logs, 25)) / 1.349)
    sigma = max(mad_sigma, iqr_sigma)
    margin = max(2.0, median_seconds * 0.10)
    return {
        "median": median_seconds,
        "p5": percentile(values, 5),
        "p25": percentile(values, 25),
        "p75": percentile(values, 75),
        "p95": percentile(values, 95),
        "lower": max(0.0, min(math.exp(median_log - 3.5 * sigma), median_seconds - margin)),
        "upper": max(math.exp(median_log + 3.5 * sigma), median_seconds + margin),
        "sigma_log": sigma,
    }


def classify(duration: float, profile: dict[str, float]) -> dict[str, float | str]:
    direction = "short" if duration < profile["lower"] else "long" if duration > profile["upper"] else "none"
    boundary = profile["lower"] if direction == "short" else profile["upper"] if direction == "long" else duration
    sigma = profile["sigma_log"]
    log_distance = abs(math.log(duration) - math.log(profile["median"]))
    score = log_distance / sigma if sigma > 1e-9 else abs(duration - profile["median"]) / max(2.0, profile["median"] * 0.10)
    return {
        "direction": direction,
        "deviation_seconds": round(duration - boundary, 3),
        "ratio_to_median": round(duration / profile["median"], 6),
        "score": round(score, 6),
    }


def positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def stable_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    for attempt in range(2):
        before = path.stat()
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        after = path.stat()
        if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            if not isinstance(data, dict):
                raise ValueError(f"JSON root is not an object: {path}")
            return data
        if attempt:
            raise RuntimeError(f"file changed while being read: {path}")
    return None


def duration_from_episode(episode_path: Path, metadata: dict[str, Any]) -> tuple[float | None, str]:
    for key in ("duration_seconds", "duration_s", "duration"):
        value = positive_number(metadata.get(key))
        if value is not None:
            return value, f"metadata.json:{key}"
    duration_ms = positive_number(metadata.get("duration_ms"))
    if duration_ms is not None:
        return duration_ms / 1000.0, "metadata.json:duration_ms"

    info = stable_json(episode_path / "meta" / "info.json")
    if info:
        duration_ms = positive_number(info.get("duration_ms"))
        if duration_ms is not None:
            return duration_ms / 1000.0, "meta/info.json:duration_ms"

    episode_json = stable_json(episode_path / "meta" / "episode.json")
    candidates = []
    for modality in (episode_json or {}).get("modalities", {}).values():
        if not isinstance(modality, dict):
            continue
        hz = positive_number(modality.get("hz_nominal"))
        samples = positive_number(modality.get("frames") if "frames" in modality else modality.get("rows"))
        if hz and samples:
            candidates.append(samples / hz)
    return (max(candidates), "meta/episode.json:modalities") if candidates else (None, "")


def context_from_path(root: Path, episode_path: Path) -> dict[str, Any] | None:
    relative = episode_path.relative_to(root)
    parts = relative.parts
    date_index = next((index for index, part in enumerate(parts) if len(part) == 8 and part.isdigit()), None)
    if date_index is None or date_index + 1 >= len(parts):
        return None
    candidates = [episode_path / "metadata.json", episode_path.parent / "metadata.json"]
    metadata_path = next((path for path in candidates if path.is_file()), None)
    return {
        "episode_id": relative.as_posix(),
        "episode_path": episode_path,
        "episode_name": episode_path.name,
        "task": parts[0],
        "date": parts[date_index],
        "operator": parts[date_index + 1],
        "metadata_path": metadata_path,
    }


def discover_task(root: Path, task_path: Path, errors: list[str]) -> list[dict[str, Any]]:
    contexts = []

    def onerror(error: OSError):
        errors.append(str(error))

    for current, dirnames, _files in os.walk(task_path, onerror=onerror, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        episode_names = [name for name in dirnames if name.startswith("episode_")]
        for name in sorted(episode_names):
            context = context_from_path(root, Path(current) / name)
            if context:
                contexts.append(context)
        if episode_names:
            dirnames[:] = [name for name in dirnames if name not in episode_names]
    return contexts


def fingerprint(root: Path, paths: Iterable[Path | None]) -> str:
    result = []
    for path in paths:
        if path is None:
            continue
        try:
            stat = path.stat()
            result.append([path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns])
        except OSError:
            result.append([str(path), -1, -1])
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


class DurationAnomalyStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def connect(self, readonly: bool = False):
        if readonly:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS episode_inventory (
                    episode_id TEXT PRIMARY KEY, task TEXT NOT NULL, date TEXT NOT NULL,
                    operator TEXT NOT NULL, episode_name TEXT NOT NULL, relative_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, labels_json TEXT NOT NULL, eligible INTEGER NOT NULL,
                    duration_seconds REAL, duration_source TEXT NOT NULL DEFAULT '',
                    generation TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_duration_inventory_context
                    ON episode_inventory(task, date, operator, eligible, duration_seconds);
                CREATE TABLE IF NOT EXISTS task_profiles (
                    task TEXT PRIMARY KEY, algorithm_version TEXT NOT NULL, generation TEXT NOT NULL,
                    baseline_total INTEGER NOT NULL, baseline_size INTEGER NOT NULL,
                    missing_duration_count INTEGER NOT NULL, median REAL, p5 REAL, p25 REAL,
                    p75 REAL, p95 REAL, lower_bound REAL, upper_bound REAL, sigma_log REAL,
                    ready INTEGER NOT NULL, reason TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS anomalies (
                    episode_id TEXT PRIMARY KEY, task TEXT NOT NULL, date TEXT NOT NULL,
                    operator TEXT NOT NULL, direction TEXT NOT NULL, duration_seconds REAL NOT NULL,
                    median REAL NOT NULL, lower_bound REAL NOT NULL, upper_bound REAL NOT NULL,
                    deviation_seconds REAL NOT NULL, ratio_to_median REAL NOT NULL,
                    score REAL NOT NULL, algorithm_version TEXT NOT NULL, generation TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_duration_anomaly_queue
                    ON anomalies(task, date, operator, direction, score DESC);
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
                    status TEXT NOT NULL, full_scan INTEGER NOT NULL, task_filter TEXT NOT NULL,
                    discovered INTEGER NOT NULL DEFAULT 0, parsed INTEGER NOT NULL DEFAULT 0,
                    reused INTEGER NOT NULL DEFAULT 0, eligible INTEGER NOT NULL DEFAULT 0,
                    anomalies INTEGER NOT NULL DEFAULT 0, incomplete_tasks TEXT NOT NULL DEFAULT '[]',
                    errors TEXT NOT NULL DEFAULT '[]', algorithm_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )

    def scan(self, root: Path, full: bool = False, task_filter: str = "") -> dict[str, Any]:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"root is not a readable directory: {root}")
        self.init()
        lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another duration scan is already running") from error
            try:
                return self._scan_locked(root, full, task_filter)
            except Exception as error:
                with self.connect() as connection:
                    running = connection.execute(
                        "SELECT run_id FROM scan_runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    if running:
                        connection.execute(
                            "UPDATE scan_runs SET status='failed', finished_at=?, errors=? WHERE run_id=?",
                            (time.time(), json.dumps([str(error)], ensure_ascii=False), running["run_id"]),
                        )
                raise

    def _scan_locked(self, root: Path, full: bool, task_filter: str) -> dict[str, Any]:
        run_id, started = uuid.uuid4().hex, time.time()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO scan_runs (run_id, started_at, status, full_scan, task_filter, algorithm_version) VALUES (?, ?, 'running', ?, ?, ?)",
                (run_id, started, int(full), task_filter, ALGORITHM_VERSION),
            )
            existing = {row["episode_id"]: dict(row) for row in connection.execute("SELECT * FROM episode_inventory")}

        task_paths = sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))
        if task_filter:
            task_paths = [path for path in task_paths if path.name == task_filter]
            if not task_paths:
                self._fail_run(run_id, f"task not found: {task_filter}")
                raise ValueError(f"task not found: {task_filter}")

        rows_by_task: dict[str, list[dict[str, Any]]] = {}
        incomplete: dict[str, list[str]] = {}
        discovered_count = parsed_count = reused_count = 0
        for task_path in task_paths:
            errors: list[str] = []
            rows = []
            try:
                contexts = discover_task(root, task_path, errors)
                discovered_count += len(contexts)
                for context in contexts:
                    episode_path = context["episode_path"]
                    source_fingerprint = fingerprint(root, [
                        context["metadata_path"], episode_path / "meta" / "info.json",
                        episode_path / "meta" / "episode.json",
                    ])
                    previous = existing.get(context["episode_id"])
                    if previous and not full and previous["fingerprint"] == source_fingerprint:
                        row = dict(previous)
                        row.update(generation=run_id, updated_at=started)
                        reused_count += 1
                    else:
                        metadata = stable_json(context["metadata_path"]) if context["metadata_path"] else {}
                        metadata = metadata or {}
                        labels = (metadata.get("quality") or {}).get("labels") or []
                        if not isinstance(labels, list):
                            labels = []
                        eligible = labels == [NORMAL_LABEL]
                        duration, source = duration_from_episode(episode_path, metadata) if eligible else (None, "")
                        task_name = str(metadata.get("task_key") or context["task"])
                        row = {
                            **{key: context[key] for key in ("episode_id", "episode_name", "date", "operator")},
                            "task": task_name, "relative_path": context["episode_id"],
                            "fingerprint": source_fingerprint,
                            "labels_json": json.dumps(labels, ensure_ascii=False), "eligible": int(eligible),
                            "duration_seconds": duration, "duration_source": source,
                            "generation": run_id, "updated_at": started,
                        }
                        parsed_count += 1
                    rows.append(row)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
                errors.append(str(error))
            if errors:
                incomplete[task_path.name] = errors
            else:
                rows_by_task[task_path.name] = rows

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for directory_task, rows in rows_by_task.items():
                affected_profile_tasks = {row["task"] for row in rows}
                old_profile_tasks = {row[0] for row in connection.execute(
                    "SELECT DISTINCT task FROM episode_inventory WHERE relative_path LIKE ?", (f"{directory_task}/%",)
                )}
                for profile_task in old_profile_tasks | affected_profile_tasks:
                    connection.execute("DELETE FROM anomalies WHERE task = ?", (profile_task,))
                connection.execute("DELETE FROM episode_inventory WHERE relative_path LIKE ?", (f"{directory_task}/%",))
                connection.executemany(
                    """
                    INSERT INTO episode_inventory (
                        episode_id, task, date, operator, episode_name, relative_path, fingerprint,
                        labels_json, eligible, duration_seconds, duration_source, generation, updated_at
                    ) VALUES (
                        :episode_id, :task, :date, :operator, :episode_name, :relative_path, :fingerprint,
                        :labels_json, :eligible, :duration_seconds, :duration_source, :generation, :updated_at
                    )
                    """, rows,
                )
                for profile_task in old_profile_tasks | affected_profile_tasks:
                    self._rebuild_task(connection, profile_task, run_id, started)

            if not task_filter:
                present = {path.name for path in task_paths}
                stored = {str(row[0]).split("/", 1)[0] for row in connection.execute("SELECT relative_path FROM episode_inventory")}
                for removed in stored - present:
                    profile_tasks = {row[0] for row in connection.execute(
                        "SELECT DISTINCT task FROM episode_inventory WHERE relative_path LIKE ?", (f"{removed}/%",)
                    )}
                    connection.execute("DELETE FROM episode_inventory WHERE relative_path LIKE ?", (f"{removed}/%",))
                    for profile_task in profile_tasks:
                        connection.execute("DELETE FROM anomalies WHERE task = ?", (profile_task,))
                        connection.execute("DELETE FROM task_profiles WHERE task = ?", (profile_task,))

            eligible = connection.execute("SELECT COUNT(*) FROM episode_inventory WHERE eligible = 1").fetchone()[0]
            anomaly_count = connection.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
            status = "partial" if incomplete else "complete"
            errors = [f"{task}: {error}" for task, values in incomplete.items() for error in values]
            connection.execute(
                """
                UPDATE scan_runs SET finished_at=?, status=?, discovered=?, parsed=?, reused=?,
                    eligible=?, anomalies=?, incomplete_tasks=?, errors=? WHERE run_id=?
                """,
                (time.time(), status, discovered_count, parsed_count, reused_count, eligible, anomaly_count,
                 json.dumps(sorted(incomplete), ensure_ascii=False), json.dumps(errors[:200], ensure_ascii=False), run_id),
            )
            connection.execute(
                "INSERT INTO index_meta VALUES ('active_generation', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (run_id,),
            )
            connection.execute("DELETE FROM scan_runs WHERE started_at < ?", (time.time() - HISTORY_DAYS * 86400,))
            connection.commit()
        return self.status()

    def _fail_run(self, run_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE scan_runs SET status='failed', finished_at=?, errors=? WHERE run_id=?",
                (time.time(), json.dumps([error], ensure_ascii=False), run_id),
            )

    def _rebuild_task(self, connection: sqlite3.Connection, task: str, generation: str, now: float) -> None:
        rows = connection.execute("SELECT * FROM episode_inventory WHERE task=? AND eligible=1", (task,)).fetchall()
        durations = [float(row["duration_seconds"]) for row in rows if row["duration_seconds"] is not None]
        profile = robust_profile(durations)
        ready = len(durations) >= MIN_BASELINE_SIZE and profile is not None
        values = profile or {key: None for key in ("median", "p5", "p25", "p75", "p95", "lower", "upper", "sigma_log")}
        connection.execute(
            """
            INSERT INTO task_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task) DO UPDATE SET algorithm_version=excluded.algorithm_version,
                generation=excluded.generation, baseline_total=excluded.baseline_total,
                baseline_size=excluded.baseline_size, missing_duration_count=excluded.missing_duration_count,
                median=excluded.median, p5=excluded.p5, p25=excluded.p25, p75=excluded.p75,
                p95=excluded.p95, lower_bound=excluded.lower_bound, upper_bound=excluded.upper_bound,
                sigma_log=excluded.sigma_log, ready=excluded.ready, reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (task, ALGORITHM_VERSION, generation, len(rows), len(durations), len(rows)-len(durations),
             values["median"], values["p5"], values["p25"], values["p75"], values["p95"],
             values["lower"], values["upper"], values["sigma_log"], int(ready),
             "" if ready else "insufficient_baseline", now),
        )
        if not ready:
            return
        for row in rows:
            if row["duration_seconds"] is None:
                continue
            result = classify(float(row["duration_seconds"]), profile)
            if result["direction"] == "none":
                continue
            connection.execute(
                "INSERT INTO anomalies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["episode_id"], task, row["date"], row["operator"], result["direction"],
                 row["duration_seconds"], profile["median"], profile["lower"], profile["upper"],
                 result["deviation_seconds"], result["ratio_to_median"], result["score"],
                 ALGORITHM_VERSION, generation),
            )

    def status(self) -> dict[str, Any]:
        if not self.db_path.is_file():
            return {"available": False, "db_path": str(self.db_path), "reason": "not_built"}
        with self.connect(readonly=True) as connection:
            run = connection.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            counts = connection.execute(
                "SELECT direction, COUNT(*) FROM anomalies GROUP BY direction"
            ).fetchall()
            eligible = connection.execute("SELECT COUNT(*) FROM episode_inventory WHERE eligible=1").fetchone()[0]
            generation = connection.execute("SELECT value FROM index_meta WHERE key='active_generation'").fetchone()
        return {
            "available": True, "db_path": str(self.db_path), "algorithm_version": ALGORITHM_VERSION,
            "generation": generation[0] if generation else "", "eligible": eligible,
            "anomaly_counts": {row[0]: row[1] for row in counts},
            "last_run": dict(run) | {
                "incomplete_tasks": json.loads(run["incomplete_tasks"] or "[]"),
                "errors": json.loads(run["errors"] or "[]"),
            } if run else None,
        }

    def anomaly_rows(self, direction: str = "", task: str = "", date: str = "", operator: str = ""):
        clauses, params = [], []
        for column, value in (("a.direction", direction), ("a.task", task), ("a.date", date), ("a.operator", operator)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect(readonly=True) as connection:
            return connection.execute(
                f"""
                SELECT a.*, e.episode_name, e.relative_path, e.duration_source
                FROM anomalies a JOIN episode_inventory e USING(episode_id)
                {where}
                ORDER BY CASE a.direction WHEN 'long' THEN 0 ELSE 1 END,
                    a.score DESC, a.duration_seconds DESC, a.episode_id
                """, params,
            ).fetchall()

    def group_rows(self):
        with self.connect(readonly=True) as connection:
            return connection.execute(
                """
                SELECT e.task, e.date, e.operator,
                    SUM(CASE WHEN e.eligible=1 AND e.duration_seconds IS NOT NULL THEN 1 ELSE 0 END) eligible,
                    SUM(CASE WHEN a.direction='long' THEN 1 ELSE 0 END) long_count,
                    SUM(CASE WHEN a.direction='short' THEN 1 ELSE 0 END) short_count
                FROM episode_inventory e LEFT JOIN anomalies a USING(episode_id)
                GROUP BY e.task, e.date, e.operator
                HAVING long_count>0 OR short_count>0
                ORDER BY long_count DESC, short_count DESC, e.task, e.date DESC, e.operator
                """
            ).fetchall()


ANOMALY_FIELDS = [
    "episode_id", "episode_name", "relative_path", "task", "date", "operator", "direction",
    "duration_seconds", "duration_source", "median", "lower_bound", "upper_bound",
    "deviation_seconds", "ratio_to_median", "score", "algorithm_version", "generation",
]


def export_csv(store: DurationAnomalyStore, output: Path, **filters: str) -> int:
    rows = store.anomaly_rows(**filters)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANOMALY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in ANOMALY_FIELDS})
    return len(rows)


def format_duration(seconds: float) -> str:
    rounded = int(round(seconds))
    minutes, remainder = divmod(rounded, 60)
    return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"


def write_html_report(store: DurationAnomalyStore, output: Path, limit: int = 1000) -> int:
    rows = store.anomaly_rows()
    groups = store.group_rows()
    status = store.status()
    output.parent.mkdir(parents=True, exist_ok=True)
    group_html = "".join(
        f"<tr><td>{html.escape(row['task'])}</td><td>{row['date']}</td><td>{html.escape(row['operator'])}</td>"
        f"<td>{row['eligible']}</td><td class='long'>{row['long_count']}</td><td>{row['short_count']}</td>"
        f"<td>{((row['long_count']+row['short_count'])/row['eligible']*100 if row['eligible'] else 0):.1f}%</td></tr>"
        for row in groups
    )
    episode_html = "".join(
        f"<tr><td>{html.escape(row['episode_name'])}</td><td>{html.escape(row['task'])}</td><td>{row['date']}</td>"
        f"<td>{html.escape(row['operator'])}</td><td class='{row['direction']}'>{'偏长' if row['direction']=='long' else '偏短'}</td>"
        f"<td>{format_duration(row['duration_seconds'])}</td><td>{format_duration(row['median'])}</td>"
        f"<td>{abs(row['deviation_seconds']):.1f}s</td><td title='{html.escape(row['relative_path'])}'>{html.escape(row['relative_path'])}</td></tr>"
        for row in rows[:max(1, limit)]
    )
    last_run = status.get("last_run") or {}
    document = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>Episode 时长异常复查</title>
<style>body{{font:14px system-ui;background:#0f172a;color:#e2e8f0;margin:24px}}h1,h2{{margin:0 0 12px}}p{{color:#94a3b8}}.cards{{display:flex;gap:12px;margin:18px 0}}.card{{padding:14px 18px;background:#1e293b;border-radius:10px}}.card b{{display:block;font-size:24px;margin-top:5px}}table{{width:100%;border-collapse:collapse;background:#111827;margin-bottom:26px}}th,td{{padding:9px;border-bottom:1px solid #263244;text-align:left}}th{{position:sticky;top:0;background:#1e293b}}.long{{color:#fca5a5}}.short{{color:#fde68a}}td:last-child{{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}</style></head><body>
<h1>Episode 时长异常复查</h1><p>只使用唯一标签为“完全正常”的样本。结果用于人工复查，不修改任何源数据。算法 {ALGORITHM_VERSION}；最近运行状态 {html.escape(str(last_run.get('status','unknown')))}。</p>
<div class='cards'><div class='card'>完全正常样本<b>{status.get('eligible',0)}</b></div><div class='card'>异常偏长<b class='long'>{status.get('anomaly_counts',{}).get('long',0)}</b></div><div class='card'>异常偏短<b>{status.get('anomaly_counts',{}).get('short',0)}</b></div></div>
<h2>任务 / 日期 / 操作员</h2><table><thead><tr><th>任务</th><th>日期</th><th>操作员</th><th>有效样本</th><th>偏长</th><th>偏短</th><th>异常比例</th></tr></thead><tbody>{group_html}</tbody></table>
<h2>待复查 Episode（偏长优先，最多显示 {max(1,limit)} 条）</h2><table><thead><tr><th>Episode</th><th>任务</th><th>日期</th><th>操作员</th><th>方向</th><th>时长</th><th>中位数</th><th>超出边界</th><th>路径</th></tr></thead><tbody>{episode_html}</tbody></table></body></html>"""
    output.write_text(document, encoding="utf-8")
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone unusual episode-duration detector.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="Scan a finalized episode root and update SQLite results.")
    scan.add_argument("root")
    scan.add_argument("--db", type=Path, default=DEFAULT_DB)
    scan.add_argument("--full", action="store_true")
    scan.add_argument("--task", default="")
    status = subparsers.add_parser("status", help="Print current result status.")
    status.add_argument("--db", type=Path, default=DEFAULT_DB)
    export = subparsers.add_parser("export", help="Export current findings to CSV.")
    export.add_argument("--db", type=Path, default=DEFAULT_DB)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--direction", choices=("", "long", "short"), default="")
    export.add_argument("--task", default="")
    export.add_argument("--date", default="")
    export.add_argument("--operator", default="")
    report = subparsers.add_parser("report", help="Create a standalone HTML review report.")
    report.add_argument("--db", type=Path, default=DEFAULT_DB)
    report.add_argument("--output", type=Path, default=Path("outputs/duration_anomalies/report.html"))
    report.add_argument("--limit", type=int, default=1000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = DurationAnomalyStore(args.db)
    try:
        if args.command == "scan":
            result = store.scan(Path(args.root), full=args.full, task_filter=args.task)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 3 if (result.get("last_run") or {}).get("status") == "partial" else 0
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "export":
            count = export_csv(store, args.output, direction=args.direction, task=args.task, date=args.date, operator=args.operator)
            print(json.dumps({"output": str(args.output.resolve()), "count": count}, ensure_ascii=False, indent=2))
            return 0
        count = write_html_report(store, args.output, args.limit)
        print(json.dumps({"output": str(args.output.resolve()), "anomaly_count": count}, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"duration anomaly command failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
