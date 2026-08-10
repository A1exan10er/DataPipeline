from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from QA_Pipeline.scripts import qa_control_dashboard as dashboard
from QA_Pipeline.scripts import run_pipeline


def _config(path: Path, phases: list[int] | None = None, criterion_id: str = "criterion") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "criteria": [
                    {
                        "id": criterion_id,
                        "enabled": True,
                        "phases": phases or [2, 7],
                        "check_names": ["check"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _state(tmp_path: Path, root: Path) -> SimpleNamespace:
    registry = tmp_path / "runs.db"
    dashboard.init_registry(registry)
    return SimpleNamespace(
        verified_root=root,
        qa_python=Path(sys.executable),
        output_root=tmp_path / "outputs",
        registry_db=registry,
        refresh_seconds=5.0,
        max_discovered_runs=0,
        lock=__import__("threading").Lock(),
    )


def _payload(config: Path | str, **overrides) -> dict:
    payload = {
        "mode": "task_folder",
        "phases": "1,2,7",
        "workers": 1,
        "batch_size": 100,
        "generate_operator_severe_report": True,
        "operator_severe_config": str(config),
    }
    payload.update(overrides)
    return payload


def test_start_panel_keeps_server_style_and_new_controls(tmp_path: Path) -> None:
    configured_root = tmp_path / "configured & root"
    configured_root.mkdir()
    html = dashboard.render_index_html(SimpleNamespace(refresh_seconds=5, verified_root=configured_root))
    panel = html.split("<h2>Start Run</h2>", 1)[1].split('<div class="panel">', 1)[0]

    assert "QA Control Dashboard" in html
    assert html.count(":root {") == 1
    assert ".panel {" in html and ".row {" in html and ".two {" in html
    assert panel.count('class="primary"') == 1
    assert panel.count('id="startRunButton"') == 1
    assert 'id="startRunButton" class="primary" type="button" onclick="startRun()">Start</button>' in panel
    assert "Configured data root:" in panel
    assert str(configured_root).replace("&", "&amp;") in panel
    assert f'id="rootPath" value="{str(configured_root).replace("&", "&amp;")}"' in panel
    assert "Use configured data root" in panel
    assert 'id="generateOperatorSevereReport"' in panel
    assert 'id="operatorSevereConfig"' in panel
    assert 'id="continueAfterFail"' in panel
    assert 'id="operatorFilter"' in panel
    assert "Severe report preset" in panel
    assert "resumeRun(" in html
    assert "Starting..." in html and "Started: " in html


def test_root_validation_accepts_exact_and_child(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    child = root / "task"
    child.mkdir(parents=True)
    assert dashboard.normalize_verified_path(str(root), root) == str(root.resolve())
    assert dashboard.normalize_verified_path(str(child), root) == str(child.resolve())


@pytest.mark.parametrize("kind", ["outside", "prefix", "traversal", "missing", "file"])
def test_root_validation_rejects_unsafe_paths(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    if kind == "outside":
        selected = tmp_path / "outside"
        selected.mkdir()
    elif kind == "prefix":
        selected = tmp_path / "samples_backup"
        selected.mkdir()
    elif kind == "traversal":
        selected = root / ".." / "private"
        (tmp_path / "private").mkdir()
    elif kind == "missing":
        selected = root / "missing"
    else:
        selected = root / "file.txt"
        selected.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=str(root.resolve())):
        dashboard.normalize_verified_path(str(selected), root)


def test_root_validation_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match=str(root.resolve())):
        dashboard.normalize_verified_path(str(link), root)


def test_server_alias_is_mapped_before_containment(monkeypatch) -> None:
    original_exists = Path.exists
    original_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: True if str(self).startswith("/mnt/nas/database/verified") else original_exists(self),
    )
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: True if str(self).startswith("/mnt/nas/database/verified") else original_is_dir(self),
    )
    assert dashboard.normalize_verified_path(
        "/database/verified/task_a", Path("/mnt/nas/database/verified")
    ) == "/mnt/nas/database/verified/task_a"


def test_command_builder_adds_severe_flags_once_and_forces_continue(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    config = _config(tmp_path / "config with spaces.json")
    state = _state(tmp_path, root)
    payload = _payload(config, root_path=str(root), continue_after_fail=False)

    request = dashboard.build_pipeline_command(
        state, payload, "run", tmp_path / "out", tmp_path / "out" / "qa.db"
    )

    assert request.argv.count("--generate-operator-severe-report") == 1
    assert request.argv.count("--operator-severe-config") == 1
    assert request.argv.count("--continue-after-fail") == 1
    config_index = request.argv.index("--operator-severe-config") + 1
    assert request.argv[config_index] == str(config.resolve())
    assert request.settings["continue_after_fail"] is True


def test_command_builder_preserves_explicit_python_symlink(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    python_link = tmp_path / "qa env" / "bin" / "python"
    python_link.parent.mkdir(parents=True)
    python_link.symlink_to(Path(sys.executable))
    state = _state(tmp_path, root)
    state.qa_python = python_link

    request = dashboard.build_pipeline_command(
        state,
        {"mode": "task_folder", "root_path": str(root), "phases": "1"},
        "run",
        tmp_path / "out",
        tmp_path / "out" / "qa.db",
    )

    assert request.argv[0] == str(python_link.absolute())


def test_command_builder_report_disabled_and_explicit_continue(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    request = dashboard.build_pipeline_command(
        state,
        {
            "mode": "task_folder",
            "root_path": str(root),
            "phases": "1,2,7",
            "continue_after_fail": True,
        },
        "run",
        tmp_path / "out",
        tmp_path / "out" / "qa.db",
    )
    assert "--generate-operator-severe-report" not in request.argv
    assert "--operator-severe-config" not in request.argv
    assert request.argv.count("--continue-after-fail") == 1


def test_command_builder_defaults_to_indefinite_resource_pause(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)

    request = dashboard.build_pipeline_command(
        state,
        {"mode": "task_folder", "root_path": str(root), "phases": "1,2,7"},
        "run",
        tmp_path / "out",
        tmp_path / "out" / "qa.db",
    )

    def option(name: str) -> str:
        return request.argv[request.argv.index(name) + 1]

    assert option("--overload-action") == "pause"
    assert option("--resource-max-wait-seconds") == "0"
    assert option("--resource-error-retries") == "3"
    assert option("--resource-retry-delay-seconds") == "30"
    assert request.settings["overload_action"] == "pause"
    assert request.settings["resource_max_wait_seconds"] == 0


def test_command_builder_defaults_to_operator_phases(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)

    request = dashboard.build_pipeline_command(
        state,
        {
            "mode": "task_folder",
            "root_path": str(root),
            "continue_after_fail": True,
        },
        "run",
        tmp_path / "out",
        tmp_path / "out" / "qa.db",
    )

    assert request.settings["phases"] == "1,2,7"
    assert request.settings["continue_after_fail"] is True
    assert "--continue-after-fail" in request.argv


def test_command_builder_accepts_bounded_resource_retry_settings(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    payload = {
        "mode": "task_folder",
        "root_path": str(root),
        "phases": "1,2,7",
        "overload_action": "pause",
        "resource_max_wait_seconds": 300,
        "resource_error_retries": 10,
        "resource_retry_delay_seconds": 45,
    }

    request = dashboard.build_pipeline_command(
        state, payload, "run", tmp_path / "out", tmp_path / "out" / "qa.db"
    )

    assert request.argv[request.argv.index("--resource-max-wait-seconds") + 1] == "300"
    assert request.argv[request.argv.index("--resource-error-retries") + 1] == "10"
    assert request.argv[request.argv.index("--resource-retry-delay-seconds") + 1] == "45"


def test_command_builder_adds_exact_operator_filter(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    request = dashboard.build_pipeline_command(
        state,
        {
            "mode": "date_range",
            "date_from": "20260713",
            "date_to": "20260724",
            "operator_filter": "Operator Name",
            "phases": "1,2,7",
        },
        "run",
        tmp_path / "out",
        tmp_path / "out" / "qa.db",
    )

    assert request.argv[request.argv.index("--operator") + 1] == "Operator Name"
    assert request.settings["operator_filter"] == "Operator Name"


def test_resume_command_preserves_scope_and_removes_force_rerun(tmp_path: Path) -> None:
    pipeline = dashboard.PROJECT_ROOT / "QA_Pipeline" / "scripts" / "run_pipeline.py"
    run = {
        "run_id": "date-run",
        "db_path": str(tmp_path / "out" / "qa.db"),
        "output_dir": str(tmp_path / "out"),
        "command": dashboard.shell_command(
            [
                sys.executable,
                str(pipeline),
                "--roots",
                str(tmp_path / "root"),
                "--date-from",
                "20260713",
                "--date-to",
                "20260724",
                "--operator",
                "alice",
                "--db-path",
                "/old/qa.db",
                "--output-dir",
                "/old/out",
                "--run-id",
                "old-run",
                "--force-rerun",
            ]
        ),
    }

    command = dashboard._resume_pipeline_command(run)

    assert "--force-rerun" not in command
    assert command[command.index("--operator") + 1] == "alice"
    assert command[command.index("--date-from") + 1] == "20260713"
    assert command[command.index("--date-to") + 1] == "20260724"
    assert command[command.index("--db-path") + 1] == run["db_path"]
    assert command[command.index("--output-dir") + 1] == run["output_dir"]
    assert command[command.index("--run-id") + 1] == run["run_id"]


def test_api_resume_reuses_database_and_starts_same_session(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    output = state.output_root / "stopped-run"
    output.mkdir(parents=True)
    db_path = output / "qa.db"
    db_path.touch()
    pipeline = dashboard.PROJECT_ROOT / "QA_Pipeline" / "scripts" / "run_pipeline.py"
    command = [
        sys.executable,
        str(pipeline),
        "--roots",
        str(root),
        "--phases",
        "1,2,7",
        "--db-path",
        str(db_path),
        "--output-dir",
        str(output),
        "--run-id",
        "stopped-run",
        "--force-rerun",
    ]
    dashboard.record_run(
        state.registry_db,
        {
            "run_id": "stopped-run",
            "mode": "date_range",
            "output_dir": str(output),
            "db_path": str(db_path),
            "command": dashboard.shell_command(command),
            "tmux_session": "qa_dash_stopped-run",
            "status": "stopped",
            "phases": "1,2,7",
            "workers": 2,
            "batch_size": 500,
            "root_path": str(root),
            "date_from": "20260713",
            "date_to": "20260724",
            "task_filter": "",
            "operator_filter": "alice",
            "started_at": dashboard.now_iso(),
            "finished_at": dashboard.now_iso(),
            "updated_at": dashboard.now_iso(),
            "notes": "",
            "severe_report_requested": 0,
            "severe_report_config": "",
            "continue_after_fail": 1,
        },
    )
    starts: list[tuple[str, str]] = []
    monkeypatch.setattr(dashboard, "tmux_session_running", lambda _session: False)
    monkeypatch.setattr(dashboard, "validate_execution_requirements", lambda *_args: None)
    monkeypatch.setattr(dashboard, "start_tmux", lambda session, cmd: starts.append((session, cmd)))

    result = dashboard.api_resume_run(state, "stopped-run")

    assert result["resumed"] is True
    assert result["db_path"] == str(db_path)
    assert "--force-rerun" not in result["command"]
    assert starts == [("qa_dash_stopped-run", f"bash {output / 'dashboard_command.sh'}")]
    row = dashboard.registered_run(state.registry_db, "stopped-run")
    assert row is not None and row["status"] == "running"


def test_command_builder_rejects_invalid_overload_action(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    with pytest.raises(ValueError, match="overload_action"):
        dashboard.build_pipeline_command(
            state,
            {
                "mode": "task_folder",
                "root_path": str(root),
                "phases": "1",
                "overload_action": "retry-forever",
            },
            "run",
            tmp_path / "out",
            tmp_path / "out" / "qa.db",
        )


def test_command_builder_old_body_and_modes(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    child = root / "task"
    child.mkdir(parents=True)
    state = _state(tmp_path, root)
    task_request = dashboard.build_pipeline_command(
        state,
        {"mode": "task_folder", "root_path": str(child), "phases": "1,2,3"},
        "task-run",
        tmp_path / "task-out",
        tmp_path / "task-out" / "qa.db",
    )
    assert task_request.argv[task_request.argv.index("--roots") + 1] == str(child.resolve())
    assert "--streaming-discovery" not in task_request.argv
    date_request = dashboard.build_pipeline_command(
        state,
        {"mode": "date_range", "date_from": "20260706", "date_to": "20260719", "phases": "1,2,3"},
        "date-run",
        tmp_path / "date-out",
        tmp_path / "date-out" / "qa.db",
    )
    assert date_request.argv[date_request.argv.index("--roots") + 1] == str(root.resolve())
    assert "--streaming-discovery" in date_request.argv


def test_relative_and_absolute_severe_config_paths(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path / "configs" / "report.json")
    monkeypatch.setattr(dashboard, "PROJECT_ROOT", tmp_path)
    relative, relative_phases = dashboard.validate_operator_severe_config("configs/report.json")
    absolute, absolute_phases = dashboard.validate_operator_severe_config(str(config))
    assert relative == config.resolve() == absolute
    assert relative_phases == absolute_phases == {2, 7}


@pytest.mark.parametrize(
    ("builder", "message"),
    [
        (lambda root: root / "missing.json", "does not exist"),
        (lambda root: root, "regular file"),
        (lambda root: (root / "config.txt"), ".json suffix"),
    ],
)
def test_invalid_severe_config_path(tmp_path: Path, builder, message: str) -> None:
    path = builder(tmp_path)
    if path.suffix == ".txt":
        path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        dashboard.validate_operator_severe_config(str(path))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{broken", "Could not load"),
        (json.dumps({}), "criteria"),
        (json.dumps({"criteria": [{"id": "x", "enabled": False}]}), "enabled criterion"),
        (json.dumps({"criteria": [{"id": "x"}, {"id": "x"}]}), "Duplicate enabled"),
    ],
)
def test_invalid_severe_config_content(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        dashboard.validate_operator_severe_config(str(path))


def test_missing_required_phase_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    config = _config(tmp_path / "config.json")
    state = _state(tmp_path, root)
    with pytest.raises(ValueError, match="Missing phases: 7"):
        dashboard.build_pipeline_command(
            state,
            _payload(config, root_path=str(root), phases="1,2"),
            "run",
            tmp_path / "out",
            tmp_path / "out" / "qa.db",
        )


def test_registry_migration_preserves_old_row(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, output_dir TEXT NOT NULL,
                db_path TEXT NOT NULL, command TEXT NOT NULL, tmux_session TEXT,
                status TEXT NOT NULL, phases TEXT, workers INTEGER, batch_size INTEGER,
                root_path TEXT, date_from TEXT, date_to TEXT, task_filter TEXT,
                started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL, notes TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO runs VALUES ('old', 'task_folder', '/out', '/db', 'cmd', '', 'complete', '1', 1, 10, '/root', '', '', '', 'start', 'end', 'update', 'note')"
        )
    dashboard.init_registry(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        row = dict(conn.execute("SELECT * FROM runs WHERE run_id = 'old'").fetchone())
    assert {"severe_report_requested", "severe_report_config", "continue_after_fail"} <= columns
    assert row["command"] == "cmd" and row["notes"] == "note"
    assert row["severe_report_requested"] == 0
    assert row["severe_report_config"] == ""
    assert row["continue_after_fail"] == 0


@pytest.mark.parametrize(
    ("requested", "status", "expected"),
    [(False, "complete", "not_requested"), (True, "running", "pending"), (True, "complete", "missing")],
)
def test_report_loader_absent_states(tmp_path: Path, requested: bool, status: str, expected: str) -> None:
    assert dashboard.load_operator_severe_report(tmp_path, requested, status)["state"] == expected


@pytest.mark.parametrize("content", ["{broken", "[]"])
def test_report_loader_invalid_json(tmp_path: Path, content: str) -> None:
    (tmp_path / "operator_severe_report.json").write_text(content, encoding="utf-8")
    result = dashboard.load_operator_severe_report(tmp_path, True, "complete")
    assert result["state"] == "invalid"
    assert result["error"]


def test_report_loader_valid_empty_and_missing_optional_fields(tmp_path: Path) -> None:
    report_path = tmp_path / "operator_severe_report.json"
    report_path.write_text(
        json.dumps({"summary": {"episode_count": 2}, "operator_ranking": [], "criterion_frequency": []}),
        encoding="utf-8",
    )
    result = dashboard.load_operator_severe_report(tmp_path, True, "complete")
    assert result["state"] == "generated"
    assert result["operator_ranking"] == []
    assert result["criteria"] == []
    assert result["report_json_path"] == str(report_path)


def test_dashboard_html_has_traceback_modal_and_server_pagination(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    html = dashboard.render_index_html(SimpleNamespace(refresh_seconds=5, verified_root=root))

    assert 'id="issueDrilldownModal"' in html
    assert "openIssueDrilldown" in html
    assert "Affected episodes" in html
    assert 'id="eventJobsPageSize"' in html
    assert "event-listener/jobs?page=" in html
    assert "Episodes With Issues" in html
    assert '<details class="compact-details">' in html
    assert "Criterion definitions / 判定标准" in html
    assert "Regenerate report / 重新生成报告" in html
    assert "escJs" not in html


def test_issue_drilldown_recovers_operator_and_groups_episode_findings(
    monkeypatch, tmp_path: Path
) -> None:
    episode_dir = (
        tmp_path
        / "task"
        / "UR5e"
        / "collector"
        / "20260720"
        / "xiaguoshun"
        / "episode_1"
    )
    episode_dir.mkdir(parents=True)
    db_path = tmp_path / "qa.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE episodes (
                episode_path TEXT PRIMARY KEY, date TEXT, task TEXT, robot TEXT,
                controller TEXT, operator TEXT, final_status TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY, episode_path TEXT, phase INTEGER,
                check_name TEXT, severity TEXT, status TEXT, message TEXT, details TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO episodes VALUES (?, '', 'task', 'UR5e', 'controller', '', 'fail')",
            (str(episode_dir),),
        )
        conn.executemany(
            "INSERT INTO findings VALUES (?, ?, 7, 'operator_standstill_middle', 'major', 'fail', ?, ?)",
            [
                (1, str(episode_dir), "first segment", json.dumps({"duration_ms": 31000})),
                (2, str(episode_dir), "summary", "{malformed"),
            ],
        )
    monkeypatch.setattr(
        dashboard,
        "find_run",
        lambda state, run_id: {"run_id": run_id, "db_path": str(db_path)},
    )

    result = dashboard.api_run_issue_drilldown(
        SimpleNamespace(),
        "run-1",
        {"check_name": ["operator_standstill_middle"]},
    )

    assert result["summary"]["finding_count"] == 2
    assert result["summary"]["affected_episode_count"] == 1
    assert result["operators"] == [
        {"operator": "xiaguoshun", "finding_count": 2, "affected_episode_count": 1}
    ]
    assert result["episodes"][0]["operator"] == "xiaguoshun"
    assert len(result["episodes"][0]["findings"]) == 2


def test_event_job_page_uses_database_count_and_offset(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, status TEXT, attempts INTEGER,
                mounted_path TEXT, run_id TEXT, output_dir TEXT, db_path TEXT,
                error TEXT, received_at TEXT, started_at TEXT, finished_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO jobs (id, status, attempts, mounted_path) VALUES (?, 'done', 1, ?)",
            [(index, f"/verified/task/robot/collector/20260720/operator/episode_{index}") for index in range(1, 46)],
        )
    monkeypatch.setattr(dashboard, "EVENT_JOB_DB", db_path)
    monkeypatch.setattr(dashboard, "job_episode_result", lambda job: {})

    result = dashboard.event_listener_jobs_page(2, 20)

    assert result["total"] == 45
    assert result["total_pages"] == 3
    assert result["page"] == 2
    assert [row["id"] for row in result["jobs"]] == list(range(25, 5, -1))


def test_api_start_records_new_settings_without_real_tmux(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    config = _config(tmp_path / "config.json")
    state = _state(tmp_path, root)
    monkeypatch.setattr(dashboard, "validate_execution_requirements", lambda *args: None)
    monkeypatch.setattr(dashboard, "start_tmux", lambda *args: None)
    result = dashboard.api_start_run(
        state,
        _payload(config, root_path=str(root), run_id="api-run", continue_after_fail=False),
    )
    assert result["ok"] is True
    assert result["run_id"] == "api-run"
    assert result["tmux_session"] == "qa_dash_api-run"
    assert result["command"].count("--generate-operator-severe-report") == 1
    row = dashboard.registered_run(state.registry_db, "api-run")
    assert row["severe_report_requested"] == 1
    assert row["continue_after_fail"] == 1
    assert row["severe_report_config"] == str(config.resolve())
    with pytest.raises(ValueError, match="already exists"):
        dashboard.api_start_run(state, _payload(config, root_path=str(root), run_id="api-run"))


def test_api_tmux_failure_is_not_success(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    monkeypatch.setattr(dashboard, "validate_execution_requirements", lambda *args: None)
    monkeypatch.setattr(dashboard, "start_tmux", lambda *args: (_ for _ in ()).throw(RuntimeError("tmux failed")))
    with pytest.raises(RuntimeError, match="tmux failed"):
        dashboard.api_start_run(
            state,
            {"mode": "task_folder", "root_path": str(root), "run_id": "failed-run"},
        )
    assert dashboard.registered_run(state.registry_db, "failed-run")["status"] == "failed"


def test_command_validation_creates_no_output(tmp_path: Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    state = _state(tmp_path, root)
    output = state.output_root / "invalid"
    with pytest.raises(ValueError):
        dashboard.api_start_run(
            state,
            {
                "mode": "task_folder",
                "root_path": str(root),
                "run_id": "invalid",
                "generate_operator_severe_report": True,
                "operator_severe_config": str(tmp_path / "missing.json"),
            },
        )
    assert not output.exists()


def test_pipeline_completion_paths_use_one_shared_report_helper() -> None:
    import inspect

    for function in (run_pipeline.main, run_pipeline._run_batched, run_pipeline._run_streaming_discovery):
        source = inspect.getsource(function)
        assert source.count("_maybe_generate_operator_severe_report(") == 1
        assert source.index("_export_reports(") < source.index("_maybe_generate_operator_severe_report(")


def test_pipeline_report_helper_skips_dry_run_and_surfaces_failure(monkeypatch, tmp_path: Path) -> None:
    calls = []
    args = SimpleNamespace(
        dry_run=True,
        generate_operator_severe_report=True,
        operator_severe_config="config.json",
    )
    monkeypatch.setattr(
        run_pipeline,
        "generate_operator_severe_report",
        lambda *values: calls.append(values) or [],
    )
    assert run_pipeline._maybe_generate_operator_severe_report(args, tmp_path / "qa.db", tmp_path)
    assert calls == []
    args.dry_run = False
    monkeypatch.setattr(
        run_pipeline,
        "generate_operator_severe_report",
        lambda *values: (_ for _ in ()).throw(RuntimeError("report failed")),
    )
    assert not run_pipeline._maybe_generate_operator_severe_report(args, tmp_path / "qa.db", tmp_path)


@pytest.mark.local_data
def test_local_mixed_robot_pipeline_is_read_only_and_report_compatible(tmp_path: Path) -> None:
    raw_root = os.environ.get("QA_LOCAL_SAMPLE_ROOT", "")
    if not raw_root:
        pytest.skip("QA_LOCAL_SAMPLE_ROOT is not set")
    root = Path(raw_root).resolve()
    if not root.is_dir():
        pytest.skip(f"QA_LOCAL_SAMPLE_ROOT is not a directory: {root}")
    before = {
        path.relative_to(root).as_posix(): (path.lstat().st_size, path.lstat().st_mtime_ns)
        for path in root.rglob("*")
    }
    output = tmp_path / "local-data"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "QA_Pipeline" / "scripts" / "run_pipeline.py"),
        "--roots",
        str(root),
        "--db-path",
        str(output / "qa.db"),
        "--output-dir",
        str(output),
        "--phases",
        "1,2,7",
        "--batch-size",
        "100",
        "--batch-mode",
        "auto",
        "--workers",
        "1",
        "--continue-after-fail",
        "--disable-quality-label-filter",
        "--force-rerun",
        "--generate-operator-severe-report",
        "--operator-severe-config",
        str(PROJECT_ROOT / "QA_Pipeline" / "configs" / "operator_severe_report.json"),
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=1800)
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-4000:]
    with sqlite3.connect(output / "qa.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] > 0
        missing_paths = {
            row[0]
            for row in conn.execute(
                "SELECT episode_path FROM findings WHERE phase = 7 AND check_name = 'standstill_motion_source_missing'"
            )
        }
        low_motion_fail_paths = {
            row[0]
            for row in conn.execute(
                "SELECT episode_path FROM findings WHERE phase = 7 AND check_name LIKE 'operator_standstill_%' AND status = 'fail'"
            )
        }
    assert missing_paths.isdisjoint(low_motion_fail_paths)
    for filename in (
        "operator_severe_ranking.csv",
        "operator_severe_evidence.csv",
        "criteria_frequency.csv",
        "operator_severe_report.md",
        "operator_severe_report.json",
    ):
        assert (output / filename).is_file()
    report = json.loads((output / "operator_severe_report.json").read_text(encoding="utf-8"))
    assert isinstance(report.get("operator_ranking"), list)
    after = {
        path.relative_to(root).as_posix(): (path.lstat().st_size, path.lstat().st_mtime_ns)
        for path in root.rglob("*")
    }
    assert before == after
