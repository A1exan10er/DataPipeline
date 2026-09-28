from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = PROJECT_ROOT / "QA_Pipeline"
for path in (PROJECT_ROOT, QA_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from QA_Pipeline.scripts import qa_control_dashboard as dashboard
from Werkzeuge import listen_episode_verified as listener


def _qa_db(path: Path, episode: Path, failed_checks: list[str]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE findings (episode_path TEXT, check_name TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO findings VALUES (?, ?, 'fail')",
            ((str(episode), check_name) for check_name in failed_checks),
        )


def _done_job(job_db: Path, episode: Path, qa_db: Path, event_id: str) -> int:
    with sqlite3.connect(job_db) as conn:
        cursor = conn.execute(
            """
            INSERT INTO jobs (
                event_id, record_id, session_id, verified_path, mounted_path,
                payload_json, status, attempts, run_id, output_dir, db_path,
                error, received_at, started_at, finished_at, updated_at
            ) VALUES (?, '', '', ?, ?, '{}', 'done', 2, 'old-run',
                      'old-output', ?, 'old-error', '2026-08-31T00:00:00Z',
                      '2026-08-31T00:00:01Z', '2026-08-31T00:00:02Z',
                      '2026-08-31T00:00:02Z')
            """,
            (event_id, str(episode), str(episode), str(qa_db)),
        )
        return int(cursor.lastrowid)


def test_selector_and_apply_only_requeue_mismatch_only_jobs(tmp_path: Path) -> None:
    job_db = tmp_path / "jobs.db"
    listener.init_job_db(job_db)

    eligible_episode = tmp_path / "insert_pcb" / "20260831" / "op" / "episode_0001"
    mixed_episode = tmp_path / "insert_pcb" / "20260831" / "op" / "episode_0002"
    prior_episode = tmp_path / "insert_pcb" / "20260830" / "op" / "episode_0003"
    eligible_qa = tmp_path / "eligible.db"
    mixed_qa = tmp_path / "mixed.db"
    prior_qa = tmp_path / "prior.db"
    _qa_db(eligible_qa, eligible_episode, ["task_robot_mismatch"])
    _qa_db(mixed_qa, mixed_episode, ["task_robot_mismatch", "duration_too_short"])
    _qa_db(prior_qa, prior_episode, ["task_robot_mismatch"])
    eligible_id = _done_job(job_db, eligible_episode, eligible_qa, "eligible")
    mixed_id = _done_job(job_db, mixed_episode, mixed_qa, "mixed")
    prior_id = _done_job(job_db, prior_episode, prior_qa, "prior")

    candidates = listener.task_robot_mismatch_requeue_candidates(
        job_db, "20260831", tmp_path
    )

    assert [candidate["id"] for candidate in candidates] == [eligible_id]
    assert listener.apply_task_robot_mismatch_requeue(job_db, candidates) == [eligible_id]
    with sqlite3.connect(job_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = {
            int(row["id"]): dict(row)
            for row in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        }
        history = dict(
            conn.execute(
                "SELECT * FROM job_reprocess_history WHERE job_id = ?", (eligible_id,)
            ).fetchone()
        )

    assert rows[eligible_id]["status"] == "pending"
    assert rows[eligible_id]["attempts"] == 0
    assert rows[eligible_id]["run_id"] == ""
    assert rows[eligible_id]["output_dir"] == ""
    assert rows[eligible_id]["db_path"] == ""
    assert rows[mixed_id]["status"] == "done"
    assert rows[prior_id]["status"] == "done"
    payload = json.loads(rows[eligible_id]["payload_json"])
    assert payload["qa_reprocess"]["reason"] == listener.TASK_ROBOT_MISMATCH_REQUEUE_REASON
    assert history["previous_status"] == "done"
    assert history["previous_attempts"] == 2
    assert history["previous_run_id"] == "old-run"
    assert history["previous_output_dir"] == "old-output"
    assert history["previous_db_path"] == str(eligible_qa)
    assert history["previous_error"] == "old-error"


def test_reprocess_marker_uses_new_timestamped_output_tree(tmp_path: Path) -> None:
    args = argparse.Namespace(
        run_id_prefix="event-verified",
        output_dir=str(tmp_path / "output"),
    )
    jobs = [
        {
            "id": 42,
            "payload_json": json.dumps(
                {"qa_reprocess": {"reason": listener.TASK_ROBOT_MISMATCH_REQUEUE_REASON}}
            ),
        }
    ]

    run_id, output = listener.pipeline_run_location(
        args, jobs, "20260831-120000-123456"
    )

    assert "reprocess" in run_id
    assert output == (
        tmp_path
        / "output"
        / "reprocesses"
        / "reprocess_20260831-120000-123456_jobs_00000042_00000042"
    )


def test_mismatch_requeue_command_defaults_to_dry_run() -> None:
    args = listener.parse_args(
        ["requeue-task-robot-mismatch", "--date", "20260831"]
    )

    assert args.apply is False
    assert args.date == "20260831"


def test_dashboard_job_detail_exposes_reprocess_audit_history(
    monkeypatch, tmp_path: Path
) -> None:
    job_db = tmp_path / "jobs.db"
    listener.init_job_db(job_db)
    episode = tmp_path / "insert_pcb" / "20260831" / "op" / "episode_0001"
    qa_db = tmp_path / "qa.db"
    _qa_db(qa_db, episode, ["task_robot_mismatch"])
    job_id = _done_job(job_db, episode, qa_db, "dashboard-history")
    candidates = listener.task_robot_mismatch_requeue_candidates(
        job_db, "20260831", tmp_path
    )
    listener.apply_task_robot_mismatch_requeue(job_db, candidates)
    monkeypatch.setattr(dashboard, "EVENT_JOB_DB", job_db)

    detail = dashboard.event_listener_job_detail(job_id, tmp_path)

    assert len(detail["reprocess_history"]) == 1
    history = detail["reprocess_history"][0]
    assert history["reason"] == listener.TASK_ROBOT_MISMATCH_REQUEUE_REASON
    assert history["previous_run_id"] == "old-run"
    assert history["previous_output_dir"] == "old-output"
