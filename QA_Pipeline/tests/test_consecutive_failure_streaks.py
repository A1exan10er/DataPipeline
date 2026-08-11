from __future__ import annotations

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from QA_Pipeline.scripts import qa_control_dashboard as dashboard
from QA_Pipeline.scripts.qa_control_dashboard import (
    api_recheck_consecutive_failure,
    active_consecutive_failure_warnings,
    consecutive_bad_segments,
    finalize_rechecked_consecutive_failures,
    init_issue_history,
    record_consecutive_failure_detections,
    render_index_html,
    resolve_consecutive_failure,
)


TASK = "task"
ROBOT = "arx5"
OPERATOR = "liangyunbo"


def bad(number: int) -> dict:
    return {
        "episode_number": number,
        "episode_name": f"episode_{number:04d}",
        "mounted_path": f"/tmp/episode_{number:04d}",
        "db_path": "",
        "job_status": "done",
        "final_status": "fail",
        "updated_at": "",
    }


def passed(number: int) -> dict:
    item = bad(number)
    item["final_status"] = "pass"
    return item


def pipeline_failed(number: int) -> dict:
    item = bad(number)
    item["job_status"] = "failed"
    item["final_status"] = ""
    return item


def detection(start: int, end: int, context_date: str = "") -> dict:
    episodes = [bad(number) for number in range(start, end + 1)]
    for episode in episodes:
        episode["date"] = context_date
    return {
        "task": TASK,
        "robot": ROBOT,
        "operator": OPERATOR,
        "context_date": context_date,
        "start_episode_number": start,
        "end_episode_number": end,
        "start_episode_name": f"episode_{start:04d}",
        "end_episode_name": f"episode_{end:04d}",
        "streak_length": end - start + 1,
        "issue_types": ["check_a"],
        "collector_ids": ["collector-test-machine"],
        "episode_status_counts": {"fail": end - start + 1},
        "issue_locations": [
            {
                "check_name": "check_a",
                "count": end - start + 1,
                "locations": [{"location": "observation.image.left_wrist", "count": end - start + 1}],
                "display": f"check_a: observation.image.left_wrist({end - start + 1})",
            }
        ],
        "_episodes": episodes,
    }


def ranges(rows: list[list[dict]]) -> list[tuple[int, int]]:
    return [(row[0]["episode_number"], row[-1]["episode_number"]) for row in rows]


def db_ranges(db_path: Path) -> list[tuple[int, int, str | None]]:
    with sqlite3.connect(db_path) as conn:
        return [
            (int(row[0]), int(row[1]), row[2])
            for row in conn.execute(
                """
                SELECT episode_start, episode_end, resolved_at
                FROM consecutive_failure_streaks
                ORDER BY episode_start, episode_end
                """
            )
        ]


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"PASS {label}: {actual!r}")


def run_segment_tests() -> None:
    assert_equal(
        ranges(consecutive_bad_segments([bad(n) for n in range(1, 6)], 5)),
        [(1, 5)],
        "isolated five-episode segment",
    )
    assert_equal(
        ranges(consecutive_bad_segments([bad(n) for n in range(9, 32)], 5)),
        [(9, 31)],
        "long segment emits one maximal range",
    )
    assert_equal(
        ranges(
            consecutive_bad_segments(
                [bad(n) for n in range(1, 6)]
                + [passed(n) for n in range(6, 9)]
                + [bad(n) for n in range(9, 14)],
                5,
            )
        ),
        [(1, 5), (9, 13)],
        "two segments separated by pass episodes",
    )
    assert_equal(
        ranges(consecutive_bad_segments([bad(1), bad(2), bad(3), bad(5), bad(6), bad(7)], 5)),
        [],
        "gap breaks continuity",
    )
    assert_equal(
        ranges(consecutive_bad_segments([bad(1), bad(2), bad(3)], 5)),
        [],
        "short segment below threshold",
    )
    assert_equal(
        ranges(
            consecutive_bad_segments(
                [bad(n) for n in range(1, 5)]
                + [pipeline_failed(5)]
                + [bad(n) for n in range(6, 11)],
                5,
            )
        ),
        [(6, 10)],
        "pipeline failed job does not count as QA failure",
    )


