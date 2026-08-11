#!/usr/bin/env bash
set -euo pipefail

RUNNER_BINARY="${1:-}"
CHECKSUM_FILE="${2:-}"
OUTPUT_ARCHIVE="${3:-datapipeline-server-runner-bootstrap.tar.gz}"
RUNNER_VERSION="${RUNNER_VERSION:-3.1.0}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$RUNNER_BINARY" || ! -f "$CHECKSUM_FILE" ]]; then
  echo "用法：$0 RUNNER_BINARY CHECKSUM_FILE [OUTPUT_ARCHIVE]" >&2
  exit 2
fi
if [[ "$OUTPUT_ARCHIVE" != *.tar.gz ]]; then
  echo "输出文件必须使用 .tar.gz 后缀。" >&2
  exit 2
fi

expected="$(awk -v name="gitea-runner-${RUNNER_VERSION}-linux-amd64" '$2 == name {print $1}' "$CHECKSUM_FILE")"
actual="$(sha256sum "$RUNNER_BINARY" | awk '{print $1}')"
if [[ -z "$expected" || "$actual" != "$expected" ]]; then
  echo "runner SHA-256 校验失败。" >&2
  exit 1
fi

staging="$(mktemp -d)"
trap 'rm -rf -- "$staging"' EXIT
mkdir -p "$staging/deploy" "$staging/Werkzeuge"

for path in \
  deploy/bootstrap_server_runner.sh \
  deploy/build_server_runner_bundle.sh \
  deploy/discover_legacy_dashboard.sh \
  deploy/run-gitea-runner-prod.sh \
  deploy/gitea-runner-prod-config.yaml \
  deploy/gitea-runner-datapipeline-prod.service \
  deploy/README_SERVER_RUNNER_ZH.md \
  deploy/datapipeline-dashboard.service \
  deploy/datapipeline-event-listener.service \
  deploy/deploy_datapipeline.sh \
  Werkzeuge/check_event_listener_health.py; do
  install -m 0644 "$REPO_ROOT/$path" "$staging/$path"
done
chmod 0755 \
  "$staging/deploy/bootstrap_server_runner.sh" \
  "$staging/deploy/build_server_runner_bundle.sh" \
  "$staging/deploy/discover_legacy_dashboard.sh" \
  "$staging/deploy/run-gitea-runner-prod.sh" \
  "$staging/deploy/deploy_datapipeline.sh" \
  "$staging/Werkzeuge/check_event_listener_health.py"
install -m 0755 "$RUNNER_BINARY" "$staging/gitea-runner-${RUNNER_VERSION}-linux-amd64"
install -m 0644 "$CHECKSUM_FILE" "$staging/gitea-runner-checksums.txt"

tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  -czf "$OUTPUT_ARCHIVE" -C "$staging" .
output_directory="$(cd "$(dirname "$OUTPUT_ARCHIVE")" && pwd)"
output_name="$(basename "$OUTPUT_ARCHIVE")"
(
  cd "$output_directory"
  sha256sum "$output_name"
) > "$OUTPUT_ARCHIVE.sha256"
echo "已生成非敏感 runner 引导包：$OUTPUT_ARCHIVE"
