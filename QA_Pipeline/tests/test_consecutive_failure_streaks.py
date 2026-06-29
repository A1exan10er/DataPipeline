from __future__ import annotations

import http.client
import json
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
    active_consecutive_failure_warnings,
    consecutive_bad_segments,
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


def detection(start: int, end: int) -> dict:
    episodes = [bad(number) for number in range(start, end + 1)]
    return {
        "task": TASK,
        "robot": ROBOT,
        "operator": OPERATOR,
        "start_episode_number": start,
        "end_episode_number": end,
        "start_episode_name": f"episode_{start:04d}",
        "end_episode_name": f"episode_{end:04d}",
        "streak_length": end - start + 1,
        "issue_types": ["check_a"],
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

        resolved = resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, 1, 5)
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

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "issue_history.db"
        init_issue_history(db_path)
        record_consecutive_failure_detections(db_path, [detection(9, 13)])
        assert_equal(resolve_consecutive_failure(db_path, TASK, ROBOT, OPERATOR, 9, 13), True, "resolve prefix")
        record_consecutive_failure_detections(db_path, [detection(9, 31)])
        assert_equal(
            [(start, end) for start, end, resolved_at in db_ranges(db_path) if resolved_at is None],
            [(14, 31)],
            "resolved prefix growth creates only new suffix",
        )


def run_dashboard_ui_tests() -> None:
    html = render_index_html(SimpleNamespace(refresh_seconds=5.0))
    assert_equal("let pendingResolveKey = null;" in html, True, "resolve confirmation uses stable identity")
    assert_equal("pendingResolveButton" in html, False, "resolve confirmation does not retain stale DOM node")
    assert_equal("if (!r.ok) throw new Error" in html, True, "POST errors are surfaced")
    assert_equal("if (!data.ok) throw new Error" in html, True, "unmatched resolve is surfaced")


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
            assert_equal(db_ranges(db_path)[0][2] is not None, True, "resolve endpoint sets resolved_at")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            dashboard.EVENT_JOB_DB = original_event_job_db


def main() -> None:
    run_segment_tests()
    run_history_tests()
    run_dashboard_ui_tests()
    run_resolve_endpoint_test()


if __name__ == "__main__":
    main()
