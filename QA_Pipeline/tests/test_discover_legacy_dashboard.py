from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_SCRIPT = PROJECT_ROOT / "deploy" / "discover_legacy_dashboard.sh"


def test_runner_registration_file_is_portable_across_host_and_sandbox() -> None:
    config = (PROJECT_ROOT / "deploy" / "gitea-runner-prod-config.yaml").read_text(
        encoding="utf-8"
    )
    assert "file: .runner" in config
    assert "file: /runner/.runner" not in config


def test_runner_exposes_real_user_systemd_config_inside_sandbox() -> None:
    launcher = (PROJECT_ROOT / "deploy" / "run-gitea-runner-prod.sh").read_text(
        encoding="utf-8"
    )
    deploy_script = (PROJECT_ROOT / "deploy" / "deploy_datapipeline.sh").read_text(
        encoding="utf-8"
    )
    workflow = (PROJECT_ROOT / ".gitea" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    dashboard_unit = (PROJECT_ROOT / "deploy" / "datapipeline-dashboard.service").read_text(
        encoding="utf-8"
    )
    assert "--setenv XDG_CONFIG_HOME /home/xinzhi/.config" in launcher
    assert 'systemctl --user enable "$SYSTEMD_USER_DIR/$DASHBOARD_SERVICE"' in deploy_script
    assert "XDG_CONFIG_HOME: /home/xinzhi/.config" in workflow
    assert "Environment=DASHBOARD_RUN_BACKEND=systemd" in dashboard_unit
    assert 'launcher.get("configured_backend") != "systemd"' in deploy_script
    assert 'not launcher.get("systemd_available")' in deploy_script


def test_production_dcs_config_is_checked_outside_runner_sandbox() -> None:
    deploy_script = (PROJECT_ROOT / "deploy" / "deploy_datapipeline.sh").read_text(
        encoding="utf-8"
    )
    workflow = (PROJECT_ROOT / ".gitea" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    control_script = (
        PROJECT_ROOT / "QA_Pipeline" / "scripts" / "event_listener_control.sh"
    ).read_text(encoding="utf-8")
    assert "DCS_CONFIG_FILE: /home/xinzhi/DataPipeline/dcp-sdk/dcs_config.json" in workflow
    assert 'PHASES: "1,2,3,7"' in workflow
    assert 'systemd-run --user --quiet --wait --pipe --collect' in deploy_script
    assert 'Werkzeuge/check_dcs_config.py' in deploy_script
    assert '--setenv "DCS_CONFIG_FILE=$DCS_CONFIG_FILE"' in deploy_script
    assert '--phases "$LISTENER_PHASES"' in deploy_script
    assert 'DCS_CONFIG_FILE="$EXPLICIT_DCS_CONFIG_FILE"' in control_script


def test_discovers_dashboard_user_service_safely(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "4242"
    process_dir.mkdir(parents=True)
    (process_dir / "cmdline").write_bytes(
        b"python3\x00QA_Pipeline/scripts/qa_control_dashboard.py\x00--port\x004131\x00"
    )
    (process_dir / "cgroup").write_text(
        "0::/user.slice/user-1001.slice/user@1001.service/app.slice/qualitycheck.service\n",
        encoding="utf-8",
    )
    (process_dir / "status").write_text("Name:\tpython3\nPPid:\t1\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ss").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' 'LISTEN 0 5 0.0.0.0:4131 0.0.0.0:* users:((\"python3\",pid=4242,fd=3))'\n",
        encoding="utf-8",
    )
    (bin_dir / "systemctl").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$*" in
              "--user show qualitycheck.service -p ActiveState --value") echo active ;;
              "--user show qualitycheck.service -p MainPID --value") echo 4242 ;;
              *) exit 2 ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "ss").chmod(0o755)
    (bin_dir / "systemctl").chmod(0o755)

    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "PROC_ROOT": str(proc_root)})
    completed = subprocess.run(
        [str(DISCOVERY_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "qualitycheck.service"
    assert "已确认旧 Dashboard" in completed.stderr


def test_rejects_non_dashboard_process(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "5151"
    process_dir.mkdir(parents=True)
    (process_dir / "cmdline").write_bytes(b"python3\0unrelated_server.py\0")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ss").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' 'LISTEN 0 5 0.0.0.0:4131 0.0.0.0:* users:((\"python3\",pid=5151,fd=3))'\n",
        encoding="utf-8",
    )
    (bin_dir / "ss").chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "PROC_ROOT": str(proc_root)})
    completed = subprocess.run(
        [str(DISCOVERY_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert completed.returncode != 0
    assert "不是 DataPipeline QA Dashboard" in completed.stderr
