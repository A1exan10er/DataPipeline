# DataPipeline

This folder collects robot data examples, NAS sample datasets, reference
documents, and small Python utilities for checking and maintaining data
collection episodes.

The data comes from real robots and UMI devices. UMI is a human-operated device
that mimics robot movement while recording synchronized movement and image data.
These datasets are intended for robot-learning workflows such as VTLA, VLA,
world models, reinforcement learning, imitation learning, and related fields.

This is not a packaged application. The scripts are intended to be run directly
against a data root such as this folder, a NAS-mounted dataset, or a single
episode.

## Top-Level Contents

```text
DataPipeline/
  NAS_Sample_Data/       Sample task folders copied from NAS
  Test_Data/             Smaller local test episodes
  Test_Folder_For_DataPipeline/
                         Larger local performance-test episode data
  QA_Pipeline/           Main multi-phase data quality checking pipeline
  UMI_Data_Validation/   UMI EEF-pose inverse-kinematics validation code
  Documents/             Data format reference PDFs
  Werkzeuge/             Extra documentation and data quality tools
  *.py                   Maintenance scripts
  run_cleanup.sh         Cron-friendly cleanup wrapper
```

## Expected Data Layout

The scripts assume data is organized in this shape:

```text
<root>/
  <task>/
    <date>/
      <operator>/
        episode_0001/
          metadata.json
          meta/episode.json
          observation.state.joint_position/data.csv
          observation.state.joint_velocity/data.csv
          observation.state.eef_pose/data.csv
          observation.state.gripper/data.csv
          observation.image.<camera>/video.mp4
          observation.image.<camera>/timestamps.csv
          actions.joint_position/data.csv
```

Episode folders have two observed naming styles:

```text
episode_0029
episode_0085_20260428-114939_wangyong_arx5_none
```

The extended form usually records episode index, collection timestamp, operator,
robot type, and controller/source.

## NAS Sample Data

`NAS_Sample_Data/` contains task folders copied from NAS. These are collected
demonstration datasets from real robots and UMI devices. Each task folder is
organized by collection date, operator, and episode.

Current task folders include:

- `Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_UMI`
- `Bind_and_secure_the_socks_UMI`
- `Folding_trousers_ARX`
- `assemble_bactory_umi`
- `assemble_the_battery`
- `classify_the_battery_ARX`
- `put_cups_in_line_flexiv`
- `stack_3D_printed_waste_parts_UMI`

The sample includes different robot and control sources, visible in folder names
and metadata, such as UMI, ARX, Flexiv, UR, GELLO, Spacemouse, and direct robot
control. Typical episode contents include:

- `metadata.json` and `meta/episode.json`
- action streams such as `actions.joint_position/data.csv` or
  `actions.eef_pose/data.csv`
- state streams such as `observation.state.joint_position/data.csv`,
  `observation.state.eef_pose/data.csv`, `observation.state.gripper/data.csv`,
  and tactile state CSVs
- camera streams such as `observation.image.<camera>/video.mp4` and
  `timestamps.csv`
- optional raw files, checksum manifests, optical-flow videos, and timing logs

More detailed NAS notes are maintained in
`Werkzeuge/docs/NAS_SAMPLE_DATA_STRUCTURE.md`.

## Main Scripts

### `clean_invalid_episodes.py`

Scans a data root and moves invalid episode folders into a quarantine directory.
An invalid episode folder is any fourth-level folder that does not match the
`episode_<digits>` naming pattern.

Important: this rule does not currently accept extended NAS episode names such
as `episode_0085_20260428-114939_wangyong_arx5_none`. Do not run this cleanup
script against `NAS_Sample_Data/` unless the validation rule is updated or you
intend to quarantine extended-format folders.

Preview changes without moving anything:

```bash
python3 clean_invalid_episodes.py --root ./Test_Data --quarantine ./quarantine_data --dry-run
```

Run the cleanup:

