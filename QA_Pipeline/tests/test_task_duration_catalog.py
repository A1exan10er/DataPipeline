from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts.pipeline import phase2_duration, task_catalog
from QA_Pipeline.scripts.pipeline.qa_core import EpisodeState


def _catalog(items: dict[str, dict[str, object]]) -> task_catalog.TaskCatalog:
    return task_catalog.TaskCatalog(
        items=items,
        source="test",
        source_url="test://catalog",
        sha256="catalog-hash",
        fetched_at="2026-07-20T00:00:00+08:00",
    )


def _state(duration: float, task: str, metadata: dict[str, object] | None = None) -> EpisodeState:
    values = {"duration_seconds": duration, **(metadata or {})}
    return EpisodeState(
        episode_path=Path(f"/tmp/{task}_{duration}"),
        task=task,
        date="20260720",
        operator="operator",
        robot="robot",
        controller="controller",
        metadata=values,
    )


def test_training_limit_reviews_then_fails_and_exempts_demo() -> None:
    catalog = _catalog(
        {
            "normal": {"min_duration_seconds": 5.0, "max_duration_seconds": 120.0, "is_demo": False},
            "demo": {"min_duration_seconds": 5.0, "max_duration_seconds": 180.0, "is_demo": True},
        }
    )
    states = [_state(121, "normal"), _state(150, "normal"), _state(999, "demo")]

    results = phase2_duration._check_training_duration_limit(states, catalog)

    assert [(item.check_name, item.status) for _state_item, item in results] == [
        ("duration_training_limit_exceeded", "needs_review"),
        ("duration_training_limit_exceeded", "fail"),
    ]
    assert results[1][1].severity == "major"
    assert results[1][1].details["policy_source"] == "task_catalog_test"
    assert results[1][1].details["catalog_sha256"] == "catalog-hash"


def test_unknown_task_uses_safe_pretraining_default() -> None:
    results = phase2_duration._check_training_duration_limit(
        [_state(151, "not-in-catalog")], _catalog({})
    )

    finding = results[0][1]
    assert finding.status == "fail"
    assert finding.details["max_duration_seconds"] == 120.0
    assert finding.details["catalog_missing"] is True
    assert finding.details["policy_source"] == "unknown_task_default"


def test_metadata_can_mark_unknown_task_as_demo() -> None:
    results = phase2_duration._check_training_duration_limit(
        [_state(999, "not-in-catalog", {"is_demo": True})], _catalog({})
    )

    assert results == []


def test_prepare_catalog_writes_reproducible_snapshot(tmp_path: Path, monkeypatch) -> None:
    payload = {
        "items": {
            "task-a": {
                "min_duration_seconds": 5,
                "max_duration_seconds": 120,
                "is_demo": False,
            }
        },
        "total": 1,
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        task_catalog,
        "config_value",
        lambda _path, _default: {
            "enabled": True,
            "url": "test://catalog",
            "timeout_seconds": 1,
            "fallback_path": "unused.json",
        },
    )

    catalog = task_catalog.prepare_task_catalog(
        tmp_path, opener=lambda _url, timeout: Response()
    )

    snapshot = json.loads((tmp_path / task_catalog.SNAPSHOT_FILENAME).read_text(encoding="utf-8"))
    assert catalog.source == "live"
    assert snapshot["sha256"] == catalog.sha256
    assert snapshot["items"]["task-a"]["max_duration_seconds"] == 120.0


def test_invalid_live_catalog_uses_valid_fallback(tmp_path: Path, monkeypatch) -> None:
    fallback = tmp_path / "fallback.json"
    fallback.write_text(
        json.dumps(
            {
                "items": {
                    "fallback-task": {
                        "min_duration_seconds": 5,
                        "max_duration_seconds": 90,
                        "is_demo": False,
                    }
                },
                "total": 1,
            }
        ),
        encoding="utf-8",
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b"{}"

    monkeypatch.setattr(
        task_catalog,
        "config_value",
        lambda _path, _default: {
            "enabled": True,
            "url": "test://invalid",
            "timeout_seconds": 1,
            "fallback_path": str(fallback),
        },
    )

    catalog = task_catalog.prepare_task_catalog(
        tmp_path, opener=lambda _url, timeout: Response()
    )

    assert catalog.source == "fallback"
    assert "Live task catalog unavailable" in catalog.error
    assert "fallback-task" in catalog.items


def test_catalog_accepts_zero_or_missing_unused_minimum_duration(
    tmp_path: Path, monkeypatch
) -> None:
    payload = {
        "items": {
            "zero-min": {
                "min_duration_seconds": 0,
                "max_duration_seconds": 120,
                "is_demo": False,
            },
            "missing-min": {
                "max_duration_seconds": None,
                "is_demo": True,
            },
        },
        "total": 2,
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        task_catalog,
        "config_value",
        lambda _path, _default: {
            "enabled": True,
            "url": "test://catalog",
            "timeout_seconds": 1,
            "fallback_path": "unused.json",
        },
    )

    catalog = task_catalog.prepare_task_catalog(
        tmp_path, opener=lambda _url, timeout: Response()
    )

    assert catalog.source == "live"
    assert catalog.items["zero-min"]["min_duration_seconds"] == 0.0
    assert "min_duration_seconds" not in catalog.items["missing-min"]
    assert "max_duration_seconds" not in catalog.items["missing-min"]
