# Data Quality Pipeline Implementation Plan

Last updated: 2026-06-09

## Goal

Build a safety-first data quality pipeline for robot and UMI datasets stored on
NAS. The pipeline will identify abnormal or disqualified episodes, produce
explainable reports, and eventually move failed episodes to a separate
quarantine location.

The pipeline must be developed and validated locally before it is allowed to
touch NAS data. It must never delete source data automatically.

## Current Context

- `Test_Data/` contains small local episodes for script behavior and performance
  checks.
- `Test_Folder_For_DataPipeline/` contains larger local task episode sets for
  pipeline performance and robustness testing.
- `NAS_Sample_Data/` contains copied NAS task folders for structure analysis and
  compatibility testing against more recent data layouts.
- `QA_Pipeline/` contains the current main multi-phase QA implementation. It
  already covers metadata, duration/count, timestamp, video, and robot-state
  checks using SQLite state and CSV/JSONL/Markdown reports.
- `UMI_Data_Validation/` contains UMI EEF-pose inverse-kinematics validation code
  that should be integrated into the QA pipeline as a later specialized phase.
- `Werkzeuge/docs/NAS_SAMPLE_DATA_STRUCTURE.md` documents observed NAS data
  structure, modality folders, episode naming styles, robot/controller variants,
  and recent structure updates.
- `Werkzeuge/docs/DATA_QUALITY_AUTOMATION_PLAN.md` contains earlier suggestions
  for a data filtering pipeline.
- The final production pipeline may run on a server that can connect to NAS, but
  it should not be deployed against NAS until local tests and dry-run reports are
  reviewed.

See `PIPELINE_INTEGRATION_PLAN.md` for the current integration roadmap based on
the newly added folders.

See `STANDSTILL_TRIM_IMPLEMENTATION_PLAN.md` for the focused plan to detect
abnormal standstill at episode beginnings/endings and later cut matching CSV and
video ranges into a separate cleaned dataset root.

See `PIPELINE_MONITORING_AND_ISSUE_REPORTING_PLAN.md` for the plan to make each
phase report live status, append exact issue events while processing, and keep
run-level monitoring records for long NAS-scale jobs.

## Safety Principles

1. The first versions must be read-only.
2. Detection, decision, and movement must be separate stages.
3. The pipeline must generate reports before planning any move.
4. The move planner must support dry-run mode.
5. Only episodes with a final `fail` decision should be moved automatically.
6. `needs_review` episodes should remain in place unless manually approved.
7. No script should delete data in the initial production design.
8. Quarantine moves must preserve the original relative folder structure.
9. All decisions and moves must be written to audit logs.
10. Rollback information must be generated for every move.

## Target Status Values

```text
pass
warning
needs_review
fail
```

- `pass`: no important quality issues found.
- `warning`: usable episode with minor issues.
- `needs_review`: suspicious or statistically unusual episode requiring human
  review.
- `fail`: clearly disqualified episode that may be moved to quarantine after
  dry-run review.

## Target Severity Values

```text
critical
major
minor
info
```

- `critical`: missing essential data, unreadable metadata, corrupt required
  files, impossible timestamps, or physically impossible values.
- `major`: serious abnormal motion, severe frame/timestamp mismatch, large
  spikes, or repeated damaging quality issues.
- `minor`: small timing drift, optional files missing, or minor schema mismatch.
- `info`: non-failing observations useful for summaries and debugging.

## Pipeline Architecture

```text
data root
  -> inventory scanner
  -> quality checks
  -> decision engine
  -> dry-run move planner
  -> reviewed quarantine mover
  -> audit and rollback reports
```

## Phase 1: Inventory Scanner

Status: not started

Purpose:

Create a read-only scanner that discovers episodes and records their structure
without evaluating quality.

Inputs:

- `Test_Data/`
- `NAS_Sample_Data/`
- later, a NAS-mounted root in read-only mode

Required behavior:

- Walk task/date/operator/episode folders.
- Support both episode naming styles:
  - `episode_0029`
  - `episode_0085_20260428-114939_wangyong_arx5_none`
- Discover `metadata.json`, `meta/episode.json`, modality folders, CSV files,
  video files, checksum files, raw files, timing logs, and optical-flow videos.
- Record file sizes and basic existence information.
- Continue scanning after per-episode errors.
- Write outputs incrementally so interrupted scans keep partial results.

Outputs:

```text
episode_inventory.csv
episode_inventory.jsonl
scan_errors.jsonl
```

Progress checklist:

