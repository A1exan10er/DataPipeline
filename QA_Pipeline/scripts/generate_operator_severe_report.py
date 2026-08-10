"""Generate an operator-level evidence report for configured severe QA criteria."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "operator_severe_report.json"
SEVERITY_ORDER = ("info", "minor", "major", "critical")
OUTPUT_FILENAMES = (
    "operator_severe_ranking.csv",
    "operator_severe_evidence.csv",
    "criteria_frequency.csv",
    "operator_severe_report.md",
    "operator_severe_report.json",
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        paths = generate_operator_severe_report(
            Path(args.db_path),
            Path(args.output_dir),
            Path(args.config),
        )
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"Error: could not generate operator severe report: {exc}")
        return 1
    for path in paths:
        print(f"Wrote operator severe report: {path}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an operator severe-QA evidence report.")
    parser.add_argument("--db-path", required=True, help="QA pipeline SQLite database path.")
    parser.add_argument("--output-dir", required=True, help="Directory for report files.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Severe report JSON config path.")
    return parser.parse_args(argv)


def generate_operator_severe_report(
    db_path: Path,
    output_dir: Path,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> list[Path]:
    """Read one completed pipeline DB and write the severe-QA report files."""
    db_path = Path(db_path).resolve()
    output_dir = Path(output_dir)
    config = load_report_config(config_path)
    episodes, findings = _query_rows_read_only(db_path)
    report = build_report(episodes, findings, config, db_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / filename for filename in OUTPUT_FILENAMES]
    criterion_ids = [criterion["id"] for criterion in config["criteria"] if criterion["enabled"]]
    ranking_fields = [
        "rank",
        "operator",
        "total_episode_count",
        "severe_episode_count",
        "severe_episode_rate_percent",
        "criterion_hit_count",
        "source_finding_count",
        *[f"{criterion_id}_episode_count" for criterion_id in criterion_ids],
        "top_reasons",
        "top_tasks",
        "top_robots",
    ]
    evidence_fields = [
        "operator",
        "episode_path",
        "date",
        "task",
        "robot",
        "controller",
        "criterion_id",
        "criterion_label",
        "source_check_names",
        "source_finding_count",
        "highest_source_severity",
        "source_severities",
        "source_statuses",
        "messages",
        "details_json",
    ]
    frequency_fields = [
        "criterion_id",
        "criterion_label",
        "affected_episode_count",
        "affected_operator_count",
        "affected_named_operator_count",
        "affected_unknown_operator_episode_count",
        "source_finding_count",
    ]
    _write_csv(paths[0], report["operator_ranking"], ranking_fields)
    _write_csv(paths[1], report["evidence"], evidence_fields)
    _write_csv(paths[2], report["criterion_frequency"], frequency_fields)
    paths[3].write_text(_render_markdown(report), encoding="utf-8")
    paths[4].write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


def load_report_config(config_path: Path) -> dict[str, Any]:
    try:
        with Path(config_path).open("r", encoding="utf-8") as file_obj:
            config = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load severe report config {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("Severe report config must contain a JSON object")
    criteria = config.get("criteria")
    if not isinstance(criteria, list):
        raise RuntimeError("Severe report config 'criteria' must be a list")

    seen_ids: set[str] = set()
    validated = []
    for index, item in enumerate(criteria):
        if not isinstance(item, dict):
            raise RuntimeError(f"Criterion at index {index} must be a JSON object")
        criterion = dict(item)
        criterion["enabled"] = criterion.get("enabled", True)
        if not isinstance(criterion["enabled"], bool):
            raise RuntimeError(f"Criterion at index {index} has non-boolean enabled value")
        criterion_id = criterion.get("id")
        if criterion["enabled"]:
            if not isinstance(criterion_id, str) or not criterion_id.strip():
                raise RuntimeError(f"Enabled criterion at index {index} must have a non-empty id")
            criterion_id = criterion_id.strip()
            if criterion_id in seen_ids:
                raise RuntimeError(f"Duplicate enabled criterion id: {criterion_id}")
            seen_ids.add(criterion_id)
        elif not isinstance(criterion_id, str):
            criterion_id = f"disabled_{index}"
        criterion["id"] = criterion_id
        criterion["label"] = str(criterion.get("label") or criterion_id)
        for field in ("check_names", "check_name_patterns", "statuses", "severities"):
            if field in criterion:
                _validate_string_list(criterion[field], f"criterion {criterion_id}.{field}")
        if "phases" in criterion:
            phases = criterion["phases"]
            if not isinstance(phases, list) or not all(isinstance(value, int) and value > 0 for value in phases):
                raise RuntimeError(f"criterion {criterion_id}.phases must be a list of positive integers")
        validated.append(criterion)

    unknown_values = config.get("unknown_operator_values", ["", "unknown", "未填写"])
    _validate_string_list(unknown_values, "unknown_operator_values", allow_empty_values=True)
    include_unknown = config.get("include_unknown_operator_in_ranking", False)
    if not isinstance(include_unknown, bool):
        raise RuntimeError("include_unknown_operator_in_ranking must be boolean")
    ranking = config.get("ranking", {})
    if not isinstance(ranking, dict):
        raise RuntimeError("ranking must be a JSON object")
    primary = ranking.get("primary", "severe_episode_count")
    tie_breakers = ranking.get(
        "tie_breakers", ["criterion_hit_count", "severe_episode_rate_percent", "operator"]
    )
    _validate_string_list(tie_breakers, "ranking.tie_breakers")
    supported = {
        "severe_episode_count",
        "criterion_hit_count",
        "severe_episode_rate_percent",
        "operator",
    }
    if primary not in supported or any(field not in supported for field in tie_breakers):
        raise RuntimeError("ranking contains an unsupported field")

    result = dict(config)
    result["criteria"] = validated
    result["unknown_operator_values"] = unknown_values
    result["include_unknown_operator_in_ranking"] = include_unknown
    result["ranking"] = {"primary": primary, "tie_breakers": tie_breakers}
    return result


def _validate_string_list(value: Any, label: str, allow_empty_values: bool = False) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{label} must be a list of strings")
    if not allow_empty_values and any(not item for item in value):
        raise RuntimeError(f"{label} must not contain empty strings")


def _query_rows_read_only(db_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not db_path.is_file():
        raise RuntimeError(f"Source database does not exist: {db_path}")
    uri = f"{db_path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        episode_rows = conn.execute(
            """
            SELECT episode_path, task, date, operator, robot, controller, final_status
            FROM episodes
            ORDER BY episode_path
            """
        ).fetchall()
        finding_rows = conn.execute(
            """
            SELECT episode_path, phase, check_name, severity, status, message, details
            FROM findings
            ORDER BY episode_path, phase, check_name, id
            """
        ).fetchall()
    return [dict(row) for row in episode_rows], [dict(row) for row in finding_rows]


def build_report(
    episode_rows: list[dict[str, Any]],
    finding_rows: list[dict[str, Any]],
    config: dict[str, Any],
    db_path: Path,
) -> dict[str, Any]:
    active_criteria = [criterion for criterion in config["criteria"] if criterion["enabled"]]
    criterion_by_id = {criterion["id"]: criterion for criterion in active_criteria}
    unknown_values = {value.strip().casefold() for value in config["unknown_operator_values"]}
    episodes: dict[str, dict[str, Any]] = {}
    totals: dict[str, set[str]] = defaultdict(set)
    unknown_episode_paths: set[str] = set()
    named_operators: set[str] = set()
    for row in episode_rows:
        path = str(row.get("episode_path") or "")
        operator, is_unknown = _effective_operator(row, unknown_values)
        item = dict(row)
        item["operator"] = operator
        item["operator_is_unknown"] = is_unknown
        item["date"] = str(item.get("date") or _date_from_episode_path(path))
        episodes[path] = item
        totals[operator].add(path)
        if is_unknown:
            unknown_episode_paths.add(path)
        else:
            named_operators.add(operator)

    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for finding in finding_rows:
        episode = episodes.get(str(finding.get("episode_path") or ""))
        if episode is None:
            continue
        for criterion in active_criteria:
            if not _criterion_matches(finding, criterion):
                continue
            key = (episode["operator"], episode["episode_path"], criterion["id"])
            evidence = deduplicated.setdefault(
                key,
                {
                    "operator": episode["operator"],
                    "operator_is_unknown": episode["operator_is_unknown"],
                    "episode_path": episode["episode_path"],
                    "date": episode.get("date") or "",
                    "task": episode.get("task") or "",
                    "robot": episode.get("robot") or "",
                    "controller": episode.get("controller") or "",
                    "criterion_id": criterion["id"],
                    "criterion_label": criterion["label"],
                    "source_check_names_list": [],
                    "source_severities_list": [],
                    "source_statuses_list": [],
                    "messages_list": [],
                    "source_details": [],
                    "source_finding_count": 0,
                },
            )
            evidence["source_finding_count"] += 1
            evidence["source_check_names_list"].append(str(finding.get("check_name") or ""))
            evidence["source_severities_list"].append(str(finding.get("severity") or ""))
            evidence["source_statuses_list"].append(str(finding.get("status") or ""))
            evidence["messages_list"].append(str(finding.get("message") or ""))
            evidence["source_details"].append(_safe_details(finding.get("details")))

    evidence_rows = []
    for key in sorted(deduplicated, key=lambda value: (value[0], value[1], value[2])):
        item = deduplicated[key]
        severities = _ordered_unique(item.pop("source_severities_list"), severity=True)
        statuses = _ordered_unique(item.pop("source_statuses_list"))
        checks = _ordered_unique(item.pop("source_check_names_list"))
        messages = _ordered_unique(item.pop("messages_list"))
        details = item.pop("source_details")
        item["highest_source_severity"] = max(
            severities,
            key=lambda value: SEVERITY_ORDER.index(value) if value in SEVERITY_ORDER else -1,
            default="",
        )
        item["source_check_names"] = " | ".join(checks)
        item["source_severities"] = " | ".join(severities)
        item["source_statuses"] = " | ".join(statuses)
        item["messages"] = " | ".join(messages)
        item["details_json"] = json.dumps(details, ensure_ascii=False, sort_keys=True)
        evidence_rows.append(item)

    ranking_rows = _build_ranking(
        evidence_rows,
        totals,
        active_criteria,
        config["include_unknown_operator_in_ranking"],
        config["ranking"],
    )
    frequency_rows = []
    for criterion_id, criterion in criterion_by_id.items():
        matched = [row for row in evidence_rows if row["criterion_id"] == criterion_id]
        frequency_rows.append(
            {
                "criterion_id": criterion_id,
                "criterion_label": criterion["label"],
                "affected_episode_count": len({row["episode_path"] for row in matched}),
                "affected_operator_count": len({row["operator"] for row in matched}),
                "affected_named_operator_count": len(
                    {row["operator"] for row in matched if not row["operator_is_unknown"]}
                ),
                "affected_unknown_operator_episode_count": len(
                    {row["episode_path"] for row in matched if row["operator_is_unknown"]}
                ),
                "source_finding_count": sum(row["source_finding_count"] for row in matched),
            }
        )
    frequency_rows.sort(
        key=lambda row: (
            -row["affected_episode_count"],
            -row["affected_operator_count"],
            row["criterion_id"],
        )
    )
    dates = sorted(str(row.get("date")) for row in episodes.values() if row.get("date"))
    named_severe_paths = {
        row["episode_path"] for row in evidence_rows if not row["operator_is_unknown"]
    }
    unknown_severe_paths = {
        row["episode_path"] for row in evidence_rows if row["operator_is_unknown"]
    }
    return {
        "source_database": str(Path(db_path).resolve()),
        "generation_time": datetime.now().astimezone().isoformat(),
        "date_range": {"from": dates[0] if dates else "", "to": dates[-1] if dates else ""},
        "summary": {
            "episode_count": len(episodes),
            "named_operator_count": len(named_operators),
            "unknown_operator_episode_count": len(unknown_episode_paths),
            "named_severe_episode_count": len(named_severe_paths),
            "unknown_operator_severe_episode_count": len(unknown_severe_paths),
            "named_ranked_operator_count": sum(
                1 for row in ranking_rows if row["operator"] != "(unknown)"
            ),
        },
        "criteria": active_criteria,
        "criterion_frequency": frequency_rows,
        "operator_ranking": ranking_rows,
        "evidence": evidence_rows,
        "disclaimer": (
            "This report lists severe QA findings and supporting evidence; "
            "it does not itself make an employment decision."
        ),
    }


def _criterion_matches(finding: dict[str, Any], criterion: dict[str, Any]) -> bool:
    check_name = str(finding.get("check_name") or "")
    exact = criterion.get("check_names")
    patterns = criterion.get("check_name_patterns")
    if exact is not None or patterns is not None:
        if check_name not in (exact or []) and not any(
            fnmatch.fnmatchcase(check_name, pattern) for pattern in (patterns or [])
        ):
            return False
    filters = (("phases", "phase"), ("statuses", "status"), ("severities", "severity"))
    for config_field, finding_field in filters:
        if config_field in criterion and finding.get(finding_field) not in criterion[config_field]:
            return False
    return True


def _operator_value(value: Any, unknown_values: set[str]) -> tuple[str, bool]:
    operator = str(value or "").strip()
    is_unknown = operator.casefold() in unknown_values
    return ("(unknown)" if is_unknown else operator), is_unknown


def _effective_operator(
    episode: dict[str, Any],
    unknown_values: set[str],
) -> tuple[str, bool]:
    """Recover missing historical operators without updating the QA database."""
    operator, is_unknown = _operator_value(episode.get("operator"), unknown_values)
    if not is_unknown:
        return operator, False
    episode_path = Path(str(episode.get("episode_path") or ""))
    metadata_path = episode_path / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        metadata = {}
    if isinstance(metadata, dict):
        for field in ("operator", "username"):
            candidate, candidate_unknown = _operator_value(metadata.get(field), unknown_values)
            if not candidate_unknown:
                return candidate, False
    parts = episode_path.parts
    for index, part in enumerate(parts[:-1]):
        if len(part) == 8 and part.isdigit() and index + 1 < len(parts):
            candidate, candidate_unknown = _operator_value(parts[index + 1], unknown_values)
            if not candidate_unknown and not candidate.startswith("episode_"):
                return candidate, False
    return operator, True


def _date_from_episode_path(episode_path: str) -> str:
    for part in Path(episode_path).parts:
        if len(part) == 8 and part.isdigit():
            return part
    return ""


def _safe_details(value: Any) -> Any:
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        return {"raw": value, "parse_error": str(exc)}


def _ordered_unique(values: list[str], severity: bool = False) -> list[str]:
    unique = set(values)
    if severity:
        order = {value: index for index, value in enumerate(SEVERITY_ORDER)}
        return sorted(unique, key=lambda value: (order.get(value, len(order)), value))
    return sorted(unique)


def _build_ranking(
    evidence_rows: list[dict[str, Any]],
    totals: dict[str, set[str]],
    criteria: list[dict[str, Any]],
    include_unknown: bool,
    ranking_config: dict[str, Any],
) -> list[dict[str, Any]]:
    by_operator: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence_rows:
        by_operator[row["operator"]].append(row)
    rows = []
    for operator, evidence in by_operator.items():
        if operator == "(unknown)" and not include_unknown:
            continue
        severe_paths = {row["episode_path"] for row in evidence}
        total_count = len(totals[operator])
        criterion_counts = Counter(row["criterion_id"] for row in evidence)
        reason_counts = Counter(row["criterion_label"] for row in evidence)
        evidence_by_episode = {row["episode_path"]: row for row in evidence}
        task_counts = Counter(
            row["task"] or "(missing)" for row in evidence_by_episode.values()
        )
        robot_counts = Counter(
            row["robot"] or "(missing)" for row in evidence_by_episode.values()
        )
        row = {
            "operator": operator,
            "total_episode_count": total_count,
            "severe_episode_count": len(severe_paths),
            "severe_episode_rate_percent": round(
                len(severe_paths) / total_count * 100.0 if total_count else 0.0,
                2,
            ),
            "criterion_hit_count": len(evidence),
            "source_finding_count": sum(item["source_finding_count"] for item in evidence),
            "top_reasons": _format_counter(reason_counts),
            "top_tasks": _format_counter(task_counts),
            "top_robots": _format_counter(robot_counts),
        }
        for criterion in criteria:
            row[f"{criterion['id']}_episode_count"] = criterion_counts[criterion["id"]]
        rows.append(row)
    fields = [ranking_config["primary"], *ranking_config["tie_breakers"]]

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        values = []
        for field in fields:
            value = row[field]
            values.append(str(value) if field == "operator" else -float(value))
        return tuple(values)

    rows.sort(key=sort_key)
    return [{"rank": rank, **row} for rank, row in enumerate(rows, start=1)]


def _format_counter(counter: Counter[str], limit: int = 3) -> str:
    values = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return " | ".join(f"{label} ({count})" for label, count in values)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    date_range = report["date_range"]
    lines = [
        "# Operator severe-QA evidence report",
        "",
        f"- Source database: `{report['source_database']}`",
        f"- Generation time: {report['generation_time']}",
        f"- Date range found in episode rows: {date_range['from'] or 'N/A'} to {date_range['to'] or 'N/A'}",
        f"- Number of episodes: {summary['episode_count']}",
        f"- Number of named operators: {summary['named_operator_count']}",
        f"- Number of unknown-operator episodes: {summary['unknown_operator_episode_count']}",
        "",
        "## Criterion definitions and thresholds",
        "",
    ]
    for criterion in report["criteria"]:
        lines.append(f"- `{criterion['id']}` — {criterion['label']}")
        if criterion.get("description"):
            lines.append(f"  - {criterion['description']}")
        filters = []
        for field in ("phases", "check_names", "check_name_patterns", "statuses", "severities"):
            if field in criterion:
                filters.append(f"{field}={criterion[field]}")
        if filters:
            lines.append("  - Filters: " + "; ".join(filters))
        for threshold in criterion.get("thresholds", []):
            lines.append(f"  - Threshold: {threshold}")
    lines.extend(["", "## Criterion frequency", "", _markdown_table(report["criterion_frequency"]), ""])
    lines.extend(["## Operator ranking (most frequent to least)", "", _markdown_table(report["operator_ranking"]), ""])
    lines.extend(["## Per-operator criteria and evidence", ""])
    evidence_by_operator: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in report["evidence"]:
        evidence_by_operator[row["operator"]].append(row)
    ranked = [row["operator"] for row in report["operator_ranking"]]
    remaining = sorted(set(evidence_by_operator) - set(ranked))
    for operator in [*ranked, *remaining]:
        rows = evidence_by_operator[operator]
        criterion_counts = Counter(row["criterion_id"] for row in rows)
        lines.append(f"### {operator}")
        lines.append("")
        lines.append("Criterion counts: " + ", ".join(
            f"`{criterion_id}`={count}" for criterion_id, count in sorted(criterion_counts.items())
        ))
        lines.append("")
        for row in rows:
            lines.append(
                f"- `{row['episode_path']}` — {row['criterion_label']}: "
                f"{row['messages'] or row['source_check_names']}"
            )
        lines.append("")
    lines.extend(["## Important note", "", report["disclaimer"], ""])
    return "\n".join(lines)


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No matching rows._"
    fields = list(rows[0])
    lines = [
        "| " + " | ".join(fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
    ]
    for row in rows:
        values = [str(row.get(field, "")).replace("|", "\\|").replace("\n", " ") for field in fields]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
