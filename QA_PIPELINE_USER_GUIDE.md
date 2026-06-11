# QA Pipeline User Guide

Last updated: 2026-06-10

## Purpose

The QA pipeline checks robot and UMI episode folders and classifies each episode
as:

```text
pass
warning
needs_review
fail
```

It is currently a report-first system. It does not delete source data, move
episodes to quarantine, or cut videos during the main QA run. Those actions must
remain separate reviewed steps.

## Main Entry Point

Run the pipeline from the repository root:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path outputs/test_run/qa.db \
  --output-dir outputs/test_run \
  --phases 1,2,3 \
  --max-episodes 10 \
  --force-rerun \
  --run-id test-run-001
```

For server or larger local runs, use workers:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --db-path outputs/server_test/qa.db \
  --output-dir outputs/server_test \
  --phases 1,2,3,4,5 \
  --workers 8 \
  --force-rerun \
  --run-id server-test-001
```

`--workers` currently speeds up Phase 4 and Phase 5. Phase 1, Phase 2, and
Phase 3 are mostly sequential.

## Inputs

The input root can be one or more folders containing episode directories:

```text
<root>/<task>/<date>/<operator>/episode_...
```

The scanner looks for folders whose name starts with `episode_`. Each episode is
expected to contain metadata, modality folders, CSV files, image timestamps, and
videos, for example:

```text
episode_0001/
  metadata.json
  observation.state.joint_position/data.csv
  actions.joint_position/data.csv
  observation.image.third_view/timestamps.csv
  observation.image.third_view/video.mp4
```

## Outputs

The normal output directory contains:

```text
quality_report.csv
quality_findings.jsonl
quality_summary.md
dashboard.html
qa.db
```

When live monitoring is enabled, each run also creates:

```text
outputs/<run>/runs/<run-id>/
  run_status.json
  phase_status.jsonl
  issue_events.jsonl
  episode_issues.csv
  live_summary.md
  final/
    quality_report.csv
    quality_findings.jsonl
    quality_summary.md
    dashboard.html
```

`quality_report.csv` has one row per episode. `quality_findings.jsonl` and
`episode_issues.csv` have one row per exact issue.

## Dashboard

After a run, open:

```text
<output-dir>/dashboard.html
```

or the run-local copy:

```text
<output-dir>/runs/<run-id>/final/dashboard.html
```

To serve it from a server:

```bash
cd outputs/server_test
python3 -m http.server 8080
```

Then open:

```text
http://<server-ip>:8080/dashboard.html
```

The dashboard shows:

- total episode count;
- `fail`, `needs_review`, `warning`, and `pass` counts;
- top issue types;
- issue counts by phase;
- a filterable episode table;
- a filterable exact issue table.

## Live Status While Running

Watch the human-readable live summary:

```bash
watch -n 2 cat outputs/server_test/runs/server-test-001/live_summary.md
```

Watch exact issues as they are recorded:

```bash
tail -f outputs/server_test/runs/server-test-001/issue_events.jsonl
```

Read machine-friendly run status:

```bash
cat outputs/server_test/runs/server-test-001/run_status.json
```

## How Status Is Decided

Each phase emits zero or more findings. Each finding has:

```text
phase
check_name
severity
status
message
details
```

Severity values:

```text
critical
major
minor
info
```

Phase status is decided from findings using this logic:

1. Any `critical` finding -> phase status `fail`.
2. Any `major` finding whose status is `fail` -> phase status `fail`.
3. Any finding with status `needs_review` -> phase status `needs_review`.
4. Any remaining `major` or `minor` finding -> phase status `warning`.
5. No meaningful findings -> phase status `pass`.

Final episode status combines all completed phase statuses:

1. Any phase `fail` -> final status `fail`.
2. Else any phase `needs_review` -> final status `needs_review`.
3. Else any phase `warning` -> final status `warning`.
4. Else final status `pass`.

Important: if an episode fails an earlier completed phase, later phases are
skipped for that episode. This avoids wasting compute on episodes that are
already clearly unusable.

## Phase 1: Structure And Metadata

File:

```text
QA_Pipeline/scripts/pipeline/phase1_metadata.py
```

Purpose:

Check that an episode has the expected folder name, metadata, modality folders,
required files, and quality labels.

Pass cases:

