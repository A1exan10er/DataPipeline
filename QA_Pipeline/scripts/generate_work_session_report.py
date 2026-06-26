"""Generate Chinese QA work-session reports for forenoon/afternoon review."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_CONFIG = REPO_ROOT / "configs" / "work_session_report.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "outputs" / "reports" / "work_sessions"
STATUS_ORDER = ("fail", "needs_review", "warning", "pass", "pending", "unknown")
SEVERITY_ORDER = ("critical", "major", "minor", "info", "unknown")


@dataclass(frozen=True)
class SessionWindow:
    key: str
    label: str
    start: datetime
    end: datetime


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"Error: database does not exist: {db_path}", file=sys.stderr)
        return 1
    window = resolve_window(args, config)
    report = build_report(db_path, window, config, include_all_when_empty=args.include_all_when_empty)
    output_dir = write_report(Path(args.output_dir), report, config)
    print(f"Wrote work-session report: {output_dir / '半日质检报告.md'}")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Chinese QA work-session report.")
    parser.add_argument("--db-path", required=True, help="QA SQLite database path.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Report output root.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Chinese report configuration JSON.")
    parser.add_argument(
        "--session",
        default="current",
        choices=("forenoon", "afternoon", "current", "previous"),
        help="Work session to report. Use --start/--end for a custom window.",
    )
    parser.add_argument("--start", help="Custom window start ISO datetime.")
    parser.add_argument("--end", help="Custom window end ISO datetime.")
    parser.add_argument(
        "--include-all-when-empty",
        action="store_true",
        help="If no episodes were updated in the window, report all DB episodes for local testing.",
    )
    return parser.parse_args(argv)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise ValueError(f"config must be a JSON object: {path}")
    return data


def resolve_window(args: argparse.Namespace, config: dict[str, Any]) -> SessionWindow:
    if args.start or args.end:
        if not args.start or not args.end:
            raise ValueError("--start and --end must be used together")
        start = parse_datetime(args.start)
        end = parse_datetime(args.end)
        if end <= start:
            raise ValueError("--end must be later than --start")
        return SessionWindow("custom", "自定义", start, end)

    now = datetime.now().astimezone()
    sessions = config.get("work_sessions") or {}
    forenoon = session_for_date("forenoon", now.date(), sessions)
    afternoon = session_for_date("afternoon", now.date(), sessions)
    if args.session == "forenoon":
        return forenoon
    if args.session == "afternoon":
        return afternoon
    if args.session == "current":
        if forenoon.start <= now <= forenoon.end:
            return forenoon
        if afternoon.start <= now <= afternoon.end:
            return afternoon
        return previous_session(now, sessions)
    return previous_session(now, sessions)


def previous_session(now: datetime, sessions: dict[str, Any]) -> SessionWindow:
    today_forenoon = session_for_date("forenoon", now.date(), sessions)
    today_afternoon = session_for_date("afternoon", now.date(), sessions)
    if now > today_afternoon.end:
        return today_afternoon
    if now > today_forenoon.end:
        return today_forenoon
    yesterday = now.date() - timedelta(days=1)
    return session_for_date("afternoon", yesterday, sessions)


def session_for_date(key: str, date_value: Any, sessions: dict[str, Any]) -> SessionWindow:
    session = sessions.get(key) or {}
    label = str(session.get("label") or key)
    start = combine_local(date_value, parse_time(str(session.get("start") or "09:00")))
    end = combine_local(date_value, parse_time(str(session.get("end") or "12:00")))
    return SessionWindow(key, label, start, end)


def combine_local(date_value: Any, value: time) -> datetime:
    now = datetime.now().astimezone()
    return datetime.combine(date_value, value, tzinfo=now.tzinfo)


def parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def build_report(
    db_path: Path,
    window: SessionWindow,
    config: dict[str, Any],
    include_all_when_empty: bool = False,
) -> dict[str, Any]:
    episode_rows, finding_rows = query_window_rows(db_path, window)
    used_fallback = False
    if include_all_when_empty and not episode_rows:
        episode_rows, finding_rows = query_all_rows(db_path)
        used_fallback = True

    episode_by_path = {row["episode_path"]: row for row in episode_rows}
    findings_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in finding_rows:
        findings_by_episode[finding["episode_path"]].append(finding)

    core_issues = core_issue_rows(
        finding_rows,
        config,
        total_episodes=len(episode_rows),
        total_issue_episodes=len(findings_by_episode),
        total_findings=len(finding_rows),
    )
    affected_episodes = affected_episode_rows(episode_rows, findings_by_episode, config)
    actions = action_rows(core_issues, config)
    status_counts = ordered_counter(Counter(row.get("final_status") or "pending" for row in episode_rows), STATUS_ORDER)
    severity_counts = ordered_counter(Counter(row.get("severity") or "unknown" for row in finding_rows), SEVERITY_ORDER)
    blocking_episodes = {
        finding["episode_path"]
        for finding in finding_rows
        if issue_action(finding.get("check_name") or "", config).get("blocks_training", True)
        and finding.get("status") != "pass"
    }

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_db": str(db_path),
        "window": {
            "key": window.key,
            "label": window.label,
            "start": window.start.isoformat(timespec="seconds"),
            "end": window.end.isoformat(timespec="seconds"),
            "used_all_episode_fallback": used_fallback,
        },
        "summary": {
            "episode_count": len(episode_rows),
            "finding_count": len(finding_rows),
            "issue_episode_count": len(findings_by_episode),
            "training_blocking_episode_count": len(blocking_episodes),
            "status_counts": status_counts,
            "severity_counts": severity_counts,
        },
        "core_issues": core_issues,
        "affected_episodes": affected_episodes,
        "suggested_actions": actions,
        "metadata": {
            "episode_paths_seen": sorted(episode_by_path),
        },
    }


def query_window_rows(db_path: Path, window: SessionWindow) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        episodes = [
            dict(row)
            for row in conn.execute(
                """
                SELECT episode_path, task, date, operator, robot, controller,
                       final_status, training_ready, last_updated
                FROM episodes
                WHERE last_updated >= ? AND last_updated < ?
                ORDER BY last_updated, episode_path
                """,
                (window.start.isoformat(), window.end.isoformat()),
            )
        ]
        findings = query_findings_for_episodes(conn, [row["episode_path"] for row in episodes])
    return episodes, findings


def query_all_rows(db_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        episodes = [
            dict(row)
            for row in conn.execute(
                """
                SELECT episode_path, task, date, operator, robot, controller,
                       final_status, training_ready, last_updated
                FROM episodes
                ORDER BY last_updated, episode_path
                """
            )
        ]
        findings = query_findings_for_episodes(conn, [row["episode_path"] for row in episodes])
    return episodes, findings


def query_findings_for_episodes(conn: sqlite3.Connection, episode_paths: list[str]) -> list[dict[str, Any]]:
    if not episode_paths:
        return []
    rows: list[dict[str, Any]] = []
    for chunk in chunks(episode_paths, 500):
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            dict(row)
            for row in conn.execute(
                f"""
                SELECT f.episode_path, f.phase, f.check_name, f.severity, f.status,
                       f.message, f.details, e.task, e.date, e.operator, e.robot,
                       e.controller, e.final_status
                FROM findings f
                LEFT JOIN episodes e ON e.episode_path = f.episode_path
                WHERE f.status != ? AND f.episode_path IN ({placeholders})
                ORDER BY f.phase, f.check_name, f.episode_path
                """,
                ("pass", *chunk),
            )
        )
    return rows


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def core_issue_rows(
    findings: list[dict[str, Any]],
    config: dict[str, Any],
    total_episodes: int = 0,
    total_issue_episodes: int = 0,
    total_findings: int = 0,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for finding in findings:
        check_name = str(finding.get("check_name") or "unknown")
        severity = str(finding.get("severity") or "unknown")
        action = issue_action(check_name, config)
        group = groups.setdefault(
            check_name,
            {
                "check_name": check_name,
                "issue_label": action.get("label") or check_name,
                "severity": severity,
                "severity_counts": Counter(),
                "finding_count": 0,
                "episode_paths": set(),
                "tasks": Counter(),
                "robots": Counter(),
                "operators": Counter(),
                "category": action.get("category", ""),
                "owner": action.get("owner", ""),
                "impact": action.get("impact", ""),
                "action": action.get("action", ""),
                "automation": action.get("automation", ""),
                "blocks_training": bool(action.get("blocks_training", True)),
            },
        )
        if severity_rank(severity) > severity_rank(group["severity"]):
            group["severity"] = severity
        group["finding_count"] += 1
        group["severity_counts"].update([severity])
        group["episode_paths"].add(finding.get("episode_path") or "")
        group["tasks"].update([finding.get("task") or ""])
        group["robots"].update([finding.get("robot") or ""])
        group["operators"].update([finding.get("operator") or ""])

    rows = []
    for group in groups.values():
        episode_count = len({path for path in group["episode_paths"] if path})
        rows.append(
            {
                "check_name": group["check_name"],
                "issue_label": group["issue_label"],
                "severity": group["severity"],
                "severity_label": label_value("severity_labels", group["severity"], config),
                "severity_summary": counter_text(group["severity_counts"], limit=4),
                "finding_count": group["finding_count"],
                "episode_count": episode_count,
                "episode_percent": percent_value(episode_count, total_episodes),
                "issue_episode_percent": percent_value(episode_count, total_issue_episodes),
                "finding_percent": percent_value(group["finding_count"], total_findings),
                "task_count": len([key for key in group["tasks"] if key]),
                "robot_count": len([key for key in group["robots"] if key]),
                "operator_count": len([key for key in group["operators"] if key]),
                "top_tasks": counter_text(group["tasks"]),
                "top_robots": counter_text(group["robots"]),
                "top_operators": counter_text(group["operators"]),
                "episode_paths": "\n".join(sorted(path for path in group["episode_paths"] if path)),
                "episode_preview": episode_preview(group["episode_paths"]),
                "category": group["category"],
                "owner": group["owner"],
                "impact": group["impact"],
                "action": group["action"],
                "automation": group["automation"],
                "blocks_training": group["blocks_training"],
                "priority_score": priority_score(group["severity"], episode_count, group["finding_count"]),
            }
        )
    rows.sort(key=lambda row: (-row["priority_score"], -row["episode_count"], row["check_name"]))
    return rows


def affected_episode_rows(
    episodes: list[dict[str, Any]],
    findings_by_episode: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for episode in episodes:
        episode_findings = findings_by_episode.get(episode["episode_path"], [])
        if not episode_findings:
            continue
        check_names = Counter(finding.get("check_name") or "unknown" for finding in episode_findings)
        severities = Counter(finding.get("severity") or "unknown" for finding in episode_findings)
        blocks_training = any(
            issue_action(finding.get("check_name") or "", config).get("blocks_training", True)
            for finding in episode_findings
        )
        rows.append(
            {
                "episode_path": episode["episode_path"],
                "episode_name": Path(episode["episode_path"]).name,
                "task": episode.get("task") or "",
                "date": episode.get("date") or "",
                "operator": episode.get("operator") or "",
                "robot": episode.get("robot") or "",
                "controller": episode.get("controller") or "",
                "final_status": episode.get("final_status") or "pending",
                "final_status_label": label_value("status_labels", episode.get("final_status") or "pending", config),
                "issue_count": len(episode_findings),
                "top_issues": counter_text(check_names, limit=6),
                "severity_summary": counter_text(severities, limit=4),
                "blocks_training": blocks_training,
                "last_updated": episode.get("last_updated") or "",
            }
        )
    rows.sort(key=lambda row: (-int(row["blocks_training"]), -row["issue_count"], row["episode_path"]))
    return rows


def action_rows(core_issues: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for issue in core_issues:
        rows.append(
            {
                "issue_label": issue["issue_label"],
                "check_name": issue["check_name"],
                "severity": issue["severity_label"],
                "affected_episodes": issue["episode_count"],
                "affected_episode_percent": issue.get("episode_percent", "0.0%"),
                "issue_episode_percent": issue.get("issue_episode_percent", "0.0%"),
                "finding_percent": issue.get("finding_percent", "0.0%"),
                "category": issue["category"],
                "owner": issue["owner"],
                "impact": issue["impact"],
                "suggested_action": issue["action"],
                "automation": issue["automation"],
                "blocks_training": "是" if issue["blocks_training"] else "否",
            }
        )
    return rows


def priority_score(severity: str, episode_count: int, finding_count: int) -> int:
    severity_weight = {"critical": 100, "major": 60, "minor": 20, "info": 5}.get(severity, 1)
    return severity_weight + episode_count * 3 + finding_count


def severity_rank(severity: str) -> int:
    return {"critical": 4, "major": 3, "minor": 2, "info": 1}.get(severity, 0)


def percent_value(count: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(count / total) * 100:.1f}%"


def episode_preview(paths: set[str], limit: int = 20) -> str:
    ordered = sorted(path for path in paths if path)
    preview = ordered[:limit]
    lines = [f"- `{Path(path).name}`：`{path}`" for path in preview]
    remaining = len(ordered) - len(preview)
    if remaining > 0:
        lines.append(f"- 其余 {remaining} 条见 `核心问题汇总.csv` 的 episode_paths 字段。")
    return "\n".join(lines)


def issue_action(check_name: str, config: dict[str, Any]) -> dict[str, Any]:
    default = dict(config.get("default_action") or {})
    specific = dict((config.get("issue_actions") or {}).get(check_name) or {})
    return {**default, **specific}


def label_value(section: str, key: str, config: dict[str, Any]) -> str:
    return str((config.get(section) or {}).get(key) or key or "未知")


def counter_text(counter: Counter[str], limit: int = 3) -> str:
    items = [(key or "未填写", count) for key, count in counter.most_common(limit)]
    return "，".join(f"{key}({count})" for key, count in items) if items else ""


def ordered_counter(counter: Counter[str], order: Sequence[str]) -> dict[str, int]:
    result = {key: int(counter[key]) for key in order if counter.get(key)}
    for key, count in sorted(counter.items()):
        if key not in result:
            result[key] = int(count)
    return result


def write_report(output_root: Path, report: dict[str, Any], config: dict[str, Any]) -> Path:
    start = parse_datetime(report["window"]["start"])
    label = report["window"]["label"]
    dirname = f"{start.strftime('%Y-%m-%d')}_{label}"
    if report["window"]["key"] == "custom":
        dirname = f"{start.strftime('%Y-%m-%d_%H%M')}_自定义"
    output_dir = output_root / dirname
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(output_dir / "核心问题汇总.csv", report["core_issues"])
    write_csv(output_dir / "问题episode清单.csv", report["affected_episodes"])
    write_csv(output_dir / "处理建议.csv", report["suggested_actions"])
    (output_dir / "半日质检报告.md").write_text(render_markdown(report, config), encoding="utf-8")
    return output_dir


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_markdown(report: dict[str, Any], config: dict[str, Any]) -> str:
    summary = report["summary"]
    window = report["window"]
    status_counts = summary["status_counts"]
    severity_counts = summary["severity_counts"]
    fallback_note = ""
    if window.get("used_all_episode_fallback"):
        fallback_note = "\n> 本时间段内没有按 `last_updated` 命中的 episode；当前报告使用全量数据库生成，便于本地测试。\n"

    lines = [
        f"# 数据质检半日报告：{parse_datetime(window['start']).strftime('%Y-%m-%d')} {window['label']}",
        "",
        f"生成时间：{report['generated_at']}",
        f"统计时间：{window['start']} 至 {window['end']}",
        f"数据来源：`{report['source_db']}`",
        fallback_note.rstrip(),
        "",
        "## 一、总体情况",
        "",
        f"- 本时段检查 episode：{summary['episode_count']} 条",
        f"- 非通过 finding：{summary['finding_count']} 条",
        f"- 存在问题的 episode：{summary['issue_episode_count']} 条",
        f"- 影响训练可用的 episode：{summary['training_blocking_episode_count']} 条",
        f"- 状态分布：{status_line(status_counts, config)}",
        f"- 严重程度分布：{severity_line(severity_counts, config)}",
        "",
        "## 二、核心问题",
        "",
    ]
    core_issues = report["core_issues"]
    if core_issues:
        for index, issue in enumerate(core_issues[:10], start=1):
            lines.extend(
                [
                    f"### {index}. {issue['issue_label']}",
                    "",
                    f"- 问题类型：`{issue['check_name']}`",
                    f"- 严重程度：{issue['severity_label']}",
                    f"- 严重程度构成：{issue.get('severity_summary') or issue['severity_label']}",
                    f"- 影响 episode 数：{issue['episode_count']}",
                    f"- 影响 episode 占比：{issue.get('episode_percent', '0.0%')}（占本时段全部 episode），{issue.get('issue_episode_percent', '0.0%')}（占本时段问题 episode）",
                    f"- finding 数：{issue['finding_count']}",
                    f"- finding 占比：{issue.get('finding_percent', '0.0%')}（占本时段非通过 finding）",
                    f"- 主要任务：{issue['top_tasks'] or '未填写'}",
                    f"- 主要机器人：{issue['top_robots'] or '未填写'}",
                    f"- 主要采集人员：{issue['top_operators'] or '未填写'}",
                    f"- 影响判断：{issue['impact']}",
                    f"- 处理建议：{issue['action']}",
                    f"- 建议负责人：{issue['owner']}",
                    "",
                    "涉及 episode：",
                    "",
                    issue.get("episode_preview") or "- 无",
                    "",
                ]
            )
    else:
        lines.append("本时段未发现非通过问题。")
        lines.append("")

    actions = report["suggested_actions"]
    lines.extend(["## 三、处理建议", ""])
    if actions:
        owner_counts = Counter(action["owner"] for action in actions)
        lines.append(f"- 建议优先处理问题数：{len(actions)} 类")
        lines.append(f"- 涉及负责人：{counter_text(owner_counts, limit=8) or '未填写'}")
        for action in actions[:8]:
            lines.append(
                f"- {action['issue_label']}：{action['suggested_action']} "
                f"负责人：{action['owner']}；影响训练：{action['blocks_training']}"
            )
    else:
        lines.append("- 暂无需要处理的问题。")
    lines.extend(
        [
            "",
            "## 四、附件",
            "",
            "- `核心问题汇总.csv`",
            "- `问题episode清单.csv`",
            "- `处理建议.csv`",
            "- `report.json`",
            "",
        ]
    )
    return "\n".join(line for line in lines if line is not None) + "\n"


def status_line(counts: dict[str, int], config: dict[str, Any]) -> str:
    if not counts:
        return "无"
    return "，".join(f"{label_value('status_labels', key, config)} {count}" for key, count in counts.items())


def severity_line(counts: dict[str, int], config: dict[str, Any]) -> str:
    if not counts:
        return "无"
    return "，".join(f"{label_value('severity_labels', key, config)} {count}" for key, count in counts.items())


if __name__ == "__main__":
    raise SystemExit(main())
