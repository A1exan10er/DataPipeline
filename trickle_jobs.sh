#!/bin/bash
# Inserts one simulated "done" job every ~12 seconds, using real fail
# episodes from outputs/local_test/qa.db that aren't already in jobs.db.
# Watch the dashboard (refresh periodically) while this runs to see jobs
# arrive one at a time, similar to a real event listener.
#
# Run from the DataPipeline project root.
#
# Usage:
#   ./trickle_jobs.sh             # default: 10 jobs, 12s apart, status=fail
#   ./trickle_jobs.sh 20 15       # 20 jobs, 15s apart
#   ./trickle_jobs.sh 10 12 pass  # 10 jobs, 12s apart, status=pass instead

set -e

JOBS_DB="outputs/event_listener/jobs.db"
QA_DB="outputs/local_test/qa.db"

COUNT="${1:-10}"
DELAY="${2:-12}"
WANT_STATUS="${3:-fail}"   # "fail" or "pass"

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

# Pick episodes from qa.db matching the desired final_status, that are NOT
# already present as a mounted_path in jobs.db (so we don't just re-trigger
# the same already-seen episodes every time you run this).
mapfile -t EPISODE_PATHS < <(sqlite3 "$QA_DB" "
  SELECT episode_path FROM episodes
  WHERE final_status = '${WANT_STATUS}'
  ORDER BY episode_path
  LIMIT 500
")

EXISTING_PATHS=$(sqlite3 "$JOBS_DB" "SELECT mounted_path FROM jobs;" 2>/dev/null || true)

PICKED=()
for EP in "${EPISODE_PATHS[@]}"; do
  if ! grep -qF "$EP" <<< "$EXISTING_PATHS"; then
    PICKED+=("$EP")
  fi
  if [ "${#PICKED[@]}" -ge "$COUNT" ]; then
    break
  fi
done

if [ "${#PICKED[@]}" -eq 0 ]; then
  echo "No unused episodes found with final_status='$WANT_STATUS'. Try a different status or check qa.db."
  exit 1
fi

echo "Trickling in ${#PICKED[@]} '$WANT_STATUS' jobs, one every ${DELAY}s..."
echo "Keep the dashboard open and refresh periodically to watch them arrive."
echo ""

i=0
for EPISODE_PATH in "${PICKED[@]}"; do
  i=$((i + 1))
  EP_NAME=$(basename "$EPISODE_PATH")
  EVENT_ID="trickle-$(date +%s)-${i}"
  TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  # Best-effort verified_path: swap local data root for the NAS-style prefix.
  VERIFIED_PATH="/database/verified${EPISODE_PATH#*/data}"

  sqlite3 "$JOBS_DB" "
INSERT OR IGNORE INTO jobs (
    event_id, record_id, session_id, verified_path, mounted_path,
    payload_json, status, attempts, run_id, output_dir, db_path,
    received_at, started_at, finished_at, updated_at
) VALUES (
    '${EVENT_ID}', '', '',
    '${VERIFIED_PATH}',
    '${EPISODE_PATH}',
    '{\"manual\": true, \"trickle\": true}', 'done', 1,
    'event-trickle-test',
    'outputs/local_test',
    'outputs/local_test/qa.db',
    '${TS}', '${TS}', '${TS}', '${TS}'
);
"

  echo "[$i/${#PICKED[@]}] $(date '+%H:%M:%S') inserted: $EP_NAME"

  if [ "$i" -lt "${#PICKED[@]}" ]; then
    sleep "$DELAY"
  fi
done

echo ""
echo "Done. Total jobs now in jobs.db:"
sqlite3 "$JOBS_DB" "SELECT COUNT(*) FROM jobs;"
