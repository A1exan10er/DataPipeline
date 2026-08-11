from __future__ import annotations

import csv
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts.pipeline import phase7_standstill, slow_motion


def _write_eef(path: Path, speed_cm_s: float, duration_seconds: int = 40) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp_ms", "x", "y", "z"])
        writer.writeheader()
        for index in range(duration_seconds * 10 + 1):
            seconds = index / 10.0
            writer.writerow(
                {
                    "timestamp_ms": int(seconds * 1000),
                    "x": speed_cm_s / 100.0 * seconds,
                    "y": 0,
                    "z": 0,
                }
            )


def _episode(tmp_path: Path, observation_speed: float, action_speed: float | None) -> Path:
    episode = tmp_path / "episode_0001"
    _write_eef(episode / "observation.state.eef_pose" / "data.csv", observation_speed)
    if action_speed is not None:
        _write_eef(episode / "actions.eef_pose" / "data.csv", action_speed)
    return episode


def test_sustained_slow_commands_are_operator_associated(tmp_path: Path) -> None:
    episode = _episode(tmp_path, observation_speed=2.0, action_speed=2.0)

    findings, metrics, _attributor = slow_motion.analyze_episode(episode, "task")

    assert [item["check_name"] for item in findings] == ["operator_sustained_slow_motion"]
    assert findings[0]["status"] == "fail"
    assert findings[0]["details"]["cause"] == "operator_associated"
    assert findings[0]["details"]["longest_segment"]["duration_seconds"] >= 15
    assert metrics["p7_slow_motion_segment_count"] >= 1


def test_normal_commands_with_slow_observation_are_equipment_associated(tmp_path: Path) -> None:
    episode = _episode(tmp_path, observation_speed=2.0, action_speed=10.0)

    findings, _metrics, _attributor = slow_motion.analyze_episode(episode, "task")

    assert [item["check_name"] for item in findings] == ["equipment_motion_response_slow"]
    assert findings[0]["status"] == "fail"
    assert findings[0]["details"]["cause"] == "equipment_associated"


def test_missing_commands_require_review_and_do_not_blame_operator(tmp_path: Path) -> None:
    episode = _episode(tmp_path, observation_speed=2.0, action_speed=None)

    findings, _metrics, _attributor = slow_motion.analyze_episode(episode, "task")

    assert [item["check_name"] for item in findings] == ["slow_motion_cause_uncertain"]
    assert findings[0]["status"] == "needs_review"


def test_static_commands_do_not_count_as_operator_slow_movement(tmp_path: Path) -> None:
    episode = _episode(tmp_path, observation_speed=2.0, action_speed=0.0)

    findings, _metrics, _attributor = slow_motion.analyze_episode(episode, "task")

    assert [item["check_name"] for item in findings] == ["slow_motion_cause_uncertain"]
    assert findings[0]["status"] == "needs_review"


def test_static_samples_are_not_slow_motion_samples(tmp_path: Path) -> None:
    episode = _episode(tmp_path, observation_speed=0.0, action_speed=0.0)

    findings, metrics, _attributor = slow_motion.analyze_episode(episode, "task")

    assert findings == []
    assert metrics["p7_slow_motion_segment_count"] == 0


def test_standstill_attribution_separates_operator_and_equipment(tmp_path: Path) -> None:
    settings = phase7_standstill._settings()
    operator_episode = _episode(tmp_path / "operator", observation_speed=0.0, action_speed=0.0)
    equipment_episode = _episode(tmp_path / "equipment", observation_speed=0.0, action_speed=10.0)

    operator_findings, _metrics = phase7_standstill._episode_findings(
        operator_episode, "task", settings
    )
    equipment_findings, _metrics = phase7_standstill._episode_findings(
        equipment_episode, "task", settings
    )

    operator_names = {item.check_name for item in operator_findings if item.status == "fail"}
    equipment_names = {item.check_name for item in equipment_findings if item.status == "fail"}
    assert any(name.startswith("operator_standstill_") for name in operator_names)
    assert "equipment_motion_response_stalled" in equipment_names
    assert not any(name.startswith("operator_standstill_") for name in equipment_names)


def test_standstill_without_action_is_review_only(tmp_path: Path) -> None:
    episode = _episode(tmp_path, observation_speed=0.0, action_speed=None)

    findings, _metrics = phase7_standstill._episode_findings(
        episode, "task", phase7_standstill._settings()
    )

    uncertain = [item for item in findings if item.check_name == "standstill_cause_uncertain"]
    assert uncertain
    assert {item.status for item in uncertain} == {"needs_review"}
