from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from QA_Pipeline.scripts import qa_control_dashboard as dashboard


def test_dashboard_renders_realtime_umi_controls() -> None:
    html = dashboard.render_index_html(SimpleNamespace(refresh_seconds=5))

    assert 'id="eventPhases" value="1,2,3,6,7"' in html
    assert 'id="eventMountPrefix" value="/mnt/nas/database/verified"' in html
    assert 'id="eventNasPrefix" value="/volume1/database/verified,/database/verified"' in html
    assert 'id="eventPhase6Profile"' in html
    assert 'id="eventPhase6OutputMode"' in html
    assert 'id="eventPhase6SidecarFilename" value="video_rotated_180.mp4"' in html
    assert 'id="eventDisableQualityFilter"' in html


def test_dashboard_passes_phase6_settings_to_listener_launcher(monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="started")

    monkeypatch.setattr(dashboard.subprocess, "run", fake_run)
    monkeypatch.setattr(dashboard, "event_listener_status", lambda: {"tmux_running": True})

    result = dashboard.api_event_listener_action(
        "restart",
        {
            "phases": "1,2,3,6,7",
            "mount_prefix": "/srv/verified",
            "nas_prefix": "/volume1/database/verified,/database/verified",
            "phase6_profile": "video_only",
            "phase6_video_output_mode": "sidecar",
            "phase6_sidecar_filename": "video_rotated_180.mp4",
            "disable_quality_label_filter": 1,
            "resource_max_wait_seconds": 240,
        },
    )

    assert result["ok"]
    assert captured["env"]["PHASES"] == "1,2,3,6,7"
    assert captured["env"]["MOUNT_PREFIX"] == "/srv/verified"
    assert captured["env"]["NAS_PREFIX"] == "/volume1/database/verified,/database/verified"
    assert captured["env"]["PHASE6_PROFILE"] == "video_only"
    assert captured["env"]["PHASE6_VIDEO_OUTPUT_MODE"] == "sidecar"
    assert captured["env"]["PHASE6_SIDECAR_FILENAME"] == "video_rotated_180.mp4"
    assert captured["env"]["DISABLE_QUALITY_LABEL_FILTER"] == "1"
    assert captured["env"]["RESOURCE_MAX_WAIT_SECONDS"] == "240"


def test_dashboard_rejects_unsafe_sidecar_filename() -> None:
    with pytest.raises(ValueError, match="plain .mp4 filename"):
        dashboard.api_event_listener_action(
            "restart",
            {
                "phases": "1,2,3,6,7",
                "phase6_profile": "video_only",
                "phase6_video_output_mode": "sidecar",
                "phase6_sidecar_filename": "bad'name.mp4",
            },
        )


def test_dashboard_rejects_relative_server_mount() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        dashboard.api_event_listener_action(
            "restart",
            {
                "phases": "1,2,3,6,7",
                "mount_prefix": "relative/verified",
                "nas_prefix": "/database/verified",
                "phase6_profile": "video_only",
                "phase6_video_output_mode": "sidecar",
                "phase6_sidecar_filename": "video_rotated_180.mp4",
            },
        )


def test_event_listener_status_reports_process_backend(monkeypatch, tmp_path: Path) -> None:
    enabled_file = tmp_path / "listener.enabled"
    error_file = tmp_path / "listener-last-error.txt"
    enabled_file.write_text("enabled\n", encoding="utf-8")
    error_file.write_text("上一次启动失败", encoding="utf-8")

    monkeypatch.setattr(dashboard, "EVENT_LISTENER_ENABLED_FILE", enabled_file)
    monkeypatch.setattr(dashboard, "EVENT_LISTENER_ERROR_FILE", error_file)
    monkeypatch.setattr(dashboard, "EVENT_JOB_DB", tmp_path / "missing.db")
    monkeypatch.setattr(
        dashboard,
        "event_listener_process_info",
        lambda: (4321, ["python", "Werkzeuge/listen_episode_verified.py", "serve"]),
    )
    monkeypatch.setattr(dashboard, "tmux_session_running", lambda _session: False)
    monkeypatch.setattr(dashboard, "event_listener_systemd_running", lambda: False)
    monkeypatch.setattr(dashboard, "directory_size", lambda *_args, **_kwargs: "0 B")
    monkeypatch.setattr(dashboard, "event_listener_settings", lambda: {"workers": "1"})

    status = dashboard.event_listener_status()

    assert status["running"] is True
    assert status["manager"] == "process"
    assert status["pid"] == 4321
    assert status["desired_running"] is True
    assert status["tmux_running"] is False
    assert status["last_start_error"] == "上一次启动失败"


def test_event_listener_status_keeps_tmux_compatibility(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dashboard, "EVENT_LISTENER_ENABLED_FILE", tmp_path / "disabled")
    monkeypatch.setattr(dashboard, "EVENT_LISTENER_ERROR_FILE", tmp_path / "no-error")
    monkeypatch.setattr(dashboard, "EVENT_JOB_DB", tmp_path / "missing.db")
    monkeypatch.setattr(dashboard, "event_listener_process_info", lambda: (None, []))
    monkeypatch.setattr(dashboard, "tmux_session_running", lambda _session: True)
    monkeypatch.setattr(dashboard, "event_listener_systemd_running", lambda: False)
    monkeypatch.setattr(dashboard, "directory_size", lambda *_args, **_kwargs: "0 B")
    monkeypatch.setattr(dashboard, "event_listener_settings", lambda: {})

    status = dashboard.event_listener_status()

    assert status["running"] is True
    assert status["manager"] == "tmux"
    assert status["tmux_running"] is True
