# QA Pipeline User Guide

Last updated: 2026-06-15

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

Enter the repository and activate the virtual environment first:

```bash
cd /home/xinzhi/DataPipeline
source datapipeline-env/bin/activate
```

Then run the pipeline from the repository root:

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

For server or larger local runs, use workers conservatively. On an 8-core
server, do not use all cores. Start with `--workers 3` or `--workers 4`, and
let the resource guard pause the run if load or available memory becomes unsafe:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260611 \
  --db-path outputs/qa_20260611/qa_pipeline.db \
  --output-dir outputs/qa_20260611 \
  --phases 1 \
  --workers 4 \
  --batch-size 5000 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 0.65 \
  --resource-check-interval 15 \
  --force-rerun
```

`--workers` is passed to all registered phases. Phases 1 through 5 have
parallel execution paths. Phase 6 accepts the argument but currently processes
UMI episodes one by one because each UMI episode may run heavier video and
trajectory processing. The resource guard is enabled by default: it limits
workers to a safe value, pauses when host load or available memory is unsafe,
and waits until the server recovers. Set `--resource-max-wait-seconds` to a
positive value only when you intentionally want a timeout; keep the default `0`
for long server runs that should not stop because of temporary overload.

If a resource-guard stop happens inside a phase, the runner retries that phase
by default. Completed episode states are saved to SQLite as each episode
finishes, so a retry continues with unfinished episodes in that phase.

```text
--resource-error-retries          default 3
--resource-retry-delay-seconds    default 30
```

`--batch-size` limits how many episode states are loaded into memory at once.
For example, 10000 episodes with `--batch-size 1000` are processed in 10
batches instead of one large in-memory set. After each batch, the pipeline
releases the per-batch state list and runs Python garbage collection. Batch
mode does not delete final reports, SQLite records, dashboards, or Phase 6 UMI
processed outputs. On a memory-constrained server, start with `--batch-size 500`
or `--batch-size 1000`; use larger batches only after checking memory behavior.

`--batch-mode` controls how batches are formed:

```text
auto         default; uses group-aware batches when Phase 2 or 3 is selected
fixed        simple fixed-size batches
group-aware  keeps Phase 2/3 outlier groups complete inside a batch
```

Group-aware batching prevents Phase 2/3 outlier checks from running on partial
groups. Phase 2 groups by task, so complete task groups are kept together.
Phase 3 groups by task and robot, so complete task+robot groups are kept
together when Phase 2 is not selected. If one complete group is larger than
`--batch-size`, the group runs as an oversized batch and the pipeline prints a
warning. This is intentional: correct group statistics are preferred over
splitting the group.

Before detailed phase checks, the runner applies the default quality-label
filter. Only episodes whose `metadata.json` contains `quality.labels` with
`完全正常` are processed. Episodes with other collector labels are skipped and
summarized on the console. This filter applies to every phase because it runs
before state loading and phase dispatch. Use:

```text
--quality-label <label>              process a different label
--disable-quality-label-filter       process all labels for a full audit
```

With `--force-rerun`, the selected phases are recomputed for the selected
episodes. Existing episode rows are updated, and findings for the same
`episode_path + phase` are replaced. At the start of each run, the database is
also pruned to the current selected episode set after `--roots`, `--date`,
`--task`, quality-label filtering, and `--max-episodes`. This prevents stale
rows from earlier filters from appearing in the current dashboard.

To continue an interrupted run, reuse the same `--db-path` and `--output-dir`
and do not pass `--force-rerun`. The current resume path still scans the input
root and loads matching episode states before it can skip completed work, so
large NAS roots may spend time rediscovering episodes even though previous
phase results are already in SQLite. A future DB-resume mode should avoid this
full discovery step.

Note: Phases 2 and 3 include group-level outlier checks. In batch mode, those
group checks are computed within each batch. Use the default `--batch-mode auto`
or explicit `--batch-mode group-aware` so these phases do not split their
outlier groups across batches. Phase 1 file-integrity checks are not affected
by this.

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

Hidden directories found during discovery, such as NAS or sync-tool temporary
folders named `.fr-*`, are skipped instead of being interpreted as episode
content. They are still recorded as `hidden_directory_skipped` findings in live
and final reports.

## Outputs

The normal output directory contains:

```text
quality_report.csv
quality_findings.jsonl
quality_summary.md
dashboard.html
dashboard_data.json
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
    dashboard_data.json