def run_history_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)

        record_consecutive_failure_detections(db_path, [detection(1, 5)])
        assert_equal(db_ranges(db_path), [(1, 5, None)], "insert initial streak")

        resolved = resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, "", 1, 5)
        assert_equal(resolved, True, "resolve initial streak")
        record_consecutive_failure_detections(db_path, [detection(1, 5)])
        rows = db_ranges(db_path)
        assert_equal(len(rows), 1, "resolved exact range is not reinserted")
        assert_equal(rows[0][0:2], (1, 5), "resolved exact range identity retained")

        record_consecutive_failure_detections(db_path, [detection(9, 31)])
        assert_equal(
            [(start, end) for start, end, resolved_at in db_ranges(db_path) if resolved_at is None],
            [(9, 31)],
            "long new segment inserts one full unresolved row",
        )
        active = active_consecutive_failure_warnings(db_path, 20)
        assert_equal(active["warnings"][0]["start_episode_number"], 9, "active warning starts at segment start")
        assert_equal(active["warnings"][0]["end_episode_number"], 31, "active warning ends at segment end")
        assert_equal(
            active["warnings"][0]["collector_ids"],
            ["collector-test-machine"],
            "active warning exposes collector ids",
        )
        assert_equal(
            active["warnings"][0]["issue_locations"][0]["locations"][0]["location"],
            "observation.image.left_wrist",
            "active warning exposes issue locations",
        )
        assert_equal(
            active["warnings"][0]["episode_status_counts"],
            {"fail": 23},
            "active warning exposes episode result counts",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(9, 13)])
        assert_equal(resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, "", 9, 13), True, "resolve prefix")
        record_consecutive_failure_detections(db_path, [detection(9, 31)])
        assert_equal(
            [(start, end) for start, end, resolved_at in db_ranges(db_path) if resolved_at is None],
            [(14, 31)],
            "resolved prefix growth creates only new suffix",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(40, 44), detection(90, 95)])
        assert_equal(
            active_consecutive_failure_warnings(db_path, 20)["warnings"][0]["start_episode_number"],
            90,
            "latest range is shown for repeated unresolved combo",
        )
        assert_equal(resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, "", 90, 95), True, "resolve repeated combo")
        assert_equal(
            [(start, end) for start, end, resolved_at in db_ranges(db_path) if resolved_at is None],
            [],
            "resolving visible range clears older unresolved ranges for same combo",
        )
        assert_equal(
            active_consecutive_failure_warnings(db_path, 20)["count"],
            0,
            "resolved combo disappears from active warnings",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(1, 5, "20260629")])
        assert_equal(
            resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, "20260629", 1, 5),
            True,
            "resolve first date range",
        )
        record_consecutive_failure_detections(db_path, [detection(1, 5, "20260630")])
        active = active_consecutive_failure_warnings(db_path, 20)
        assert_equal(active["count"], 1, "same episode numbers on new date are detected")
        assert_equal(active["warnings"][0]["context_date"], "20260630", "active warning retains date context")


def run_dashboard_ui_tests() -> None:
    html = render_index_html(SimpleNamespace(refresh_seconds=5.0))
    assert_equal("let pendingResolveKey = null;" in html, True, "resolve confirmation uses stable identity")
    assert_equal("pendingResolveButton" in html, False, "resolve confirmation does not retain stale DOM node")
    assert_equal("const locallyResolvedFailureKeys = new Set();" in html, True, "resolved identities persist across refresh renders")
    assert_equal("locallyResolvedFailureKeys.add(resolveKey);" in html, True, "successful resolve updates local state immediately")
    assert_equal("rawWarnings.filter(item => !locallyResolvedFailureKeys.has" in html, True, "stale refresh cannot restore resolved warning")
    assert_equal("if (!r.ok) throw new Error" in html, True, "POST errors are surfaced")
    assert_equal("if (!data.ok) throw new Error" in html, True, "unmatched resolve is surfaced")
    assert_equal("采集设备：" in html, True, "consecutive failures render collector ids")
    assert_equal("涉及位置：" in html, True, "consecutive failures render issue locations")
    assert_equal("个连续异常组合" in html, True, "mixed QA outcomes use accurate heading")
    assert_equal("不合格项" in html, True, "hard failures have a separate group")
    assert_equal("待复核项" in html, True, "review findings have a separate group")
    assert_equal("操作员质量标记" in html, True, "operator labels have a separate group")
    assert_equal("affected_episode_count" in html, True, "issue rows use affected episode counts")
    assert_equal("user-select: text" in html, True, "issue list explicitly permits text selection")
    assert_equal(
        "fingerprint === consecutiveFailureRenderFingerprint" in html,
        True,
        "unchanged issue data does not replace the DOM",
    )
    assert_equal(
        "consecutiveFailureHasSelection(body)" in html,
        True,
        "active issue-list selection defers DOM replacement",
    )
    assert_equal(
        "flushDeferredConsecutiveFailureRender" in html and "selectionchange" in html,
        True,
        "deferred updates render after selection clears",
    )
    assert_equal("重新检查" in html, True, "consecutive failures render recheck button")
    assert_equal("/api/consecutive-failures/recheck" in html, True, "consecutive failures call recheck endpoint")


