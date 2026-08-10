from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from QA_Pipeline.scripts.generate_work_session_report import (
    SessionWindow,
    build_report_from_rows,
    collector_id_for_episode,
    device_failure_summary_rows,
    load_config,
    DEFAULT_CONFIG,
    write_report,
)
from QA_Pipeline.scripts import qa_control_dashboard as dashboard
from QA_Pipeline.scripts.qa_control_dashboard import (
    api_event_work_session_issue_detail,
    event_work_session_report,
    render_device_failure_detail_html,
    render_device_failure_summary_html,
    render_event_device_failure_report_html,
    render_event_work_session_issue_html,
    render_event_work_session_report_html,
    work_session_report_payload,
)


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"PASS {label}: {actual!r}")


def episode_row(path: Path) -> dict:
    return {
        "episode_path": str(path),
        "task": "task",
        "date": "2026-06-29",
        "operator": "operator",
        "robot": "arx5",
        "controller": "",
        "final_status": "fail",
        "training_ready": 0,
        "last_updated": datetime.now().astimezone().isoformat(),
    }


def finding(path: Path, check_name: str, status: str) -> dict:
    return {
        "episode_path": str(path),
        "phase": 3,
        "check_name": check_name,
        "severity": "major",
        "status": status,
        "message": "test",
        "details": "{}",
        "task": "task",
        "date": "2026-06-29",
        "operator": "operator",
        "robot": "arx5",
        "controller": "",
        "final_status": "fail",
    }


