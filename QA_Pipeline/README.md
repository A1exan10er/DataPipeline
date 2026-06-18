# QA_Pipeline

This directory contains the multi-phase QA runner for robot and UMI episode
datasets. The pipeline is report-first: it classifies episodes and writes
structured reports, but it does not delete source data, move episode folders, or
cut videos during the main QA run.

Main entry point:

```bash
python3 QA_Pipeline/scripts/run_pipeline.py
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

Current default behavior:

- only episodes whose `metadata.json` has `quality.labels` containing
  `完全正常` are processed; use `--disable-quality-label-filter` for a full
  audit;
- hidden directories such as `.fr-*` are skipped and reported as
  `hidden_directory_skipped`;
- batch mode limits the number of loaded episode states, and `group-aware`
  batching keeps Phase 2/3 outlier groups complete;
- the dashboard can run as a separate process with
  `QA_Pipeline/scripts/live_dashboard.py`; it updates `dashboard.html` plus
  `dashboard_data.json` from the SQLite DB and can serve the output directory
  over HTTP;
- Excel export is manual-only via `QA_Pipeline/scripts/export_excel_report.py`;
  normal runs write CSV, JSONL, Markdown, SQLite, and dashboard outputs.

Detailed documentation:

- `../QA_PIPELINE_USER_GUIDE.md`
- `../QA_PIPELINE_USER_GUIDE_ZH.md`
