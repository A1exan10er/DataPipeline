from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts import run_pipeline
from QA_Pipeline.scripts.pipeline.task_catalog import TaskCatalog


def _write_eef(path: Path, speed_cm_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp_ms", "x", "y", "z"])
        writer.writeheader()
        for index in range(401):
            seconds = index / 10.0
            writer.writerow(
                {
                    "timestamp_ms": index * 100,
                    "x": speed_cm_s / 100.0 * seconds,
                    "y": 0,
                    "z": 0,
                }
            )


def _episode(
    root: Path,
    index: int,
    operator: str,
    duration_seconds: float,
    observation_speed: float,
    action_speed: float,
) -> Path:
    episode = root / "test_task" / "20260710" / operator / f"episode_{index:04d}"
    episode.mkdir(parents=True)
    metadata = {
        "task_key": "test_task",
        "robot": "test_robot",
        "controller": "test_controller",
        "duration_seconds": duration_seconds,
        "total_frames": 401,
        "fps_actual": 10,
        "fps_config": 10,
        "quality": {"labels": ["完全正常"]},
        "modalities": {
            "observation.state.eef_pose": {"type": "csv", "rows": 401, "hz_nominal": 10},
            "actions.eef_pose": {"type": "csv", "rows": 401, "hz_nominal": 10},
        },
    }
    (episode / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    _write_eef(episode / "observation.state.eef_pose" / "data.csv", observation_speed)
    _write_eef(episode / "actions.eef_pose" / "data.csv", action_speed)
    return episode


def test_actual_pipeline_detects_deduplicates_ranks_and_excludes_equipment(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "fixture"
    _episode(source, 1, "test_operator_a", 40, 2.0, 2.0)
    _episode(source, 2, "test_operator_a", 160, 10.0, 10.0)
    _episode(source, 3, "test_operator_b", 40, 0.0, 0.0)
    _episode(source, 4, "test_equipment_case", 40, 2.0, 10.0)
    _episode(source, 5, "test_baseline_operator", 40, 10.0, 10.0)
    _episode(source, 6, "test_baseline_operator", 40, 10.0, 10.0)
    output = tmp_path / "output"
    db_path = output / "qa.db"

    def fake_catalog(output_dir: Path) -> TaskCatalog:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "task_time_ranges_snapshot.json").write_text("{}\n", encoding="utf-8")
        return TaskCatalog(
            items={
                "test_task": {
                    "min_duration_seconds": 5.0,
                    "max_duration_seconds": 120.0,
                    "is_demo": False,
                }
            },
            source="test",
            source_url="test://catalog",
            sha256="test-hash",
            fetched_at="2026-07-20T00:00:00+08:00",
        )

    monkeypatch.setattr(run_pipeline.task_catalog, "prepare_task_catalog", fake_catalog)

    exit_code = run_pipeline.main(
        [
            "--roots",
            str(source),
            "--db-path",
            str(db_path),
            "--output-dir",
            str(output),
            "--phases",
            "1,2,7",
            "--workers",
            "1",
            "--continue-after-fail",
            "--disable-quality-label-filter",
            "--disable-live-monitor",
            "--generate-operator-severe-report",
            "--operator-severe-config",
            str(QA_PIPELINE_ROOT / "configs" / "operator_severe_report.json"),
        ]
    )

    assert exit_code == 0
    with sqlite3.connect(db_path) as conn:
        findings = conn.execute(
            "SELECT check_name, episode_path FROM findings WHERE status = 'fail'"
        ).fetchall()
    names_by_path = {(name, Path(path).parent.name) for name, path in findings}
    assert ("operator_sustained_slow_motion", "test_operator_a") in names_by_path
    assert ("duration_training_limit_exceeded", "test_operator_a") in names_by_path
    assert ("duration_extreme_long", "test_operator_a") in names_by_path
    assert ("operator_standstill_motion_too_short", "test_operator_b") in names_by_path
    assert ("equipment_motion_response_slow", "test_equipment_case") in names_by_path

    report = json.loads((output / "operator_severe_report.json").read_text(encoding="utf-8"))
    ranking = report["operator_ranking"]
    assert [row["operator"] for row in ranking] == ["test_operator_a", "test_operator_b"]
    assert ranking[0]["severe_episode_count"] == 2
    assert ranking[0]["criterion_hit_count"] == 2
    assert ranking[1]["severe_episode_count"] == 1
    assert {row["criterion_id"] for row in report["evidence"]} == {
        "extreme_long_duration",
        "low_motion",
        "slow_motion",
    }
    duration_evidence = next(
        row for row in report["evidence"]
        if row["operator"] == "test_operator_a"
        and row["criterion_id"] == "extreme_long_duration"
    )
    standstill_evidence = next(
        row for row in report["evidence"]
        if row["operator"] == "test_operator_b"
        and row["criterion_id"] == "low_motion"
    )
    assert duration_evidence["source_finding_count"] == 2
    assert standstill_evidence["source_finding_count"] >= 2
    assert not any(row["operator"] == "test_equipment_case" for row in report["evidence"])
