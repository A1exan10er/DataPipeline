#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ACTION="${1:-start}"
SESSION="${EVENT_LISTENER_SESSION:-qa_event_listener}"
JOB_DB="${EVENT_LISTENER_JOB_DB:-outputs/event_listener/jobs.db}"
OUTPUT_DIR="${EVENT_LISTENER_OUTPUT_DIR:-outputs/event_listener}"
LOG_FILE="${EVENT_LISTENER_LOG_FILE:-$OUTPUT_DIR/listener.log}"
DCS_CONFIG_FILE="${DCS_CONFIG_FILE:-$HOME/DataPipeline/dcp-sdk/dcs_config.json.example}"
DC_ROOT="${DC_ROOT:-$REPO_ROOT/dcp-sdk}"
MOUNT_PREFIX="${MOUNT_PREFIX:-/mnt/nas/database/verified}"
QA_PYTHON="${QA_PYTHON:-datapipeline-env/bin/python}"
WORKERS="${WORKERS:-1}"
EVENT_BATCH_SIZE="${EVENT_BATCH_SIZE:-16}"
STABILITY_INTERVAL="${STABILITY_INTERVAL:-3}"
STABILITY_TIMEOUT="${STABILITY_TIMEOUT:-90}"
MIN_FREE_MEM_GB="${MIN_FREE_MEM_GB:-6}"
MAX_LOAD_RATIO="${MAX_LOAD_RATIO:-0.75}"
RESOURCE_MAX_WAIT_SECONDS="${RESOURCE_MAX_WAIT_SECONDS:-300}"
TASK_DB_ENV_FILE="${TASK_DB_ENV_FILE:-$HOME/.qa_task_db_env}"

require_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required."
    exit 1
  fi
}

status() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session: running ($SESSION)"
  else
    echo "tmux session: not running ($SESSION)"
  fi
  if [[ -f "$JOB_DB" ]]; then
    "$QA_PYTHON" Werkzeuge/listen_episode_verified.py status --job-db "$JOB_DB" --limit 10
  else
    echo "job db: not found ($JOB_DB)"
  fi
  if [[ -d "$OUTPUT_DIR" ]]; then
    echo
    echo "output size:"
    du -sh "$OUTPUT_DIR" 2>/dev/null || true
  fi
}

case "$ACTION" in
  start)
    require_tmux
    mkdir -p "$OUTPUT_DIR"
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Event listener already running: $SESSION"
      echo
      status
      exit 0
    fi
    "$QA_PYTHON" Werkzeuge/listen_episode_verified.py recover-running --job-db "$JOB_DB" || true
    tmux new-session -d -s "$SESSION" \
      "cd '$REPO_ROOT' && \
       set -a && [ -f '$TASK_DB_ENV_FILE' ] && . '$TASK_DB_ENV_FILE'; set +a; \
       export DCS_CONFIG_FILE='$DCS_CONFIG_FILE'; \
       exec '$QA_PYTHON' Werkzeuge/listen_episode_verified.py serve \
         --job-db '$JOB_DB' \
         --output-dir '$OUTPUT_DIR' \
         --dc-root '$DC_ROOT' \
         --mount-prefix '$MOUNT_PREFIX' \
         --qa-python '$QA_PYTHON' \
         --workers '$WORKERS' \
         --batch-size '$EVENT_BATCH_SIZE' \
         --stability-interval '$STABILITY_INTERVAL' \
         --stability-timeout '$STABILITY_TIMEOUT' \
         --max-load-ratio '$MAX_LOAD_RATIO' \
         --min-free-mem-gb '$MIN_FREE_MEM_GB' \
         --resource-max-wait-seconds '$RESOURCE_MAX_WAIT_SECONDS' \
         --recover-running \
         >> '$LOG_FILE' 2>&1"
    echo "Started event listener: $SESSION"
    echo "Log: $LOG_FILE"
    ;;
  stop)
    require_tmux
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      tmux kill-session -t "$SESSION"
      echo "Stopped event listener: $SESSION"
    else
      echo "Event listener is not running: $SESSION"
    fi
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status
    ;;
  log)
    mkdir -p "$OUTPUT_DIR"
    touch "$LOG_FILE"
    tail -f "$LOG_FILE"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|log}"
    exit 2
    ;;
esac
