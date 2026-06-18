# DataPipeline

<p align="right">
  <a href="README.md"><kbd>中文</kbd></a>
  <a href="README_EN.md"><kbd>English</kbd></a>
</p>

DataPipeline contains the QA pipeline and supporting tools for validating robot
and UMI episode datasets. The current main workflow is report-first: it
classifies episodes and writes structured reports, but it does not delete source
data, move episodes, or cut videos during the QA run.

For detailed phase criteria and command examples, see:

- `QA_PIPELINE_USER_GUIDE.md`
- `QA_PIPELINE_USER_GUIDE_ZH.md`

## Repository Layout

```text
DataPipeline/
  QA_Pipeline/            Main multi-phase QA pipeline
  DataProcessUMI/         UMI validation, preprocessing, and world-frame export
  UMI_Data_Validation/    Additional UMI validation/prototype code
  Documents/              Reference documents and PDFs
  Werkzeuge/              Extra analysis utilities and documentation
  Test_Folder_For_DataPipeline/
                          Local test samples; exclude from server deploys
  datapipeline-env/       Local/server Python virtual environment
```

Generated run outputs are usually written under `outputs/` and should not be
committed.

## Expected Episode Layout

The scanner looks for folders whose name starts with `episode_`.

Old layout:

```text
<root>/<task>/<date>/<operator>/episode_...
```

Newer robot/collector layout:

```text
<root>/<task>/<robot_type>/<collector_id>/<date>/<operator>/episode_...
```

Typical episode contents:

```text
episode_0001/
  metadata.json
  observation.state.joint_position/data.csv
  actions.joint_position/data.csv
  observation.image.<camera>/timestamps.csv
  observation.image.<camera>/video.mp4
```

Robot type is inferred from `metadata.json` first, then from the episode name,
then from the path layout when needed. UMI episodes may be named simply
`episode_0094`, so metadata and path context are important.

## Setup

Activate the virtual environment from the repository root:

```bash
source datapipeline-env/bin/activate
```

Main Python dependencies are listed in:

```text
QA_Pipeline/requirements.txt
```

Current important dependencies:

- `opencv-python-headless` for Phase 4 video checks;
- `scipy` for UMI processing;
- `openpyxl` only for manual Excel export; normal QA runs do not need it;
- host `ffmpeg` and `ffprobe` for UMI video processing.

Install Python dependencies into the repo virtual environment:

```bash
python3 -m pip install -r QA_Pipeline/requirements.txt
```

On Ubuntu servers, install FFmpeg if Phase 6 UMI processing is used:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

## QA Pipeline

Entry point:

```text
QA_Pipeline/scripts/run_pipeline.py
```

Phases:

```text
1  Structure, metadata, required files, labels, robot/task mismatch
2  Duration, frame counts, row counts, task-level duration outliers
3  Timestamp, FPS, frame drops, and image-modality start/end alignment
4  Video health: openability, metadata, sampled black/white/frozen frames
5  Robot state/action reasonableness and standstill checks
6  UMI-specific validation, preprocessing, and world-frame export
```

All phases are optional via `--phases`.

See `QA_PHASE_DECISION_RULES.md` for detailed decision rules for every phase.

By default, the runner reads each episode's `metadata.json` before phase work
and only processes episodes whose `quality.labels` contains `完全正常`.
Episodes marked by collectors with other quality labels are skipped to avoid
wasted checks and server load. Use `--disable-quality-label-filter` for a full
audit, or `--quality-label` to select another label.

Episode discovery skips hidden directories such as `.fr-*`. They are not
processed as episodes, but they are reported as `hidden_directory_skipped` in
live and final reports so temporary NAS/sync folders remain visible.

Run a small local test:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Folder_For_DataPipeline \
  --db-path outputs/test_run/qa_pipeline.db \
  --output-dir outputs/test_run \
  --phases 1,2,3 \
  --max-episodes 10 \
  --workers 2 \
  --force-rerun
```

Run a conservative server date scan:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260612 \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output-dir outputs/qa_20260612_phase1_5 \
  --phases 1,2,3,4,5 \
  --workers 3 \
  --batch-size 5000 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 1.20 \
  --resource-check-interval 60 \
  --resource-max-wait-seconds 0 \
  --resource-error-retries 5 \
  --resource-retry-delay-seconds 20 \
  --force-rerun
```

For an interrupted run, reuse the same DB/output path and omit
`--force-rerun`. Episode states are saved incrementally, so completed phases are
skipped after matching episode states are loaded.

## Batching And Resume

`--batch-size` limits how many episode states are loaded at once. After each
batch, the pipeline releases the per-batch state list and runs Python garbage
collection.

`--batch-mode auto` is recommended. When Phase 2 or Phase 3 is selected, it uses
group-aware batching so outlier statistics do not run on partial task or
task+robot groups. If a complete group is larger than `--batch-size`, it runs as
an oversized complete batch and prints a warning.

