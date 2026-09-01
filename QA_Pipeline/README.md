# QA_Pipeline

This directory contains the multi-phase QA runner for robot and UMI episode
datasets. The pipeline is report-first: it classifies episodes and writes
structured reports, but it does not delete source data, move episode folders, or
cut videos during the main QA run.

Main entry point:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py
```

Implemented phases:

```text
1  structure, metadata, required files, labels, task/robot consistency
2  duration, frame/row counts, task training limits, alignment, duration outliers
3  timestamps, FPS, frame drops, cross-camera start/end alignment
4  video health: openability, properties, black/white/frozen sampled frames
5  robot state/action reasonableness, limits, steps, velocity, jitter, EEF jumps
6  UMI-specific validation, preprocessing, and world-frame export
7  attributed standstill and sustained slow-motion detection
```

From the repository root, activate the virtual environment first:

```bash
source datapipeline-env/bin/activate
```

Typical server command for phases 1 and 2:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --phases 1,2 \
  --db-path outputs/qa_verified_p1_p2/qa_pipeline.db \
  --output-dir outputs/qa_verified_p1_p2 \
  --workers 4 \
  --max-workers-safe 4 \
  --batch-size 10000 \
  --batch-mode group-aware \
  --run-id verified-p1-p2 \
  --force-rerun
```

## Operator severe-QA evidence report

Use a separate database and output directory for each one-week or two-week
window. The optional report ranks operators by affected episode frequency and
retains episode paths, measured values, source checks, and reasons as QA
evidence. It does not itself make an employment decision.

```bash
DATE_FROM=20260706
DATE_TO=20260719
RUN_DIR="outputs/operator_severe_${DATE_FROM}_${DATE_TO}"

python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date-from "${DATE_FROM}" \
  --date-to "${DATE_TO}" \
  --operator operator_name \
  --db-path "${RUN_DIR}/qa_pipeline.db" \
  --output-dir "${RUN_DIR}" \
  --phases 1,2,7 \
  --batch-size 5000 \
  --batch-mode auto \
  --workers 3 \
  --continue-after-fail \
  --disable-quality-label-filter \
  --generate-operator-severe-report \
  --operator-severe-config QA_Pipeline/configs/operator_severe_report.json
```

`--continue-after-fail` is needed to collect findings from later selected
phases after an earlier severe failure. `--disable-quality-label-filter`
includes all quality labels; it can be omitted when a run intentionally
includes only the normal quality label. Add other phase numbers whenever the
configured criteria depend on those phases. Omit `--operator` to process all
operators; when supplied, it is an exact case-insensitive operator name from
the standard date/operator path.

The same report can be regenerated from a completed window database without
rerunning pipeline phases:

```bash
python3 QA_Pipeline/scripts/generate_operator_severe_report.py \
  --db-path "${RUN_DIR}/qa_pipeline.db" \
  --output-dir "${RUN_DIR}" \
  --config QA_Pipeline/configs/operator_severe_report.json
```

Dashboard-requested severe reports are also published while a run is active.
The publisher reads the QA database without changing it and creates immutable
snapshots under `reports/operator_severe/`. Evidence becomes available as soon
as the phase required by its criterion has completed. Combined operator rates
use only episodes that completed every enabled report phase, so partial data
does not distort rankings.

Open the Chinese-first standalone view at
`http://<server>:4131/severe-reports/<run-id>`. It omits pipeline controls and
technical server details and provides an Excel download for the exact displayed
snapshot. A running legacy process is supported: after a dashboard deployment,
its already-committed results are published without restarting the run or
migrating its active database.

Current default behavior:

- episodes with `quality.labels` containing `完全正常` are normal, `测试任务`
  is allowed test data, and missing/unexpected labels are processed by Phase 1
  so they can be reported instead of silently skipped;
- hidden directories such as `.fr-*` are skipped and reported as
  `hidden_directory_skipped`;
- batch mode limits the number of loaded episode states, and `group-aware`
  batching keeps Phase 2/3 outlier groups complete;
- pipeline phase writes use bounded SQLite transactions and the live monitor
  updates cached aggregates from changed episode states, avoiding historical
  full-table scans as a run grows;
- the dashboard can run as a separate process with
  `QA_Pipeline/scripts/live_dashboard.py`; it updates `dashboard.html` plus
  `dashboard_data.json` from the SQLite DB and can serve the output directory
  over HTTP;
- the server control dashboard runs with
  `QA_Pipeline/scripts/qa_control_dashboard.py --host 0.0.0.0 --port 4131`;
  it can start date-range/task-folder runs, manage the event listener, show run
  status/logs, generate Chinese work-session reports, and display reviewer
  actions. Dashboard-started runs do not require tmux: production uses transient
  user-systemd services and local `auto` mode falls back to a validated detached
  process;
- the verified-event listener defaults to phases `1,2,3,6,7`; Phase 6 runs as
  `video_only` with `sidecar` output and requires the receipt plus both wrist
  sidecars before marking a job done;
- Phase 1 preserves every task `compatible_device_types` category and accepts
  the episode robot when it matches any appointed category. A missing/query-failed
  catalog produces `task_device_reference_unavailable` (`needs_review`), while a
  task absent from a successfully loaded catalog remains informational;
- Chinese work-session reports are generated by
  `QA_Pipeline/scripts/generate_work_session_report.py`. The dashboard can
  generate a current-run cumulative report while a multi-day run is still in
  progress, or half-day reports for forenoon/afternoon review;
- reports include operator issue share, searchable operator/issue episode
  indexes, device failure summaries, action suggestions, and Chinese detection
  rule explanations. The dashboard exposes rule details through a popup instead
  of requiring reviewers to open `检测规则说明.csv`;
- the control dashboard detects consecutive-failure combinations using
  `task + robot + operator + date`; reviewers click `标记已解决` and then
  `确认解决？` to resolve the visible combination. Resolution is immediate in
  the UI and persisted in `outputs/dashboard_manager/issue_history.db`. New
  failures on a later date are detected again as independent issues;
- Excel export is manual-only via `QA_Pipeline/scripts/export_excel_report.py`;
  normal runs write CSV, JSONL, Markdown, SQLite, and dashboard outputs.

After deploying a task compatibility correction, inspect one date's jobs that
failed only `task_robot_mismatch` (dry-run by default), then explicitly apply:

```bash
datapipeline-env/bin/python3 Werkzeuge/listen_episode_verified.py \
  requeue-task-robot-mismatch --date YYYYMMDD
datapipeline-env/bin/python3 Werkzeuge/listen_episode_verified.py \
  requeue-task-robot-mismatch --date YYYYMMDD --apply
```

The apply form writes `job_reprocess_history` before resetting jobs to pending;
the next artifacts are written under `outputs/event_listener/reprocesses/`.

For local or server regression checks, `pytest` is available in
`datapipeline-env`, but the current dashboard tests are script-style and should
also be runnable directly, for example:

```bash
datapipeline-env/bin/python QA_Pipeline/tests/test_consecutive_failure_streaks.py
datapipeline-env/bin/python QA_Pipeline/tests/test_work_session_device_summary.py
```

Detailed documentation:

- `../QA_PIPELINE_USER_GUIDE.md`
- `../QA_PIPELINE_USER_GUIDE_ZH.md`