def write_metadata(path: Path, data: object) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "metadata.json").write_text(json.dumps(data), encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        primary = root / "collector-path-fallback" / "episode_0001"
        missing_field = root / "collector-must-not-be-used" / "episode_0002"
        missing_file = root / "collector-path-only" / "episode_0003"
        invalid_json = root / "collector-invalid-json" / "episode_0004"
        write_metadata(primary, {"collector_id": "collector-metadata-primary"})
        write_metadata(missing_field, {"task_key": "task"})
        missing_file.mkdir(parents=True)
        invalid_json.mkdir(parents=True)
        (invalid_json / "metadata.json").write_text("{invalid", encoding="utf-8")

        episodes = [episode_row(path) for path in (primary, missing_field, missing_file, invalid_json)]
        findings = [
            finding(primary, "frame_drop_ratio", "fail"),
            finding(primary, "frame_drop_ratio", "needs_review"),
            finding(primary, "frame_drop_ratio", "fail"),
            finding(primary, "abnormal_fps_loss", "fail"),
            finding(primary, "ignored_warning", "warning"),
            finding(primary, "ignored_pass", "pass"),
            finding(missing_field, "metadata_exists", "fail"),
            finding(missing_file, "frame_drop_ratio", "fail"),
            finding(missing_file, "frame_drop_ratio", "fail"),
            finding(invalid_json, "abnormal_fps_loss", "needs_review"),
        ]
        rows = device_failure_summary_rows(episodes, findings)

        assert_equal([row["collector_id"] for row in rows], [
            "collector-metadata-primary",
            "collector-path-only",
            "collector-invalid-json",
            "unknown_collector",
        ], "collectors sorted by failure count")
        assert_equal(rows[0]["finding_count"], 4, "only fail and needs_review findings counted")
        assert_equal(rows[0]["check_name_counts"][0], {
            "check_name": "frame_drop_ratio",
            "finding_count": 3,
            "percent": 75.0,
        }, "check breakdown sorted by frequency")
        assert_equal(rows[0]["hardware_issue_signal"], True, "dominant issue above 70 percent flagged")
        assert_equal(rows[-1]["collector_id"], "unknown_collector", "valid metadata missing collector stays unknown")

        with patch("pathlib.Path.open", side_effect=PermissionError("denied")):
            assert_equal(
                collector_id_for_episode(str(missing_file), {}),
                "collector-path-only",
                "permission error uses path fallback",
            )
        invalid_encoding = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with patch("pathlib.Path.open", side_effect=invalid_encoding):
            assert_equal(
                collector_id_for_episode(str(invalid_json), {}),
                "collector-invalid-json",
                "invalid metadata encoding uses path fallback",
            )

        cache: dict[str, str] = {}
        assert_equal(collector_id_for_episode(str(primary), cache), "collector-metadata-primary", "metadata is authoritative")
        write_metadata(primary, {"collector_id": "collector-modified"})
        assert_equal(collector_id_for_episode(str(primary), cache), "collector-metadata-primary", "collector lookup cached per path")

        now = datetime.now().astimezone()
        report = build_report_from_rows(
            root / "qa.db",
            SessionWindow("custom", "测试", now - timedelta(hours=1), now),
            load_config(DEFAULT_CONFIG),
            episodes,
            findings,
            rule_config={"default": {}, "rules": {}},
        )
        assert_equal(report["device_failure_summary"][0]["collector_id"], "collector-modified", "report JSON includes device summary")
        report["device_failure_report_url"] = "/event-listener/device-failure-report.html?report=test-report"
        report_dir = write_report(root / "reports", report, load_config(DEFAULT_CONFIG))
        saved = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
        markdown = (report_dir / "半日质检报告.md").read_text(encoding="utf-8")
        assert_equal("device_failure_summary" in saved, True, "saved report.json includes device summary")
        assert_equal(saved["operator_issue_breakdown"][0]["finding_count"], 5, "saved report has structured operator issue counts")
        assert_equal(bool(saved["operator_issue_details"][0]["findings"]), True, "saved report has per-finding evidence snapshot")
        event_db = root / "event-source.db"
        event_episode = str(root / "collector-event" / "20260728" / "operator" / "episode_event")
        with sqlite3.connect(event_db) as conn:
            conn.executescript(
                """
                CREATE TABLE episodes (
                    episode_path TEXT PRIMARY KEY, task TEXT, date TEXT, operator TEXT,
                    robot TEXT, controller TEXT, final_status TEXT, training_ready INTEGER,
                    last_updated TEXT
                );
                CREATE TABLE findings (
                    id INTEGER PRIMARY KEY, episode_path TEXT, phase INTEGER, check_name TEXT,
                    severity TEXT, status TEXT, message TEXT, details TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO episodes VALUES (?, 'task', '20260728', 'operator', 'arx5', 'gello', 'fail', 0, ?)",
                (event_episode, now.isoformat()),
            )
            conn.execute(
                "INSERT INTO findings VALUES (1, ?, 3, 'abnormal_fps_loss', 'major', 'fail', 'fps low', ?)",
                (event_episode, json.dumps({"modality": "observation.image.left", "deviation_ratio": 0.4})),
            )
        with patch.object(dashboard, "event_listener_report_jobs", return_value=[{"db_path": str(event_db)}]):
            event_report = dashboard.build_event_work_session_report(
                SessionWindow("custom", "测试事件", now - timedelta(hours=1), now + timedelta(hours=1)),
                load_config(DEFAULT_CONFIG),
            )
        assert_equal(len(event_report["rule_explanations"]), 1, "event report snapshots rule explanations")
        assert_equal(
            bool(event_report["operator_issue_episodes"][0].get("threshold_summary")),
            True,
            "event report enriches operator evidence with actual thresholds",
        )
        payload = work_session_report_payload(report_dir)
        assert_equal(payload["device_failure_summary"][0]["collector_id"], "collector-modified", "HTML payload includes device summary")
        assert_equal("## 二、设备故障统计" in markdown, True, "Markdown includes device summary section")
        assert_equal("重点设备风险" in markdown, False, "Markdown omits full device breakdown")
        assert_equal("[设备故障统计](/event-listener/device-failure-report.html?report=test-report)" in markdown, True, "Markdown links to detail page")

        many_rows = [
            {
                **report["device_failure_summary"][0],
                "collector_id": f"collector-{index:02d}",
                "finding_count": 20 - index,
            }
            for index in range(12)
        ]
        summary_html = render_device_failure_summary_html(many_rows, "/detail?report=selected")
        assert_equal("collector-09" in summary_html, True, "main HTML includes tenth collector")
        assert_equal("collector-10" in summary_html, True, "main HTML includes every collector")
        assert_equal("另有" in summary_html, False, "main HTML does not hide extra collectors")
        assert_equal("frame_drop_ratio" in summary_html, False, "main HTML omits check breakdown")
        assert_equal('href="/detail?report=selected"' in summary_html, True, "main HTML links to selected detail report")
        detail_html = render_device_failure_detail_html(many_rows)
        assert_equal("collector-11" in detail_html, True, "detail HTML includes every collector")
        assert_equal("frame_drop_ratio" in detail_html and "device-summary-item risk" in detail_html, True, "detail HTML retains breakdown and risk")

        with patch.object(dashboard, "event_work_session_report_root", return_value=root / "reports"):
            selected = event_work_session_report(report_dir.name)
            assert_equal(selected["report_key"], report_dir.name, "report key selects exact report directory")
            assert_equal(event_work_session_report("../outside"), {}, "report key rejects path traversal")
            main_page = render_event_work_session_report_html(report_dir.name)
            detail_page = render_event_device_failure_report_html(report_dir.name)
            issue_data = api_event_work_session_issue_detail(
                {
                    "report": [report_dir.name],
                    "operator": ["operator"],
                    "check_name": ["frame_drop_ratio"],
                    "page": ["1"],
                    "page_size": ["1"],
                }
            )
            issue_page = render_event_work_session_issue_html(
                {
                    "report": [report_dir.name],
                    "operator": ["operator"],
                    "check_name": ["frame_drop_ratio"],
                }
            )
            expected_query = dashboard.event_report_url("", report_dir.name).removeprefix("?")
            assert_equal(
                '<article class="work-session-report">' in main_page,
                True,
                "main HTML renders Markdown as structured content",
            )
            assert_equal("<pre>#" in main_page, False, "main HTML does not show raw Markdown headings")
            assert_equal("operatorIssueModal" in main_page, True, "main page includes operator issue modal")
            assert_equal("/work-session-report-issue.html?" in main_page, True, "operator issues are clickable")
            assert_equal("采集人员问题明细" in main_page, False, "flat eight-sample section is removed")
            assert_equal(expected_query in main_page, True, "main page carries report context to detail URL")
            assert_equal(expected_query in detail_page, True, "detail page carries report context back")
            assert_equal("collector-modified" in detail_page, True, "detail page reads selected report JSON")
            assert_equal(issue_data["detail_level"], "完整快照", "issue API uses immutable complete snapshot")
            assert_equal(issue_data["summary"]["affected_episode_count"], 2, "issue API counts distinct episodes")
            assert_equal(issue_data["pagination"]["page_size"], 1, "issue API applies bounded pagination")
            assert_equal(len(issue_data["episodes"]), 1, "issue API returns one requested page")
            assert_equal("这个问题是什么意思" in issue_page, True, "fallback detail page explains issue")
            assert_equal("受影响的 episode 与检测证据" in issue_page, True, "fallback detail page lists evidence")


if __name__ == "__main__":
    main()
