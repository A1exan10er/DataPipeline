# Pipeline Monitoring And Issue Reporting Plan

Last updated: 2026-06-09

## Intention

The data quality pipeline should be usable as a long-running NAS-scale process,
not only as a final batch report. While each phase is running, operators should
be able to see:

- which phase is currently running;
- how many episodes have passed, failed, warned, or need review;
- which episodes have already produced issues;
- the exact issue name, severity, status, message, and evidence details;
- which episodes were skipped because an earlier phase already failed;
- where the durable report files are being written.

The system should keep good and usable data separate from bad or suspicious data
through reports first, and only later through reviewed quarantine/move actions.

## Current Implementation

The current `QA_Pipeline/scripts/run_pipeline.py` already has useful foundations:

- discovers episode folders;
- runs Phase 1 through Phase 5;
- stores per-episode phase status in SQLite;
- stores exact findings in SQLite through `save_findings`;
- exports final CSV, JSONL, and Markdown reports;
- skips later phases for episodes that failed earlier phases;
- supports parallel execution for Phases 4 and 5.

Remaining limitations:

- group-level checks in Phase 2 and Phase 3 delay some findings until the group
  check finishes;
- there is no SQLite `pipeline_runs` or `phase_runs` table yet;
- trim planning and future quarantine planning are separate from the main phase
  status model.

## Target Outputs

Every pipeline run should write a run directory:

```text
outputs/runs/<run_id>/
  run_status.json
  phase_status.jsonl
  issue_events.jsonl
  episode_status.csv
  issue_summary.csv
  live_summary.md
  final/
    quality_report.csv
    quality_findings.jsonl
    quality_summary.md
    dashboard.html
```

Recommended behavior:

- `issue_events.jsonl` is append-only and updated as soon as an issue is found.
- `phase_status.jsonl` records phase start/end and periodic progress snapshots.
- `run_status.json` is overwritten atomically with the current run state.
- `live_summary.md` is refreshed periodically for quick human review.
- final reports remain the authoritative end-of-run exports.

## Issue Event Schema

Each issue event should contain enough evidence to understand the exact problem
without opening the database:

```json
{
  "run_id": "20260609-153000",
  "timestamp": "2026-06-09T15:30:21+08:00",
  "episode_path": ".../episode_0001",
  "task": "assemble_the_battery",
  "date": "20260422",
  "operator": "wangyong",
  "robot": "arx5",
  "controller": "none",
  "phase": 3,
  "check_name": "frame_drop_ratio",
  "severity": "major",
  "status": "fail",
  "message": "Frame drop ratio exceeds configured threshold.",
  "details": {
    "modality": "observation.image.third_view",
    "drop_ratio": 0.18,
    "threshold": 0.15
  }
}
```

This should be generated from the existing `Finding` object plus episode
context.

## Live Status Model

Add a lightweight run monitor object used by `run_pipeline.py`.

Responsibilities:

- create `run_id` and run output directory;
- track current phase, total episodes, processed episodes, skipped episodes,
  elapsed time, worker count, and throughput;
- maintain counters by status and severity;
- append exact issue events as findings are saved;
- periodically refresh `run_status.json` and `live_summary.md`;
- write phase start/end records to `phase_status.jsonl`.

Suggested terminal display:

```text
[Run 20260609-153000] roots=NAS_Sample_Data workers=8
[Phase 3 timestamp] 3200/4465 processed, 420 skipped, 182.4 eps/s
status: pass=2980 warning=47 fail=119 needs_review=54
issues: frame_drop_ratio=83, abnormal_fps_loss=28, frame_drop_consecutive=12
latest issue: episode_0026 frame_drop_ratio observation.image.third_view drop_ratio=0.18 threshold=0.15
```

The terminal should stay concise. The detailed records should go to JSONL/CSV.

## Database Additions

The existing `episodes` and `findings` tables should remain. Add optional
run-level tables:

```sql
CREATE TABLE pipeline_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  roots TEXT,
  phases TEXT,
  workers INTEGER,
  status TEXT,
  output_dir TEXT
);

CREATE TABLE phase_runs (
  run_id TEXT,
  phase INTEGER,
  started_at TEXT,
  finished_at TEXT,
  total_episodes INTEGER,
  processed_episodes INTEGER,
  skipped_episodes INTEGER,
  elapsed_seconds REAL,
  status_counts TEXT,
  issue_counts TEXT,
  PRIMARY KEY (run_id, phase)
);
```