- [ ] Define inventory schema.
- [ ] Implement episode discovery.
- [ ] Parse metadata safely.
- [ ] Discover modality files.
- [ ] Write CSV and JSONL outputs.
- [ ] Test on `Test_Data/`.
- [ ] Test on `NAS_Sample_Data/`.

## Phase 2: Structural And Metadata Checks

Status: not started

Purpose:

Catch cheap, reliable problems before reading large CSV or video payloads.

Checks:

- `metadata.json` exists.
- `metadata.json` is valid JSON.
- episode folder starts with `episode_`.
- parent path follows `<task>/<date>/<operator>/<episode>`.
- required metadata fields are present where expected:
  - `task_key`
  - `episode_index`
  - `duration_seconds`
  - `total_frames`
  - `fps_actual` or `fps_config`
  - `modalities`
  - `quality`
- required modality files exist:
  - CSV modality: `data.csv`
  - image modality: `video.mp4` and `timestamps.csv`
- required files are non-empty.
- metadata modalities match discovered folders where possible.

Outputs:

```text
quality_findings.jsonl
quality_report.csv
quality_summary.md
```

Progress checklist:

- [ ] Define finding schema.
- [ ] Implement metadata checks.
- [ ] Implement required-file checks.
- [ ] Implement modality consistency checks.
- [ ] Add summary report.
- [ ] Validate findings against known sample episodes.

## Phase 3: Duration And Count Checks

Status: not started

Purpose:

Detect episodes that are abnormally short, abnormally long, or internally
inconsistent.

Checks:

- `duration_seconds` is present and positive.
- `total_frames` is present and positive.
- `duration_seconds`, frame count, and FPS are roughly consistent.
- episode duration is not outside configured hard bounds.
- episode duration is not a task-level statistical outlier.
- CSV row counts are close to metadata rows or expected frame count.
- image timestamp row counts are close to video frame count when available.
- image timestamp row counts and primary action row counts differ by no more
  than the configured absolute threshold, currently `3`.

Initial policy:

- impossible or clearly invalid duration: `fail`
- task-level statistical outlier only: `needs_review`
- small mismatch: `warning`
- large mismatch: `needs_review` or `fail`, depending on severity

Existing useful code:

- `Werkzeuge/check_episode_durations.py`

Progress checklist:

- [ ] Reuse duration extraction logic.
- [ ] Add task-level duration statistics.
- [ ] Add configurable hard min/max duration rules.
- [ ] Add row-count checks for CSV files.
- [ ] Generate unusual-duration report.
- [ ] Test on `NAS_Sample_Data/`.

## Phase 4: Motion Abnormality Checks

Status: prototype started

Purpose:

Filter abnormal robot and UMI motion data, including large velocities, jitter,
unwanted joint positions, sudden jumps, and physically impossible states.

Checks:

- joint position bounds
- joint velocity bounds
- acceleration bounds
- jerk bounds
- frame-to-frame joint position jump
- end-effector pose jump
- end-effector velocity spike
- gripper bounds
- NaN, Inf, missing, or non-numeric values
- repeated constant state for too long
- timestamp gaps in action/state CSVs
- non-monotonic or duplicated timestamps

Design requirements:

- Thresholds must be configured outside code.
- Robot-specific and task-specific rules must be supported.
- Checks should stream CSV rows instead of loading large files into memory.
- Findings must include concrete evidence such as column name, value,
  threshold, timestamp, and file path.

Example config shape:

```yaml
robots:
  arx5:
    joint_position_min: []
    joint_position_max: []
    joint_velocity_max: []
    joint_acceleration_max: []
  flexiv:
    joint_position_min: []
    joint_position_max: []
  ur:
    joint_position_min: []
    joint_position_max: []

tasks:
  assemble_the_battery:
    min_duration_seconds: 10
    max_duration_seconds: 300
```

Initial policy:

- physical joint limit violation: `fail`
- extreme velocity or jump above hard limit: `fail`
- statistical jitter or moderate spike: `needs_review`
- isolated borderline value: `warning` or `needs_review`

Progress checklist:

- [ ] Define quality rule config file.
- [x] Implement prototype CSV streaming reader.
- [x] Implement prototype joint position bound checks.
- [x] Implement prototype velocity spike checks.
- [ ] Implement acceleration and jerk checks.
- [x] Implement prototype timestamp checks.
- [x] Implement prototype EEF checks.
- [x] Run bounded sample analysis on `NAS_Sample_Data/`.
- [ ] Validate thresholds with sample data before enabling `fail` decisions.

Current prototype:

```text
Werkzeuge/analyze_motion_abnormalities.py
```

Related design note:

```text
Werkzeuge/docs/MOTION_ABNORMALITY_CHECKS.md
```

Sample result from 64 representative NAS sample episodes:

```text
pass: 47
needs_review: 15
fail_candidate: 2
findings: 251
```

Important lesson from the prototype: tactile state streams also use `x,y,z`
columns, so motion checks must use modality folder names as well as column names.
EEF checks should only apply to `eef_pose` modalities, not tactile state folders.

## Phase 5: Video And Synchronization Checks

Status: not started

Purpose:

Detect corrupt, blank, frozen, or unsynchronized image data without decoding all
videos by default.

Checks:

- MP4 can be opened by a reliable reader.
- sampled frames are readable.
- sampled frames are not all black or all white.
- sampled frames are not frozen across the episode.
- video duration roughly matches metadata and timestamps.
- timestamp start/end times are aligned across modalities.
- timestamp gaps are not excessive.

Efficiency rule:

Sample frames first. Full video decoding should only run for suspicious
episodes, selected audits, or explicit deep-check mode.

Progress checklist:

- [ ] Decide video backend.
- [ ] Implement sampled frame checks.
- [ ] Implement timestamp synchronization checks.
- [ ] Add deep-check mode.
- [ ] Test performance on copied data.

## Phase 6: Decision Engine

Status: not started

Purpose:

Convert per-check findings into one final status per episode.

Initial rules:

- any `critical` finding: `fail`
- repeated `major` findings: `fail`
- one `major` finding: `needs_review` or `fail`, depending on check type
- only `minor` findings: `warning`
- no important findings: `pass`

Required outputs:

```text
quality_report.csv
quality_summary.md
quality_findings.jsonl
```

Progress checklist:

- [ ] Define decision rules.
- [ ] Implement episode-level aggregation.
- [ ] Include reasons and top evidence in report.
- [ ] Add counts by task, operator, robot, and controller.
- [ ] Review decisions manually on `NAS_Sample_Data/`.

## Phase 7: Dry-Run Move Planner

Status: not started

Purpose:

Generate a move plan for failed episodes without touching data.

Required behavior:

- Plan only final `fail` episodes by default.
- Preserve relative paths below the source root.
- Refuse destination overwrite.
- Report total episodes, file count, and estimated bytes to move.
- Support filters for task, date, operator, and episode.
- Support allowlist-only planning for cautious rollout.

Output:

```text
move_plan.csv
move_plan.jsonl
```

Progress checklist:

- [ ] Define move plan schema.
- [ ] Implement dry-run planner.
- [ ] Add destination collision checks.
- [ ] Add source-root path safety checks.
- [ ] Test on local sample data.

## Phase 8: Safe Quarantine Mover

Status: not started

Purpose:

Move confirmed failed episodes to a separated quarantine location after dry-run
review.

Required behavior:

- Never delete source data directly.
- Never overwrite destination data.
- Preserve relative folder structure.
- Write a log before and after each move.
- Verify destination exists after move.
- Record enough information to support rollback.
- Support resume after interruption.
- Use a lock file to avoid concurrent movers touching the same data.
- Fail closed on permission, mount, or path safety errors.

Outputs:

```text
move_log.jsonl
rollback_plan.csv
move_summary.md
```

Progress checklist:

- [ ] Implement lock file.
- [ ] Implement safe move operation.
- [ ] Implement post-move verification.
- [ ] Implement resume behavior.
- [ ] Implement rollback plan generation.
- [ ] Test on copied local data only.

## Phase 9: NAS-Scale Hardening

Status: not started

Purpose:

Prepare the pipeline for server execution against a NAS-mounted dataset.

Requirements:

- Read-only scan mode.
- Configurable worker count.
- Conservative NAS I/O defaults.
- JSONL streaming outputs.
- Resume support.
- Task/date/operator filters.
- Clear run directory layout.
- Run manifest with command, config hash, code version, timestamp, and host.
- Log rotation or bounded log size.
- Human-readable summary after every run.

Suggested run directory:

```text
reports/
  run_YYYYMMDD_HHMMSS/
    run_manifest.json
    episode_inventory.csv
    episode_inventory.jsonl
    quality_findings.jsonl
    quality_report.csv
    quality_summary.md
    move_plan.csv
    move_log.jsonl
    rollback_plan.csv
```

Progress checklist:

- [ ] Add run directory management.
- [ ] Add run manifest.
- [ ] Add resume mode.
- [ ] Add worker limits.
- [ ] Add filters.
- [ ] Add server deployment notes.

## Validation Path

Status: not started

The pipeline should be promoted through these gates:

1. Run on `Test_Data/`.
2. Run on `NAS_Sample_Data/`.
3. Run on a copied subset of real NAS data.
4. Run read-only against NAS.
5. Generate NAS dry-run move plan.
6. Review failed samples manually.
7. Move a tiny allowlisted subset to quarantine.
8. Verify rollback information.
9. Expand slowly by task/date/operator.

Progress checklist:

- [ ] `Test_Data/` read-only scan passed.
- [ ] `NAS_Sample_Data/` read-only scan passed.
- [ ] copied NAS subset scan passed.
- [ ] NAS read-only scan passed.
- [ ] NAS dry-run move plan reviewed.
- [ ] tiny quarantine move test passed.
- [ ] rollback plan verified.

## Near-Term Next Steps

1. Use `QA_Pipeline/scripts/run_pipeline.py` as the core QA entry point.
2. Add a real `QA_Pipeline/README.md`.
3. Move hardcoded thresholds into `QA_Pipeline/configs/quality_rules.yaml`.
4. Extend calibration into a general statistical baseline builder.
5. Integrate `UMI_Data_Validation/ik_benchmark.py` as a QA phase.
6. Add dry-run quarantine planning.
7. Add safe quarantine moving with audit and rollback logs.
8. Run smoke tests on `Test_Data/`.
9. Run performance tests on `Test_Folder_For_DataPipeline/`.
10. Review false positives and update thresholds before NAS use.

## Progress Log

- 2026-06-04: Created implementation plan. No pipeline code has been started
  yet.
- 2026-06-04: Added read-only motion abnormality prototype and sample analysis
  note. Ran a bounded scan on `NAS_Sample_Data/`; results are useful for review
  but thresholds are not yet approved for automatic quarantine.
- 2026-06-08: Added `PIPELINE_INTEGRATION_PLAN.md` after reviewing the newly
  added `Test_Folder_For_DataPipeline/`, `QA_Pipeline/`, and
  `UMI_Data_Validation/` folders. `QA_Pipeline` is now the recommended core
  implementation; missing production layers are quarantine planning, safe moving,
  config management, statistical baselines, and UMI IK phase integration.
- 2026-06-09: Added a report-only Phase 2 video/action length alignment check in
  `QA_Pipeline`. It compares image `timestamps.csv` row counts against the
  primary action `data.csv` row count and emits `video_action_length_mismatch`
  with `status: fail` when the absolute difference is greater than the central
  config threshold. No quarantine move is performed.
- 2026-06-09: Added `STANDSTILL_TRIM_IMPLEMENTATION_PLAN.md` after reviewing
  `annotate_standstill.py`, sample episode CSV/video layout, and current Phase 5
  standstill findings. The proposed first step is report-only edge standstill
  trim planning, with synchronized CSV/video materialization deferred to a
  separate safe output-root workflow.
- 2026-06-09: Implemented `QA_Pipeline/scripts/plan_standstill_trim.py` as a
  read-only edge-standstill trim planner with CSV/JSONL/Markdown outputs,
  central config, and multiprocessing via `--workers`. On local samples,
  `NAS_Sample_Data` scanned 4,465 episodes in 15.236s with 8 workers, and
  `Test_Folder_For_DataPipeline` scanned 5,946 episodes in 8.518s with 8
  workers. Serial and parallel reports were byte-identical.
- 2026-06-09: Added `PIPELINE_MONITORING_AND_ISSUE_REPORTING_PLAN.md` after
  reviewing the current phase runner, SQLite state model, finding exports, and
  progress callbacks. The plan keeps existing final reports but adds live
  run-status files and append-only exact issue events.
- 2026-06-09: Implemented the first live-monitoring layer through
  `QA_Pipeline/scripts/pipeline/run_monitor.py` and wired it into
  `QA_Pipeline/scripts/run_pipeline.py`. Runs now create `run_status.json`,
  `phase_status.jsonl`, `issue_events.jsonl`, `episode_issues.csv`, and
  `live_summary.md` under `<output-dir>/runs/<run-id>/`, plus a run-local copy
  of final reports.
- 2026-06-09: Added `QA_Pipeline/scripts/generate_dashboard.py` and wired
  `dashboard.html` into normal and run-local final report export. The dashboard
  shows final episode status counts, issue breakdowns, filters, and exact issue
  records without requiring a web server.
- 2026-06-10: Added `QA_PIPELINE_USER_GUIDE.md`, explaining pipeline inputs,
  outputs, live status, dashboard usage, status decision rules, phase-by-phase
  pass/not-pass criteria, standstill trim planning, server/NAS workflow, and
  safety rules.