```bash
python3 clean_invalid_episodes.py --root ./Test_Data --quarantine ./quarantine_data --log cleanup.log
```

The script preserves the relative path below the root when moving invalid data
to quarantine.

### `run_cleanup.sh`

Shell wrapper for `clean_invalid_episodes.py`, intended for cron scheduling. By
default it scans this repository folder, writes Python logs to
`cleanup_python.log`, and appends wrapper output to `cleanup_cron.log`.

Run manually:

```bash
./run_cleanup.sh
```

For server or NAS deployment, edit the `--root` and `--quarantine` arguments in
the script.

### `annotate_standstill.py`

Detects long robot standstill periods from joint-position CSV data and annotates
all CSV files in each affected episode with an `is_standstill` column.

Detection uses `observation.state.joint_position/data.csv` by default. Movement
is detected by comparing numeric non-gripper columns between consecutive rows.
Only standstill time beyond the built-in 4000 ms buffer is marked as
`is_standstill=True`.

Scan a data root:

```bash
python3 annotate_standstill.py ./Test_Data --threshold 0.05
```

Process one episode:

```bash
python3 annotate_standstill.py ./Test_Data/20260421/test_data/episode_0029 --threshold 0.05
```

Inspect one CSV and print detailed stop ranges:

```bash
python3 annotate_standstill.py ./Test_Data/20260421/test_data/episode_0029/observation.state.joint_position/data.csv --threshold 0.05 --show-stop-log
```

The script rewrites CSV files in place. Existing `is_standstill` columns are
updated; missing columns are appended.

### `QA_Pipeline/scripts/plan_standstill_trim.py`

Creates a read-only trim plan for abnormal standstill at the beginning and end
of episodes. It does not rewrite CSVs, cut videos, or move data. Reports are
written as CSV, JSONL, and Markdown.

Run a parallel sample scan:

```bash
python3 QA_Pipeline/scripts/plan_standstill_trim.py --roots NAS_Sample_Data --output-dir /tmp/standstill_trim_nas_sample --workers 8 --progress
```

The thresholds and source modality order are configured in
`QA_Pipeline/configs/quality_rules.json` under `standstill_trim`.

### `QA_Pipeline/scripts/run_pipeline.py`

Runs the multi-phase QA pipeline. By default it now creates a live run directory
under `<output-dir>/runs/<run-id>/` while processing.

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path /tmp/qa_pipeline/qa.db \
  --output-dir /tmp/qa_pipeline/out \
  --phases 1,2,3 \
  --workers 8 \
  --run-id sample-run
```

Useful live files:

- `run_status.json`: current phase, progress, issue counts, latest issue.
- `phase_status.jsonl`: phase start/end and progress snapshots.
- `issue_events.jsonl`: append-only exact issue records.
- `episode_issues.csv`: CSV issue ledger for the run.
- `live_summary.md`: human-readable live summary.

Final reports include `dashboard.html`, a static dashboard showing episode
status counts, issue breakdowns, episode filters, and exact issue details. Open
it directly in a browser from the output directory or the run-specific
`final/` directory.

To disable live monitoring, pass `--disable-live-monitor`.

### `correct_teleop_folders.py`

Finds folders named `actions.joint_position` whose `data.csv` actually contains
TCP end-effector pose columns such as `tcp.x` through `tcp.r6`, then renames the
folder to `actions.eef_pose`.

Run from the repository root:

```bash
python3 correct_teleop_folders.py
```

The script skips an episode if `actions.eef_pose` already exists to avoid
overwriting data.

### `Werkzeuge/check_episode_durations.py`

Scans episode `metadata.json` files and reports episode durations. It filters to
episodes labeled `完全正常`, prints aggregate duration statistics, and writes an
unusual-duration report by default.

Run against the NAS sample:

```bash
python3 Werkzeuge/check_episode_durations.py ./NAS_Sample_Data --summary-only
```

Write a full CSV duration report:

```bash
python3 Werkzeuge/check_episode_durations.py ./NAS_Sample_Data --csv duration_report.csv
```

Override unusual-duration thresholds:

```bash
python3 Werkzeuge/check_episode_durations.py ./NAS_Sample_Data --min-seconds 5 --max-seconds 300
```

By default, unusual duration reports are written under the scanned root as:

- `unusual_episode_durations.csv`
- `unusual_episode_durations.txt`
- `unusual_episode_durations_operator_stats.csv`

### `Werkzeuge/analyze_motion_abnormalities.py`

Read-only prototype checker for abnormal robot and UMI motion values. It scans
motion CSVs and reports candidate issues such as EEF pose jumps, derived EEF
velocity spikes, reported joint velocity spikes, timestamp problems, and
non-numeric values.

Run a bounded sample analysis:

```bash
python3 Werkzeuge/analyze_motion_abnormalities.py ./NAS_Sample_Data \
  --output /tmp/motion_abnormality_report \
  --max-episodes-per-task 8
