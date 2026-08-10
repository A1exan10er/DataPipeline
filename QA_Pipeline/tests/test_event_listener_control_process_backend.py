from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SCRIPT = PROJECT_ROOT / "QA_Pipeline" / "scripts" / "event_listener_control.sh"


def test_process_backend_start_status_duplicate_and_stop(tmp_path: Path) -> None:
    fake_python = tmp_path / "fake-qa-python"
    fake_python.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import signal
            import sys
            import time

            if "serve" not in sys.argv:
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, lambda *_args: raise_exit())

            def wait_forever():
                while True:
                    time.sleep(1)

            def raise_exit():
                raise SystemExit(0)

            wait_forever()
            """
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    output_dir = tmp_path / "event-listener"
    control_dir = output_dir / "control"
    env = os.environ.copy()
    env.update(
        {
            "EVENT_LISTENER_BACKEND": "process",
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
            timeout=15,
            check=False,
        )

    try:
        started = run("start")
        assert started.returncode == 0, started.stdout
        assert "托管方式=process" in started.stdout
        assert (control_dir / "listener.enabled").is_file()
        pid = int((control_dir / "listener.pid").read_text(encoding="utf-8").strip())
        os.kill(pid, 0)

        status = run("status")
        assert status.returncode == 0, status.stdout
        assert f"托管方式=process，进程号={pid}" in status.stdout

        duplicate = run("start")
        assert duplicate.returncode == 0, duplicate.stdout
        assert "已在运行" in duplicate.stdout
        assert int((control_dir / "listener.pid").read_text().strip()) == pid
    finally:
        stopped = run("stop")

    assert stopped.returncode == 0, stopped.stdout
    assert not (control_dir / "listener.enabled").exists()
    assert not (control_dir / "listener.pid").exists()
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"listener process {pid} is still alive after stop")
