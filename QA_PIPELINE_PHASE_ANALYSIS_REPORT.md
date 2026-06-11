# QA Pipeline Phase Analysis Report

Date: 2026-06-10

Scope: analysis of `QA_Pipeline/scripts/run_pipeline.py`, phases 1-5 under
`QA_Pipeline/scripts/pipeline/`, shared QA utilities, live monitoring, and
`QA_Pipeline/configs/quality_rules.json`.

## Executive Summary

The pipeline is a read-only QA/reporting pipeline. It discovers episode folders,
loads metadata, runs selected phase checks, writes SQLite state to `qa.db`, and
exports CSV/JSONL/Markdown/HTML reports. The main runner does not move, delete,
quarantine, or cut source data.

For a large NAS date run such as:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified/*/20260606 \
  --db-path outputs/nas_20260606/qa.db \
  --output-dir outputs/nas_20260606 \
  --phases 1,2,3,4,5 \
  --workers 8 \
  --force-rerun \
  --run-id nas-20260606
```

the command correctly limits discovery to date `20260606`, assuming the shell
expands `/mnt/nas/database/verified/*/20260606` into task/date directories.

The structure in `NAS_Sample_Data` matches the expected layout:

```text
<task>/<date>/<operator>/episode_...
```

The largest speed risks are:

- per-episode SQLite connections and writes;
- repeated reads of `metadata.json`;
- full CSV scans in phases 2, 3, and 5;
- OpenCV random frame reads in phase 4;
- live-monitor refreshes that repeatedly query SQLite and rewrite summaries;
- final report generation that can perform many per-episode finding queries.

Phases 4 and 5 support multiprocessing through `--workers`. Phases 1, 2, and 3
are currently sequential.

## Important Behavioral Notes

The runner filters later phases after earlier phase failures. Before each phase,
`_filter_runnable_states()` skips episodes that already have a `fail` status in
an earlier completed phase. This means:

- Phase 1 failures stop an episode from entering phases 2-5.
- Phase 2 failures stop an episode from entering phases 3-5.
- Warnings and `needs_review` do not stop later phases.

This is good for speed and avoids expensive checks on clearly invalid episodes,
but it also means full diagnostic coverage is not collected for failed episodes.
If the desired behavior is "always collect every possible problem," this policy
does not meet that expectation.

The `--force-rerun` flag clears `phases_completed` for discovered states, so
selected phases recompute. It does not delete the DB file; findings are replaced
per episode and phase when each phase saves results.

Live monitor output is created only after discovery and state loading. On a large
NAS run, there can be a visible delay after `Episodes discovered: N` before:

```text
Live run monitor: outputs/.../runs/<run-id>
```

appears.

## Phase 1: Structure and Metadata

File: `QA_Pipeline/scripts/pipeline/phase1_metadata.py`

Purpose: verify that each episode has the basic folder and metadata structure
needed by later phases.

Checks performed:

- episode folder name starts with `episode_`;
- `metadata.json` exists, is readable, valid JSON, and is a JSON object;
- parent path looks like `<task>/<date>/<operator>/<episode>`;
- required metadata fields exist and are valid:
  - `task_key`;
  - `episode_index`;
  - `duration_seconds`;
  - `total_frames`;
  - `modalities`;
  - either positive `fps_actual` or positive `fps_config`;
  - `quality`;
- every modality listed in metadata has a matching folder;
- `.checksum_manifest` exists;
- required files exist and are non-empty:
  - image modality: `video.mp4`, `timestamps.csv`;
  - flow image modality: `video.mp4`;
  - state/action CSV modality: `data.csv`;
- `quality.labels` is a non-empty list.

Severity/status behavior:

- missing or invalid metadata is `critical/fail`;
- missing required metadata fields are `major/fail`;
- missing modality folder or required modality file is `major/fail`;
- missing checksum manifest is `minor/warning`;
- missing quality labels is `minor/warning`;
- bad parent path is `minor/warning`.

Performance profile:

- mostly metadata and filesystem existence checks;
- sequential;
- relatively cheap per episode, but many small NAS metadata operations can still
  be slow.

Concerns:

- `_record_metrics()` checks `.checksum_manifest` even after metadata failed,
  adding a small extra filesystem operation.
- Parent path validation checks the absolute path tail, so it still passes for
  normal NAS paths. However, report context inference may be less complete when
  roots are passed as date folders rather than the common `verified` root.

## Phase 2: Duration and Count Consistency

File: `QA_Pipeline/scripts/pipeline/phase2_duration.py`

Purpose: verify duration, frame counts, FPS consistency, and row-count alignment.

Checks performed:

- `duration_seconds >= 5.0`; shorter episodes are `critical/fail`;
- `duration_seconds` is positive;
- `total_frames` is a positive integer;
- `duration_seconds * fps` matches `total_frames` within 10%;
- image `timestamps.csv` row count matches `total_frames` within 10%;
- state `data.csv` row count matches `duration_seconds * fps` within 15%;
- modality frame/row counts from metadata are aligned:
  - spread <= 3: pass;
  - spread 4-10: `minor/warning`;
  - spread > 10: `major/needs_review`;
- task-level duration outliers using IQR when group size is at least 5;
- task-relative absolute duration checks:
  - less than 20% of task median: `major/fail`;
  - less than 40% of task median: `minor/needs_review`;
  - greater than 250% of task median: `minor/needs_review`.

Performance profile:

- sequential;
- reads metadata already loaded when available;
- counts full CSV rows for image timestamp files and state CSV files;
- group checks are in-memory and cheap after per-episode checks.

Concerns:

- CSV row counting fully scans each checked CSV. On large NAS runs this can be
  substantial.
- Group outlier logic depends on `state.task`. If date-folder roots cause weak
  task inference and metadata lacks `task_key`, grouping may be degraded.
- The configured `phase2_duration.length_alignment.max_video_action_difference`
  is not used directly; the implemented threshold is hard-coded as 3 frames.

## Phase 3: Timestamp Synchronization and Frequency

File: `QA_Pipeline/scripts/pipeline/phase3_timestamp.py`

Purpose: validate image timestamp quality and cross-camera temporal alignment.

Checks performed:

- image timestamp files are readable;
- timestamps are strictly increasing;
- duplicate timestamps are detected;
- image frame drops are checked from metadata `frame_integrity`;
- actual FPS is computed from timestamps and compared with expected FPS:
  - FPS loss above configured threshold is `major/fail`;
  - FPS gain above configured threshold is `minor/warning`;
- `timestamps.csv` and `timestamps_raw.csv` row counts are compared when both
  exist; difference > 2 produces a finding;
- start and end alignment across readable image modalities must be within
  500 ms;
- task+robot group frequency outliers are detected with IQR;
- max consecutive frame drops are checked with group IQR, or fallback threshold
  for small groups.

Configured thresholds from `quality_rules.json`:

- FPS loss fail ratio: `0.10`;
- FPS gain warning ratio: `0.10`;
- normal video drop-ratio fail: `0.15`;
- tactile video drop-ratio fail: `0.20`;
- max consecutive drops fail: `25`;
- max consecutive drops warning fallback: `10`.

Performance profile:

- sequential;
- reads full image `timestamps.csv` files into memory;
- may also count both `timestamps.csv` and `timestamps_raw.csv`;
- group checks are in-memory after per-episode work.

Concerns:

- The docstring mentions state/action timestamps, but `_timestamp_modalities()`
  currently checks only `observation.image.*` modalities and explicitly says
  state/action modality timestamps are checked in Phase 5.
- All timestamp rows are loaded into Python lists; this is simple but can be
  expensive for very large episode counts.
- The alignment threshold is hard-coded at 500 ms, not configurable.

## Phase 4: Video Health

File: `QA_Pipeline/scripts/pipeline/phase4_video.py`

Purpose: verify that image videos are openable and look basically healthy.

Checks performed for each image modality with `video.mp4`:

- OpenCV can open the file;
- video frame count is readable;
- video frame count matches metadata `total_frames` within 10%;
- video duration from `frame_count / fps` matches metadata duration within 10%;
- resolution matches metadata camera config or modality `config.csv`;
- sampled frames are checked for black frames;
- sampled frames are checked for white frames;
- sampled frames are checked for frozen video;
- for ARX5 only, left and right wrist views are checked to ensure both are not
  simultaneously still.

Sampling behavior:

- samples up to 8 positions: 0%, 15%, 30%, 45%, 60%, 75%, 90%, 100%;
- for videos with 8 frames or fewer, reads every frame.

Severity/status behavior:

- OpenCV missing is `critical/fail`;
- video not openable is `critical/fail`;
- frame count unreadable or frame count mismatch is `major/fail`;
- duration mismatch and resolution mismatch are warnings;
- many bad sampled frames can become `critical/fail`;
- any black/white sampled frames are at least `major/needs_review`;
- frozen video is `major/fail`;
- both ARX5 wrist views still is `major/fail`.

Performance profile:

- supports multiprocessing with `--workers`;
- expensive on NAS because it opens each video and seeks to multiple frame
  positions;
- repeated `metadata.json` reads occur inside helper functions, including frame
  count, duration, and resolution checks.

Concerns:

- In parallel mode, workers receive only `episode_path` and `robot`; task/date/
  operator/controller context is preserved in the parent state, but worker-local
  checks cannot use the full metadata-loaded state.
- `_metadata_total_frames()`, `_metadata_duration()`, and
  `_expected_resolution()` each load `metadata.json` separately. This is a clear
  optimization target.
- Random video seeks over NAS can be much slower than local storage.

## Phase 5: Robot State Reasonableness

File: `QA_Pipeline/scripts/pipeline/phase5_robot_state.py`

Purpose: detect bad robot/action numeric data, impossible state values, abnormal
motion, operator standstill, and gripper mapping issues.

Modalities checked:

- `actions.joint_position`;
- `observation.state.joint_position`;
- `observation.state.joint_velocity`, when present;
- estimated velocity/acceleration from joint position when measured velocity is
  absent;
- `actions.eef_pose`;
- `observation.state.eef_pose`.

Checks performed:

- CSV parseability;
- joint/gripper column detection;
- NaN, Inf, or unparseable values in relevant columns;
- `timestamp_ms` exists and is strictly increasing;
- joint position limits;
- gripper limits;
- low mean gripper distance, reported as remap needed;
- per-frame joint steps;
- per-frame gripper steps;
- jitter score after moving-average smoothing;
- measured joint velocity and acceleration if velocity CSV exists;
- estimated joint velocity and acceleration if velocity CSV does not exist;
- standstill segments longer than 5000 ms;
- excessive standstill if total excess idle time is more than 20% of episode
  duration;
- EEF position step too large.

Robot configuration:

- known configs exist for `arx5`, `flexiv`, and `aloha`;
- unknown robot values fall back to ARX5 defaults;
- some values are calibrated comments, while others are explicitly conservative
  defaults.

Severity/status behavior:

- unparseable CSV is `critical/fail`;
- missing/unparseable timestamps are `major/fail`;
- timestamp monotonic issues scale by violation ratio:
  - >= 5%: `major/fail`;
  - >= 1%: `major/needs_review`;
  - otherwise: `minor/warning`;
- joint/gripper limit, step, velocity, and EEF step issues are mostly
  `needs_review`;
- acceleration high is `minor/warning`;
- high jitter can be warning or fail depending on score;
- standstill segment is `minor/warning`;
- excessive standstill is `major/needs_review`.

Performance profile:

- supports multiprocessing with `--workers`;
- expensive because it loads full CSV files into memory and repeatedly extracts
  columns;
- pure Python list processing dominates for large CSVs;
- NAS bandwidth and latency can be limiting.

Concerns:

- Unknown robot types silently use ARX5 limits. This can create misleading
  findings for non-ARX5 data.
- `_moving_average()` is O(n * window). With the current window of 5 this is
  acceptable, but it would not scale well if the window grows.
- `_finite_column_values()` is called many times and scans rows repeatedly for
  each column. A column-oriented representation would reduce repeated work.
- Standstill message says "beyond 4-second buffer" but
  `STANDSTILL_BUFFER_MS = 5000`, so the message is inaccurate.
- Phase 5 only checks configured modality names. If a dataset uses variants
  such as `action.eef_pose` rather than `actions.eef_pose`, Phase 5 does not
  currently include it in `EEF_MODALITIES`, although the standstill trim config
  mentions `action.eef_pose`.

## Orchestration, Database, and Reporting

File: `QA_Pipeline/scripts/run_pipeline.py`

The runner:

1. validates root paths;
2. parses selected phases;
3. discovers episode directories recursively;
4. optionally applies `--max-episodes`;
5. initializes SQLite;
6. loads or creates `EpisodeState` for every discovered episode;
7. starts live monitoring;
8. runs selected phases;
9. writes final verdicts;
10. exports reports.

The database schema stores one row per episode and one row per finding.

Important speed detail: `load_episode_state()`, `save_episode_state()`, and
`save_findings()` each open a new SQLite connection. This is simple and robust
for small runs but inefficient for thousands of episodes.

Final exports:

- `quality_report.csv`;
- `quality_findings.jsonl`;
- `quality_summary.md`;
- `dashboard.html`.

Potential reporting bottleneck: `export_quality_report()` loads all episode
states, then queries findings once per episode. With 5989 episodes this is 5989
additional SQLite queries, plus each state load rereads `metadata.json`.

## NAS Date-Filtered Running

For your NAS layout:

```text
/mnt/nas/database/verified/<task>/<date>/<operator>/episode_...
```

this date-limited pattern is appropriate:

```bash
--roots /mnt/nas/database/verified/*/20260606
```

It avoids scanning other dates. Bash expands it to many task/date roots before
Python starts.

Recommended shell sanity checks:

```bash
printf '%s\n' /mnt/nas/database/verified/*/20260606 | wc -l
printf '%s\n' /mnt/nas/database/verified/*/20260606 | head
```

If a date exists under many task folders and has thousands of episodes, the
startup and phase runtimes are expected to be non-trivial.

## Performance Recommendations

### Operational Recommendations

For fast first feedback on a large date:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified/*/20260606 \
  --db-path outputs/nas_20260606_smoke/qa.db \
  --output-dir outputs/nas_20260606_smoke \
  --phases 1,2,3 \
  --max-episodes 100 \
  --workers 8 \
  --force-rerun \
  --run-id nas-20260606-smoke