- episode folder name starts with `episode_`;
- `metadata.json` exists and is valid JSON;
- required metadata fields are present and valid;
- metadata modalities have matching folders;
- required files exist and are non-empty;
- quality labels exist.

Main not-pass cases:

| Check | Meaning | Status effect |
| --- | --- | --- |
| `episode_folder_name` | Folder does not start with `episode_` | fail |
| `metadata_exists` / `metadata_valid_json` | Metadata missing or invalid | fail |
| `required_metadata_field` | Required metadata field missing/invalid | fail |
| `modality_folder_missing` | Metadata names a missing modality folder | fail |
| `required_modality_file_missing` | Required `data.csv`, `timestamps.csv`, or `video.mp4` missing | fail |
| `required_modality_file_empty` | Required file exists but is empty | fail |
| `parent_path_structure` | Path does not look like `<task>/<date>/<operator>/<episode>` | warning |
| `checksum_manifest_missing` | `.checksum_manifest` missing | warning |
| `quality_labels_missing` | `quality.labels` missing or empty | warning |

## Phase 2: Duration And Count Consistency

File:

```text
QA_Pipeline/scripts/pipeline/phase2_duration.py
```

Purpose:

Check metadata duration/frame consistency, CSV row counts, image timestamp row
counts, video/action length alignment, and duration outliers within task groups.

Pass cases:

- `duration_seconds` is positive;
- `total_frames` is positive;
- `duration_seconds * FPS` roughly matches `total_frames`;
- image timestamp row counts roughly match `total_frames`;
- state CSV row counts roughly match expected rows;
- image timestamp rows and primary action rows differ by no more than configured
  absolute threshold;
- duration is not an extreme task-level outlier.

Main not-pass cases:

| Check | Criteria | Status effect |
| --- | --- | --- |
| `duration_not_positive` | `duration_seconds` missing or not positive | fail |
| `total_frames_not_positive` | `total_frames` missing or not positive | fail |
| `duration_frames_fps_inconsistent` | `total_frames` differs from `duration_seconds * fps` by more than 10% | fail |
| `timestamps_unreadable` | Image `timestamps.csv` unreadable | fail |
| `timestamps_row_count_mismatch` | Image timestamp row count differs from `total_frames` by more than 10% | fail |
| `state_csv_row_count_mismatch` | State CSV rows differ from expected rows by more than 15% | warning |
| `video_action_length_mismatch` | Image timestamp rows and primary action rows differ by more than config threshold, default 3 | fail |
| `duration_task_outlier` | Duration IQR distance within task group is greater than 3 | needs_review |
| `duration_absolute_too_short` | Duration less than 20% of task median | fail |
| `duration_absolute_too_short` | Duration less than 40% of task median | needs_review |
| `duration_absolute_too_long` | Duration greater than 250% of task median | needs_review |

The video/action threshold is configured in:

```json
phase2_duration.length_alignment.max_video_action_difference
```

## Phase 3: Timestamp, FPS, And Frame Drop Checks

File:

```text
QA_Pipeline/scripts/pipeline/phase3_timestamp.py
```

Purpose:

Check image timestamp quality, frame drops, actual FPS, raw/processed timestamp
consistency, and cross-image-modality start/end alignment. State/action
timestamp checks are handled in Phase 5.

Pass cases:

- image timestamp files are readable;
- timestamps are strictly increasing;
- duplicate timestamp ratio is low or zero;
- frame drop ratio and consecutive drops are within configured thresholds;
- actual FPS is close to expected FPS;
- image modalities start and end within the alignment threshold.

Main not-pass cases:

| Check | Criteria | Status effect |
| --- | --- | --- |
| `timestamps_unreadable` | Timestamp source missing/unreadable | fail |
| `timestamps_not_monotonic` | >=5% violations | fail |
| `timestamps_not_monotonic` | >=1% and <5% violations | needs_review |
| `timestamps_not_monotonic` | <1% violations | warning |
| `duplicate_timestamps` | Same ratio rules as monotonic check | fail / needs_review / warning |
| `frame_drop_ratio` | Drop ratio exceeds threshold | fail |
| `frame_drop_consecutive` | Consecutive frame drops exceed threshold | fail |
| `abnormal_fps_loss` | Actual FPS lower than expected beyond threshold, default 10% | fail |
| `abnormal_fps_gain` | Actual FPS higher than expected beyond threshold, default 10% | warning |
| `timestamps_raw_inconsistency` | Raw and processed timestamp rows differ by more than 2 | warning |
| `modality_alignment_start` / `modality_alignment_end` | Start/end timestamp spread exceeds 500 ms | fail |
| `frequency_group_outlier` | Actual FPS is IQR outlier within task+robot group | needs_review |
| `consecutive_drops_outlier` | Max consecutive drops is IQR outlier, or exceeds fallback warning threshold in small groups | needs_review / warning |

