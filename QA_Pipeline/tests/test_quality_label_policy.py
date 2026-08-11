from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = PROJECT_ROOT / "QA_Pipeline"
for path in (PROJECT_ROOT, QA_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from QA_Pipeline.scripts import run_pipeline
from scripts.pipeline import phase1_metadata
from scripts.pipeline.qa_core import EpisodeState, Finding


def make_state(metadata: dict) -> EpisodeState:
    return EpisodeState(
        episode_path=Path("/tmp/episode_0001"),
        task="task",
        date="20260703",
        operator="operator",
        robot="umi",
        controller="none",
        metadata=metadata,
    )


def finding_names(metadata: dict) -> list[str]:
    return [finding.check_name for finding in phase1_metadata._check_quality_labels(make_state(metadata))]


def test_missing_quality_labels_are_fail_findings() -> None:
    findings = phase1_metadata._check_quality_labels(make_state({}))
    assert [finding.check_name for finding in findings] == ["quality_labels_missing"]
    assert findings[0].severity == "major"
    assert findings[0].status == "fail"


def test_normal_and_test_task_labels_are_not_errors() -> None:
    assert finding_names({"quality": {"labels": ["完全正常"]}}) == []
    assert finding_names({"quality": {"labels": ["测试任务"]}}) == []


def test_unexpected_quality_labels_are_internal_review_findings() -> None:
    findings = phase1_metadata._check_quality_labels(make_state({"quality": {"labels": ["动作异常"]}}))
    assert [finding.check_name for finding in findings] == ["quality_label_unexpected"]
    assert findings[0].severity == "minor"
    assert findings[0].status == "needs_review"
    assert findings[0].details["labels"] == ["动作异常"]


def test_missing_quality_label_failure_does_not_block_later_phases() -> None:
    missing_label_state = make_state({})
    missing_label_state.phase_status[1] = "fail"
    missing_label_state.phases_completed.append(1)
    missing_label_state.findings.append(
        Finding(
            episode_path=str(missing_label_state.episode_path),
            phase=1,
            check_name="quality_labels_missing",
            severity="major",
            status="fail",
            message="missing label",
        )
    )

    other_failure_state = make_state({})
    other_failure_state.phase_status[1] = "fail"
    other_failure_state.phases_completed.append(1)
    other_failure_state.findings.append(
        Finding(
            episode_path=str(other_failure_state.episode_path),
            phase=1,
            check_name="required_metadata_field",
            severity="major",
            status="fail",
            message="missing metadata",
        )
    )

    assert run_pipeline._filter_runnable_states([missing_label_state], 2) == [missing_label_state]
    assert run_pipeline._filter_runnable_states([other_failure_state], 2) == []
