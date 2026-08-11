#!/usr/bin/env bash
set -euo pipefail

TARGET_SHA="${1:-}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/home/xinzhi/DataPipeline-deploy}"
PERSISTENT_OUTPUTS="${PERSISTENT_OUTPUTS:-/home/xinzhi/DataPipeline/outputs}"
CURRENT_LINK="$DEPLOY_ROOT/current"
RELEASES_DIR="$DEPLOY_ROOT/releases"
DASHBOARD_SERVICE="${DASHBOARD_SERVICE_NAME:-datapipeline-dashboard.service}"
LISTENER_SERVICE="${EVENT_LISTENER_SYSTEMD_SERVICE:-datapipeline-event-listener.service}"
START_LISTENER="${DEPLOY_START_EVENT_LISTENER:-1}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
DEPLOY_CONFIG_FILE="${DEPLOY_CONFIG_FILE:-$DEPLOY_ROOT/legacy-dashboard.env}"
LEGACY_SERVICE="${LEGACY_DASHBOARD_SERVICE_NAME:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$LEGACY_SERVICE" && -f "$DEPLOY_CONFIG_FILE" ]]; then
  LEGACY_SERVICE="$(
    sed -n 's/^LEGACY_DASHBOARD_SERVICE_NAME=//p' "$DEPLOY_CONFIG_FILE" | head -n 1
  )"
fi