Configured thresholds:

```json
phase3_timestamp.abnormal_fps.loss_fail_ratio = 0.10
phase3_timestamp.abnormal_fps.gain_warning_ratio = 0.10
phase3_timestamp.frame_drops.normal_video_drop_ratio_fail = 0.15
phase3_timestamp.frame_drops.tactile_video_drop_ratio_fail = 0.20
phase3_timestamp.frame_drops.max_consecutive_fail = 25
phase3_timestamp.frame_drops.max_consecutive_warn = 10
```

## Phase 4: Video Health

File:

```text
QA_Pipeline/scripts/pipeline/phase4_video.py
```

Purpose:

Open video files, inspect video metadata, sample frames, and detect obvious
visual corruption.

Dependency:

```text
opencv-python-headless
```

If OpenCV is not installed, Phase 4 stops before writing episode QA results.
This is an environment/configuration failure, not an episode-quality finding.

Pass cases:

- each video opens successfully;
- video frame count is readable and roughly matches metadata;
- video duration roughly matches metadata duration;
- resolution is compatible with metadata/config;
- sampled frames are not mostly black/white;
- sampled frames are not frozen;
- ARX wrist views are not both still.

Main not-pass cases:

| Check | Criteria | Status effect |
| --- | --- | --- |
| `video_not_openable` | `video.mp4` cannot be opened | fail |
| `video_frame_count_unreadable` | Frame count unavailable or <=0 | fail |
| `video_frame_count_mismatch` | Video frame count differs from metadata by more than 10% | fail |
| `video_duration_mismatch` | Video duration differs from metadata by more than 10% | warning |
| `video_resolution_mismatch` | Resolution differs from expected camera resolution | warning |
| `video_black_frames` / `video_white_frames` | Bad sampled frames found | needs_review, or fail if most samples are bad |
| `video_frozen` | Sampled frames appear frozen | fail |
| `both_wrist_views_still` | For ARX5, both wrist-view cameras appear still | fail |

## Phase 5: Robot State And Motion Reasonableness

File:

```text
QA_Pipeline/scripts/pipeline/phase5_robot_state.py
```

Purpose:

Check joint/action/state CSV data for non-numeric values, timestamp problems,
joint limits, gripper limits, gripper remap need, sudden steps, velocity,
acceleration, jitter, EEF jumps, and operator standstill.

Pass cases:

- numeric columns parse cleanly;
- timestamps are valid and increasing;
- joint and gripper values stay within configured robot ranges;
- per-frame steps, velocity, acceleration, and jitter stay under thresholds;
- no excessive operator standstill;
- EEF pose steps stay within configured threshold.

Main not-pass cases:

| Check | Meaning | Status effect |
| --- | --- | --- |
| `csv_not_parseable` | `data.csv` cannot be read/parsed | fail |
| `joint_nan_inf` | NaN, Inf, or unparseable values in motion columns | fail |
| `timestamps_missing_or_unparseable` | `timestamp_ms` missing or unusable | fail |
| `timestamps_not_monotonic` | Same ratio rules as Phase 3 | fail / needs_review / warning |
| `joint_out_of_limits` | Joint values exceed robot limit | needs_review |
| `gripper_out_of_limits` | Gripper values exceed robot limit | needs_review |
| `gripper_mean_too_low_remap_needed` | Mean gripper distance below threshold, default 0.005 m | needs_review |
| `joint_step_too_large` | Per-frame joint step too large | needs_review |
| `gripper_step_too_large` | Per-frame gripper step too large | needs_review |
| `joint_velocity_exceeded` | Joint velocity p99 exceeds threshold | needs_review |
| `joint_acceleration_high` | Acceleration p99 exceeds threshold | warning |
| `jitter_high` | Jitter score exceeds warning/fail thresholds | warning or fail |
| `operator_standstill` | Standstill segment beyond 4 second buffer | warning |
| `operator_standstill_excessive` | Total excess standstill exceeds 20% of episode duration | needs_review |
| `eef_position_step_too_large` | EEF position step exceeds threshold | needs_review |
| `joint_columns_not_detected` | Joint columns not detected | pass/info |

