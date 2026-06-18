"""Generate a live-updating HTML dashboard from QA pipeline SQLite results."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


STATUS_ORDER = ("fail", "needs_review", "warning", "pass", "pending")
SEVERITY_ORDER = ("critical", "major", "minor", "info")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    generate_dashboard(
        Path(args.db_path),
        Path(args.output),
        args.refresh_interval,
        max_episodes=_none_if_non_positive(args.max_episodes),
        max_findings=_none_if_non_positive(args.max_findings),
    )
    print(f"Wrote dashboard: {args.output}")
    return 0


def generate_dashboard(
    db_path: Path,
    output_path: Path,
    refresh_interval_seconds: float = 5.0,
    max_episodes: int | None = None,
    max_findings: int | None = None,
) -> None:
    """Write a dashboard HTML shell plus atomically updated JSON data."""
    payload = _dashboard_payload(db_path, max_episodes=max_episodes, max_findings=max_findings)
    payload["refresh_interval_seconds"] = max(1.0, float(refresh_interval_seconds))
    html = _render_html(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_dashboard_data_path(output_path), payload)
    _write_text_atomic(output_path, html)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a live-updating QA dashboard HTML file.")
    parser.add_argument("--db-path", required=True, help="QA pipeline SQLite database path.")
    parser.add_argument("--output", required=True, help="Dashboard HTML output path.")
    parser.add_argument(
        "--refresh-interval",
        type=float,
        default=5.0,
        help="Browser polling interval in seconds. Default: 5.",
    )
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=0,
        help="Maximum episode detail rows to embed. 0 means unlimited. Default: 0.",
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=0,
        help="Maximum finding detail rows to embed. 0 means unlimited. Default: 0.",
    )
    return parser.parse_args(argv)


def _none_if_non_positive(value: int) -> int | None:
    return value if value and value > 0 else None


def _dashboard_data_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_data.json")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False))


def _write_text_atomic(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def _dashboard_payload(
    db_path: Path,
    max_episodes: int | None = None,
    max_findings: int | None = None,
) -> dict[str, Any]:
    episodes = _episode_rows(db_path, max_episodes)
    findings = _finding_rows(db_path, max_findings)
    summary = _summary_from_db(db_path)
    findings_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        findings_by_episode[finding["episode_path"]].append(finding)
    issue_counts_by_episode = _issue_counts_by_episode(db_path, [episode["episode_path"] for episode in episodes])
    for episode in episodes:
        episode_findings = findings_by_episode.get(episode["episode_path"], [])
        episode["issue_count"] = issue_counts_by_episode.get(episode["episode_path"], len(episode_findings))
        episode["top_issues"] = _top_issue_names(episode_findings, 4)

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "db_path": str(db_path),
        "episodes": episodes,
        "findings": findings,
        "summary": summary,
        "detail_limits": {
            "max_episodes": max_episodes,
            "max_findings": max_findings,
            "episode_rows": len(episodes),
            "finding_rows": len(findings),
        },
    }


def _episode_rows(db_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    limit_sql = "LIMIT ?" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT episode_path, task, date, operator, robot, controller,
                   phases_completed, phase_status, final_status, last_updated
            FROM episodes
            ORDER BY
                CASE final_status
                    WHEN 'fail' THEN 0
                    WHEN 'needs_review' THEN 1
                    WHEN 'warning' THEN 2
                    WHEN 'pending' THEN 3
                    WHEN 'pass' THEN 4
                    ELSE 5
                END,
                last_updated DESC,
                episode_path
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [
        {
            "episode_path": row["episode_path"],
            "episode_name": Path(row["episode_path"]).name,
            "task": row["task"] or "",
            "date": row["date"] or "",
            "operator": row["operator"] or "",
            "robot": row["robot"] or "",
            "controller": row["controller"] or "",
            "phases_completed": _json_value(row["phases_completed"], []),
            "phase_status": _json_value(row["phase_status"], {}),
            "final_status": row["final_status"] or "pending",
            "last_updated": row["last_updated"] or "",
        }
        for row in rows
    ]


def _finding_rows(db_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    limit_sql = "LIMIT ?" if limit is not None else ""
    params: tuple[Any, ...] = ("pass",) + ((limit,) if limit is not None else ())
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT f.id, f.episode_path, f.phase, f.check_name, f.severity,
                   f.status, f.message, f.details,
                   e.task, e.date, e.operator, e.robot, e.controller
            FROM findings f
            LEFT JOIN episodes e ON e.episode_path = f.episode_path
            WHERE f.status != ?
            ORDER BY f.id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
    rows = list(reversed(rows)) if limit is not None else rows
    return [
        {
            "id": row["id"],
            "episode_path": row["episode_path"],
            "episode_name": Path(row["episode_path"]).name,
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
            "details": _json_value(row["details"], {}),
        }
        for row in rows
    ]


def _summary_from_db(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        episode_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        issue_count = conn.execute("SELECT COUNT(*) FROM findings WHERE status != ?", ("pass",)).fetchone()[0]
        status_counts = Counter(
            {
                (status or "pending"): count
                for status, count in conn.execute(
                    "SELECT final_status, COUNT(*) FROM episodes GROUP BY final_status"
                )
            }
        )
        severity_counts = Counter(
            {
                severity: count
                for severity, count in conn.execute(
                    "SELECT severity, COUNT(*) FROM findings WHERE status != ? GROUP BY severity",
                    ("pass",),
                )
            }
        )
        check_counts = conn.execute(
            """
            SELECT check_name, COUNT(*) AS count
            FROM findings
            WHERE status != ?
            GROUP BY check_name
            ORDER BY count DESC
            LIMIT 20
            """,
            ("pass",),
        ).fetchall()
        phase_counts = {
            str(phase): count
            for phase, count in conn.execute(
                """
                SELECT phase, COUNT(*)
                FROM findings
                WHERE status != ?
                GROUP BY phase
                ORDER BY phase
                """,
                ("pass",),
            )
        }
        task_rows = conn.execute(
            """
            SELECT task, final_status, COUNT(*)
            FROM episodes
            GROUP BY task, final_status
            ORDER BY task
            """
        ).fetchall()
    task_status: dict[str, Counter[str]] = defaultdict(Counter)
    for task, status, count in task_rows:
        task_status[task or "(unknown)"][status or "pending"] += count
    return {
        "episode_count": episode_count,
        "issue_count": issue_count,
        "status_counts": {status: status_counts.get(status, 0) for status in STATUS_ORDER},
        "severity_counts": {severity: severity_counts.get(severity, 0) for severity in SEVERITY_ORDER},
        "check_counts": [(name, count) for name, count in check_counts],
        "phase_counts": phase_counts,
        "task_status": {
            task: {status: counts.get(status, 0) for status in STATUS_ORDER}
            for task, counts in sorted(task_status.items())
        },
    }


def _issue_counts_by_episode(db_path: Path, episode_paths: list[str]) -> dict[str, int]:
    if not episode_paths:
        return {}
    counts: dict[str, int] = {}
    chunk_size = 500
    with sqlite3.connect(db_path) as conn:
        for start in range(0, len(episode_paths), chunk_size):
            chunk = episode_paths[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT episode_path, COUNT(*)
                FROM findings
                WHERE status != ? AND episode_path IN ({placeholders})
                GROUP BY episode_path
                """,
                ("pass", *chunk),
            ).fetchall()
            counts.update({path: count for path, count in rows})
    return counts


