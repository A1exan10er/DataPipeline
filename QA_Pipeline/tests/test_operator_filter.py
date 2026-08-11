from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from QA_Pipeline.scripts import run_pipeline


def test_operator_filter_matches_standard_layout_case_insensitively() -> None:
    episode = Path(
        "/mnt/nas/database/verified/task/robot/collector/20260720/Alice/episode_0001"
    )

    assert run_pipeline._episode_matches_operator_filter(episode, "alice")
    assert not run_pipeline._episode_matches_operator_filter(episode, "bob")


def test_operator_filter_matches_legacy_layout() -> None:
    episode = Path("/mnt/nas/database/verified/task/20260720/alice/episode_0001")

    assert run_pipeline._episode_matches_operator_filter(episode, "alice")


def test_operator_filter_cli_combines_with_date_range() -> None:
    args = run_pipeline._parse_args(
        [
            "--roots",
            "/tmp",
            "--date-from",
            "20260713",
            "--date-to",
            "20260724",
            "--operator",
            "alice",
        ]
    )

    assert args.date_from == "20260713"
    assert args.date_to == "20260724"
    assert args.operator == "alice"
