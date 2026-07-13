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
DCS_CONFIG_FILE="${DCS_CONFIG_FILE:-$HOME/DataPipeline/dcp-sdk/dcs_config.json}"
DC_ROOT="${DC_ROOT:-$REPO_ROOT/dcp-sdk}"
MOUNT_PREFIX="${MOUNT_PREFIX:-/mnt/nas/database/verified}"
NAS_PREFIX="${NAS_PREFIX:-/volume1/database/verified,/database/verified}"
QA_PYTHON="${QA_PYTHON:-datapipeline-env/bin/python}"
PHASES="${PHASES:-1,2,3,6,7}"
WORKERS="${WORKERS:-1}"
PHASE6_PROFILE="${PHASE6_PROFILE:-video_only}"
PHASE6_VIDEO_OUTPUT_MODE="${PHASE6_VIDEO_OUTPUT_MODE:-sidecar}"
PHASE6_SIDECAR_FILENAME="${PHASE6_SIDECAR_FILENAME:-video_rotated_180.mp4}"
DISABLE_QUALITY_LABEL_FILTER="${DISABLE_QUALITY_LABEL_FILTER:-1}"
EVENT_BATCH_SIZE="${EVENT_BATCH_SIZE:-16}"
EVENT_QUEUE_NAME="${EVENT_QUEUE_NAME:-qa_pipeline.listen_episode_verified.v2}"
EVENT_DATE="${EVENT_DATE:-}"
EVENT_DATE_FROM="${EVENT_DATE_FROM:-}"
EVENT_DATE_TO="${EVENT_DATE_TO:-}"
STABILITY_INTERVAL="${STABILITY_INTERVAL:-3}"
STABILITY_TIMEOUT="${STABILITY_TIMEOUT:-90}"
MIN_FREE_MEM_GB="${MIN_FREE_MEM_GB:-6}"
MAX_LOAD_RATIO="${MAX_LOAD_RATIO:-0.75}"
RESOURCE_MAX_WAIT_SECONDS="${RESOURCE_MAX_WAIT_SECONDS:-300}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
RETENTION_MAX_RUNS="${RETENTION_MAX_RUNS:-0}"
RETENTION_MAX_GB="${RETENTION_MAX_GB:-0}"
TASK_DB_ENV_FILE="${TASK_DB_ENV_FILE:-$HOME/.qa_task_db_env}"
QA_DCS_NOTIFY_ENABLED="${QA_DCS_NOTIFY_ENABLED:-1}"
QA_DCS_NOTIFY_DRY_RUN="${QA_DCS_NOTIFY_DRY_RUN:-0}"
QA_DCS_NOTIFY_WAIT="${QA_DCS_NOTIFY_WAIT:-1}"
QA_DCS_NOTIFY_EVENT="${QA_DCS_NOTIFY_EVENT:-qa.episode_abnormal.detected}"
QA_DCS_NOTIFY_STATUSES="${QA_DCS_NOTIFY_STATUSES:-fail}"
QA_DCS_NOTIFY_ACTIONABLE_STATUSES="${QA_DCS_NOTIFY_ACTIONABLE_STATUSES:-needs_review}"
QA_DCS_NOTIFY_ACTIONABLE_CHECKS="${QA_DCS_NOTIFY_ACTIONABLE_CHECKS:-}"
QA_DCS_NOTIFY_EXCLUDE_CHECKS="${QA_DCS_NOTIFY_EXCLUDE_CHECKS:-timestamps_raw_inconsistency,quality_label_unexpected}"
for value_name in EVENT_DATE EVENT_DATE_FROM EVENT_DATE_TO; do
  value="${!value_name}"
  if [[ -n "$value" && "$value" != "today" && "$value" != "yesterday" && ! "$value" =~ ^[0-9]{8}$ ]]; then
    echo "$value_name must be YYYYMMDD, today, or yesterday"
    exit 2
  fi
done
if [[ -n "$EVENT_DATE" && ( -n "$EVENT_DATE_FROM" || -n "$EVENT_DATE_TO" ) ]]; then
  echo "EVENT_DATE cannot be combined with EVENT_DATE_FROM/EVENT_DATE_TO"
  exit 2
