# Data Quality Pipeline Integration Plan

Last updated: 2026-06-08

## Goal

Build one safety-first pipeline that can process robot and UMI episodes at NAS
scale, keep good/usable data in place, and move bad/unusable data to a
quarantine area only after validated reports and dry-run review.

This plan integrates the three newly added folders with the existing repository
tools.

## Newly Added Folders

### `Test_Folder_For_DataPipeline/`

Purpose:

- Local performance and behavior test data for the pipeline.
- Contains larger task episode sets than `Test_Data/`.
- Includes real-robot and UMI-style data roots:
  - `Data_Robots/`
  - `Data_UMI/`

Usage:

- Primary local stress-test root before NAS deployment.
- Use this to test runtime, memory, report size, resume behavior, and false
  positive rate.
- This folder is data, not source code, and must stay ignored by git.

### `QA_Pipeline/`

Purpose:

- Main data quality checking implementation.
- Contains a multi-phase SQLite-backed QA pipeline.
- Already has reports in `QA_Pipeline/outputs/`, which show it has processed
  large local samples.

Important files:

```text
QA_Pipeline/scripts/run_pipeline.py
QA_Pipeline/scripts/pipeline/qa_core.py
QA_Pipeline/scripts/pipeline/phase1_metadata.py
QA_Pipeline/scripts/pipeline/phase2_duration.py
QA_Pipeline/scripts/pipeline/phase3_timestamp.py
QA_Pipeline/scripts/pipeline/phase4_video.py
QA_Pipeline/scripts/pipeline/phase5_robot_state.py
QA_Pipeline/scripts/calibrate_phase5.py
QA_Pipeline/docs/QA_DECISIONS.md
```

Current implemented phases:

| Phase | Module | Purpose |
| --- | --- | --- |
| 1 | `phase1_metadata.py` | Structure, metadata, modalities, required files, checksum manifest, quality labels |
| 2 | `phase2_duration.py` | Duration, total frame count, FPS consistency, row counts, duration outliers |
| 3 | `phase3_timestamp.py` | Timestamp monotonicity, duplicates, gaps, frame drops, frequency, modality alignment |
| 4 | `phase4_video.py` | Video openability, frame count, duration, resolution, black/white/frozen sampled frames |
| 5 | `phase5_robot_state.py` | Joint/gripper limits, joint steps, velocity, acceleration, jitter, standstill, EEF pose steps |

Supporting behavior:

- Discovers any folder beginning with `episode_`.
- Stores episode state in SQLite.
- Can resume by skipping completed phases.
- Skips later phases for episodes that already failed earlier phases.
- Exports:
  - `quality_report.csv`
  - `quality_findings.jsonl`
  - `quality_summary.md`

Usage:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Folder_For_DataPipeline/Data_Robots \
  --db-path /tmp/qa_pipeline_test/qa_pipeline.db \
  --output-dir /tmp/qa_pipeline_test/reports \
  --max-episodes 100 \
  --workers 4
```

Dry-run discovery only:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Folder_For_DataPipeline/Data_Robots \
  --dry-run
```

Calibration:

```bash
python3 QA_Pipeline/scripts/calibrate_phase5.py \
  --roots Test_Folder_For_DataPipeline/Data_Robots \
  --robot arx5 \
  --output /tmp/qa_pipeline_test/phase5_calibration_arx5.json
```

### `UMI_Data_Validation/`

Purpose:

- Specialized inverse-kinematics validation for UMI `eef_pose` streams.
- Checks whether UMI EEF trajectories are executable by a robot model.

Important files:

```text
UMI_Data_Validation/ik_benchmark.py
UMI_Data_Validation/requirements.txt
UMI_Data_Validation/deploy_ubuntu.sh
```

Current behavior:

- Reads `observation.state.eef_pose/data_raw.csv` when present, otherwise
  falls back to `data.csv`.
- Loads left/right EEF pose streams.
- Converts 6D rotation representation to rotation matrices and quaternions.
- Uses PyBullet inverse kinematics against robot URDFs.
- Current robot pool includes Franka Panda and UR5e, with README noting right
  stream validation against `Universal_Robots_UR5e`.

