# QA Pipeline Improvement Backlog

This document records potential improvements found during review. Changes should
be made step by step and validated on small runs before full NAS runs.

## Fixed In Current Step

### Rerun Should Overwrite Old Findings

Problem:

- Some phases saved findings without passing explicit `phase` and
  `episode_path`.
- If a rerun produced zero findings for an episode, old findings for that
  episode/phase could remain in SQLite and continue to appear in dashboard and
  reports.

Current fix:

- Phase 1 and Phase 3 now call `save_findings()` with explicit `phase` and
  `episode_path`, so reruns clear old rows even when the new finding list is
  empty.

## Future Improvements

### Date Filtering Semantics

Current behavior:

- `--date` matches the path string, so data under a date folder such as
  `20260615` can still be included when the episode folder name contains a
  timestamp such as `20260616-...`.

Important data-collection requirement:

- Collection can run continuously across midnight.
- A task may start under one date folder and finish after midnight, so a single
  date-filtered run may intentionally need to include cross-day episode names.

Future plan:

- Do not change `--date` until the desired semantics are explicit.
- Consider adding separate modes instead of replacing current behavior:
  - `--date-folder`: exact directory date.
  - `--episode-date`: episode timestamp date.
  - `--date`: documented broad compatibility mode, or an alias to one of the
    explicit modes after migration.
- Add dashboard/report metadata showing which date mode was used.

### Final Report Export Cost

Current risk:

- Large runs can spend significant time and I/O generating final CSV, JSONL,
  Markdown, and dashboard outputs.
- Exporting the same reports under both `<output-dir>` and run-local `final/`
  duplicates work.

Future plan:

- Export full reports once under `<output-dir>`.
- Store pointers or small summaries in the run directory.
- Keep Excel manual-only.

### Markdown Summary Memory Use

Current risk:

- The summary appendix still loads non-pass findings for examples.

Future plan:

- Query examples per task/status on demand.
- Limit appendix size by default.
- Keep full details in `quality_findings.jsonl` and SQLite.

### Phase 4 Tactile Video Frozen Rule

Current risk:

- Generic frozen-frame detection can mark tactile videos as `fail`, even though
  tactile frames naturally have low frame-to-frame differences.

Future plan:

- Exclude tactile videos from generic frozen detection, or use a tactile-specific
  threshold and status.
- Validate the rule on known tactile examples before enabling fail-level output.

### Dashboard Robustness

Current risk:

- Dashboard port restart can fail briefly with `address already in use`.
- Relative run paths in `latest_run.txt` depend on launch working directory.
- Repeated dashboard update failures are printed but not summarized.

Future plan:

- Enable socket address reuse.
- Resolve relative run paths against `output_dir`.
- Write a small `dashboard_status.json` with last update/error state.

### Episode Selection Cache Safety

Current risk:

- Cached episode selection can be stale if the NAS source tree changes.

Future plan:

- Include cache timestamp/source fingerprint.
- Print stronger warnings when cache is used.
- Consider requiring explicit opt-in for full-folder runs.
