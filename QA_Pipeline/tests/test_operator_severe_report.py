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

from QA_Pipeline.scripts.generate_operator_severe_report import (
    generate_operator_severe_report,
    load_report_config,
)


DEFAULT_CONFIG = QA_PIPELINE_ROOT / "configs" / "operator_severe_report.json"


def _create_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE episodes (
                episode_path TEXT PRIMARY KEY,
                task TEXT,
                date TEXT,
                operator TEXT,
                robot TEXT,
                controller TEXT,
                final_status TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_path TEXT,
                phase INTEGER,
                check_name TEXT,
                severity TEXT,
                status TEXT,
                message TEXT,
                details TEXT
            )
            """
        )


def _episode(conn: sqlite3.Connection, path: str, operator: str, task: str = "task") -> None:
    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, '20260710', ?, 'robot', 'controller', 'fail')",
        (path, task, operator),
    )


def _finding(
    conn: sqlite3.Connection,
    path: str,
    phase: int,
    check_name: str,
    status: str = "fail",
    details: str | None = "{}",
    message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO findings (
            episode_path, phase, check_name, severity, status, message, details
        ) VALUES (?, ?, ?, 'major', ?, ?, ?)
        """,
        (path, phase, check_name, status, message or check_name, details),
    )


def test_historical_blank_operator_is_recovered_from_metadata(tmp_path: Path) -> None:
    episode_dir = tmp_path / "task" / "robot" / "collector" / "20260720" / "xiaguoshun" / "episode_1"
    episode_dir.mkdir(parents=True)
    (episode_dir / "metadata.json").write_text(
        json.dumps({"username": "xiaguoshun"}),
        encoding="utf-8",
    )
    db_path = tmp_path / "qa.db"
    _create_db(db_path)
    with sqlite3.connect(db_path) as conn:
        _episode(conn, str(episode_dir), "")
        _finding(conn, str(episode_dir), 7, "operator_sustained_slow_motion")

    output_dir = tmp_path / "report"
    generate_operator_severe_report(db_path, output_dir, DEFAULT_CONFIG)
    report = json.loads(
        (output_dir / "operator_severe_report.json").read_text(encoding="utf-8")
    )

    assert report["summary"]["unknown_operator_episode_count"] == 0
    assert report["summary"]["named_operator_count"] == 1
    assert report["operator_ranking"][0]["operator"] == "xiaguoshun"
    assert report["operator_ranking"][0]["severe_episode_count"] == 1
    frequency = {row["criterion_id"]: row for row in report["criterion_frequency"]}
    assert frequency["slow_motion"]["affected_named_operator_count"] == 1
    assert frequency["slow_motion"]["affected_unknown_operator_episode_count"] == 0


def _fixture(path: Path) -> None:
    _create_db(path)
    with sqlite3.connect(path) as conn:
        for episode_path in ("/a1", "/a2", "/a3"):
            _episode(conn, episode_path, "Alice")
        _finding(conn, "/a1", 7, "operator_standstill_leading")
        _finding(conn, "/a1", 7, "operator_standstill_excessive")
        _finding(conn, "/a2", 2, "duration_extreme_long", details='{"ratio_to_median": 5}')
        _finding(conn, "/a3", 7, "operator_standstill_middle", status="needs_review")
        _finding(conn, "/a3", 5, "standstill_segment")
        _finding(conn, "/a3", 2, "duration_absolute_too_long")

        for episode_path in ("/b1", "/b2"):
            _episode(conn, episode_path, "Bob")
        _finding(conn, "/b1", 7, "operator_standstill_leading", details=None)
        _finding(conn, "/b1", 7, "operator_standstill_middle", details="{broken")
        _finding(conn, "/b1", 7, "operator_standstill_trailing")
        _finding(conn, "/b1", 7, "operator_standstill_excessive")

        _episode(conn, "/c1", "Carol", task="other")
        _finding(conn, "/c1", 7, "operator_standstill_motion_too_short")
        _finding(conn, "/c1", 2, "duration_extreme_long")

        _episode(conn, "/u1", "unknown")
        _finding(conn, "/u1", 7, "operator_standstill_excessive")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        return list(csv.DictReader(file_obj))


