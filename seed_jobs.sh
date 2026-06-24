#!/bin/bash
# Seeds outputs/event_listener/jobs.db with 50 simulated "done" jobs,
# pointing at real episode paths that exist in outputs/local_test/qa.db.
# Run this from the DataPipeline project root.

set -e

JOBS_DB="outputs/event_listener/jobs.db"
QA_DB="outputs/local_test/qa.db"
TASK_DIR="/Users/jacky/Documents/Neoteai/data/Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_ARX/20260531/liangyunbo"

# Episode folder names (just the basename) - taken from the real findings table.
# Add/remove lines here if you want different episodes; each must exist in qa.db.
EPISODES=(
  "episode_0001_20260531-202207_liangyunbo_arx5_none"
  "episode_0002_20260531-202346_liangyunbo_arx5_none"
  "episode_0003_20260531-202641_liangyunbo_arx5_none"
  "episode_0004_20260531-202927_liangyunbo_arx5_none"
  "episode_0005_20260531-203407_liangyunbo_arx5_none"
  "episode_0009_20260531-204722_liangyunbo_arx5_none"
  "episode_0010_20260531-204950_liangyunbo_arx5_none"
  "episode_0011_20260531-205329_liangyunbo_arx5_none"
  "episode_0012_20260531-205606_liangyunbo_arx5_none"
  "episode_0013_20260531-205839_liangyunbo_arx5_none"
  "episode_0014_20260531-210111_liangyunbo_arx5_none"
  "episode_0015_20260531-210413_liangyunbo_arx5_none"
  "episode_0016_20260531-210800_liangyunbo_arx5_none"
  "episode_0017_20260531-210958_liangyunbo_arx5_none"
  "episode_0018_20260531-212913_liangyunbo_arx5_none"
  "episode_0019_20260531-213202_liangyunbo_arx5_none"
  "episode_0020_20260531-213435_liangyunbo_arx5_none"
  "episode_0021_20260531-213759_liangyunbo_arx5_none"
  "episode_0022_20260531-214037_liangyunbo_arx5_none"
  "episode_0023_20260531-214315_liangyunbo_arx5_none"
  "episode_0024_20260531-214541_liangyunbo_arx5_none"
  "episode_0025_20260531-215454_liangyunbo_arx5_none"
  "episode_0026_20260531-215631_liangyunbo_arx5_none"
  "episode_0027_20260531-215719_liangyunbo_arx5_none"
  "episode_0028_20260531-215758_liangyunbo_arx5_none"
  "episode_0029_20260531-220023_liangyunbo_arx5_none"
  "episode_0030_20260531-220317_liangyunbo_arx5_none"
  "episode_0031_20260531-220541_liangyunbo_arx5_none"
)

if [ ! -f "$QA_DB" ]; then
  echo "ERROR: $QA_DB not found. Run this from the DataPipeline project root."
  exit 1
fi

mkdir -p "$(dirname "$JOBS_DB")"

# Make sure the jobs table exists (no-op if it already does).
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

COUNT=0
BASE_EPOCH=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "2026-06-24T15:00:00Z" "+%s" 2>/dev/null || date -u -d "2026-06-24T15:00:00Z" "+%s")

for EP in "${EPISODES[@]}"; do
  COUNT=$((COUNT + 1))
  # Spread timestamps out by 90 seconds each, so "Updated" sort has real variation.
  OFFSET=$((COUNT * 90))
  TS_EPOCH=$((BASE_EPOCH + OFFSET))
  # macOS date vs GNU date compatibility
  TS=$(date -u -j -f "%s" "$TS_EPOCH" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -d "@$TS_EPOCH" "+%Y-%m-%dT%H:%M:%SZ")

  EVENT_ID="seed-test-$(printf '%03d' "$COUNT")"
  MOUNTED_PATH="${TASK_DIR}/${EP}"
  VERIFIED_PATH="/database/verified/Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_ARX/20260531/liangyunbo/${EP}"

  sqlite3 "$JOBS_DB" <<SQL
INSERT OR IGNORE INTO jobs (
    event_id, record_id, session_id, verified_path, mounted_path,
    payload_json, status, attempts, run_id, output_dir, db_path,
    received_at, started_at, finished_at, updated_at
) VALUES (
    '${EVENT_ID}', '', '',
    '${VERIFIED_PATH}',
    '${MOUNTED_PATH}',
    '{"manual": true, "seed": true}', 'done', 1,
    'event-seed-test',
    'outputs/local_test',
    'outputs/local_test/qa.db',
    '${TS}', '${TS}', '${TS}', '${TS}'
);
SQL
done

echo "Inserted/verified $COUNT seed jobs into $JOBS_DB"
sqlite3 "$JOBS_DB" "SELECT COUNT(*) AS total_jobs FROM jobs;"
