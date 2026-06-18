#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ROOTS="${ROOTS:-/mnt/nas/database/verified/assemble_the_remotecontraller_put_into_drawer_umi}"
MAX_EPISODES="${MAX_EPISODES:-20}"
PHASES="${PHASES:-1,2,3,6}"
DATE_FILTER="${DATE_FILTER:-}"
TASK_FILTER="${TASK_FILTER:-}"
RUN_ID="${RUN_ID:-umi-ik-small-batch-$(date '+%Y%m%d-%H%M%S')}"
OUTPUT_DIR="${OUTPUT_DIR:-outputs/umi_ik_small_batch}"
DB_PATH="${DB_PATH:-$OUTPUT_DIR/qa.db}"
CONFIG="${QA_PIPELINE_CONFIG:-QA_Pipeline/configs/quality_rules_umi_ik_test.json}"
WORKERS="${WORKERS:-1}"
MAX_LOAD_RATIO="${MAX_LOAD_RATIO:-0.60}"
MIN_FREE_MEM_GB="${MIN_FREE_MEM_GB:-8}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-5}"
RESOURCE_LOG_DIR="${RESOURCE_LOG_DIR:-outputs/resource_logs/umi_ik_small_batch}"
PIPELINE_SESSION="${PIPELINE_SESSION:-qa_umi_ik_small}"
MONITOR_SESSION="${MONITOR_SESSION:-qa_resource_log_small}"
FORCE_RERUN="${FORCE_RERUN:-0}"
BATCH_SIZE="${BATCH_SIZE:-}"
BATCH_MODE="${BATCH_MODE:-auto}"
STREAMING_DISCOVERY="${STREAMING_DISCOVERY:-0}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required for this launcher. Install tmux or run the pipeline command manually."
  exit 1
fi

if [[ ! -d datapipeline-env ]]; then
  echo "Missing datapipeline-env under $REPO_ROOT"
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG"
  exit 1
fi

mkdir -p "$OUTPUT_DIR" "$RESOURCE_LOG_DIR"

if tmux has-session -t "$PIPELINE_SESSION" 2>/dev/null; then
  echo "Pipeline tmux session already exists: $PIPELINE_SESSION"
  echo "Attach with: tmux attach -t $PIPELINE_SESSION"
  exit 1
fi

if ! tmux has-session -t "$MONITOR_SESSION" 2>/dev/null; then
  tmux new-session -d -s "$MONITOR_SESSION" \
    "cd '$REPO_ROOT' && export DB_PATH='$DB_PATH' RUN_ID='$RUN_ID' MAX_LOAD_RATIO='$MAX_LOAD_RATIO' MIN_FREE_MEM_GB='$MIN_FREE_MEM_GB' && exec bash QA_Pipeline/scripts/resource_monitor.sh '$RESOURCE_LOG_DIR' '$MONITOR_INTERVAL'"
fi

run_script="$OUTPUT_DIR/${RUN_ID}_pipeline_command.sh"
pipeline_log="$OUTPUT_DIR/${RUN_ID}_pipeline.log"
{
  echo "#!/usr/bin/env bash"
  echo "set -euo pipefail"
  printf "cd %q\n" "$REPO_ROOT"
  printf "mkdir -p %q\n" "$OUTPUT_DIR"
  printf "exec > >(tee -a %q) 2>&1\n" "$pipeline_log"
  echo "echo \"Pipeline started at \$(date '+%F %T')\""
  echo "source datapipeline-env/bin/activate"
  printf "export QA_PIPELINE_CONFIG=%q\n" "$CONFIG"
  echo "python3 QA_Pipeline/scripts/run_pipeline.py \\"
  printf "  --roots %q \\\\\n" "$ROOTS"
  printf "  --phases %q \\\\\n" "$PHASES"
  if [[ -n "$DATE_FILTER" ]]; then
    printf "  --date %q \\\\\n" "$DATE_FILTER"
  fi
  if [[ -n "$TASK_FILTER" ]]; then
    printf "  --task %q \\\\\n" "$TASK_FILTER"
  fi
  if [[ "$MAX_EPISODES" != "0" && "$MAX_EPISODES" != "none" ]]; then
    printf "  --max-episodes %q \\\\\n" "$MAX_EPISODES"
  fi
  if [[ "$FORCE_RERUN" == "1" || "$FORCE_RERUN" == "true" ]]; then
    echo "  --force-rerun \\"
  fi
  if [[ "$STREAMING_DISCOVERY" == "1" || "$STREAMING_DISCOVERY" == "true" ]]; then
    echo "  --streaming-discovery \\"
  fi
  if [[ -n "$BATCH_SIZE" && "$BATCH_SIZE" != "0" && "$BATCH_SIZE" != "none" ]]; then
    printf "  --batch-size %q \\\\\n" "$BATCH_SIZE"
    printf "  --batch-mode %q \\\\\n" "$BATCH_MODE"
  fi
  printf "  --db-path %q \\\\\n" "$DB_PATH"
  printf "  --output-dir %q \\\\\n" "$OUTPUT_DIR"
  printf "  --run-id %q \\\\\n" "$RUN_ID"
  printf "  --workers %q \\\\\n" "$WORKERS"
  printf "  --max-load-ratio %q \\\\\n" "$MAX_LOAD_RATIO"
  printf "  --min-free-mem-gb %q\n" "$MIN_FREE_MEM_GB"
} > "$run_script"
chmod +x "$run_script"

tmux new-session -d -s "$PIPELINE_SESSION" "$REPO_ROOT/$run_script"

echo "Started resource monitor session: $MONITOR_SESSION"
echo "Started pipeline session: $PIPELINE_SESSION"
echo
echo "Attach to pipeline:"
echo "  tmux attach -t $PIPELINE_SESSION"
echo
echo "Attach to resource monitor:"
echo "  tmux attach -t $MONITOR_SESSION"
echo
echo "Watch resource log:"
echo "  tail -f $RESOURCE_LOG_DIR/resource_\$(date '+%F').log"
echo
echo "Watch compact status CSV:"
echo "  tail -f $RESOURCE_LOG_DIR/status_\$(date '+%F').csv"
echo
echo "Dashboard command:"
echo "  source datapipeline-env/bin/activate"
echo "  python3 QA_Pipeline/scripts/live_dashboard.py --db-path $DB_PATH --output-dir $OUTPUT_DIR --interval 5 --port 1234"
echo
echo "Pipeline command saved at:"
echo "  $run_script"
echo
echo "Pipeline console log:"
echo "  $pipeline_log"
