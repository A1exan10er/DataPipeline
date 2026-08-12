from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Werkzeuge import listen_episode_verified as listener


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        phases="1,2,3,6,7",
        phase6_profile="video_only",
        phase6_video_output_mode="sidecar",
        phase6_sidecar_filename="video_rotated_180.mp4",
    )


def _write_qa_state(db_path: Path, episode: Path, status: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE episodes (episode_path TEXT, metrics TEXT)")
        conn.execute(
            "INSERT INTO episodes VALUES (?, ?)",
            (str(episode), json.dumps({"p6_umi_status": status})),
        )


def test_listener_defaults_enable_realtime_umi_sidecars() -> None:
    args = listener.parse_args(["worker"])

    assert "6" in args.phases.split(",")
    assert args.phase6_profile == "video_only"
    assert args.phase6_video_output_mode == "sidecar"
    assert args.phase6_sidecar_filename == "video_rotated_180.mp4"


def test_phase6_postcondition_requires_successful_receipt(tmp_path: Path) -> None:
    episode = tmp_path / "episode_0001_umi"
    episode.mkdir()
    (episode / "metadata.json").write_text('{"robot":"umi"}\n', encoding="utf-8")
    qa_db = tmp_path / "qa.db"
    _write_qa_state(qa_db, episode, "error")

    error = listener.phase6_postcondition_error(qa_db, [episode], _args())

    assert "status=error" in error


def test_phase6_postcondition_accepts_sidecars_and_receipt(tmp_path: Path) -> None:
    episode = tmp_path / "episode_0001_umi"
    episode.mkdir()
    for stream in (
        "observation.image.left_wrist_view",
        "observation.image.right_wrist_view",
    ):
        stream_dir = episode / stream
        stream_dir.mkdir()
        (stream_dir / "video_rotated_180.mp4").write_bytes(b"rotated")
    metadata = {
        "data_pipeline": {
            "completed_procedures": {
                "umi.video_orientation_sidecar": {"version": 1}
            }
        }
    }
    (episode / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    qa_db = tmp_path / "qa.db"
    _write_qa_state(qa_db, episode, "passed")

    assert listener.phase6_postcondition_error(qa_db, [episode], _args()) == ""


def test_phase6_postcondition_errors_are_scoped_per_episode(tmp_path: Path) -> None:
    failed = tmp_path / "episode_0001_umi"
    passed = tmp_path / "episode_0002_robot"
    failed.mkdir()
    passed.mkdir()
    (failed / "metadata.json").write_text('{"robot":"umi"}\n', encoding="utf-8")
    (passed / "metadata.json").write_text('{"robot":"ur5"}\n', encoding="utf-8")
    qa_db = tmp_path / "qa.db"
    with sqlite3.connect(qa_db) as conn:
        conn.execute("CREATE TABLE episodes (episode_path TEXT, metrics TEXT)")
        conn.executemany(
            "INSERT INTO episodes VALUES (?, ?)",
            (
                (str(failed), json.dumps({"p6_umi_status": "error"})),
                (str(passed), json.dumps({"p6_umi_status": "not_applicable"})),
            ),
        )

    errors = listener.phase6_postcondition_errors(qa_db, [failed, passed], _args())

    assert list(errors) == [str(failed.resolve())]
    assert "status=error" in errors[str(failed.resolve())]


def _insert_failed_job(
    db_path: Path,
    *,
    event_id: str,
    mounted_path: Path,
    error: str,
) -> int:
    listener.init_job_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO jobs (
                event_id, record_id, session_id, verified_path, mounted_path,
                payload_json, status, attempts, run_id, output_dir, db_path,
                error, received_at, started_at, finished_at, updated_at
            ) VALUES (?, '', '', ?, ?, '{}', 'failed', 3, 'old-run', 'old-output',
                      'old.db', ?, '2026-08-12T00:00:00Z', '2026-08-12T00:00:01Z',
                      '2026-08-12T00:00:02Z', '2026-08-12T00:00:02Z')
            """,
            (event_id, str(mounted_path), str(mounted_path), error),
        )
        return int(cursor.lastrowid)


def test_missing_episode_is_rejected_before_batch_stability_check(tmp_path: Path) -> None:
    missing = tmp_path / "episode_0001_missing"

    try:
        listener._resolve_job_episode({"mounted_path": str(missing)})
    except FileNotFoundError as exc:
        assert str(missing) in str(exc)
    else:
        raise AssertionError("missing episode must be rejected before batching")


def test_recover_only_collateral_missing_path_failures(tmp_path: Path) -> None:
    job_db = tmp_path / "jobs.db"
    existing = tmp_path / "episode_0001_existing"
    existing.mkdir()
    (existing / "metadata.json").write_text("{}\n", encoding="utf-8")
    truly_missing = tmp_path / "episode_0002_missing"
    unrelated_missing = tmp_path / "episode_9999_missing"
    pipeline_episode = tmp_path / "episode_0003_pipeline_error"
    pipeline_episode.mkdir()
    (pipeline_episode / "metadata.json").write_text("{}\n", encoding="utf-8")

    collateral_id = _insert_failed_job(
        job_db,
        event_id="collateral",
        mounted_path=existing,
        error=f"episode path does not exist: {unrelated_missing}",
    )
    missing_id = _insert_failed_job(
        job_db,
        event_id="actual-missing",
        mounted_path=truly_missing,
        error=f"episode path does not exist: {truly_missing}",
    )
    pipeline_id = _insert_failed_job(
        job_db,
        event_id="pipeline-error",
        mounted_path=pipeline_episode,
        error="pipeline exit code 1",
    )

    assert listener.recover_collateral_missing_path_failures(job_db) == 1
    with sqlite3.connect(job_db) as conn:
        rows = dict(
            conn.execute(
                "SELECT id, status || ':' || attempts FROM jobs ORDER BY id"
            ).fetchall()
        )
    assert rows[collateral_id] == "pending:0"
    assert rows[missing_id] == "failed:3"
    assert rows[pipeline_id] == "failed:3"
