#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="${1:-outputs/resource_logs/umi_ik_small_batch}"
INTERVAL="${2:-5}"
DB_PATH="${DB_PATH:-}"
RUN_ID="${RUN_ID:-}"
MAX_LOAD_RATIO="${MAX_LOAD_RATIO:-}"
MIN_FREE_MEM_GB="${MIN_FREE_MEM_GB:-}"

mkdir -p "$LOG_DIR"

csv_file="$LOG_DIR/status_$(date '+%F').csv"
if [[ ! -f "$csv_file" ]]; then
  echo "timestamp,run_id,load_1m,load_5m,load_15m,load_threshold,mem_total_gb,mem_available_gb,min_free_mem_gb,swap_used_gb,disk_available_gb,pipeline_count,pipeline_cpu_pct,pipeline_rss_mb,ffmpeg_count,ffmpeg_cpu_pct,ffmpeg_rss_mb,ik_count,ik_cpu_pct,ik_rss_mb,db_total,db_pass,db_warning,db_fail,db_needs_review,db_unknown" > "$csv_file"
fi

process_stats() {
  local pattern="$1"
  ps -eo pcpu=,rss=,args= | awk -v pat="$pattern" '
    $0 ~ pat && $0 !~ /resource_monitor.sh/ && $0 !~ /awk -v pat=/ {
      count += 1
      cpu += $1
      rss += $2
    }
    END {
      printf "%d,%.1f,%.1f", count, cpu, rss / 1024
    }'
}

db_counts() {
  if [[ -z "$DB_PATH" || ! -f "$DB_PATH" ]]; then
    echo "0,0,0,0,0,0"
    return
  fi
  python3 - "$DB_PATH" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    rows = dict(conn.execute(
        "SELECT COALESCE(final_status, 'unknown'), COUNT(*) FROM episodes GROUP BY COALESCE(final_status, 'unknown')"
    ).fetchall())
    total = sum(rows.values())
    print(",".join(str(v) for v in [
        total,
        rows.get("pass", 0),
        rows.get("warning", 0),
        rows.get("fail", 0),
        rows.get("needs_review", 0),
        rows.get("unknown", 0),
    ]))
except Exception:
    print("0,0,0,0,0,0")
PY
}

while true; do
  ts="$(date '+%F %T')"
  log_file="$LOG_DIR/resource_$(date '+%F').log"
  csv_file="$LOG_DIR/status_$(date '+%F').csv"
  if [[ ! -f "$csv_file" ]]; then
    echo "timestamp,run_id,load_1m,load_5m,load_15m,load_threshold,mem_total_gb,mem_available_gb,min_free_mem_gb,swap_used_gb,disk_available_gb,pipeline_count,pipeline_cpu_pct,pipeline_rss_mb,ffmpeg_count,ffmpeg_cpu_pct,ffmpeg_rss_mb,ik_count,ik_cpu_pct,ik_rss_mb,db_total,db_pass,db_warning,db_fail,db_needs_review,db_unknown" > "$csv_file"
  fi

  read -r load_1m load_5m load_15m _ < /proc/loadavg
  nproc_count="$(nproc 2>/dev/null || echo 1)"
  if [[ -n "$MAX_LOAD_RATIO" ]]; then
    load_threshold="$(awk -v n="$nproc_count" -v r="$MAX_LOAD_RATIO" 'BEGIN { printf "%.2f", n * r }')"
  else
    load_threshold=""
  fi
  mem_total_gb="$(awk '/MemTotal:/ { printf "%.2f", $2 / 1024 / 1024 }' /proc/meminfo)"
  mem_available_gb="$(awk '/MemAvailable:/ { printf "%.2f", $2 / 1024 / 1024 }' /proc/meminfo)"
  swap_used_gb="$(free -g | awk '/Swap:/ { printf "%.2f", $3 }')"
  disk_available_gb="$(df -BG . | awk 'NR==2 { gsub("G", "", $4); print $4 }')"
  pipeline_stats="$(process_stats 'python3 .*QA_Pipeline/scripts/run_pipeline.py')"
  ffmpeg_stats="$(process_stats '(^|[ /])ffmpeg([ ]|$)')"
  ik_stats="$(process_stats 'python3 .*solve_executability.py')"
  qa_counts="$(db_counts)"

  echo "$ts,$RUN_ID,$load_1m,$load_5m,$load_15m,$load_threshold,$mem_total_gb,$mem_available_gb,$MIN_FREE_MEM_GB,$swap_used_gb,$disk_available_gb,$pipeline_stats,$ffmpeg_stats,$ik_stats,$qa_counts" >> "$csv_file"

  {
    echo "===== $ts ====="
    echo "--- run ---"
    echo "run_id=${RUN_ID:-unknown}"
    echo "db_path=${DB_PATH:-not_set}"
    echo "csv_status=$csv_file"
    echo "--- uptime ---"
    uptime
    echo "--- memory ---"
    free -h
    echo "--- disk usage ---"
    df -h .
    echo "--- disk io ---"
    if command -v iostat >/dev/null 2>&1; then
      iostat -xz 1 1
    else
      echo "iostat not installed"
    fi
    echo "--- top cpu ---"
    ps -eo pid,ppid,pcpu,pmem,rss,etime,cmd --sort=-pcpu | head -40
    echo "--- top memory ---"
    ps -eo pid,ppid,pcpu,pmem,rss,etime,cmd --sort=-rss | head -40
    echo
  } >> "$log_file" 2>&1

  sleep "$INTERVAL"
done
