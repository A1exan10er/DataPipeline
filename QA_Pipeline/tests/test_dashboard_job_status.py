from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = PROJECT_ROOT / "QA_Pipeline"
for path in (PROJECT_ROOT, QA_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from QA_Pipeline.scripts import qa_control_dashboard
from scripts.pipeline.qa_core import EpisodeState, save_episode_state


def test_failed_job_with_empty_phase_status_is_not_reported_as_qa_pass(tmp_path: Path) -> None:
    db_path = tmp_path / "qa.db"
    episode_path = tmp_path / "episode_0017_20260703-133750_luochao_flexiv_spacemouse"
    state = EpisodeState(
        episode_path=episode_path,
        task="put_cups_in_line",
        date="20260703",
        operator="luochao",
        robot="flexiv",
        controller="spacemouse",
        phase_status={},
        final_status="pass",
    )
    save_episode_state(db_path, state)

    result = qa_control_dashboard.job_episode_result(
        {
            "status": "failed",
            "error": "pipeline exit code 2",
            "db_path": str(db_path),
            "mounted_path": str(episode_path),
        }
    )

    assert result["final_status"] == ""
    assert result["qa_result_missing"] is True
    assert "failed before any phase completed" in result["qa_missing_reason"]