```

The current prototype is for analysis and threshold calibration only. Its
`fail_candidate` output should not be used to move data until thresholds are
reviewed and approved.

## Included Data And Documents

`Test_Data/` contains example robot episodes from `20260421/test_data`, including
CSV state/action streams, video files, timestamps, metadata, and checksum
manifests.

`Documents/` contains reference PDFs:

- `Data.pdf`
- `数据格式.pdf`

`Werkzeuge/docs/` contains supporting documentation:

- `NAS_SAMPLE_DATA_STRUCTURE.md`
- `DATA_QUALITY_AUTOMATION_PLAN.md`
- `DATA_QUALITY_AUTOMATION_PLAN_ZH.md`
- `MOTION_ABNORMALITY_CHECKS.md`

`IMPLEMENTATION_PLAN.md` tracks the planned safety-first NAS data quality
pipeline, including phases, safety gates, abnormal-value checks, quarantine
design, and current progress.

`QA_PIPELINE_USER_GUIDE.md` explains how to run the QA pipeline, how phase and
final statuses are decided, how each phase separates pass and not-pass cases,
and how to read the reports and dashboard.

`PIPELINE_INTEGRATION_PLAN.md` explains how the new `Test_Folder_For_DataPipeline`,
`QA_Pipeline`, and `UMI_Data_Validation` folders should be used together to build
the full NAS-scale QA and quarantine workflow.

`DATA_QUALITY_STEPS_2_3_GAP_ANALYSIS.md` compares the current scripts against
`Documents/数据质检.pdf` Step 2 and Step 3 requirements and lists the required
fixes.

`QA_Pipeline/configs/quality_rules.json` is the central QA threshold config. For
example, Step 2 video/action length mismatch defaults to `3` frames or timestamp
rows, meaning an episode is marked unusable in the reports when an image
timestamp stream differs from the primary action stream by more than three rows.
The same config controls abnormal-FPS loss (`10%` by default) and hard
frame-drop thresholds: normal image videos default to `15%`, tactile videos
default to `20%`, and any image stream with `25` consecutive dropped frames is
marked unusable.

## Dependencies

The active scripts use only Python standard-library modules. Python 3.10 or
newer is recommended because the folder contains a Python 3.10 virtual
environment (`datapipeline-env/`).

No `requirements.txt` or packaging metadata is currently present.

## Operational Notes

- Run cleanup in `--dry-run` mode first when targeting production or NAS data.
- Do not run `clean_invalid_episodes.py` on NAS data with extended episode names
  until its episode-name regex is updated.
- `annotate_standstill.py` modifies CSV files in place, so use it on copied data
  first if the annotations need validation.
- The scripts temporarily relax file or directory permissions where needed, then
  attempt to restore the original modes. This is intended to support read-only
  or NAS-style dataset folders.
- Log files such as `cleanup.log`, `cleanup_python.log`, and
  `cleanup_cron.log` are generated by cleanup runs.