```

The live dashboard now runs as a separate process. Start it in another terminal
or tmux pane while the pipeline is running, using the same DB and output
directory:

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/<run>/qa_pipeline.db \
  --output-dir outputs/<run> \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

This process writes and refreshes:

```text
outputs/<run>/dashboard.html
outputs/<run>/dashboard_data.json
```

`dashboard.html` is a stable page shell; current data is written to
`dashboard_data.json`. When served over HTTP, the browser polls that JSON and
updates the page in place, so automatic refresh should not show a full blank
page. `live_dashboard.py --interval` controls the updater interval. The main
pipeline's `--live-dashboard-interval` only controls the suggested dashboard
command printed at run start. `--port 0` updates files without serving HTTP;
`--once` writes one snapshot and exits. When opened directly with `file://`,
browser security prevents JSON fetch; serve the output directory with
`live_dashboard.py --port <port>` or another HTTP server.

`quality_report.csv` has one row per episode. `quality_findings.jsonl` and
`episode_issues.csv` have one row per exact issue. Excel is not generated by
normal pipeline runs; generate it manually only when needed.

## Dashboard

During or after a run, open:

```text
<output-dir>/dashboard.html
```

After completion, the main pipeline also writes a final run-local copy to
`<output-dir>/runs/<run-id>/final/dashboard.html`.

To update and serve it from a server, run this in a separate terminal or tmux
pane:

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/qa_20260611/qa_pipeline.db \
  --output-dir outputs/qa_20260611 \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

Then open:

```text
http://<server-ip>:1234/dashboard.html
```

Any free port can be used. For example, if port 8080 is occupied, use 1234. If
the port is not directly reachable from the local machine, forward it with SSH:

```bash
ssh -L 1234:localhost:1234 xinzhi@192.168.50.209
```

Then open `http://localhost:1234/dashboard.html`.

The dashboard shows:

- total episode count;
- `fail`, `needs_review`, `warning`, and `pass` counts;
- top issue types;
- issue counts by phase;
- a filterable episode table;
- a filterable exact issue table.

## Live Status While Running

Show the latest run's live summary without typing the run ID:

```bash
python3 QA_Pipeline/scripts/qa_status.py --output-dir outputs/qa_20260611
```

Refresh continuously:

```bash
python3 QA_Pipeline/scripts/qa_status.py --output-dir outputs/qa_20260611 --watch
```

Show a specific run:

```bash
python3 QA_Pipeline/scripts/qa_status.py --output-dir outputs/qa_20260611 --run-id server-test-001
```

The main pipeline writes `latest_run.txt` under the output directory. The status
helper reads that pointer and finds the latest run automatically. To inspect
exact issue events, use `cat outputs/qa_20260611/latest_run.txt` to find the run
directory, then open `issue_events.jsonl`.

The HTML dashboard exists from the beginning of the independent dashboard
process and refreshes while that process is running:

```bash
ls outputs/qa_20260611/dashboard.html
```

The pipeline prints progress during discovery filtering, state loading, batch
planning, and phase execution. Phase progress includes elapsed time, rough ETA,
and processing rate so long NAS runs do not appear stuck after discovery.

## Excel Reports

Excel is optional and manual-only. The normal pipeline does not generate
`quality_report.xlsx`, so large runs can finish without spending memory and CPU
on a workbook that may not be needed. When generated manually, the workbook is
easier to share with non-technical reviewers than CSV/JSONL. It contains:

- `Summary`: total episodes, status counts, finding severity counts;
- `Episodes`: one row per episode;
- `Findings`: one row per non-pass finding with details;
- `Issue Counts`: counts by check name;
- `Task Status`: status counts per task.

To convert an existing SQLite result database into Excel without rerunning QA:

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output outputs/qa_20260612_phase1_5/quality_report.xlsx
```

This requires `openpyxl` in `datapipeline-env`. Manual export has a safety limit
for very large runs; by default it skips databases over 100,000 episodes unless
`QA_EXCEL_MAX_EPISODES=0` is set deliberately.

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
already clearly unusable. Use `--continue-after-fail` only when you explicitly
want later phases to run even after an earlier phase has failed.

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
| `action_modality_singular_name` | Modality/folder uses `action.*` instead of `actions.*`; fix item is reported | warning |
| `unknown_modality_detected` | Unknown modality names are reported for review | pass/info by default |
| `task_robot_mismatch` | Metadata/name/path robot source conflicts with robot token in task folder | fail in current Phase 1 |

`observation.image.flow_*` modalities are intentionally ignored by Phase 1 file
integrity checks. Their presence or absence does not affect pass/fail status.

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
| `state_csv_row_count_mismatch` | Non-tactile state CSV rows differ from expected rows by more than 15% | warning |
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
| `consecutive_drops_outlier` | Max consecutive drops is IQR outlier, or exceeds fallback warning threshold in small groups | needs_review / warning |

Configured thresholds:

```json
phase3_timestamp.abnormal_fps.loss_fail_ratio = 0.10
phase3_timestamp.abnormal_fps.gain_warning_ratio = 0.10
phase3_timestamp.frame_drops.normal_video_drop_ratio_fail = 0.10
phase3_timestamp.frame_drops.tactile_video_drop_ratio_fail = 0.15
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

