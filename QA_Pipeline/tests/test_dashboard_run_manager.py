from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from QA_Pipeline.scripts import qa_control_dashboard as dashboard


def test_auto_backend_uses_process_when_user_systemd_is_unavailable(monkeypatch) -> None:
    state = SimpleNamespace(run_backend="auto")
    monkeypatch.setattr(dashboard, "systemd_user_available", lambda: False)

    assert dashboard.effective_run_backend(state) == "process"
    assert dashboard.run_launcher_status(state) == {
        "configured_backend": "auto",
        "effective_backend": "process",
        "systemd_available": False,
    }


def test_explicit_systemd_backend_does_not_silently_fallback(monkeypatch) -> None:
    state = SimpleNamespace(run_backend="systemd")
    monkeypatch.setattr(dashboard, "systemd_user_available", lambda: False)

    with pytest.raises(RuntimeError, match="user-systemd is required"):
        dashboard.effective_run_backend(state)


def test_systemd_launcher_creates_unique_transient_units(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)
    command_path = tmp_path / "dashboard_command.sh"

    first = dashboard.launch_systemd_run("selected-range", command_path)
    second = dashboard.launch_systemd_run("selected-range", command_path)

    assert first.manager == second.manager == "systemd"
    assert first.manager_id != second.manager_id
    assert first.manager_id.startswith("datapipeline-qa-run-selected-range-")
    assert first.manager_id.endswith(".service")
    assert calls[0][:5] == ["systemd-run", "--user", "--quiet", "--collect", "--service-type=exec"]
    assert "--property=TimeoutStopSec=30s" in calls[0]
    assert calls[0][-2:] == ["bash", str(command_path)]


def test_systemd_launcher_reports_start_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        dashboard.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="unit rejected"),
    )

    with pytest.raises(RuntimeError, match="unit rejected"):
        dashboard.launch_systemd_run("failed", tmp_path / "dashboard_command.sh")


def test_systemd_manager_stops_only_valid_active_run_unit(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(dashboard, "systemd_run_unit_running", lambda _unit: True)

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)
    run = {
        "manager": "systemd",
        "manager_id": "datapipeline-qa-run-selected-range-123.service",
    }

    assert dashboard.stop_run_manager(run) is True
    assert calls == [
        [
            "systemctl",
            "--user",
            "stop",
            "datapipeline-qa-run-selected-range-123.service",
        ]
    ]
    assert dashboard.stop_run_manager(
        {"manager": "systemd", "manager_id": "unrelated.service"}
    ) is False


def test_process_launcher_tracks_identity_and_stops_process_group(tmp_path: Path) -> None:
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    command_path = output_dir / "dashboard_command.sh"
    command_path.write_text("#!/usr/bin/env bash\nsleep 30\n", encoding="utf-8")
    command_path.chmod(0o755)

    handle = dashboard.launch_process_run(command_path)
    run = {
        "manager": handle.manager,
        "manager_pid": handle.manager_pid,
        "manager_start_ticks": handle.manager_start_ticks,
        "output_dir": str(output_dir),
    }
    try:
        assert dashboard.managed_run_process_pid(run) == handle.manager_pid
        assert dashboard.run_manager_running(run) is True
        assert dashboard.stop_run_manager(run) is True
        assert dashboard.run_manager_running(run) is False
    finally:
        pid = int(handle.manager_pid or 0)
        if pid > 1:
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass


def test_process_identity_rejects_reused_pid(monkeypatch, tmp_path: Path) -> None:
    run = {
        "manager": "process",
        "manager_pid": 4242,
        "manager_start_ticks": "100",
        "output_dir": str(tmp_path),
    }
    monkeypatch.setattr(dashboard, "process_start_ticks", lambda _pid: "101")
    monkeypatch.setattr(
        dashboard,
        "process_cmdline",
        lambda _pid: ["bash", str(tmp_path / "dashboard_command.sh")],
    )

    assert dashboard.managed_run_process_pid(run) is None
    assert dashboard.stop_run_manager(run) is False


def test_execution_validation_has_no_tmux_requirement(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "verified"
    root.mkdir()
    qa_python = Path(sys.executable)
    state = SimpleNamespace(qa_python=qa_python, verified_root=root)
    monkeypatch.setattr(dashboard.shutil, "which", lambda _name: None)

    dashboard.validate_execution_requirements(state, root, tmp_path / "outputs" / "run")


def test_legacy_tmux_row_remains_compatible(monkeypatch) -> None:
    run = {"manager": "", "tmux_session": "qa_dash_legacy"}
    monkeypatch.setattr(dashboard, "tmux_session_running", lambda session: session == "qa_dash_legacy")

    assert dashboard.run_manager_name(run) == "tmux"
    assert dashboard.run_manager_running(run) is True


@pytest.mark.parametrize(("exit_code", "expected"), [(0, "complete"), (7, "failed")])
def test_sync_uses_exit_status_after_manager_finishes(
    tmp_path: Path,
    monkeypatch,
    exit_code: int,
    expected: str,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "dashboard_exit_status.txt").write_text(f"{exit_code}\n", encoding="utf-8")
    registry = tmp_path / "runs.db"
    dashboard.init_registry(registry)
    state = SimpleNamespace(registry_db=registry, lock=threading.Lock())
    dashboard.record_run(
        registry,
        {
            "run_id": "finished",
            "mode": "task_folder",
            "output_dir": str(output_dir),
            "db_path": str(output_dir / "qa.db"),
            "command": f"{sys.executable} run_pipeline.py",
            "status": "running",
            "phases": "1",
            "workers": 1,
            "batch_size": 1,
            "root_path": str(tmp_path),
            "date_from": "",
            "date_to": "",
            "task_filter": "",
            "operator_filter": "",
            "started_at": dashboard.now_iso(),
            "finished_at": "",
            "updated_at": dashboard.now_iso(),
            "notes": "",
            "severe_report_requested": 0,
            "severe_report_config": "",
            "continue_after_fail": 0,
            "manager": "systemd",
            "manager_id": "datapipeline-qa-run-finished.service",
        },
    )
    monkeypatch.setattr(dashboard, "run_manager_running", lambda _run: False)
    monkeypatch.setattr(dashboard, "latest_run_status", lambda _output: {})

    dashboard.sync_registered_run_statuses(state)

    assert dashboard.registered_run(registry, "finished")["status"] == expected


def test_parse_args_accepts_process_backend() -> None:
    args = dashboard.parse_args(["--run-backend", "process"])
    assert args.run_backend == "process"