valid_service_name() {
  [[ "$1" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]
}

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "部署提交必须是完整的 40 位小写 SHA。"
  exit 2
fi
if [[ "$START_LISTENER" != "0" && "$START_LISTENER" != "1" ]]; then
  echo "DEPLOY_START_EVENT_LISTENER 只能是 0 或 1。"
  exit 2
fi
if ! valid_service_name "$DASHBOARD_SERVICE" || ! valid_service_name "$LISTENER_SERVICE"; then
  echo "DataPipeline systemd 服务名格式无效。"
  exit 2
fi
if [[ -n "$LEGACY_SERVICE" ]] && ! valid_service_name "$LEGACY_SERVICE"; then
  echo "旧 Dashboard 服务名格式无效。"
  exit 2
fi
if [[ "$LEGACY_SERVICE" == "$DASHBOARD_SERVICE" ]]; then
  LEGACY_SERVICE=""
fi
if [[ "$DEPLOY_ROOT" != /* || "$DEPLOY_ROOT" == "/" || "$DEPLOY_ROOT" == "/home" ]]; then
  echo "部署根目录必须是明确的绝对目录，且不能是系统根目录或 /home。"
  exit 2
fi
if [[ ! -d "$PERSISTENT_OUTPUTS" ]]; then
  echo "持久化输出目录不存在：$PERSISTENT_OUTPUTS"
  exit 1
fi

cd "$REPO_ROOT"
git cat-file -e "$TARGET_SHA^{commit}"
if ! git merge-base --is-ancestor "$TARGET_SHA" "origin/master"; then
  echo "拒绝部署：提交不属于 origin/master。"
  exit 1
fi

mkdir -p "$RELEASES_DIR"
RELEASE_DIR="$RELEASES_DIR/$TARGET_SHA"
TEMP_RELEASE="$RELEASES_DIR/.${TARGET_SHA}.tmp.$$"
PREVIOUS_RELEASE=""
SWITCHED=0
DASHBOARD_WAS_ACTIVE=0
LEGACY_WAS_ACTIVE=0
LEGACY_WAS_ENABLED=0
LEGACY_STOPPED=0
LISTENER_WAS_DESIRED=0

if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
fi
if [[ -f "$PERSISTENT_OUTPUTS/event_listener/control/listener.enabled" ]]; then
  LISTENER_WAS_DESIRED=1
fi
if [[ "$START_LISTENER" == "1" ]]; then
  LISTENER_WAS_DESIRED=1
fi
if systemctl --user is-active --quiet "$DASHBOARD_SERVICE" 2>/dev/null; then
  DASHBOARD_WAS_ACTIVE=1
fi
if [[ -n "$LEGACY_SERVICE" ]]; then
  if systemctl --user is-active --quiet "$LEGACY_SERVICE" 2>/dev/null; then
    LEGACY_WAS_ACTIVE=1
  fi
  if systemctl --user is-enabled --quiet "$LEGACY_SERVICE" 2>/dev/null; then
    LEGACY_WAS_ENABLED=1
  fi
fi

restore_listener_if_needed() {
  if [[ "$LISTENER_WAS_DESIRED" == "1" && -L "$CURRENT_LINK" ]]; then
    if grep -q 'BACKEND.*systemd' "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh"; then
      EVENT_LISTENER_BACKEND=systemd \
        EVENT_LISTENER_SYSTEMD_SERVICE="$LISTENER_SERVICE" \
        "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" start || true
    else
      EVENT_LISTENER_BACKEND=process \
        "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" start || true
    fi
  fi
}

rollback() {
  code="$?"
  if [[ "$code" == "0" ]]; then
    return
  fi
  echo "部署失败，正在恢复部署前的服务状态。"
  systemctl --user stop "$LISTENER_SERVICE" >/dev/null 2>&1 || true
  systemctl --user stop "$DASHBOARD_SERVICE" >/dev/null 2>&1 || true

  if [[ "$SWITCHED" == "1" && -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]]; then
    rollback_link="$DEPLOY_ROOT/.current.rollback.$$"
    ln -s "$PREVIOUS_RELEASE" "$rollback_link"
    mv -Tf "$rollback_link" "$CURRENT_LINK"
    systemctl --user daemon-reload || true
    if [[ "$DASHBOARD_WAS_ACTIVE" == "1" ]]; then
      systemctl --user restart "$DASHBOARD_SERVICE" || true
      restore_listener_if_needed
    fi
  elif [[ "$SWITCHED" == "1" && -L "$CURRENT_LINK" ]]; then
    unlink "$CURRENT_LINK"
  fi

  if [[ "$LEGACY_STOPPED" == "1" && -n "$LEGACY_SERVICE" ]]; then
    if [[ "$LEGACY_WAS_ENABLED" == "1" ]]; then
      systemctl --user enable "$LEGACY_SERVICE" >/dev/null 2>&1 || true
    fi
    systemctl --user start "$LEGACY_SERVICE" || true
  fi
  [[ -d "$TEMP_RELEASE" ]] && rm -rf -- "$TEMP_RELEASE"
  exit "$code"
}
trap rollback EXIT

# 在停止任何现有服务之前完成构建、依赖安装和测试。
if [[ ! -d "$RELEASE_DIR" ]]; then
  mkdir -p "$TEMP_RELEASE"
  git archive "$TARGET_SHA" | tar -x -C "$TEMP_RELEASE"
  ln -s "$PERSISTENT_OUTPUTS" "$TEMP_RELEASE/outputs"
  /usr/bin/python3 -m venv "$TEMP_RELEASE/datapipeline-env"
  "$TEMP_RELEASE/datapipeline-env/bin/python" -m pip install --upgrade pip
  "$TEMP_RELEASE/datapipeline-env/bin/python" -m pip install -r "$TEMP_RELEASE/QA_Pipeline/requirements.txt"
  (
    cd "$TEMP_RELEASE"
    bash -n QA_Pipeline/scripts/event_listener_control.sh \
      deploy/deploy_datapipeline.sh \
      deploy/bootstrap_server_runner.sh \
      deploy/discover_legacy_dashboard.sh
    datapipeline-env/bin/python -m pytest -q
  )
  mv "$TEMP_RELEASE" "$RELEASE_DIR"
fi

mkdir -p "$SYSTEMD_USER_DIR"
install -m 0644 "$RELEASE_DIR/deploy/datapipeline-dashboard.service" \
  "$SYSTEMD_USER_DIR/$DASHBOARD_SERVICE"
install -m 0644 "$RELEASE_DIR/deploy/datapipeline-event-listener.service" \
  "$SYSTEMD_USER_DIR/$LISTENER_SERVICE"
systemctl --user daemon-reload

# 停止旧 listener；保留部署前的期望状态，切换成功后再恢复。
if systemctl --user is-active --quiet "$LISTENER_SERVICE" 2>/dev/null; then
  systemctl --user stop "$LISTENER_SERVICE"
fi
if [[ -x "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" ]]; then
  EVENT_LISTENER_BACKEND=process \
    "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" stop || true
fi

if [[ "$DASHBOARD_WAS_ACTIVE" == "1" ]]; then
  systemctl --user stop "$DASHBOARD_SERVICE"
fi
if [[ "$LEGACY_WAS_ACTIVE" == "1" ]]; then
  echo "正在切换已确认的旧 Dashboard 服务：$LEGACY_SERVICE"
  systemctl --user stop "$LEGACY_SERVICE"
  LEGACY_STOPPED=1
fi

if ! /usr/bin/python3 - <<'PY'
from pathlib import Path

listeners = []
for path in Path("/proc").iterdir():
    if not path.name.isdigit():
        continue
    try:
        args = [part for part in (path / "cmdline").read_bytes().split(b"\0") if part]
    except OSError:
        continue
    decoded = [part.decode("utf-8", "replace") for part in args]
    if any("Werkzeuge/listen_episode_verified.py" in arg for arg in decoded) and "serve" in decoded:
        listeners.append((path.name, " ".join(decoded)))
if listeners:
    for pid, command in listeners:
        print(f"仍存在未受本次部署管理的实时检测进程：PID={pid}，命令={command}")
    raise SystemExit(1)
PY
then
  echo "检测到遗留实时检测进程，拒绝启动第二个 RabbitMQ consumer。"
  exit 1
fi

if ss -H -ltn "sport = :4131" | grep -q .; then
  echo "4131 端口在停止已确认的 Dashboard 后仍被占用，拒绝继续部署。"
  exit 1
fi

next_link="$DEPLOY_ROOT/.current.next.$$"
ln -s "$RELEASE_DIR" "$next_link"
mv -Tf "$next_link" "$CURRENT_LINK"
SWITCHED=1

# 使用绝对 unit 路径，避免隔离 runner 的 HOME 影响 systemctl 客户端查找路径。
systemctl --user enable "$SYSTEMD_USER_DIR/$DASHBOARD_SERVICE"
systemctl --user restart "$DASHBOARD_SERVICE"

healthy=0
for _ in {1..30}; do
  if "$CURRENT_LINK/datapipeline-env/bin/python" - <<'PY'
import json
import urllib.request

with urllib.request.urlopen(
    "http://127.0.0.1:4131/api/event-listener/status", timeout=3
) as response:
    payload = json.load(response)
status = payload.get("event_listener", {})
if "running" not in status or "manager" not in status or "systemd_running" not in status:
    raise SystemExit("连接到的 dashboard 不是待部署的新版本")
PY
  then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != "1" ]]; then
  echo "Dashboard 健康检查失败。"
  exit 1
fi

if [[ "$LISTENER_WAS_DESIRED" == "1" ]]; then
  EVENT_LISTENER_BACKEND=systemd \
    EVENT_LISTENER_SYSTEMD_SERVICE="$LISTENER_SERVICE" \
    "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" start
  "$CURRENT_LINK/datapipeline-env/bin/python" \
    "$CURRENT_LINK/Werkzeuge/check_event_listener_health.py" \
    --api-url "http://127.0.0.1:4131/api/event-listener/status" \
    --queue-name "${EVENT_QUEUE_NAME:-qa_pipeline.listen_episode_verified.v2}" \
    --timeout-seconds 60
fi

if [[ "$LEGACY_STOPPED" == "1" && "$LEGACY_WAS_ENABLED" == "1" ]]; then
  systemctl --user disable "$LEGACY_SERVICE"
fi

trap - EXIT
echo "部署完成：$TARGET_SHA"