fi
if [[ "$EVENT_DATE_FROM" =~ ^[0-9]{8}$ && "$EVENT_DATE_TO" =~ ^[0-9]{8}$ && "$EVENT_DATE_FROM" > "$EVENT_DATE_TO" ]]; then
  echo "EVENT_DATE_FROM must be earlier than or equal to EVENT_DATE_TO"
  exit 2
fi
EVENT_DATE_ARGS=""
QUALITY_FILTER_ARGS=""
if [[ -n "$EVENT_DATE" ]]; then
  EVENT_DATE_ARGS="$EVENT_DATE_ARGS --event-date '$EVENT_DATE'"
fi
if [[ -n "$EVENT_DATE_FROM" ]]; then
  EVENT_DATE_ARGS="$EVENT_DATE_ARGS --event-date-from '$EVENT_DATE_FROM'"
fi
if [[ -n "$EVENT_DATE_TO" ]]; then
  EVENT_DATE_ARGS="$EVENT_DATE_ARGS --event-date-to '$EVENT_DATE_TO'"
fi
if [[ "$DISABLE_QUALITY_LABEL_FILTER" == "1" ]]; then
  QUALITY_FILTER_ARGS="--disable-quality-label-filter"
fi

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
  echo "configured phases: $PHASES"
  echo "configured path mapping: nas=$NAS_PREFIX mount=$MOUNT_PREFIX"
  echo "configured UMI Phase 6: profile=$PHASE6_PROFILE output=$PHASE6_VIDEO_OUTPUT_MODE sidecar=$PHASE6_SIDECAR_FILENAME"
  echo "quality-label filter disabled: $DISABLE_QUALITY_LABEL_FILTER"
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
       export QA_DCS_NOTIFY_ENABLED='$QA_DCS_NOTIFY_ENABLED'; \
       export QA_DCS_NOTIFY_DRY_RUN='$QA_DCS_NOTIFY_DRY_RUN'; \
       export QA_DCS_NOTIFY_WAIT='$QA_DCS_NOTIFY_WAIT'; \
       export QA_DCS_NOTIFY_EVENT='$QA_DCS_NOTIFY_EVENT'; \
       export QA_DCS_NOTIFY_STATUSES='$QA_DCS_NOTIFY_STATUSES'; \
       export QA_DCS_NOTIFY_ACTIONABLE_STATUSES='$QA_DCS_NOTIFY_ACTIONABLE_STATUSES'; \
       export QA_DCS_NOTIFY_ACTIONABLE_CHECKS='$QA_DCS_NOTIFY_ACTIONABLE_CHECKS'; \
       export QA_DCS_NOTIFY_EXCLUDE_CHECKS='$QA_DCS_NOTIFY_EXCLUDE_CHECKS'; \
       exec '$QA_PYTHON' Werkzeuge/listen_episode_verified.py serve \
         --job-db '$JOB_DB' \
         --output-dir '$OUTPUT_DIR' \
         --dc-root '$DC_ROOT' \
         --mount-prefix '$MOUNT_PREFIX' \
         --nas-prefix '$NAS_PREFIX' \
         --qa-python '$QA_PYTHON' \
         --queue-name '$EVENT_QUEUE_NAME' \
         $EVENT_DATE_ARGS \
         --phases '$PHASES' \
         --phase6-profile '$PHASE6_PROFILE' \
         --phase6-video-output-mode '$PHASE6_VIDEO_OUTPUT_MODE' \
         --phase6-sidecar-filename '$PHASE6_SIDECAR_FILENAME' \
         $QUALITY_FILTER_ARGS \
         --workers '$WORKERS' \
         --batch-size '$EVENT_BATCH_SIZE' \
         --stability-interval '$STABILITY_INTERVAL' \
         --stability-timeout '$STABILITY_TIMEOUT' \
         --max-load-ratio '$MAX_LOAD_RATIO' \
         --min-free-mem-gb '$MIN_FREE_MEM_GB' \
         --resource-max-wait-seconds '$RESOURCE_MAX_WAIT_SECONDS' \
         --retention-days '$RETENTION_DAYS' \
         --retention-max-runs '$RETENTION_MAX_RUNS' \
         --retention-max-gb '$RETENTION_MAX_GB' \
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