def run_human_readable_summary_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "qa.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE findings (
                    episode_path TEXT, check_name TEXT, severity TEXT,
                    status TEXT, details TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO findings VALUES (?, ?, ?, ?, ?)",
                [
                    ("/tmp/episode_0001", "abnormal_fps_loss", "major", "fail", json.dumps({"modality": "camera_a"})),
                    ("/tmp/episode_0001", "abnormal_fps_loss", "major", "fail", json.dumps({"modality": "camera_b"})),
                    ("/tmp/episode_0002", "quality_label_unexpected", "minor", "needs_review", "{}"),
                ],
            )
        streak = [
            {**bad(1), "db_path": str(db_path)},
            {**bad(2), "db_path": str(db_path), "final_status": "needs_review"},
        ]
        summary = dashboard.issue_location_summary_for_streak(streak)
        assert_equal(summary[0]["check_name"], "abnormal_fps_loss", "hard failure sorts first")
        assert_equal(summary[0]["affected_episode_count"], 1, "two modality findings affect one episode")
        assert_equal(summary[0]["count"], 2, "finding count remains available separately")
        assert_equal(summary[0]["title_zh"], "视频有效帧率低于预期", "issue uses human-readable title")
        assert_equal(summary[0]["episode_ranges"], ["episode_0001"], "affected episode range is explicit")
        assert_equal(summary[1]["category"], "operator_label", "operator quality labels are separated")
        assert_equal(
            dashboard.episode_status_counts_for_streak(streak),
            {"fail": 1, "needs_review": 1},
            "mixed streak retains both episode outcomes",
        )
        assert_equal(
            dashboard.consecutive_streak_message(streak),
            "episode_0001 ~ episode_0002 连续2个异常 episode",
            "mixed streak is not called consecutive failure",
        )


