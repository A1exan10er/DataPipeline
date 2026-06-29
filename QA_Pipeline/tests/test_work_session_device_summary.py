from __future__ import annotations

import json
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
from QA_Pipeline.scripts.qa_control_dashboard import render_device_failure_summary_html, work_session_report_payload


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
        report_dir = write_report(root / "reports", report, load_config(DEFAULT_CONFIG))
        saved = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
        markdown = (report_dir / "半日质检报告.md").read_text(encoding="utf-8")
        assert_equal("device_failure_summary" in saved, True, "saved report.json includes device summary")
        payload = work_session_report_payload(report_dir)
        assert_equal(payload["device_failure_summary"][0]["collector_id"], "collector-modified", "HTML payload includes device summary")
        assert_equal("## 二、设备故障统计" in markdown, True, "Markdown includes device summary section")
        assert_equal("重点设备风险" in markdown, True, "Markdown highlights dominant device issue")
        html = render_device_failure_summary_html(report["device_failure_summary"])
        assert_equal("设备故障统计" in html and "device-summary-item risk" in html, True, "HTML highlights dominant device issue")


if __name__ == "__main__":
    main()