```

For full QA, prefer staged runs:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified/*/20260606 \
  --db-path outputs/nas_20260606/qa.db \
  --output-dir outputs/nas_20260606 \
  --phases 1,2,3 \
  --force-rerun \
  --run-id nas-20260606-p123
```

Then run heavier checks:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified/*/20260606 \
  --db-path outputs/nas_20260606/qa.db \
  --output-dir outputs/nas_20260606 \
  --phases 4,5 \
  --workers 8 \
  --force-rerun \
  --run-id nas-20260606-p45
```

If live monitoring overhead becomes visible, increase refresh interval:

```bash
--live-report-interval 10
```

or disable it for maximum throughput:

```bash
--disable-live-monitor
```

### Code-Level Optimizations

Highest impact:

1. Reuse SQLite connections or batch DB writes per phase.
   Current code opens SQLite connections per episode save and per findings save.

2. Avoid repeated `metadata.json` reads.
   Store metadata in `EpisodeState` and pass it into Phase 4 helper functions
   instead of rereading it for frame count, duration, and resolution.

3. Start the live monitor earlier or print setup progress.
   Current large-run delay after discovery can look like a hang.

4. Add a native `--date` filter or `--include-date` option.
   This would avoid relying on shell glob expansion and preserve better context
   when scanning from `/mnt/nas/database/verified`.

5. Make Phase 2 and Phase 3 optionally parallel.
   They are mostly independent per episode before group checks.

6. Optimize final report export.
   Query all findings once and group in memory, instead of one query per episode.

Medium impact:

7. Add a "metadata-only" or "fast" mode for Phase 2.
   Use metadata row/frame counts where available and skip full CSV row counts
   unless metadata is missing or inconsistent.

8. Use column-oriented CSV loading in Phase 5.
   Current logic repeatedly scans row lists per column.

9. Make hard-coded thresholds configurable:
   - Phase 3 alignment threshold, currently 500 ms;
   - Phase 2 modality spread thresholds;
   - Phase 2 duration ratio thresholds;
   - Phase 5 standstill ratio threshold.

10. Add indexes to SQLite:

```sql
CREATE INDEX IF NOT EXISTS idx_findings_episode_path ON findings(episode_path);
CREATE INDEX IF NOT EXISTS idx_findings_phase ON findings(phase);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
```

Lower impact:

11. Reduce duplicate directory listing in phase checks.
12. Use a deterministic episode ordering that can preserve task/date grouping
    while still limiting `--max-episodes`.
13. Add explicit logging around discovery, state loading, monitor startup, and
    final export.

## Correctness Recommendations

1. Decide whether skipped later phases after early failure are desired.
   If the goal is training readiness, skipping is reasonable. If the goal is
   full diagnostics, add an option such as `--continue-after-fail`.

2. Treat unknown robot config as a warning.
   Falling back to ARX5 defaults should be visible in the report.

3. Fix the standstill message.
   The message says 4 seconds but the code uses 5000 ms.

4. Align `EEF_MODALITIES` with supported dataset naming.
   Consider adding `action.eef_pose` if that naming appears in real NAS data.

5. Verify robot thresholds against real specs or calibrated datasets.
   Several Phase 5 thresholds are conservative defaults. They may be useful for
   anomaly detection but should not be treated as final physical truth without
   calibration.

6. Clarify expected path-root usage.
   Date-folder roots are fast and practical, but scanning from the common
   `verified` root with a native date filter would preserve context more cleanly.

## Recommended Next Implementation Steps

1. Add progress output for state loading:

```text
Episodes discovered: 5989
Loading episode states: 1000/5989
...
Live run monitor: ...
```

2. Batch SQLite operations in phase finish paths.

3. Cache metadata per episode and remove Phase 4 repeated metadata reads.

4. Add CLI options:

```text
--date 20260606
--task arrange_flowers_UR
--continue-after-fail
--fast
```

5. Parallelize Phase 2 and Phase 3 per-episode checks, then run group checks in
   the parent process.

6. Optimize report export query patterns and add SQLite indexes.

## Bottom Line

The current phases broadly match a QA purpose for training-data readiness:

- Phase 1: structure and metadata validity;
- Phase 2: duration/count consistency;
- Phase 3: timestamp and camera timing health;
- Phase 4: video health;
- Phase 5: robot state/action reasonableness.

The implementation is conservative and report-only, which is appropriate for NAS
source data. For 5989 episodes on NAS, however, current performance will be
limited by sequential metadata/CSV work, repeated small-file reads, and many
SQLite operations. The most useful near-term improvements are DB batching,
metadata caching, clearer state-loading progress, and optional parallelism for
phases 2 and 3.