Robot-specific gripper limits can be overridden in:

```json
phase5_robot_state.robots
```

Current central config includes:

```json
aloha gripper range: 0.0 to 0.1 m
arx5 gripper range: 0.0 to 0.082 m
```

## Standstill Trim Planner

File:

```text
QA_Pipeline/scripts/plan_standstill_trim.py
```

This is not part of the main phase runner. It is a separate report-only planner
for detecting unwanted standstill at the beginning or end of episodes.

Run:

```bash
python3 QA_Pipeline/scripts/plan_standstill_trim.py \
  --roots Test_Data \
  --output-dir outputs/standstill_trim_test \
  --workers 8 \
  --progress
```

Outputs:

```text
standstill_trim_plan.csv
standstill_trim_plan.jsonl
standstill_trim_summary.md
```

Planner decisions:

| Decision | Meaning |
| --- | --- |
| `no_trim` | No eligible beginning/end standstill found |
| `trim_candidate` | Safe-looking edge trim candidate |
| `needs_review` | Trim candidate removes too much duration |
| `reject_too_short_after_trim` | Remaining episode would be too short |
| `missing_motion_source` | No configured motion source found |
| `invalid_timestamps` | Timestamp source is invalid |

The planner does not cut videos or rewrite CSVs. A separate materialization step
is still required for actual synchronized cutting.

## Recommended Server/NAS Workflow

1. Mount NAS read-only on the server.
2. Deploy the current repository to the server from the local repository root:

```bash
rsync -av \
  --exclude 'Test_Data/' \
  --exclude 'NAS_Sample_Data/' \
  --exclude 'Test_Folder_For_DataPipeline/' \
  ./ \
  xinzhi@192.168.50.209:~/DataPipeline/
```

3. Run a dry discovery:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --dry-run
```

4. Run a small smoke test:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --db-path outputs/server_smoke/qa.db \
  --output-dir outputs/server_smoke \
  --phases 1,2,3 \
  --max-episodes 10 \
  --workers 8 \
  --force-rerun \
  --run-id server-smoke-001
```

5. Open dashboard:

```bash
cd outputs/server_smoke
python3 -m http.server 8080
```

Then browse:

```text
http://<server-ip>:8080/dashboard.html
```

6. Review `fail` and `needs_review` episodes before running a larger test.
7. Run larger batches only after the smoke test results are understood.

## Practical Command Reference

Dry-run discovery:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py --roots Test_Data --dry-run
```

Run phases 1 to 3 on 10 episodes:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path outputs/test/qa.db \
  --output-dir outputs/test \
  --phases 1,2,3 \
  --max-episodes 10 \
  --force-rerun \
  --run-id test-001
```

Run all current phases with 8 workers:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path outputs/full/qa.db \
  --output-dir outputs/full \
  --phases 1,2,3,4,5 \
  --workers 8 \
  --force-rerun \
  --run-id full-001
```

Generate dashboard manually from an existing database:

```bash
python3 QA_Pipeline/scripts/generate_dashboard.py \
  --db-path outputs/full/qa.db \
  --output outputs/full/dashboard.html
```

## Safety Rules

- Run on copied samples or read-only NAS mounts first.
- Use `--max-episodes` for smoke tests.
- Review `dashboard.html`, `quality_report.csv`, and `quality_findings.jsonl`
  before any cleanup or quarantine step.
- Do not run cutting or quarantine actions on NAS source data until they have
  been validated locally and reviewed.
- Keep output directories separate from source episode folders.
- Use `--force-rerun` only when you intentionally want to recompute selected
  phases.

## Current Limitations

- The main QA pipeline reports classifications but does not move or delete data.
- The standstill trim planner reports trim candidates but does not yet cut
  videos or CSVs.
- Phase 4 requires OpenCV; without it, the pipeline exits before writing episode QA results.
- `--workers` currently accelerates Phase 4 and Phase 5, plus the separate
  standstill trim planner. Earlier phases are still mostly sequential.
- Some thresholds are calibrated from current sample data and should be reviewed
  before full NAS-scale enforcement.
