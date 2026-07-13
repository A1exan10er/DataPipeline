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

from QA_Pipeline.scripts.generate_work_session_report import query_all_rows


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"PASS {label}: {actual!r}")


def insert_finding(conn: sqlite3.Connection, episode_path: str, modality: str, deviation: float) -> None:
    conn.execute(
        """
        INSERT INTO findings (
            episode_path, phase, check_name, severity, status, message, details
        )
        VALUES (?, 3, 'abnormal_fps_loss', 'major', 'fail', 'old threshold result', ?)
        """,
        (
            episode_path,
            json.dumps(
                {
                    "modality": modality,
                    "expected_fps": 30.0,
                    "actual_fps": 30.0 * (1.0 - deviation),
                    "deviation_ratio": deviation,
                    "threshold": 0.10,
                }
            ),
        ),
    )


def run_current_rule_filter_tests() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "qa.db"
        episode_path = "/tmp/episode_0000"
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
                VALUES (?, 'task', '20260701', 'operator', 'robot', '', 'fail', 0, '2026-07-01T10:00:00')
                """,
                (episode_path,),
            )
            insert_finding(conn, episode_path, "observation.image.left_wrist_left_tactile", 0.12)
            insert_finding(conn, episode_path, "observation.image.left_wrist_right_tactile", 0.16)
            insert_finding(conn, episode_path, "observation.image.left_wrist_view", 0.12)

        _episodes, findings = query_all_rows(db_path)
        modalities = sorted(json.loads(row["details"])["modality"] for row in findings)
        assert_equal(
            modalities,
            [
                "observation.image.left_wrist_right_tactile",
                "observation.image.left_wrist_view",
            ],
            "report filters stale tactile FPS findings using current threshold",
        )


if __name__ == "__main__":
    run_current_rule_filter_tests()