Usage:

```bash
python3 UMI_Data_Validation/ik_benchmark.py \
  --sample-root <root-containing-episode-folders>
```

Integration role:

- Should become a later QA pipeline phase, for example Phase 6.
- Should only run on UMI episodes or episodes with UMI-style `eef_pose` streams.
- Should output structured findings instead of only printing text.

## Existing Supporting Tools

### `Werkzeuge/check_episode_durations.py`

Useful for duration-only analysis and unusual duration reports. Its logic is
partly superseded by `QA_Pipeline/scripts/pipeline/phase2_duration.py`, but it
remains useful for quick independent checks.

### `Werkzeuge/analyze_motion_abnormalities.py`

Read-only prototype for motion abnormality detection. Its main lesson has
already been folded into the plan: motion checks must be modality-aware, because
tactile streams can also have `x,y,z` columns.

Reference:

```text
Werkzeuge/docs/MOTION_ABNORMALITY_CHECKS.md
```

## Recommended Unified Architecture

The unified pipeline should use `QA_Pipeline` as the core and add missing
production layers around it.

```text
data root
  -> episode discovery
  -> QA phases 1-5
  -> UMI IK phase 6
  -> statistical calibration/baseline phase
  -> final decision engine
  -> dry-run quarantine planner
  -> reviewed safe mover
  -> audit and rollback reports
```

## Status Flow

Use the existing status model:

```text
pass
warning
needs_review
fail
```

Operational meaning:

- `pass`: usable for training.
- `warning`: usable, but with minor known issues.
- `needs_review`: not automatically moved; requires human review.
- `fail`: disqualified and eligible for quarantine after dry-run review.

Recommended training policy:

```text
training-ready set = pass + optionally warning
not-training-ready = needs_review + fail
auto-quarantine candidate = fail only
manual-review queue = needs_review
```

## Current QA Coverage

The QA pipeline already covers:

- metadata existence and validity;
- folder and modality structure;
- required files and non-empty checks;
- metadata duration/frame/FPS consistency;
- image timestamp and state CSV row counts;
- task-level duration outliers;
- duplicate and non-monotonic timestamps;
- frame drops and consecutive frame-drop outliers;
- modality start/end alignment;
- video openability and sampled video health;
- black/white/frozen sampled frames;
- joint/gripper bounds;
- joint/gripper step spikes;
- reported and estimated velocity;
- acceleration;
- jitter;
- long standstill segments;
- EEF pose step checks.

## Main Gaps To Fill

The current detailed gap analysis for `Documents/数据质检.pdf` Step 2 and Step 3
is tracked in:

```text
DATA_QUALITY_STEPS_2_3_GAP_ANALYSIS.md
```

### 1. Quarantine Planner

Missing today.

Need a command that reads `quality_report.csv` and creates a move plan without
moving anything.

Output:

```text
move_plan.csv
move_plan.jsonl
```

Required columns:

```text
source_path
destination_path
status
severity
reasons
source_root
quarantine_root
file_count
total_bytes
destination_exists
planned_at
```

Rules:

- Plan only `status == fail` by default.
- Preserve the relative path under the source root.
- Refuse destination overwrites.
- Do not plan anything outside the configured source root.
- Support filters: task, date, operator, robot, controller.
- Support allowlist mode for small production rollout.

### 2. Safe Quarantine Mover

Missing today.

Need a separate command that consumes an approved `move_plan.csv`.

Outputs:

```text
move_log.jsonl
rollback_plan.csv
move_summary.md
```

Safety rules:

- Never delete data directly.
- Never overwrite destination data.
- Create a lock file before moving.
- Log before and after every episode move.
- Verify destination exists after move.
- Verify source is absent only after successful move.
- Support resume.
- Support rollback planning.
- Fail closed on path, permission, mount, or checksum errors.

### 3. UMI IK Integration

`UMI_Data_Validation/ik_benchmark.py` should be converted into a QA phase.

Target module:

```text
QA_Pipeline/scripts/pipeline/phase6_umi_ik.py
```

Required behavior:

- Run only when robot/controller/task indicates UMI or when UMI-style EEF pose
  files are present.
