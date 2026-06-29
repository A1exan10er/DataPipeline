from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from QA_Pipeline.scripts.qa_control_dashboard import (
    active_consecutive_failure_warnings,
    consecutive_bad_segments,
    init_issue_history,
    record_consecutive_failure_detections,
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


def main() -> None:
    run_segment_tests()
    run_history_tests()


if __name__ == "__main__":
    main()