Each run first selects the active episode set from `--roots`, `--date`,
`--task`, the quality-label filter, and `--max-episodes`, then prunes old
episode-scoped SQLite rows outside that selection. This prevents stale rows
from earlier filters from polluting the current dashboard. Resume still scans
the input root before loading saved states from SQLite, so rediscovery can take
time on very large NAS roots even when previous records already exist.

## Resource Guard

The resource guard is enabled by default. It can:

- lower requested worker count to a safe value;
- pause when load or memory is unsafe and wait until recovery by default;
- retry a phase after a resource-guard stop.

Useful options:

```text
--min-free-mem-gb
--max-load-ratio
--resource-check-interval
--resource-max-wait-seconds
--resource-error-retries
--resource-retry-delay-seconds
```

Phase 4 is often the slowest NAS phase because it opens and randomly seeks
inside many MP4 files. If it causes high load, run it separately with fewer
workers:

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

## Reports And Dashboard

Normal outputs:

```text
quality_report.csv
quality_findings.jsonl
quality_summary.md
dashboard.html
dashboard_data.json
qa_pipeline.db
```

Live monitoring also writes:

```text
<output-dir>/runs/<run-id>/
  run_status.json
  phase_status.jsonl
  issue_events.jsonl
  episode_issues.csv
  live_summary.md
  dashboard.html
  dashboard_data.json
```

View live terminal status:

```bash
python3 QA_Pipeline/scripts/qa_status.py \
  --output-dir outputs/qa_20260612_phase1_5 \
  --watch
```

Start the independent dashboard process:

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output-dir outputs/qa_20260612_phase1_5 \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

Then open:

```text
http://<server-ip>:1234/dashboard.html
```

`dashboard.html` is now a live shell and the data lives beside it in
`dashboard_data.json`. When served over HTTP, the browser polls that JSON every
5 seconds by default and updates the page in place, avoiding a blank page during
auto-refresh. `live_dashboard.py --interval` controls the independent updater;
the main pipeline's `--live-dashboard-interval` only controls the suggested
command printed at run start. Use `--port 0` to write dashboard files without
serving HTTP, or `--once` to generate once and exit. Direct `file://` opening
cannot fetch the JSON because of browser security limits, so use
`live_dashboard.py --port <port>` or another HTTP server for the output
directory.

Excel is not part of normal pipeline output. Generate it manually from an
existing DB when needed, without rerunning QA:

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output outputs/qa_20260612_phase1_5/quality_report.xlsx
```

The Excel workbook contains sheets for summary counts, episodes, exact findings,
issue counts, and task status counts. It requires `openpyxl` in the virtual
environment and should be run as a separate command on demand.

## UMI Processing

Phase 6 integrates `DataProcessUMI` into the QA pipeline. It is selected with:

```bash
--phases 6
```

UMI detection uses metadata robot values, episode-name robot tokens, and path
context. Non-UMI episodes are skipped with a pass/info finding.

Phase 6 does not run IK. It performs UMI raw-data assessment, trajectory
preprocessing, and world-frame export. UMI processing can be slow because it may
open videos, copy/transform episode folders, and use FFmpeg.

Default Phase 6 output root:

```text
outputs/umi_processed/
```

## Standstill Trim Planner

The standstill trim planner is separate from the main phase runner. It is
report-only and does not cut videos or rewrite CSVs.

```bash
python3 QA_Pipeline/scripts/plan_standstill_trim.py \
  --roots Test_Folder_For_DataPipeline \
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

## Server Deployment Notes

When deploying to the server, exclude test samples and generated outputs. The
server also needs `datapipeline-env` or equivalent dependencies.

For long runs, start the command inside `tmux` or `screen` on the server so a
VS Code SSH disconnect or local PC freeze does not interrupt the pipeline:

```bash
tmux new -s qa_verified
cd /home/xinzhi/DataPipeline
source datapipeline-env/bin/activate
```

Example:

```bash
rsync -azv \
  --exclude '.git/' \
  --exclude '.vscode/' \
  --exclude 'Test_Data/' \
  --exclude 'NAS_Sample_Data/' \
  --exclude 'Test_Folder_For_DataPipeline/' \
  --exclude 'outputs/' \
  --exclude 'qa_feature_test/' \
  --exclude 'qa_umi_test/' \
  ./ \
  xinzhi@192.168.50.209:~/DataPipeline/
```

## Legacy Utilities

Older standalone scripts remain in the repo:

- `clean_invalid_episodes.py`
- `run_cleanup.sh`
- `annotate_standstill.py`
- `correct_teleop_folders.py`
- tools under `Werkzeuge/`

Treat these as separate utilities. Some modify files in place or move folders,
so use dry-run/copy-first workflows before running them on production data.

## Safety Rules

- The QA pipeline itself is report-first and does not modify source episodes.
- Use `--force-rerun` only when you intentionally want to recompute selected
  phases.
- Keep output directories separate from source episode folders.
- Review `dashboard.html`, `quality_report.csv`, and `quality_findings.jsonl`
  before any cleanup, quarantine, or deletion step. Generate Excel separately
  only when needed.
- Do not run all CPU cores on shared servers; start conservatively and adjust
  workers after checking load and memory.