Performance note:

Phase 4 is usually the slowest NAS phase. For every image `video.mp4`, it opens
the MP4 with OpenCV, reads video properties, then seeks to and decodes up to 8
sample frames. Random seeking inside compressed MP4 files over NAS is expensive
and can raise Linux load average through I/O wait. If Phase 4 is slow or trips
the resource guard, run it separately with fewer workers and a higher load
threshold:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260612 \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output-dir outputs/qa_20260612_phase1_5 \
  --phases 4,5 \
  --workers 2 \
  --batch-size 500 \
  --batch-mode fixed \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 1.20 \
  --resource-check-interval 60 \
  --resource-max-wait-seconds 0 \
  --resource-error-retries 5 \
  --resource-retry-delay-seconds 20
```

Do not add `--force-rerun` when continuing from a stopped run.

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
  --workers 3 \
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
2. For long runs, use a persistent terminal session on the server:

```bash
tmux new -s qa_verified
cd /home/xinzhi/DataPipeline
source datapipeline-env/bin/activate
```

This keeps the pipeline running if VS Code SSH disconnects or the local PC
freezes. Detach with `Ctrl-b d`, and later resume with
`tmux attach -t qa_verified`.

3. Deploy the current repository to the server from the local repository root:

```bash
rsync -av \
  --exclude 'Test_Data/' \
  --exclude 'NAS_Sample_Data/' \
  --exclude 'Test_Folder_For_DataPipeline/' \
  ./ \
  xinzhi@192.168.50.209:~/DataPipeline/
```

4. Run a dry discovery:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --dry-run
```

5. Run a small smoke test:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260611 \
  --db-path outputs/server_smoke/qa.db \
  --output-dir outputs/server_smoke \
  --phases 1 \
  --max-episodes 1000 \
  --workers 3 \
  --batch-size 500 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 0.65 \
  --force-rerun \
  --run-id server-smoke-001
```

6. Open the dashboard from a separate terminal or tmux pane:

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/server_smoke/qa.db \
  --output-dir outputs/server_smoke \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

Then browse:

```text
http://<server-ip>:1234/dashboard.html
```

7. Review `fail` and `needs_review` episodes before running a larger test.
8. Run larger batches only after the smoke test results are understood.

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

Run all current phases with conservative server parallelism:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260611 \
  --db-path outputs/full/qa.db \
  --output-dir outputs/full \
  --phases 1,2,3,4,5,6 \
  --workers 3 \
  --batch-size 500 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 0.65 \
  --force-rerun \
  --run-id full-001
```

Generate one dashboard snapshot from an existing database:

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/full/qa.db \
  --output-dir outputs/full \
  --once \
  --port 0
```

Generate Excel manually from an existing database:

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/full/qa.db \
  --output outputs/full/quality_report.xlsx
```

## Safety Rules

- Run on copied samples or read-only NAS mounts first.
- Use `--max-episodes` for smoke tests.
- Review `dashboard.html`, `quality_report.csv`, and `quality_findings.jsonl`
  before any cleanup or quarantine step. Generate Excel separately only when
  needed.
- Do not run cutting or quarantine actions on NAS source data until they have
  been validated locally and reviewed.
- Keep output directories separate from source episode folders.
- Use `--force-rerun` only when you intentionally want to recompute selected
  phases. It replaces findings for the selected episode and phase, but it does
  not delete records for episodes outside the current filters.
- To resume an interrupted run, reuse the same database/output directory and
  omit `--force-rerun`.
- Do not use all CPU cores on an 8-core/16GB server. Start with `--workers 2`
  and raise it only after checking load and memory.
- Use `--batch-size 500` or `--batch-size 1000` for large NAS date runs so the
  pipeline does not load all selected episode states at once.
- Keep the default `--batch-mode auto` for Phase 2/3 runs. It avoids splitting
  task or task+robot outlier groups across batches.
- The resource guard pauses on unsafe load or memory. By default,
  `--resource-max-wait-seconds 0` means it waits indefinitely until the server
  recovers. Use a positive timeout only for fail-fast test runs.

## Current Limitations

- The main QA pipeline reports classifications but does not move or delete data.
- The standstill trim planner reports trim candidates but does not yet cut
  videos or CSVs.
- Phase 4 requires OpenCV; without it, the pipeline exits before writing episode QA results.
- Current resume still discovers episodes from the input root before skipping
  completed work from SQLite. This can be slow on large NAS roots.
- `--workers` is passed to all registered phases. Phases 1 through 5 can use
  multiprocessing; Phase 6 currently processes UMI episodes sequentially.
  The resource guard may lower the effective worker count to protect the
  server.
- Some thresholds are calibrated from current sample data and should be reviewed
  before full NAS-scale enforcement.
