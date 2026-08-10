#!/usr/bin/env bash
set -euo pipefail

RUNNER_ROOT="${DATAPIPELINE_RUNNER_ROOT:-/home/xinzhi/.local/share/gitea-runner-datapipeline-prod}"
DEPLOY_ROOT="${DATAPIPELINE_DEPLOY_ROOT:-/home/xinzhi/DataPipeline-deploy}"
PERSISTENT_ROOT="${DATAPIPELINE_PERSISTENT_ROOT:-/home/xinzhi/DataPipeline}"
SYSTEMD_USER_DIR="${DATAPIPELINE_SYSTEMD_USER_DIR:-/home/xinzhi/.config/systemd/user}"
USER_ID="$(id -u)"
RUNTIME_DIR="/run/user/$USER_ID"

args=(
  --die-with-parent
  --new-session
  --unshare-all
  --share-net
  --ro-bind /usr /usr
  --ro-bind /bin /bin
  --ro-bind /lib /lib
  --ro-bind /lib64 /lib64
  --ro-bind /etc /etc
  --dir /run
  --dir /run/systemd
  --proc /proc
  --dev /dev
  --tmpfs /tmp
  --dir /runner
  --bind "$RUNNER_ROOT" /runner
  --dir /home
  --dir /home/xinzhi
  --dir /home/xinzhi/DataPipeline
  --dir /home/xinzhi/DataPipeline-deploy
  --bind "$DEPLOY_ROOT" "$DEPLOY_ROOT"
  --bind "$PERSISTENT_ROOT/outputs" "$PERSISTENT_ROOT/outputs"
  --dir /home/xinzhi/.config
  --dir /home/xinzhi/.config/systemd
  --bind "$SYSTEMD_USER_DIR" "$SYSTEMD_USER_DIR"
  --dir /runner/home
  --chdir /runner
  --setenv HOME /runner/home
  --setenv PATH /usr/local/bin:/usr/bin:/bin
)

if [[ -f /run/systemd/resolve/resolv.conf ]]; then
  args+=(--ro-bind /run/systemd/resolve /run/systemd/resolve)
fi

if [[ -S "$RUNTIME_DIR/bus" ]]; then
  args+=(
    --dir /run/user
    --dir "$RUNTIME_DIR"
    --ro-bind "$RUNTIME_DIR/bus" "$RUNTIME_DIR/bus"
    --setenv XDG_RUNTIME_DIR "$RUNTIME_DIR"
    --setenv DBUS_SESSION_BUS_ADDRESS "unix:path=$RUNTIME_DIR/bus"
  )
fi

exec /usr/bin/bwrap "${args[@]}" \
  /runner/gitea-runner daemon --config /runner/config.yaml
