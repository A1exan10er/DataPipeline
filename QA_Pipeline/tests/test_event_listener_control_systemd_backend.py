from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SCRIPT = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "event_listener_control.sh"


def test_listener_default_phases_do_not_require_unavailable_video_tools() -> None:
    script = CONTROL_SCRIPT.read_text(encoding="utf-8")
    assert 'PHASES="${PHASES:-1,2,3,7}"' in script


def test_systemd_explicit_dcs_config_overrides_saved_runner_path(tmp_path: Path) -> None:
    captured = tmp_path / "captured-config"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        '#!/usr/bin/env bash\nprintf "%s" "$DCS_CONFIG_FILE" > "$CAPTURED_CONFIG"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    settings = tmp_path / "listener-settings.env"
    settings.write_text(
        "DCS_CONFIG_FILE=/runner/home/DataPipeline/dcp-sdk/dcs_config.json\n",
        encoding="utf-8",
    )
    production_config = "/home/xinzhi/DataPipeline/dcp-sdk/dcs_config.json"
    env = os.environ.copy()
    env.update(
        {
            "CAPTURED_CONFIG": str(captured),
            "DCS_CONFIG_FILE": production_config,
            "EVENT_LISTENER_SETTINGS_FILE": str(settings),
            "EVENT_LISTENER_USE_SAVED_SETTINGS": "1",
            "QA_PYTHON": str(fake_python),
            "TASK_DB_ENV_FILE": str(tmp_path / "missing-task-db-env"),
        }
    )
    completed = subprocess.run(
        [str(CONTROL_SCRIPT), "serve"],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    assert captured.read_text(encoding="utf-8") == production_config


def test_systemd_backend_start_status_and_stop(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "systemd-state"
    state_dir.mkdir()
    fake_systemctl = bin_dir / "systemctl"
    fake_systemctl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -eu
            state="${FAKE_SYSTEMD_STATE:?}"
            args="$*"
            case "$args" in
              "--user is-active --quiet datapipeline-event-listener.service")
                test -f "$state/active"
                ;;
              "--user enable datapipeline-event-listener.service")
                touch "$state/enabled"
                ;;
              "--user start datapipeline-event-listener.service")
                touch "$state/active"
                ;;
              "--user stop datapipeline-event-listener.service")
                rm -f "$state/active"
                ;;
              "--user disable datapipeline-event-listener.service")
                rm -f "$state/enabled"
                ;;
              *)
                echo "unexpected systemctl call: $args" >&2
                exit 2
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    output_dir = tmp_path / "event-listener"
    control_dir = output_dir / "control"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_SYSTEMD_STATE": str(state_dir),
            "EVENT_LISTENER_BACKEND": "systemd",
            "EVENT_LISTENER_OUTPUT_DIR": str(output_dir),
            "EVENT_LISTENER_JOB_DB": str(output_dir / "jobs.db"),
            "EVENT_LISTENER_LOG_FILE": str(output_dir / "listener.log"),
            "EVENT_LISTENER_CONTROL_DIR": str(control_dir),
            "EVENT_LISTENER_PID_FILE": str(control_dir / "listener.pid"),
            "EVENT_LISTENER_ENABLED_FILE": str(control_dir / "listener.enabled"),
            "EVENT_LISTENER_SETTINGS_FILE": str(control_dir / "listener-settings.env"),
            "QA_PYTHON": str(fake_python),
            "TASK_DB_ENV_FILE": str(tmp_path / "missing-task-db-env"),
        }
    )

    def run(action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CONTROL_SCRIPT), action],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )

    started = run("start")
    assert started.returncode == 0, started.stdout
    assert "托管方式=systemd" in started.stdout
    assert (state_dir / "active").is_file()
    assert (state_dir / "enabled").is_file()
    assert (control_dir / "listener.enabled").is_file()

    status = run("status")
    assert status.returncode == 0, status.stdout
    assert "托管方式=systemd" in status.stdout

    stopped = run("stop")
    assert stopped.returncode == 0, stopped.stdout
    assert not (state_dir / "active").exists()
    assert not (state_dir / "enabled").exists()
    assert not (control_dir / "listener.enabled").exists()