Keep `findings` unchanged for compatibility. The append-only `issue_events.jsonl`
can be regenerated from the database if needed, but writing it live is more
operator-friendly.

## Phase Integration

Short-term integration should avoid rewriting every phase from scratch.

1. Keep each phase returning `EpisodeState` and saving findings as it does now.
2. After each phase finishes an episode, call a monitor hook with:

```text
on_episode_phase_complete(state, phase_number, new_findings)
```

3. For parallel phases, collect worker results in the parent process and emit
   monitor events there. This avoids multiple processes writing the same JSONL
   file.
4. For group-level checks in Phase 2 and Phase 3, emit issue events after group
   findings are attached.
5. Keep final report export unchanged, but also copy final exports into the
   run-specific output directory.

Medium-term improvement:

- refactor phase runners to accept a richer callback object instead of only
  `progress_callback(current, total)`.

## Episode Issue Recording

Add a dedicated issue ledger:

```text
episode_issues.csv
```

Columns:

```text
episode_path
final_status
phase
check_name
severity
status
message
details_json
task
date
operator
robot
controller
recorded_at
```

This is different from `quality_report.csv`:

- `quality_report.csv` gives one row per episode.
- `episode_issues.csv` gives one row per exact issue.

For long runs, write this file incrementally or export it every N episodes.

## UI Options

Start with file and terminal monitoring:

```bash
tail -f outputs/runs/<run_id>/issue_events.jsonl
watch -n 5 cat outputs/runs/<run_id>/live_summary.md
```

Implemented initial static dashboard:

- generated as `dashboard.html` from SQLite;
- shows final episode status counts;
- shows top issue and phase issue breakdowns;
- filters episodes by status, task, robot, operator, and search text;
- filters exact issues by status, severity, phase, check name, and search text;
- works as a standalone HTML file with no server dependency.

Do not make the dashboard responsible for modifying or moving data.

## Relationship To Quarantine

This monitoring layer should not move files. It should produce reliable input
for a later quarantine planner:

```text
quality findings -> final decision -> quarantine plan -> reviewed mover
```

Only `final_status = fail` should become automatic quarantine candidates after
dry-run review. `needs_review` should remain in place until approved manually.

## Implementation Sequence

1. Done: add `RunMonitor` helper in
   `QA_Pipeline/scripts/pipeline/run_monitor.py`.
2. Done: add run output directory creation and `run_id` to `run_pipeline.py`.
3. Done: write `run_status.json`, `phase_status.jsonl`, and
   `issue_events.jsonl`.
4. Done: poll new SQLite findings during phase progress and at phase end.
5. Done: capture group-level issue events after Phase 2 and Phase 3 group
   checks at phase finish.
6. Done: add `episode_issues.csv` live issue ledger.
7. Done: add periodic live Markdown summary refresh.
8. Done: add smoke test on `Test_Data` to ensure issue events match final
   `quality_findings.jsonl`.
9. Done: add static dashboard generation to final report export.
10. Run on `NAS_Sample_Data` and compare:
   - number of issue events;
   - final findings count;
   - phase status counts;
   - no duplicated events on rerun with `--force-rerun`.
11. Add operator documentation for watching live files during server runs.

## Current Smoke Test

Command:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py --roots Test_Data \
  --db-path /tmp/qa_monitor_test/qa.db \
  --output-dir /tmp/qa_monitor_test/out \
  --phases 1,2,3 \
  --force-rerun \
  --run-id monitor-smoke \
  --live-report-interval 0.1
```

Result:

- live run directory: `/tmp/qa_monitor_test/out/runs/monitor-smoke`
- final status: 1 pass, 3 warning
- `issue_events.jsonl`: 12 issue events
- `episode_issues.csv`: 12 issue rows plus header
- `quality_findings.jsonl`: 12 final findings
- `run_status.json`: ended with `status = complete`
- `dashboard.html`: generated in the output directory and run-specific `final/`
  directory

## Safety Notes

- Live reporting must not slow down phase execution significantly. Use buffered
  file writes and periodic summary refresh.
- In parallel phases, only the parent process should write live report files.
- Use atomic replace for `run_status.json` and `live_summary.md`.
- Keep append-only logs under the output directory, not inside episode folders.
- Preserve existing final reports so current workflows keep working.
