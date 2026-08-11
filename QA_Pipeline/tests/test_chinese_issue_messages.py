from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts.generate_work_session_report import (
    build_cumulative_report,
    load_config,
    load_rule_explanations,
    render_markdown,
    render_markdown_html,
)
from QA_Pipeline.scripts.pipeline.issue_messages_zh import (
    details_summary_zh,
    finding_presentation_zh,
    issue_location_zh,
    issue_title_zh,
)
from QA_Pipeline.scripts.pipeline.qa_dcs_notifier import DcsIssueNotifier


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label}: expected {needle!r} in {text!r}")
    print(f"PASS {label}: found {needle!r}")


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"PASS {label}: {actual!r}")


def sample_event() -> dict:
    return {
        "run_id": "run-test",
        "recorded_at": "2026-07-01T10:00:00+08:00",
        "finding_id": 1,
        "episode_path": (
            "/mnt/nas/database/verified/task/Robot/"
            "collector-test-machine/20260701/zhangsan/episode_0001"
        ),
        "task": "task",
        "date": "20260701",
        "operator": "zhangsan",
        "robot": "robot",
        "controller": "gello",
        "phase": 3,
        "check_name": "abnormal_fps_loss",
        "severity": "major",
        "status": "fail",
        "message": "Effective FPS is below expected FPS.",
        "details": {
            "modality": "observation.image.right_wrist_right_tactile",
            "expected_fps": 30.0,
            "actual_fps": 18.0,
            "deviation_ratio": 0.4,
            "threshold": 0.15,
        },
    }


def run_dcs_payload_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        notifier = DcsIssueNotifier(Path(tmpdir), "run-test")
        payload = notifier._validated_payload(sample_event())
    assert_contains(payload["message"], "质检发现", "DCS message is Chinese")
    assert_contains(payload["message"], "视频有效帧率", "DCS message includes Chinese issue title")
    assert_contains(payload["recommended_action"], "请检查", "DCS action is Chinese")
    assert_contains(payload["message"], "位置：右腕右侧触觉视频", "DCS message includes readable location")
    assert_equal(payload["details"]["original_message"], "Effective FPS is below expected FPS.", "original English message retained")
    assert_contains(payload["details"]["details_summary_zh"], "帧率偏离比例：40.0%", "details summary uses Chinese percentages")
    assert_contains(payload["details"]["details_summary_zh"], "位置：右腕右侧触觉视频", "details summary includes readable location")


def run_report_chinese_field_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "qa.db"
        episode_path = sample_event()["episode_path"]
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE episodes (
                    episode_path TEXT PRIMARY KEY,
                    task TEXT,
                    date TEXT,
                    operator TEXT,
                    robot TEXT,
                    controller TEXT,
                    final_status TEXT,
                    training_ready INTEGER,
                    last_updated TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE findings (
                    episode_path TEXT,
                    phase INTEGER,
                    check_name TEXT,
                    severity TEXT,
                    status TEXT,
                    message TEXT,
                    details TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO episodes (
                    episode_path, task, date, operator, robot, controller,
                    final_status, training_ready, last_updated
                )
                VALUES (?, 'task', '20260701', 'zhangsan', 'robot', 'gello', 'fail', 0, '2026-07-01T10:00:00+08:00')
                """,
                (episode_path,),
            )
            conn.execute(
                """
                INSERT INTO findings (
                    episode_path, phase, check_name, severity, status, message, details
                )
                VALUES (?, 3, 'abnormal_fps_loss', 'major', 'fail', 'Effective FPS is below expected FPS.', ?)
                """,
                (episode_path, json.dumps(sample_event()["details"])),
            )
        report_config = load_config(QA_PIPELINE_ROOT / "configs" / "work_session_report.json")
        report = build_cumulative_report(
            db_path,
            report_config,
            rule_config=load_rule_explanations(QA_PIPELINE_ROOT / "configs" / "report_rule_explanations_zh.json"),
        )
        markdown = render_markdown(report, report_config)
    operator_index = report["operator_issue_episodes"][0]
    affected_episode = report["affected_episodes"][0]
    assert_equal(operator_index["issue_title_zh"], "视频有效帧率低于预期", "operator index has Chinese issue title")
    assert_contains(operator_index["messages_zh"], "实际有效帧率", "operator index has Chinese message")
    assert_contains(operator_index["details_summary_zh"], "帧率偏离比例：40.0%", "operator index has Chinese evidence")
    assert_contains(operator_index["details_summary_zh"], "位置：右腕右侧触觉视频", "operator index has readable location")
    assert_contains(affected_episode["top_issues_zh"], "视频有效帧率低于预期", "affected episode has Chinese top issue")
    assert_contains(
        markdown,
        "/event-listener/work-session-report-issue.html?",
        "main report links operator issues to complete drill-down",
    )
    assert_contains(markdown, "条问题记录 / 1 个问题 episode", "operator link explains both count units")
    assert_contains(
        markdown,
        "点击具体问题可查看问题含义、判定方法、检测阈值",
        "main report tells readers how to inspect complete evidence",
    )


def run_detail_summary_tests() -> None:
    summary = details_summary_zh({"modality": "observation.image.left", "drop_ratio": 0.125, "max_consecutive_drops": 8})
    assert_contains(summary, "位置：observation.image.left", "detail summary includes location")
    assert_contains(summary, "丢帧比例：12.5%", "detail summary formats ratio")
    assert_contains(
        issue_location_zh({"source_modality": "observation.state.joint_position"}),
        "关节位置数据",
        "state modality has readable location",
    )
    assert_contains(
        issue_location_zh({"segments": [{"source_modality": "observation.image.left_wrist_right_tactile"}]}),
        "左腕右侧触觉视频",
        "nested segment location is detected",
    )
    presentation = finding_presentation_zh(
        {
            "check_name": "operator_standstill_middle",
            "details": {
                "source_modality": "observation.state.joint_position",
                "start_ms": 12000,
                "end_ms": 28000,
                "duration_ms": 16000,
            },
        }
    )
    assert_contains(presentation["time_range_zh"], "12.00–28.00 秒", "time range is human readable")
    assert_contains(presentation["trigger_reason_zh"], "报告条件", "presentation explains why finding was reported")


def run_fallback_and_markdown_tests() -> None:
    assert_equal(
        issue_title_zh("slow_motion_cause_uncertain"),
        "持续低速原因不确定",
        "configured action title beats generic fallback",
    )
    unknown_title = issue_title_zh("future_detector_check")
    assert_contains(unknown_title, "future_detector_check", "unknown issue keeps its technical identity")
    if unknown_title == "数据质检异常":
        raise AssertionError("unknown issue must not collapse to the old generic title")

    rendered = render_markdown_html(
        "# 中文标题\n\n## 小节\n\n- **问题**：`episode_0001`\n\n"
        "[内部链接](/safe)\n\n[危险链接](javascript:alert(1))\n\n<script>alert(1)</script>\n"
    )
    assert_contains(rendered, "<h1>中文标题</h1>", "Markdown heading renders as HTML")
    assert_contains(rendered, "<strong>问题</strong>", "Markdown bold renders as HTML")
    assert_contains(rendered, 'href=\"/safe\"', "safe report link is retained")
    assert_equal("javascript:" in rendered, False, "unsafe link protocol is removed")
    assert_contains(rendered, "&lt;script&gt;", "raw HTML is escaped")


def main() -> None:
    run_dcs_payload_tests()
    run_report_chinese_field_tests()
    run_detail_summary_tests()
    run_fallback_and_markdown_tests()


if __name__ == "__main__":
    main()
