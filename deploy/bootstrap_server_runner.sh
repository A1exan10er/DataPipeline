#!/usr/bin/env bash
set -euo pipefail

INSTANCE_URL="http://192.168.50.209:3000"
RUNNER_VERSION="3.1.0"
RUNNER_ROOT="/home/xinzhi/.local/share/gitea-runner-datapipeline-prod"
DEPLOY_ROOT="/home/xinzhi/DataPipeline-deploy"
PERSISTENT_ROOT="/home/xinzhi/DataPipeline"
SERVICE_DIR="/home/xinzhi/.config/systemd/user"
TOKEN_FILE=""
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "用法：$0 --token-file /安全路径/runner-token [--instance URL]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token-file)
      TOKEN_FILE="${2:-}"
      shift 2
      ;;
    --instance)
      INSTANCE_URL="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$(id -un)" != "xinzhi" ]]; then
  echo "必须以 xinzhi 用户执行该脚本。"
  exit 1
fi
if [[ -z "$TOKEN_FILE" || ! -f "$TOKEN_FILE" ]]; then
  echo "缺少 runner 注册令牌文件。"
  usage
  exit 2
fi
if [[ "$(stat -c '%a' "$TOKEN_FILE")" != "600" ]]; then
  echo "注册令牌文件权限必须为 600。"
  exit 1
fi
for command_name in bwrap curl git install python3 systemctl tar; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "服务器缺少必要命令：$command_name。未进行安装，请联系维护者。"
    exit 1
  fi
done
if [[ ! -d "$PERSISTENT_ROOT/outputs" ]]; then
  echo "持久化输出目录不存在：$PERSISTENT_ROOT/outputs"
  exit 1
fi
if ! systemctl --user is-system-running >/dev/null 2>&1; then
  echo "xinzhi 的 user-systemd 不可用；请维护者启用 linger 后重试。"
  exit 1
fi

mkdir -p "$RUNNER_ROOT/home" "$RUNNER_ROOT/work" "$DEPLOY_ROOT/releases" "$SERVICE_DIR"
chmod 0700 "$RUNNER_ROOT" "$RUNNER_ROOT/home" "$RUNNER_ROOT/work"

runner_url="https://gitea.com/gitea/runner/releases/download/v${RUNNER_VERSION}/gitea-runner-${RUNNER_VERSION}-linux-amd64"
checksum_url="https://gitea.com/gitea/runner/releases/download/v${RUNNER_VERSION}/checksums.txt"
temporary_binary="$RUNNER_ROOT/.gitea-runner.tmp.$$"
temporary_checksums="$RUNNER_ROOT/.checksums.tmp.$$"
trap 'rm -f -- "$temporary_binary" "$temporary_checksums"' EXIT
curl --fail --location --show-error --output "$temporary_binary" "$runner_url"
curl --fail --location --show-error --output "$temporary_checksums" "$checksum_url"
expected="$(awk -v name="gitea-runner-${RUNNER_VERSION}-linux-amd64" '$2 == name {print $1}' "$temporary_checksums")"
actual="$(sha256sum "$temporary_binary" | awk '{print $1}')"
if [[ -z "$expected" || "$actual" != "$expected" ]]; then
  echo "runner SHA-256 校验失败。"
  exit 1
fi

install -m 0755 "$temporary_binary" "$RUNNER_ROOT/gitea-runner"
install -m 0600 "$REPO_ROOT/deploy/gitea-runner-prod-config.yaml" "$RUNNER_ROOT/config.yaml"
install -m 0755 "$REPO_ROOT/deploy/run-gitea-runner-prod.sh" "$RUNNER_ROOT/run-runner.sh"
install -m 0644 "$REPO_ROOT/deploy/gitea-runner-datapipeline-prod.service" \
  "$SERVICE_DIR/gitea-runner-datapipeline-prod.service"

if [[ ! -f "$RUNNER_ROOT/.runner" ]]; then
  (
    cd "$RUNNER_ROOT"
    ./gitea-runner register \
      --config "$RUNNER_ROOT/config.yaml" \
      --no-interactive \
      --instance "$INSTANCE_URL" \
      --token-file "$TOKEN_FILE" \
      --name "datapipeline-prod-server" \
      --labels "datapipeline-prod:host"
  )
fi

systemctl --user daemon-reload
systemctl --user enable --now gitea-runner-datapipeline-prod.service
systemctl --user --no-pager --full status gitea-runner-datapipeline-prod.service
echo "生产 runner 已启动。确认 Gitea 显示 online 后，应立即删除令牌文件。"
