from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = PROJECT_ROOT / "QA_Pipeline"
for path in (PROJECT_ROOT, QA_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.pipeline import phase1_metadata, task_device_reference
from scripts.pipeline.qa_core import EpisodeState


def _state(robot: str = "ARX5") -> EpisodeState:
    return EpisodeState(
        episode_path=Path(
            "/mnt/nas/database/verified/insert_pcb/20260831/operator/"
            "episode_0001_20260831-120000_ARX5_none"
        ),
        task="insert_pcb",
        date="20260831",
        operator="operator",
        robot=robot,
        controller="none",
        metadata={"task_key": "insert_pcb", "robot": robot},
    )


def _reference(categories: list[str]) -> dict:
    return {
        "category": categories[0] if len(categories) == 1 else "",
        "categories": categories,
        "defined": bool(categories),
        "matched_task_key": "insert_pcb",
        "source": "postgres:test/tasks",
        "raw_compatible_device_types": ["双臂", "umi"],
        "reference_available": True,
        "reference_error": "",
    }


def test_all_compatible_device_types_are_preserved() -> None:
    mapping = task_device_reference._rows_to_mapping(
        [
            {
                "task_key": "insert_pcb",
                "compatible_device_types": '["双臂", "umi", "双臂"]',
            }
        ],
        "test",
    )

    assert mapping["insert_pcb"]["categories"] == ["dual_arm", "umi"]
    assert mapping["insert_pcb"]["category"] == ""
    assert task_device_reference.categories_compatible(
        mapping["insert_pcb"]["categories"], "dual_arm"
    )
    assert task_device_reference.categories_compatible(
        mapping["insert_pcb"]["categories"], "umi"
    )
    assert not task_device_reference.categories_compatible(
        mapping["insert_pcb"]["categories"], "single_arm"
    )


def test_multi_category_reference_is_returned_without_legacy_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = task_device_reference._rows_to_mapping(
        [{"task_key": "insert_pcb", "compatible_device_types": ["双臂", "umi"]}],
        "postgres:test/tasks",
    )
    snapshot = task_device_reference.TaskReferenceSnapshot(
        mapping, "postgres:test/tasks", True
    )
    monkeypatch.setattr(task_device_reference, "_task_reference_snapshot", lambda: snapshot)

    result = task_device_reference.task_device_category(["insert_pcb"])

    assert result["categories"] == ["dual_arm", "umi"]
    assert result["category"] == ""
    assert result["defined"] is True


@pytest.mark.parametrize(
    ("robot", "robot_category"),
    (("ARX5", "dual_arm"), ("UMI", "umi")),
)
def test_phase1_accepts_any_appointed_robot_category(
    monkeypatch: pytest.MonkeyPatch,
    robot: str,
    robot_category: str,
) -> None:
    monkeypatch.setattr(
        phase1_metadata, "task_device_category", lambda _names: _reference(["dual_arm", "umi"])
    )
    monkeypatch.setattr(phase1_metadata, "robot_device_category", lambda _robot: robot_category)
    state = _state(robot)

    findings = phase1_metadata._check_task_context_consistency(state)

    assert "task_robot_mismatch" not in {finding.check_name for finding in findings}
    assert state.metrics["p1_task_device_categories"] == ["dual_arm", "umi"]
    assert state.metrics["p1_task_device_category"] == ""


def test_phase1_rejects_robot_outside_every_appointed_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        phase1_metadata, "task_device_category", lambda _names: _reference(["dual_arm", "umi"])
    )
    monkeypatch.setattr(phase1_metadata, "robot_device_category", lambda _robot: "single_arm")

    findings = phase1_metadata._check_task_context_consistency(_state("UR5"))

    mismatch = next(finding for finding in findings if finding.check_name == "task_robot_mismatch")
    assert mismatch.status == "fail"
    assert mismatch.details["expected_task_device_categories"] == ["dual_arm", "umi"]
    assert "expected_task_device_category" not in mismatch.details


def test_unavailable_reference_requires_review_instead_of_silent_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        phase1_metadata,
        "task_device_category",
        lambda _names: {
            "category": "",
            "categories": [],
            "defined": False,
            "matched_task_key": "",
            "source": "postgres:test/tasks",
            "raw_compatible_device_types": None,
            "reference_available": False,
            "reference_error": "connection refused",
        },
    )
    monkeypatch.setattr(phase1_metadata, "robot_device_category", lambda _robot: "dual_arm")

    findings = phase1_metadata._check_task_context_consistency(_state())

    unavailable = next(
        finding
        for finding in findings
        if finding.check_name == "task_device_reference_unavailable"
    )
    assert unavailable.status == "needs_review"
    assert unavailable.details["reference_error"] == "connection refused"
    assert "task_device_reference_missing" not in {
        finding.check_name for finding in findings
    }


def test_task_absent_from_available_catalog_remains_informational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        phase1_metadata,
        "task_device_category",
        lambda _names: {
            "category": "",
            "categories": [],
            "defined": False,
            "matched_task_key": "",
            "source": "postgres:test/tasks",
            "raw_compatible_device_types": None,
            "reference_available": True,
            "reference_error": "",
        },
    )
    monkeypatch.setattr(phase1_metadata, "robot_device_category", lambda _robot: "dual_arm")

    findings = phase1_metadata._check_task_context_consistency(_state())

    missing = next(
        finding
        for finding in findings
        if finding.check_name == "task_device_reference_missing"
    )
    assert missing.status == "pass"
    assert missing.severity == "info"


def test_database_query_failure_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "QA_TASK_DB_HOST": "db.internal",
        "QA_TASK_DB_NAME": "collector",
        "QA_TASK_DB_USER": "qa",
        "QA_TASK_DB_PASSWORD": "secret",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        task_device_reference,
        "_query_postgres",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("connection refused password=secret")
        ),
    )

    snapshot = task_device_reference._load_reference_database()

    assert snapshot.available is False
    assert snapshot.mapping == {}
    assert snapshot.source == "postgres:db.internal/collector.public.tasks"
    assert "connection refused" in snapshot.error
    assert "secret" not in snapshot.error
    assert "<redacted>" in snapshot.error
