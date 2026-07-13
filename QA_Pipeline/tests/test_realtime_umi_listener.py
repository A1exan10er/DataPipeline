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