def _top_issue_names(findings: list[dict[str, Any]], limit: int) -> str:
    counts = Counter(finding["check_name"] for finding in findings)
    return "; ".join(name for name, _ in counts.most_common(limit))


def _json_value(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _render_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    title = "QA Dashboard"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #18202a;
      --muted: #657182;
      --line: #d9dee7;
      --fail: #b42318;
      --review: #9a5b00;
      --warn: #8a6a00;
      --pass: #147447;
      --pending: #5b6675;
      --accent: #255f85;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
      letter-spacing: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 5;
      background: rgba(255,255,255,.96);
      border-bottom: 1px solid var(--line);
      padding: 14px 24px;
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
      font-weight: 700;
    }}
    .subtle {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .refresh-state {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    main {{ padding: 18px 24px 28px; }}
    .grid {{
      display: grid;
      gap: 12px;
    }}
    .metrics {{
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      margin-bottom: 14px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }}
    .metric .label {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      font-weight: 700;
    }}
    .metric .value {{
      font-size: 28px;
      line-height: 1.1;
      font-weight: 800;
      margin-top: 8px;
    }}
    .fail {{ color: var(--fail); }}
    .needs_review {{ color: var(--review); }}
    .warning {{ color: var(--warn); }}
    .pass {{ color: var(--pass); }}
    .pending {{ color: var(--pending); }}
    .sections {{
      grid-template-columns: minmax(340px, 1.2fr) minmax(340px, 1fr);
      align-items: start;
    }}
    h2 {{ font-size: 15px; margin: 0 0 12px; }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(140px, 1fr) 80px;
      gap: 10px;
      align-items: center;
      margin: 8px 0;
      font-size: 13px;
    }}
    .bar-track {{
      height: 9px;
      background: #edf0f4;
      border-radius: 999px;
      overflow: hidden;
      margin-top: 4px;
    }}
    .bar-fill {{
      height: 100%;
      background: var(--accent);
      border-radius: 999px;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: 2fr repeat(4, minmax(120px, 1fr));
      gap: 8px;
      margin: 14px 0 10px;
    }}
    input, select {{
      width: 100%;
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 0 10px;
      font-size: 13px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 8px 7px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 3;
      background: #fbfcfd;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .table-wrap {{
      max-height: 520px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 2px 8px;
      font-weight: 700;
      font-size: 11px;
      background: #eef2f6;
      white-space: nowrap;
    }}
    .pill.fail {{ background: #fee4e2; color: var(--fail); }}
    .pill.needs_review {{ background: #fff2cc; color: var(--review); }}
    .pill.warning {{ background: #fff6d6; color: var(--warn); }}
    .pill.pass {{ background: #dcfae6; color: var(--pass); }}
    .path {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; word-break: break-all; }}
    .details {{ color: var(--muted); max-width: 420px; word-break: break-word; }}
    .tabs {{ display: flex; gap: 6px; margin-top: 14px; }}
    button {{
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 0 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    button.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
    .hidden {{ display: none; }}
    @media (max-width: 1100px) {{
      .metrics {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
      .sections {{ grid-template-columns: 1fr; }}
      .toolbar {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 640px) {{
      header, main {{ padding-left: 12px; padding-right: 12px; }}
      .metrics, .toolbar {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>QA Dashboard</h1>
    <div class="subtle" id="subtitle"></div>
    <div class="refresh-state" id="refreshState"></div>
  </header>
  <main>
    <section class="grid metrics" id="metrics"></section>
    <section class="grid sections">
      <div class="panel">
        <h2>Top Issues</h2>
        <div id="topIssues"></div>
      </div>
      <div class="panel">
        <h2>Issues By Phase</h2>
        <div id="phaseIssues"></div>
      </div>
    </section>
    <div class="tabs">
      <button id="episodesTab" class="active" type="button">Episodes</button>
      <button id="issuesTab" type="button">Issues</button>
    </div>
    <section class="panel" id="episodesPanel">
      <h2>Episode Status</h2>
      <div class="toolbar">
        <input id="episodeSearch" placeholder="Search episode, task, operator, robot">
        <select id="statusFilter"></select>
        <select id="taskFilter"></select>
        <select id="robotFilter"></select>
        <select id="operatorFilter"></select>
      </div>
      <div class="subtle" id="episodeCount"></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Status</th><th>Episode</th><th>Task</th><th>Robot</th><th>Operator</th><th>Issues</th><th>Top Issue Names</th></tr></thead>
          <tbody id="episodeRows"></tbody>
        </table>
      </div>
    </section>
    <section class="panel hidden" id="issuesPanel">
      <h2>Exact Issues</h2>
      <div class="toolbar">
        <input id="issueSearch" placeholder="Search check, episode, message, details">
        <select id="issueStatusFilter"></select>
        <select id="severityFilter"></select>
        <select id="phaseFilter"></select>
        <select id="checkFilter"></select>
      </div>
      <div class="subtle" id="issueCount"></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Status</th><th>Severity</th><th>Phase</th><th>Check</th><th>Episode</th><th>Message</th><th>Details</th></tr></thead>
          <tbody id="issueRows"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script id="dashboardData" type="application/json">{data}</script>
  <script>
    let DATA = JSON.parse(document.getElementById("dashboardData").textContent);
    let GENERATED_AT = DATA.generated_at;
    const AUTO_REFRESH_MS = Math.max(1000, Number(DATA.refresh_interval_seconds || 5) * 1000);
    const DATA_URL = "dashboard_data.json";
    const STATUS = ["fail", "needs_review", "warning", "pass", "pending"];
    const STATUS_LABELS = {{fail: "Fail", needs_review: "Needs Review", warning: "Warning", pass: "Pass", pending: "Pending"}};
    const MAX_ROWS = 500;

    function esc(value) {{
      return String(value ?? "").replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function pct(count, total) {{
      if (!total) return 0;
      return Math.round((count / total) * 1000) / 10;
    }}
    function optionList(values, label) {{
      const opts = [`<option value="">${{label}}</option>`];
      values.filter(Boolean).sort().forEach(v => opts.push(`<option value="${{esc(v)}}">${{esc(v)}}</option>`));
      return opts.join("");
    }}
    function statusPill(status) {{
      return `<span class="pill ${{esc(status)}}">${{esc(STATUS_LABELS[status] || status)}}</span>`;
    }}
    function renderMetrics() {{
      const s = DATA.summary.status_counts;
      const total = DATA.summary.episode_count;
      const items = [
        ["Episodes", total, ""],
        ["Issues", DATA.summary.issue_count, ""],
        ["Fail", s.fail || 0, "fail"],
        ["Needs Review", s.needs_review || 0, "needs_review"],
        ["Warning", s.warning || 0, "warning"],
        ["Pass", s.pass || 0, "pass"],
      ];
      document.getElementById("metrics").innerHTML = items.map(([label, value, cls]) => `
        <div class="panel metric">
          <div class="label">${{label}}</div>
          <div class="value ${{cls}}">${{value}}</div>
          ${{label !== "Episodes" && label !== "Issues" ? `<div class="subtle">${{pct(value, total)}}% of episodes</div>` : ""}}
        </div>`).join("");
      document.getElementById("subtitle").textContent = `Generated ${{DATA.generated_at}} from ${{DATA.db_path}}`;
    }}
    function setRefreshState(text) {{
      const node = document.getElementById("refreshState");
      if (node) node.textContent = text;
    }}
    function setSelectOptions(id, html) {{
      const element = document.getElementById(id);
      const previous = element.value;
      element.innerHTML = html;
      if ([...element.options].some(option => option.value === previous)) {{
        element.value = previous;
      }}
    }}
    async function checkForDashboardUpdate() {{
      if (location.protocol === "file:") {{
        setRefreshState("Live update is unavailable from file://. Serve this directory with python3 -m http.server.");
        return;
      }}
      try {{
        const response = await fetch(DATA_URL, {{cache: "no-store"}});
        if (!response.ok) {{
          setRefreshState(`Live update failed: HTTP ${{response.status}}`);
          return;
        }}
        const latestPayload = await response.json();
        const latest = latestPayload ? latestPayload.generated_at : "";
        const checkedAt = new Date().toLocaleTimeString();
        if (latest && latest !== GENERATED_AT) {{
          DATA = latestPayload;
          GENERATED_AT = latest;
          renderAll(false);
          setRefreshState(`Updated ${{checkedAt}} from dashboard_data.json. Live update on.`);
          return;
        }}
        setRefreshState(`Live update on. Last checked ${{checkedAt}}.`);
      }} catch (error) {{
        setRefreshState(`Live update failed: ${{error}}`);
      }}
    }}
    function renderBars(target, entries, total) {{
      document.getElementById(target).innerHTML = entries.length ? entries.map(([name, count]) => `
        <div class="bar-row">
          <div>
            <div>${{esc(name)}}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${{Math.max(2, pct(count,total))}}%"></div></div>
          </div>
          <strong>${{count}}</strong>
        </div>`).join("") : `<div class="subtle">No issues recorded.</div>`;
    }}
    function renderCharts() {{
      renderBars("topIssues", DATA.summary.check_counts, Math.max(1, DATA.summary.issue_count));
      renderBars("phaseIssues", Object.entries(DATA.summary.phase_counts), Math.max(1, DATA.summary.issue_count));
    }}
    function initFilters() {{
      refreshFilterOptions(false);
      ["episodeSearch","statusFilter","taskFilter","robotFilter","operatorFilter"].forEach(id => document.getElementById(id).addEventListener("input", renderEpisodes));
      ["issueSearch","issueStatusFilter","severityFilter","phaseFilter","checkFilter"].forEach(id => document.getElementById(id).addEventListener("input", renderIssues));
    }}
    function refreshFilterOptions(reset) {{
      const statusOptions = `<option value="">All Statuses</option>` + STATUS.map(s => `<option value="${{s}}">${{STATUS_LABELS[s]}}</option>`).join("");
      const issueStatusOptions = statusOptions;
      const optionUpdates = [
        ["statusFilter", statusOptions],
        ["taskFilter", optionList([...new Set(DATA.episodes.map(e => e.task))], "All Tasks")],
        ["robotFilter", optionList([...new Set(DATA.episodes.map(e => e.robot))], "All Robots")],
        ["operatorFilter", optionList([...new Set(DATA.episodes.map(e => e.operator))], "All Operators")],
        ["issueStatusFilter", issueStatusOptions],
        ["severityFilter", optionList([...new Set(DATA.findings.map(f => f.severity))], "All Severities")],
        ["phaseFilter", optionList([...new Set(DATA.findings.map(f => String(f.phase)))], "All Phases")],
        ["checkFilter", optionList([...new Set(DATA.findings.map(f => f.check_name))], "All Checks")],
      ];
      optionUpdates.forEach(([id, html]) => {{
        if (reset) {{
          document.getElementById(id).innerHTML = html;
        }} else {{
          setSelectOptions(id, html);
        }}
      }});
    }}
    function renderEpisodes() {{
      const q = document.getElementById("episodeSearch").value.toLowerCase();
      const st = document.getElementById("statusFilter").value;
      const task = document.getElementById("taskFilter").value;
      const robot = document.getElementById("robotFilter").value;
      const op = document.getElementById("operatorFilter").value;
      let rows = DATA.episodes.filter(e =>
        (!st || e.final_status === st) && (!task || e.task === task) && (!robot || e.robot === robot) && (!op || e.operator === op) &&
        (!q || [e.episode_path,e.task,e.operator,e.robot,e.top_issues].join(" ").toLowerCase().includes(q))
      );
      rows.sort((a,b) => STATUS.indexOf(a.final_status) - STATUS.indexOf(b.final_status) || b.issue_count - a.issue_count || a.episode_path.localeCompare(b.episode_path));
      document.getElementById("episodeCount").textContent = `Showing ${{Math.min(rows.length, MAX_ROWS)}} of ${{rows.length}} matching episodes`;
      document.getElementById("episodeRows").innerHTML = rows.slice(0, MAX_ROWS).map(e => `
        <tr>
          <td>${{statusPill(e.final_status)}}</td>
          <td><div>${{esc(e.episode_name)}}</div><div class="path">${{esc(e.episode_path)}}</div></td>
          <td>${{esc(e.task)}}</td>
          <td>${{esc(e.robot)}}</td>
          <td>${{esc(e.operator)}}</td>
          <td><strong>${{e.issue_count}}</strong></td>
          <td>${{esc(e.top_issues)}}</td>
        </tr>`).join("");
    }}
    function renderIssues() {{
      const q = document.getElementById("issueSearch").value.toLowerCase();
      const st = document.getElementById("issueStatusFilter").value;
      const sev = document.getElementById("severityFilter").value;
      const phase = document.getElementById("phaseFilter").value;
      const check = document.getElementById("checkFilter").value;
      let rows = DATA.findings.filter(f =>
        (!st || f.status === st) && (!sev || f.severity === sev) && (!phase || String(f.phase) === phase) && (!check || f.check_name === check) &&
        (!q || [f.episode_path,f.check_name,f.message,JSON.stringify(f.details)].join(" ").toLowerCase().includes(q))
      );
      rows.sort((a,b) => STATUS.indexOf(a.status) - STATUS.indexOf(b.status) || a.phase - b.phase || a.episode_path.localeCompare(b.episode_path));
      document.getElementById("issueCount").textContent = `Showing ${{Math.min(rows.length, MAX_ROWS)}} of ${{rows.length}} matching issues`;
      document.getElementById("issueRows").innerHTML = rows.slice(0, MAX_ROWS).map(f => `
        <tr>
          <td>${{statusPill(f.status)}}</td>
          <td><span class="pill">${{esc(f.severity)}}</span></td>
          <td>${{esc(f.phase)}}</td>
          <td>${{esc(f.check_name)}}</td>
          <td><div>${{esc(f.episode_name)}}</div><div class="path">${{esc(f.episode_path)}}</div></td>
          <td>${{esc(f.message)}}</td>
          <td class="details">${{esc(JSON.stringify(f.details))}}</td>
        </tr>`).join("");
    }}
    function initTabs() {{
      const epTab = document.getElementById("episodesTab");
      const isTab = document.getElementById("issuesTab");
      epTab.addEventListener("click", () => {{
        epTab.classList.add("active"); isTab.classList.remove("active");
        document.getElementById("episodesPanel").classList.remove("hidden");
        document.getElementById("issuesPanel").classList.add("hidden");
      }});
      isTab.addEventListener("click", () => {{
        isTab.classList.add("active"); epTab.classList.remove("active");
        document.getElementById("issuesPanel").classList.remove("hidden");
        document.getElementById("episodesPanel").classList.add("hidden");
      }});
    }}
    function renderAll(resetFilters) {{
      renderMetrics();
      renderCharts();
      refreshFilterOptions(resetFilters);
      renderEpisodes();
      renderIssues();
    }}
    renderMetrics();
    renderCharts();
    initFilters();
    initTabs();
    renderEpisodes();
    renderIssues();
    setRefreshState(`Live update on. Polling dashboard_data.json every ${{AUTO_REFRESH_MS / 1000}}s.`);
    setInterval(checkForDashboardUpdate, AUTO_REFRESH_MS);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