def create_event_job_db(db_path: Path, start: int, end: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                mounted_path TEXT NOT NULL UNIQUE,
                run_id TEXT,
                output_dir TEXT,
                db_path TEXT,
                error TEXT,
                received_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT
            )
            """
        )
        for number in range(start, end + 1):
            conn.execute(
                """
                INSERT INTO jobs (
                    status, attempts, mounted_path, run_id, output_dir, db_path,
                    error, received_at, started_at, finished_at, updated_at
                )
                VALUES ('done', 1, ?, 'old-run', 'old-output', 'old.db', '', '', '', '', '')
                """,
                (
                    "/mnt/nas/database/verified/"
                    f"{TASK}/Franka_Research_3/collector-test-machine/20260630/{OPERATOR}/"
                    f"episode_{number:04d}_20260630-120000_{OPERATOR}_franka_gello",
                ),
            )


def run_recheck_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        issue_db = tmp_path / "issue_history.db"
        job_db = tmp_path / "jobs.db"
        init_issue_history(issue_db)
        create_event_job_db(job_db, 1, 5)
        record_consecutive_failure_detections(issue_db, [detection(1, 5, "20260630")])
        state = SimpleNamespace(
            issue_history_db=issue_db,
            lock=threading.Lock(),
            failure_warning_signature=None,
            failure_warning_cache={"count": 1, "warnings": [], "checked_jobs": 5},
            failure_warning_last_checked=0.0,
        )
        original_event_job_db = dashboard.EVENT_JOB_DB
        try:
            dashboard.EVENT_JOB_DB = job_db
            result = api_recheck_consecutive_failure(
                state,
                {
                    "task": TASK,
                    "robot": ROBOT,
                    "operator": OPERATOR,
                    "context_date": "20260630",
                    "episode_start": 1,
                    "episode_end": 5,
                    "collector_ids": ["collector-test-machine"],
                },
            )
            assert_equal(result["requeued_count"], 5, "recheck requeues matching episode jobs")
            with sqlite3.connect(job_db) as conn:
                rows = conn.execute("SELECT status, attempts, db_path FROM jobs ORDER BY id").fetchall()
            assert_equal(rows, [("pending", 0, None)] * 5, "recheck resets jobs to pending")
            active = active_consecutive_failure_warnings(issue_db, 20)
            assert_equal(active["warnings"][0]["recheck_status"]["state"], "pending", "active warning shows pending recheck")
            with sqlite3.connect(job_db) as conn:
                conn.execute("UPDATE jobs SET status = 'done', updated_at = '2026-06-30T12:00:00Z'")
            finalize_rechecked_consecutive_failures(issue_db, [])
            assert_equal(active_consecutive_failure_warnings(issue_db, 20)["count"], 0, "clear rechecked warning when not redetected")
        finally:
            dashboard.EVENT_JOB_DB = original_event_job_db

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(10, 14)])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE consecutive_failure_streaks
                SET recheck_requested_at = ?, recheck_job_ids = ?
                WHERE episode_start = ? AND episode_end = ?
                """,
                ("2026-07-01T16:20:00+08:00", "[1, 2, 3, 4, 5]", 10, 14),
            )
        original_recheck_job_status = dashboard.recheck_job_status
        try:
            dashboard.recheck_job_status = lambda job_ids: {"state": "done", "counts": {"done": 5}, "terminal": True}
            finalize_rechecked_consecutive_failures(db_path, [detection(9, 14)])
            active = active_consecutive_failure_warnings(db_path, 20)
            assert_equal(active["count"], 1, "covered rechecked range remains active")
            assert_equal(
                active["warnings"][0]["recheck_requested_at"],
                "",
                "covered rechecked range clears pending recheck state",
            )
        finally:
            dashboard.recheck_job_status = original_recheck_job_status

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(89, 94)])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE consecutive_failure_streaks
                SET resolved_at = ?, resolution_time_seconds = ?, resolution_source = ?
                WHERE episode_start = ? AND episode_end = ?
                """,
                ("2026-07-01T16:36:46+08:00", 6597, "recheck_clear", 89, 94),
            )
        record_consecutive_failure_detections(db_path, [detection(89, 94)])
        active = active_consecutive_failure_warnings(db_path, 20)
        assert_equal(active["count"], 1, "auto-cleared recheck warning reopens when still detected")
        assert_equal(
            active["warnings"][0]["start_episode_number"],
            89,
            "reopened auto-cleared warning keeps current detected start",
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(1, 5)])
        resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, "", 1, 5)
        record_consecutive_failure_detections(db_path, [detection(1, 5)])
        assert_equal(
            active_consecutive_failure_warnings(db_path, 20)["count"],
            0,
            "manually resolved exact warning stays resolved",
        )


def run_resolve_endpoint_test() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        missing_jobs_db = Path(tmpdir) / "missing-jobs.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(1, 5)])
        state = SimpleNamespace(
            issue_history_db=db_path,
            lock=threading.Lock(),
            failure_warning_signature=None,
            failure_warning_cache={"count": 0, "warnings": [], "checked_jobs": 0},
            failure_warning_last_checked=0.0,
        )
        original_event_job_db = dashboard.EVENT_JOB_DB
        server = dashboard.DashboardHTTPServer(("127.0.0.1", 0), dashboard.make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            dashboard.EVENT_JOB_DB = missing_jobs_db
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            body = json.dumps(
                {
                    "task": TASK,
                    "robot": ROBOT,
                    "operator": OPERATOR,
                    "episode_start": 1,
                    "episode_end": 5,
                }
            )
            connection.request("POST", "/api/consecutive-failures/resolve", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            assert_equal(response.status, 200, "resolve endpoint HTTP status")
            assert_equal(data["ok"], True, "resolve endpoint reports persisted update")
            assert_equal(data["consecutive_failures"]["count"], 0, "resolve endpoint returns updated active count")
            assert_equal(db_ranges(db_path)[0][2] is not None, True, "resolve endpoint sets resolved_at")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            dashboard.EVENT_JOB_DB = original_event_job_db


def run_warning_signature_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        job_db = tmp_path / "jobs.db"
        result_db = tmp_path / "qa.db"
        result_db.write_text("first", encoding="utf-8")
        with sqlite3.connect(job_db) as conn:
            conn.execute(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY,
                    status TEXT,
                    mounted_path TEXT,
                    db_path TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO jobs (id, status, mounted_path, db_path, updated_at)
                VALUES (1, 'done', '/tmp/episode_0001', ?, '2026-07-01T00:00:00Z')
                """,
                (str(result_db),),
            )
        original_event_job_db = dashboard.EVENT_JOB_DB
        try:
            dashboard.EVENT_JOB_DB = job_db
            first = dashboard.event_job_warning_signature()
            os.utime(result_db, (result_db.stat().st_atime + 2, result_db.stat().st_mtime + 2))
            second = dashboard.event_job_warning_signature()
            assert_equal(first[:3], second[:3], "job-table portion remains stable when only qa.db changes")
            assert_equal(first != second, True, "warning signature changes when sampled qa.db mtime changes")
        finally:
            dashboard.EVENT_JOB_DB = original_event_job_db


def main() -> None:
    run_segment_tests()
    run_history_tests()
    run_dashboard_ui_tests()
    run_human_readable_summary_tests()
    run_recheck_tests()
    run_resolve_endpoint_test()
    run_warning_signature_tests()


if __name__ == "__main__":
    main()