- Load `data_raw.csv` first, then `data.csv`.
- Support left/right streams independently.
- Produce `Finding` records rather than console-only output.
- Store metrics:
  - frames checked;
  - side;
  - robot model tested;
  - first failed timestamp;
  - position error;
  - orientation error.
- Use configurable tolerances.
- Start as `needs_review`, not automatic `fail`, until validated on more data.

### 4. Statistical Threshold Baselines

Phase 5 currently has hardcoded robot configs and `calibrate_phase5.py` can
derive suggested thresholds. This should become a first-class baseline system.

Target output:

```text
configs/baselines/motion_baseline_<version>.json
```

Baseline should be grouped by:

```text
task
robot
controller
modality
column
metric
```

Metrics:

- duration;
- joint position min/max;
- joint step p95/p99/p99.9;
- joint velocity p95/p99/p99.9;
- acceleration p95/p99/p99.9;
- gripper step/velocity;
- EEF step/velocity;
- timestamp gap;
- frame-drop rate.

Use robust thresholds:

```text
warning = p99
needs_review = p99.5 or p99.9
fail_candidate = p99.9 * safety_factor
hard fail = physical limit violation
```

Statistical outliers should start as `needs_review`, not automatic `fail`.

### 5. Config Management

Move thresholds and phase settings out of code.

Target:

```text
QA_Pipeline/configs/quality_rules.yaml
```

Should include:

- robot physical limits;
- robot velocity/acceleration limits;
- task-specific duration rules;
- video thresholds;
- timestamp thresholds;
- UMI IK tolerances;
- phase enable/disable switches;
- quarantine policy.

### 6. Production Run Layout

Do not write production outputs inside `QA_Pipeline/outputs/`. Use explicit run
directories.

Recommended layout:

```text
runs/
  run_YYYYMMDD_HHMMSS/
    run_manifest.json
    qa_pipeline.db
    quality_report.csv
    quality_findings.jsonl
    quality_summary.md
    move_plan.csv
    move_log.jsonl
    rollback_plan.csv
```

`run_manifest.json` should record:

- command;
- code commit hash;
- config hash;
- source roots;
- output paths;
- hostname;
- user;
- start/end time;
- worker count;
- phase list;
- dry-run flag.

## Recommended Processing Workflow

### Stage 1: Local Smoke Test

Run small subsets.

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path /tmp/dp_smoke/qa_pipeline.db \
  --output-dir /tmp/dp_smoke/reports \
  --max-episodes 20 \
  --workers 1 \
  --force-rerun
```

Expected result:

- pipeline completes;
- reports are generated;
- no data is moved;
- no generated report is committed.

### Stage 2: Local Performance Test

Use the new test folder.

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Folder_For_DataPipeline/Data_Robots \
  --db-path /tmp/dp_perf/qa_pipeline.db \
  --output-dir /tmp/dp_perf/reports \
  --workers 4 \
  --force-rerun
```

Also run UMI root separately:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Folder_For_DataPipeline/Data_UMI \
  --db-path /tmp/dp_perf_umi/qa_pipeline.db \
  --output-dir /tmp/dp_perf_umi/reports \
  --workers 4 \
  --force-rerun
```

Measure:

- episodes/hour;
- peak disk output size;
- memory use;
- false positive rate;
- top failure reasons;
- phase timing.

### Stage 3: Threshold Calibration

Build robot/task baselines from reviewed-good data.

Start with the existing calibration script:

```bash
python3 QA_Pipeline/scripts/calibrate_phase5.py \
  --roots Test_Folder_For_DataPipeline/Data_Robots \
  --robot arx5 \
  --output /tmp/dp_calibration/phase5_arx5.json
```

Then extend it to:

- support `--quality-label 完全正常`;
- support `--final-status pass`;
- generate per-task/per-robot/per-controller baselines;
- include EEF and timestamp metrics, not only joint/gripper metrics.

### Stage 4: Read-Only NAS Trial

Run only report generation on NAS.

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/Data \
  --db-path /server/runs/run_YYYYMMDD_HHMMSS/qa_pipeline.db \
  --output-dir /server/runs/run_YYYYMMDD_HHMMSS \
  --workers 4
```