def test_report_deduplicates_ranks_and_counts_rates(tmp_path: Path) -> None:
    db_path = tmp_path / "qa.db"
    output_dir = tmp_path / "report"
    _fixture(db_path)

    paths = generate_operator_severe_report(db_path, output_dir, DEFAULT_CONFIG)

    assert [path.name for path in paths] == [
        "operator_severe_ranking.csv",
        "operator_severe_evidence.csv",
        "criteria_frequency.csv",
        "operator_severe_report.md",
        "operator_severe_report.json",
    ]
    ranking = _read_csv(output_dir / "operator_severe_ranking.csv")
    assert [row["operator"] for row in ranking] == ["Alice", "Carol", "Bob"]
    assert ranking[0]["severe_episode_count"] == "2"
    assert ranking[0]["criterion_hit_count"] == "2"
    assert ranking[0]["severe_episode_rate_percent"] == "66.67"
    assert ranking[2]["source_finding_count"] == "4"
    assert ranking[2]["severe_episode_count"] == "1"

    evidence = _read_csv(output_dir / "operator_severe_evidence.csv")
    alice_low = next(
        row for row in evidence if row["operator"] == "Alice" and row["criterion_id"] == "low_motion"
    )
    bob_low = next(
        row for row in evidence if row["operator"] == "Bob" and row["criterion_id"] == "low_motion"
    )
    carol = [row for row in evidence if row["operator"] == "Carol"]
    assert alice_low["source_finding_count"] == "2"
    assert bob_low["source_finding_count"] == "4"
    assert len(carol) == 2
    assert len({row["episode_path"] for row in carol}) == 1
    assert not any("needs_review" in row["source_statuses"] for row in evidence)
    assert not any("duration_absolute_too_long" in row["source_check_names"] for row in evidence)
    assert not any("standstill_segment" in row["source_check_names"] for row in evidence)

    parsed_details = json.loads(bob_low["details_json"])
    assert None in parsed_details
    assert any(item.get("raw") == "{broken" for item in parsed_details if isinstance(item, dict))

    report = json.loads((output_dir / "operator_severe_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["unknown_operator_episode_count"] == 1
    assert report["summary"]["named_operator_count"] == 3
    assert report["criterion_frequency"][0]["criterion_id"] == "low_motion"
    assert report["criterion_frequency"][0]["affected_episode_count"] == 4
    assert "does not itself make an employment decision" in report["disclaimer"]


def test_report_output_ordering_is_deterministic(tmp_path: Path) -> None:
    db_path = tmp_path / "qa.db"
    _fixture(db_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    generate_operator_severe_report(db_path, first, DEFAULT_CONFIG)
    generate_operator_severe_report(db_path, second, DEFAULT_CONFIG)

    for filename in (
        "operator_severe_ranking.csv",
        "operator_severe_evidence.csv",
        "criteria_frequency.csv",
    ):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_report_includes_operator_slow_motion_and_excludes_equipment(tmp_path: Path) -> None:
    db_path = tmp_path / "qa.db"
    output_dir = tmp_path / "report"
    _create_db(db_path)
    with sqlite3.connect(db_path) as conn:
        _episode(conn, "/slow", "Alice")
        _episode(conn, "/equipment", "Bob")
        _finding(conn, "/slow", 7, "operator_sustained_slow_motion")
        _finding(conn, "/equipment", 7, "equipment_motion_response_slow")
        _finding(conn, "/equipment", 7, "equipment_motion_response_stalled")

    generate_operator_severe_report(db_path, output_dir, DEFAULT_CONFIG)

    ranking = _read_csv(output_dir / "operator_severe_ranking.csv")
    evidence = _read_csv(output_dir / "operator_severe_evidence.csv")
    assert [row["operator"] for row in ranking] == ["Alice"]
    assert ranking[0]["slow_motion_episode_count"] == "1"
    assert [row["criterion_id"] for row in evidence] == ["slow_motion"]


def test_invalid_criterion_configuration_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "criteria": [
                    {"id": "duplicate", "enabled": True},
                    {"id": "duplicate", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        load_report_config(config_path)
    except RuntimeError as exc:
        assert "Duplicate enabled criterion id" in str(exc)
    else:
        raise AssertionError("duplicate criterion id was accepted")
