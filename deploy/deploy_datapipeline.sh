#!/usr/bin/env bash
set -euo pipefail

TARGET_SHA="${1:-}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/home/xinzhi/DataPipeline-deploy}"
PERSISTENT_OUTPUTS="${PERSISTENT_OUTPUTS:-/home/xinzhi/DataPipeline/outputs}"
CURRENT_LINK="$DEPLOY_ROOT/current"
RELEASES_DIR="$DEPLOY_ROOT/releases"
SERVICE_NAME="${DASHBOARD_SERVICE_NAME:-datapipeline-dashboard.service}"
LEGACY_SERVICE_NAME="${LEGACY_DASHBOARD_SERVICE_NAME:-}"
START_LISTENER="${DEPLOY_START_EVENT_LISTENER:-1}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "部署提交必须是完整的 40 位小写 SHA。"
  exit 2
fi
if [[ "$START_LISTENER" != "0" && "$START_LISTENER" != "1" ]]; then
  echo "DEPLOY_START_EVENT_LISTENER 只能是 0 或 1。"
  exit 2
fi
if [[ ! "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
  echo "dashboard 服务名格式无效。"
  exit 2
fi
if [[ -n "$LEGACY_SERVICE_NAME" && ! "$LEGACY_SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
  echo "旧 dashboard 服务名格式无效。"
  exit 2
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

rollback() {
  code="$?"
  if [[ "$code" == "0" ]]; then
    return
  fi
  echo "部署失败，正在恢复上一版本。"
  if [[ "$SWITCHED" == "1" && -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]]; then
    rollback_link="$DEPLOY_ROOT/.current.rollback.$$"
    ln -s "$PREVIOUS_RELEASE" "$rollback_link"
    mv -Tf "$rollback_link" "$CURRENT_LINK"
    systemctl --user restart "$SERVICE_NAME" || true
    if [[ "$LISTENER_WAS_DESIRED" == "1" ]]; then
      EVENT_LISTENER_BACKEND=process "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" start || true
    fi
  elif [[ "$SWITCHED" == "1" && -L "$CURRENT_LINK" ]]; then
    unlink "$CURRENT_LINK"
  fi
  [[ -d "$TEMP_RELEASE" ]] && rm -rf -- "$TEMP_RELEASE"
  exit "$code"
}
trap rollback EXIT

if [[ ! -d "$RELEASE_DIR" ]]; then
  mkdir -p "$TEMP_RELEASE"
  git archive "$TARGET_SHA" | tar -x -C "$TEMP_RELEASE"
  ln -s "$PERSISTENT_OUTPUTS" "$TEMP_RELEASE/outputs"
  /usr/bin/python3 -m venv "$TEMP_RELEASE/datapipeline-env"
  "$TEMP_RELEASE/datapipeline-env/bin/python" -m pip install --upgrade pip
  "$TEMP_RELEASE/datapipeline-env/bin/python" -m pip install -r "$TEMP_RELEASE/QA_Pipeline/requirements.txt"
  (
    cd "$TEMP_RELEASE"
    bash -n QA_Pipeline/scripts/event_listener_control.sh
    datapipeline-env/bin/python -m pytest -q
  )
  mv "$TEMP_RELEASE" "$RELEASE_DIR"
fi

if [[ -x "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" ]]; then
  "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" stop || true
fi
if [[ -n "$LEGACY_SERVICE_NAME" && "$LEGACY_SERVICE_NAME" != "$SERVICE_NAME" ]]; then
  systemctl --user stop "$LEGACY_SERVICE_NAME"
fi

mkdir -p "$SYSTEMD_USER_DIR"
install -m 0644 "$RELEASE_DIR/deploy/datapipeline-dashboard.service" \
  "$SYSTEMD_USER_DIR/$SERVICE_NAME"

next_link="$DEPLOY_ROOT/.current.next.$$"
ln -s "$RELEASE_DIR" "$next_link"
mv -Tf "$next_link" "$CURRENT_LINK"
SWITCHED=1

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

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
if "running" not in status or "manager" not in status:
    raise SystemExit("连接到的 dashboard 不是待部署的新版本")
PY
  then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != "1" ]]; then
  echo "dashboard 健康检查失败。"
  exit 1
fi

if [[ "$LISTENER_WAS_DESIRED" == "1" ]]; then
  EVENT_LISTENER_BACKEND=process "$CURRENT_LINK/QA_Pipeline/scripts/event_listener_control.sh" start
  "$CURRENT_LINK/datapipeline-env/bin/python" \
    "$CURRENT_LINK/Werkzeuge/check_event_listener_health.py" \
    --api-url "http://127.0.0.1:4131/api/event-listener/status" \
    --queue-name "${EVENT_QUEUE_NAME:-qa_pipeline.listen_episode_verified.v2}" \
    --timeout-seconds 60
fi

trap - EXIT
echo "部署完成：$TARGET_SHA"