Rules:

- no quarantine;
- no write under NAS source root;
- reports reviewed manually.

### Stage 5: Dry-Run Quarantine Plan

After reports are trusted, generate a plan only.

Example future command:

```bash
python3 QA_Pipeline/scripts/plan_quarantine.py \
  --quality-report /server/runs/run_YYYYMMDD_HHMMSS/quality_report.csv \
  --source-root /mnt/nas/Data \
  --quarantine-root /mnt/nas/Data_Quarantine \
  --status fail \
  --output /server/runs/run_YYYYMMDD_HHMMSS/move_plan.csv \
  --dry-run
```

Review:

- count by task/operator/robot;
- top reasons;
- sample failed episodes manually;
- destination collision report.

### Stage 6: Tiny Allowlisted Move

Move only a small approved subset.

Example future command:

```bash
python3 QA_Pipeline/scripts/move_to_quarantine.py \
  --move-plan /server/runs/run_YYYYMMDD_HHMMSS/move_plan.csv \
  --allow-task Some_Task \
  --allow-date 20260601 \
  --max-episodes 10 \
  --confirm
```

Verify:

- destination contents;
- source removal only after successful move;
- logs;
- rollback plan.

### Stage 7: Gradual NAS Rollout

Increase scope slowly:

1. one operator;
2. one date;
3. one task;
4. one robot type;
5. full root.

Keep `needs_review` in place. Only `fail` moves automatically.

## How Modules Should Work Together

### Primary Flow

1. `QA_Pipeline/scripts/run_pipeline.py` discovers episodes and runs phases.
2. `qa_core.py` stores states/findings in SQLite.
3. Phase modules write structured findings.
4. `run_pipeline.py` exports reports.
5. `plan_quarantine.py` reads `quality_report.csv` and writes `move_plan.csv`.
6. `move_to_quarantine.py` executes approved moves and writes audit logs.

### UMI-Specific Flow

1. Phase 1-5 run normally.
2. Phase 6 runs IK validation for UMI episodes.
3. IK findings affect final status:
   - unreachable trajectory: `needs_review` first;
   - confirmed impossible after calibration: `fail`;
   - parser/rotation errors: `fail` if essential.

### Calibration Flow

1. Run QA on local performance data.
2. Select trusted `pass` or `完全正常` episodes.
3. Build statistical baseline.
4. Review thresholds.
5. Re-run QA using baseline.
6. Promote only reviewed threshold versions to NAS.

## Immediate Implementation Tasks

1. Add a real `QA_Pipeline/README.md`.
2. Add `QA_Pipeline/configs/quality_rules.yaml`.
3. Refactor Phase 5 hardcoded thresholds to read config.
4. Extend `calibrate_phase5.py` into a general baseline builder.
5. Convert `UMI_Data_Validation/ik_benchmark.py` into
   `phase6_umi_ik.py`.
6. Add `plan_quarantine.py`.
7. Add `move_to_quarantine.py`.
8. Add run manifest generation.
9. Run smoke test on `Test_Data/`.
10. Run performance test on `Test_Folder_For_DataPipeline/`.
11. Review false positives and update thresholds.

## Quarantine Safety Checklist

Before any production NAS move:

- [ ] QA report reviewed.
- [ ] `needs_review` excluded from auto-move.
- [ ] Move plan generated.
- [ ] Destination collision check passed.
- [ ] Relative paths preserved.
- [ ] Source root and quarantine root are different.
- [ ] No path escapes source root.
- [ ] Tiny allowlisted move tested.
- [ ] Move log generated.
- [ ] Rollback plan generated.
- [ ] NAS permissions verified.
- [ ] Server has enough disk/network bandwidth.

## Git Hygiene

Keep these out of git:

- `Test_Data/`
- `Test_Folder_For_DataPipeline/`
- `NAS_Sample_Data/`
- `QA_Pipeline/outputs/`
- local virtual environments;
- generated reports;
- nested `.git/` folders;
- local UMI validation samples.

Only source code, configs, documentation, and small non-sensitive examples should
be committed.
