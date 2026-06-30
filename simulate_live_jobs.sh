#!/bin/bash
# Simulates event-listener jobs arriving one at a time (with a delay between each),
# so you can test "live" dashboard behavior (e.g. consecutive-fail notifications)
# without a real NAS/tmux event listener running.
#
# Run from the DataPipeline project root.
#
# Usage:
#   ./simulate_live_jobs.sh                  # default: 5 consecutive fails, 5s apart
#   ./simulate_live_jobs.sh 8 3              # 8 jobs, 3s apart
#   ./simulate_live_jobs.sh 5 5 pass         # 5 jobs but force them to "pass" instead of fail

set -e

JOBS_DB="outputs/event_listener/jobs.db"
QA_DB="outputs/local_test/qa.db"
TASK_DIR="/Users/jacky/Documents/Neoteai/data/Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_ARX/20260531/liangyunbo"

COUNT="${1:-5}"
DELAY="${2:-5}"
FORCE_STATUS="${3:-fail}"   # "fail" or "pass" — controls which episodes we pick

if [ ! -f "$QA_DB" ]; then
  echo "ERROR: $QA_DB not found. Run this from the DataPipeline project root."
  exit 1
fi

mkdir -p "$(dirname "$JOBS_DB")"

sqlite3 "$JOBS_DB" <<'SCHEMA'
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE,
    record_id TEXT,
    session_id TEXT,
    verified_path TEXT NOT NULL,
    mounted_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    output_dir TEXT,
    db_path TEXT,
    error TEXT,
    received_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
SCHEMA

# Pick N episode paths from qa.db that actually have findings with the
# desired outcome (fail -> has a non-pass finding; pass -> no non-pass finding).
if [ "$FORCE_STATUS" = "fail" ]; then
  MAPFILE_QUERY="SELECT DISTINCT episode_path FROM findings WHERE status != 'pass' LIMIT $COUNT;"
else
  MAPFILE_QUERY="SELECT DISTINCT episode_path FROM episodes WHERE episode_path NOT IN (SELECT episode_path FROM findings WHERE status != 'pass') LIMIT $COUNT;"
fi

mapfile -t EPISODE_PATHS < <(sqlite3 "$QA_DB" "$MAPFILE_QUERY")

if [ "${#EPISODE_PATHS[@]}" -eq 0 ]; then
  echo "No episodes found matching status='$FORCE_STATUS'. Check your qa.db data."
  exit 1
fi

echo "Simulating ${#EPISODE_PATHS[@]} incoming '$FORCE_STATUS' jobs, $DELAY seconds apart..."
echo "Watch the dashboard now — refresh it after each insert to see jobs arrive live."
echo ""

i=0
for EPISODE_PATH in "${EPISODE_PATHS[@]}"; do
  i=$((i + 1))
  EP_NAME=$(basename "$EPISODE_PATH")
  EVENT_ID="live-sim-$(date +%s)-${i}"
  TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  VERIFIED_PATH="/database/verified/Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_ARX/20260531/liangyunbo/${EP_NAME}"

  sqlite3 "$JOBS_DB" <<SQL
INSERT INTO jobs (
    event_id, record_id, session_id, verified_path, mounted_path,
    payload_json, status, attempts, run_id, output_dir, db_path,
    received_at, started_at, finished_at, updated_at
) VALUES (
    '${EVENT_ID}', '', '',
    '${VERIFIED_PATH}',
    '${EPISODE_PATH}',
    '{"manual": true, "live_sim": true}', 'done', 1,
    'event-live-sim',
    'outputs/local_test',
    'outputs/local_test/qa.db',
    '${TS}', '${TS}', '${TS}', '${TS}'
);
SQL

  echo "[$i/${#EPISODE_PATHS[@]}] Inserted job for: $EP_NAME (at $TS)"

  if [ "$i" -lt "${#EPISODE_PATHS[@]}" ]; then
    sleep "$DELAY"
  fi
done

echo ""
echo "Done. Total jobs now in DB:"
sqlite3 "$JOBS_DB" "SELECT COUNT(*) FROM jobs;"
