#!/usr/bin/env bash
set -euo pipefail

PORT="${DASHBOARD_PORT:-4131}"
PROC_ROOT="${PROC_ROOT:-/proc}"

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "Dashboard 端口无效：$PORT" >&2
  exit 2
fi

for command_name in ss systemctl tr; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "缺少识别 Dashboard 服务所需的命令：$command_name" >&2
    exit 1
  fi
done

socket_output="$(ss -H -ltnp "sport = :$PORT" 2>/dev/null || true)"
mapfile -t listener_pids < <(
  printf '%s\n' "$socket_output" \
    | sed -nE 's/.*pid=([0-9]+).*/\1/p' \
    | sort -u
)

if [[ "${#listener_pids[@]}" != "1" ]]; then
  echo "无法唯一识别监听 $PORT 端口的进程；检测到 ${#listener_pids[@]} 个 PID。" >&2
  echo "请维护者执行：ss -ltnp 'sport = :$PORT'" >&2
  exit 1
fi

listener_pid="${listener_pids[0]}"
if [[ ! -r "$PROC_ROOT/$listener_pid/cmdline" ]]; then
  echo "无法读取 4131 监听进程 $listener_pid 的启动参数。" >&2
  exit 1
fi
cmdline="$(tr '\0' ' ' < "$PROC_ROOT/$listener_pid/cmdline")"
if [[ "$cmdline" != *"qa_control_dashboard.py"* ]]; then
  echo "拒绝自动接管：$PORT 端口进程不是 DataPipeline QA Dashboard。" >&2
  echo "PID=$listener_pid，启动参数=$cmdline" >&2
  exit 1
fi

service_name=""
if [[ -r "$PROC_ROOT/$listener_pid/cgroup" ]]; then
  service_name="$(
    sed -nE 's|.*/([^/]+\.service)(/.*)?$|\1|p' "$PROC_ROOT/$listener_pid/cgroup" \
      | head -n 1
  )"
fi

belongs_to_pid_tree() {
  local child="$1" parent="$2" current="$1" next=""
  while [[ "$current" =~ ^[0-9]+$ ]] && [[ "$current" != "0" ]] && [[ "$current" != "1" ]]; do
    [[ "$current" == "$parent" ]] && return 0
    [[ -r "$PROC_ROOT/$current/status" ]] || return 1
    next="$(awk '/^PPid:/ {print $2}' "$PROC_ROOT/$current/status")"
    [[ "$next" != "$current" ]] || return 1
    current="$next"
  done
  return 1
}

if [[ -z "$service_name" ]]; then
  while read -r candidate _rest; do
    [[ "$candidate" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || continue
    main_pid="$(systemctl --user show "$candidate" -p MainPID --value 2>/dev/null || true)"
    if [[ "$main_pid" =~ ^[0-9]+$ ]] && (( main_pid > 0 )) \
      && belongs_to_pid_tree "$listener_pid" "$main_pid"; then
      if [[ -n "$service_name" && "$service_name" != "$candidate" ]]; then
        echo "4131 进程同时匹配多个 user-systemd 服务，拒绝自动接管。" >&2
        exit 1
      fi
      service_name="$candidate"
    fi
  done < <(systemctl --user list-units --type=service --state=running --no-legend --plain)
fi

if [[ ! "$service_name" =~ ^[A-Za-z0-9_.@-]+\.service$ ]]; then
  echo "无法确认 4131 进程属于哪个 user-systemd 服务，拒绝自动接管。" >&2
  exit 1
fi

active_state="$(systemctl --user show "$service_name" -p ActiveState --value)"
main_pid="$(systemctl --user show "$service_name" -p MainPID --value)"
if [[ "$active_state" != "active" ]] || ! belongs_to_pid_tree "$listener_pid" "$main_pid"; then
  echo "服务 $service_name 与 4131 监听进程不一致，拒绝自动接管。" >&2
  exit 1
fi

echo "已确认旧 Dashboard：服务=$service_name，PID=$listener_pid" >&2
echo "启动参数：$cmdline" >&2
printf '%s\n' "$service_name"
