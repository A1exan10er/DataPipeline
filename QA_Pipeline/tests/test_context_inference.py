from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts.pipeline.qa_core import infer_context, operator_from_episode_path


def test_task_folder_root_still_recovers_absolute_date_and_operator(tmp_path: Path) -> None:
    selected_root = tmp_path / "task" / "UR5e" / "collector" / "20260720" / "xiaguoshun"
    episode = selected_root / "episode_1"
    episode.mkdir(parents=True)

    context = infer_context(
        [selected_root],
        episode,
        {"task_key": "classify_spoons", "robot": "UR5e", "username": "xiaguoshun"},
    )

    assert context["date"] == "20260720"
    assert context["operator"] == "xiaguoshun"
    assert context["task"] == "classify_spoons"
    assert operator_from_episode_path(episode) == "xiaguoshun"


def test_metadata_operator_has_precedence_over_username_and_path(tmp_path: Path) -> None:
    root = tmp_path / "20260720" / "path_operator"
    episode = root / "episode_1"
    episode.mkdir(parents=True)

    context = infer_context(
        [root],
        episode,
        {"operator": "explicit_operator", "username": "metadata_username"},
    )

    assert context["operator"] == "explicit_operator"
