from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = PROJECT_ROOT / "QA_Pipeline"
for value in (PROJECT_ROOT, QA_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from scripts.pipeline import qa_core, run_monitor
from scripts.pipeline.qa_core import EpisodeState, Finding


def _state(index: int) -> EpisodeState:
    return EpisodeState(
        episode_path=Path(f"/verified/task/robot/collector/20260831/operator/episode_{index:06d}"),
        task="task",
        date="20260831",
        operator="operator",
        robot="ur",
        controller="none",
        phases_completed=[2],
        phase_status={2: "fail"},
        metrics={"p2_duration_seconds": 10.0},
        final_status="fail",
    )


def test_write_session_reuses_one_connection_for_many_episode_results(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qa.db"
    qa_core.init_db(db_path)
    real_connect = qa_core.sqlite3.connect
    connection_count = 0

    def counting_connect(*args, **kwargs):
        nonlocal connection_count
        connection_count += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(qa_core.sqlite3, "connect", counting_connect)
    with qa_core.qa_write_session(db_path, max_episodes=25, max_seconds=60):
        for index in range(200):
            state = _state(index)
            qa_core.save_episode_state(db_path, state)
            qa_core.save_findings(
                db_path,
                [
                    Finding(
                        episode_path=str(state.episode_path),
                        phase=2,
                        check_name="duration_extreme_long",
                        severity="major",
                        status="fail",
                        message="extreme",
                    )
                ],
                phase=2,
                episode_path=str(state.episode_path),
            )

    assert connection_count == 1
    with real_connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 200
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 200


def test_bulk_state_loader_does_not_open_one_connection_per_episode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qa.db"
    qa_core.init_db(db_path)
    states = [_state(index) for index in range(1200)]
    with qa_core.qa_write_session(db_path, max_episodes=200, max_seconds=60) as session:
        for state in states:
            qa_core.save_episode_state(db_path, state)
            session.episode_complete()

    real_connect = qa_core.sqlite3.connect
    connection_count = 0

    def counting_connect(*args, **kwargs):
        nonlocal connection_count
        connection_count += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(qa_core.sqlite3, "connect", counting_connect)
    loaded = qa_core.load_episode_states(db_path, [state.episode_path for state in states])

    assert len(loaded) == 1200
    assert connection_count == 1


def test_live_monitor_refresh_uses_incremental_cache_not_full_table_helpers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "qa.db"
    qa_core.init_db(db_path)
    existing = [_state(index) for index in range(20)]
    with qa_core.qa_write_session(db_path, max_episodes=20, max_seconds=60) as session:
        for state in existing:
            qa_core.save_episode_state(db_path, state)
            session.episode_complete()

    monkeypatch.setattr(
        run_monitor,
        "_duration_by_robot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full duration scan")),
    )
    monkeypatch.setattr(
        run_monitor,
        "_final_counts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full status scan")),
    )
    monitor = run_monitor.RunMonitor(
        db_path=db_path,
        output_root=tmp_path / "output",
        run_id="stable-monitor",
        roots=[Path("/verified")],
        phases=[1, 2, 7],
        workers=2,
        refresh_interval_seconds=0.1,
    )
    monitor.start([])
    assert monitor.final_status_counts == {"fail": 20}
    assert monitor._robot_duration_rows()[0]["episodes"] == 20

    changed = existing[0]
    changed.final_status = "pass"
    changed.metrics["p2_duration_seconds"] = 15.0
    monitor.overall_progress(10, 100, "batch")
    monitor.refresh([changed], force=True)

    assert monitor.final_status_counts == {"fail": 19, "pass": 1}
    assert monitor._robot_duration_rows()[0]["duration_seconds"] == 205.0
    status = json.loads(
        (monitor.run_dir / "run_status.json").read_text(encoding="utf-8")
    )
    assert set(status["throughput"]) == {
        "current_phase_per_second",
        "overall_1m_per_second",
        "overall_5m_per_second",
        "overall_15m_per_second",
    }
