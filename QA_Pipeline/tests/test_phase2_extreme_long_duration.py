from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts import run_pipeline
from QA_Pipeline.scripts.pipeline import phase2_duration
from QA_Pipeline.scripts.pipeline.qa_core import EpisodeState


SETTINGS = {
    "enabled": True,
    "group_by": ["task", "robot"],
    "min_group_size": 5,
    "ratio_fail": 4.0,
    "iqr_distance_fail": 5.0,
}


def _state(index: int, duration: float, task: str = "task", robot: str = "robot") -> EpisodeState:
    return EpisodeState(
        episode_path=Path(f"/tmp/episode_{index:04d}"),
        task=task,
        date="20260710",
        operator="operator",
        robot=robot,
        controller="controller",
        metadata={"duration_seconds": duration},
    )


def test_extreme_long_keeps_robot_groups_separate() -> None:
    states = [
        _state(0, 10, robot="a"),
        _state(1, 10, robot="a"),
        _state(2, 10, robot="a"),
        _state(3, 100, robot="a"),
        _state(4, 10, robot="b"),
        _state(5, 10, robot="b"),
        _state(6, 10, robot="b"),
        _state(7, 10, robot="b"),
    ]

    findings, skipped = phase2_duration._check_duration_extreme_long(states, SETTINGS)

    assert findings == []
    assert skipped == 0


def test_extreme_long_minimum_group_size_and_ratio() -> None:
    below_size = [_state(index, duration) for index, duration in enumerate([10, 10, 10, 100])]
    below_ratio = [_state(index, duration) for index, duration in enumerate([10, 10, 10, 10, 39])]

    assert phase2_duration._check_duration_extreme_long(below_size, SETTINGS)[0] == []
    assert phase2_duration._check_duration_extreme_long(below_ratio, SETTINGS)[0] == []


def test_extreme_long_uses_upper_iqr_distance_and_major_fail() -> None:
    durations = [8, 9, 10, 10, 10, 11, 12, 100]
    states = [_state(index, duration) for index, duration in enumerate(durations)]

    findings, _skipped = phase2_duration._check_duration_extreme_long(states, SETTINGS)

    assert len(findings) == 1
    finding = findings[0][1]
    assert finding.check_name == "duration_extreme_long"
    assert finding.severity == "major"
    assert finding.status == "fail"
    assert finding.details["ratio_to_median"] >= SETTINGS["ratio_fail"]
    assert finding.details["upper_iqr_distance"] >= SETTINGS["iqr_distance_fail"]
    assert finding.details["group_values"] == {"task": "task", "robot": "robot"}


def test_extreme_long_zero_iqr_uses_ratio_without_division() -> None:
    states = [_state(index, duration) for index, duration in enumerate([10, 10, 10, 10, 10, 40])]

    findings, _skipped = phase2_duration._check_duration_extreme_long(states, SETTINGS)

    assert len(findings) == 1
    details = findings[0][1].details
    assert details["group_iqr_seconds"] == 0
    assert details["upper_iqr_distance"] is None


def test_extreme_long_skips_missing_default_group_values() -> None:
    states = [_state(index, 10, robot="") for index in range(5)]

    findings, skipped = phase2_duration._check_duration_extreme_long(states, SETTINGS)

    assert findings == []
    assert skipped == 5


def test_legacy_duration_checks_retain_task_only_behavior() -> None:
    durations = [8, 9, 10, 10, 10, 11, 12, 100]
    states = [
        _state(index, duration, robot=f"robot-{index}")
        for index, duration in enumerate(durations)
    ]

    absolute = phase2_duration._check_duration_absolute(states)
    outliers = phase2_duration._check_duration_task_outlier(states)

    assert [finding.check_name for _state_item, finding in absolute] == ["duration_absolute_too_long"]
    assert [finding.check_name for _state_item, finding, _distance in outliers] == ["duration_task_outlier"]


def test_phase2_group_aware_batching_keeps_task_and_robot_subgroups_together() -> None:
    episodes = [Path(f"/tmp/episode_{index}") for index in range(6)]
    cache = {
        str(episode): ("task", "robot-a" if index < 3 else "robot-b")
        for index, episode in enumerate(episodes)
    }

    plan = run_pipeline._make_batch_plan(episodes, [], [2], 2, "auto", cache)

    assert len(plan.batches) == 1
    assert plan.batches[0] == episodes


def test_run_pipeline_severe_report_flags_are_optional() -> None:
    args = run_pipeline._parse_args(["--roots", "/tmp"])

    assert args.generate_operator_severe_report is False
    assert Path(args.operator_severe_config).name == "operator_severe_report.json"
