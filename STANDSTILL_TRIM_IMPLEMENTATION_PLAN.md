# Standstill Trim Implementation Plan

Last updated: 2026-06-09

## Goal

Build a safety-first workflow for abnormal standstill handling in collected robot
episodes. The first implementation should only detect abnormal standstill at the
beginning and end of an episode, report the removable range, and create a trim
plan. Cutting robot CSVs and videos should come later as an explicit
materialization step that writes to a new cleaned dataset root.

This must not modify NAS data in place.

## Current Situation

`annotate_standstill.py` detects long standstill segments from
`observation.state.joint_position/data.csv` by comparing consecutive
non-gripper joint-position rows. If no joint moves more than a configured
threshold, the pair is treated as still. Segments longer than the built-in
`STANDSTILL_BUFFER_MS = 4000` are marked as standstill.

The script currently rewrites every CSV under the episode by adding or updating
an `is_standstill` column. This is useful for local annotation, but it is not
safe enough for NAS-scale cleaning because it mutates source files in place and
does not cut matching videos.

The QA pipeline also has Phase 5 standstill findings in
`QA_Pipeline/scripts/pipeline/phase5_robot_state.py`, but those findings report
all long idle segments, not specifically beginning/end trim ranges.

## Sample Observations

Checked local samples under `Test_Data/20260421/test_data`.

- Episodes already contain `is_standstill` columns in robot CSVs and image
  `timestamps.csv` files.
- Image modalities contain both `timestamps.csv` and `video.mp4`, for example
  `observation.image.third_view/timestamps.csv` and `video.mp4`.
- Episode `episode_0029` has 4230 joint-position rows from `0` to `140966 ms`
  and 4231 third-view timestamp rows from `1` to `140983 ms`.
- With the current example threshold `0.05`, the detector can classify very
  large parts of sample episodes as standstill. For example, some sample
  episodes are reported as almost fully still. This threshold is too permissive
  for safe cutting.
- In the same sample set, max per-frame joint deltas are often far below `0.05`.
  Example p50/p99/max per-frame maximum joint deltas:
  - `episode_0023`: `0.002814 / 0.013152 / 0.021756`
  - `episode_0024`: `0.003894 / 0.015996 / 0.044057`
  - `episode_0028`: `0.001365 / 0.004367 / 0.008052`
  - `episode_0029`: `0.005963 / 0.033910 / 0.068513`
- With lower thresholds such as `0.001` or `0.005`, detected segments are more
  plausible, but still need task/robot validation before automated cutting.

Conclusion: the first version must be report-only and threshold-calibrated
before any data is rewritten.

## First-Step Scope

Only detect removable standstill at episode boundaries:

- leading standstill: a still segment that starts at, or very near, the first
  valid robot timestamp.
- trailing standstill: a still segment that ends at, or very near, the last
  valid robot timestamp.

Interior standstill should be reported separately as idle/standstill evidence,
but should not be cut in the first implementation.

## Proposed Detection Criteria

Use `observation.state.joint_position/data.csv` as the primary motion source.
Ignore gripper columns and non-motion metadata columns such as `timestamp_ms` and
`is_standstill`.

Configurable parameters should live in `QA_Pipeline/configs/quality_rules.json`:

```json
{
  "standstill_trim": {
    "enabled": true,
    "motion_delta_threshold_rad": 0.001,
    "standstill_min_duration_ms": 5000,
    "edge_tolerance_ms": 1000,
    "keep_context_ms": 500,
    "min_remaining_duration_ms": 5000,
    "max_trim_ratio": 0.40
  }
}
```

Recommended initial interpretation:

- A row pair is still when every non-gripper joint-position delta is less than
  `motion_delta_threshold_rad`.
- A still segment is trim-eligible only when its duration is at least
  `standstill_min_duration_ms`.
- A leading segment is edge-eligible when
  `segment_start_ms <= first_timestamp_ms + edge_tolerance_ms`.
- A trailing segment is edge-eligible when
  `segment_end_ms >= last_timestamp_ms - edge_tolerance_ms`.
- Keep a small context margin around the retained action using
  `keep_context_ms`, so cutting does not remove the exact first/last movement
  frame.
- Reject the trim plan if remaining duration would be below
  `min_remaining_duration_ms`.
- Mark the plan as `needs_review` if the total removed duration exceeds
  `max_trim_ratio` of the episode, even if the edge rule matches.

The exact default threshold should be calibrated from sample data before use on
NAS. Based on the local sample measurements, `0.05` should not be used as a
production trimming threshold.

## Trim Plan Output

Add a non-destructive planner, for example:

```text
QA_Pipeline/scripts/plan_standstill_trim.py
```

The planner should scan episodes and write:

```text
standstill_trim_plan.csv
standstill_trim_plan.jsonl
standstill_trim_summary.md
```

Each plan row should include:

```text
episode_path
task
date
operator
robot
source_modality
first_timestamp_ms
last_timestamp_ms
leading_standstill_start_ms
leading_standstill_end_ms
trailing_standstill_start_ms
trailing_standstill_end_ms
trim_start_before_ms
trim_end_after_ms
first_kept_timestamp_ms
last_kept_timestamp_ms
removed_leading_ms
removed_trailing_ms
removed_total_ms
remaining_duration_ms
removed_ratio
decision
reason
affected_csv_modalities
affected_video_modalities
```

Suggested decisions:

```text
no_trim
trim_candidate
needs_review
reject_too_short_after_trim
reject_too_much_removed
missing_motion_source
invalid_timestamps
```

## Synchronized Cutting Design

Materialization should be a separate command, not part of detection:

```text
QA_Pipeline/scripts/materialize_standstill_trim.py
```

Inputs:

- source dataset root
- trim plan JSONL/CSV
- cleaned output root
- `--dry-run` default
- optional `--apply`

Required behavior:

- Never overwrite source episode files.
- Copy each selected episode to a new cleaned root, preserving the relative
  path.
- For every CSV modality with `timestamp_ms`, keep rows where:

```text
first_kept_timestamp_ms <= timestamp_ms <= last_kept_timestamp_ms
```

- Rebase timestamps only if a later training format requires it. The safer
  first behavior is to preserve original timestamps and record the trim offset
  in metadata.
- For every image modality:
  - trim `timestamps.csv` using the same timestamp window.
  - cut `video.mp4` to the corresponding frame/time window.
  - verify the output video frame count matches the trimmed timestamp rows
    within the existing configured tolerance.
- Update copied `metadata.json` and `meta/episode.json` with new row/frame
  counts, duration, and a `trim_history` block.
- Write a per-episode manifest with source paths, output paths, row counts,
  frame counts, checksums, and command details.

Video cutting options:

- Prefer frame-index cutting when frame/timestamp rows are reliable. This avoids
  accumulated timestamp drift.
- Use `timestamps.csv` to map `first_kept_timestamp_ms` and
  `last_kept_timestamp_ms` to `start_frame_index` and `end_frame_index`.
- Use `ffmpeg` for materialization because it is available locally. For exact
  frame cuts, re-encoding is safer than stream copy:

```text
ffmpeg -y -i input.mp4 -vf trim=start_frame=START:end_frame=END,setpts=PTS-STARTPTS -an output.mp4
```

The command should be generated and logged by the materializer, not run during
planning.

## Safety Gates

Before allowing `--apply`:

1. Run detection on `Test_Data/` and `NAS_Sample_Data/`.
2. Review the summary manually, especially high removed ratios.
3. Compare before/after row counts across all CSV and image modalities.
4. Verify output videos can be opened and have expected frame counts.
5. Verify Phase 1, Phase 2, Phase 3, and Phase 5 pass or improve on cleaned
   output.
6. Keep source and cleaned roots separate.
7. Write audit logs and checksums.
8. Do not add quarantine movement until trimming behavior is validated.

## Recommended Implementation Sequence

1. Done: refactor standstill detection into a read-only planner path that does
   not rewrite files.
2. Done: add central config for standstill trim thresholds.
3. Done: implement `plan_standstill_trim.py` in dry-run/report-only mode.
4. Done: add multiprocessing with `--workers`; 8 workers was faster than 16 on
   the current local disk.
5. Done: test planner on `Test_Data/`, `NAS_Sample_Data/`, and
   `Test_Folder_For_DataPipeline/`. Threshold calibration still needs human
   review of candidates.
6. Add unit tests using small synthetic CSVs:
   - leading standstill only
   - trailing standstill only
   - both edges
   - interior standstill only
   - no standstill
   - malformed/non-monotonic timestamps
7. Add materializer to copy and trim CSV files into a new output root.
8. Add video materialization with `ffmpeg`, still dry-run by default.
9. Re-run QA on cleaned output and compare reports.
10. Only after review, allow server execution against NAS-mounted data.

## Current Test Results

- `Test_Data`: 4 episodes in 0.023s with 4 workers; 1 trim candidate and 3
  no-trim episodes.
- `NAS_Sample_Data`: 4,465 episodes in 15.236s with 8 workers; 4,446 no-trim,
  18 trim candidates, and 1 reject-too-short-after-trim. Serial and 8-worker
  outputs were byte-identical.
- `Test_Folder_For_DataPipeline`: 5,946 episodes in 8.518s with 8 workers; 5,755
  no-trim, 165 trim candidates, 22 reject-too-short-after-trim, 3 needs-review,
  and 1 invalid-timestamps episode. Serial and 8-worker outputs were
  byte-identical.

## Open Decisions

- Whether timestamps should be preserved or rebased to start at zero in cleaned
  episodes.
- Whether a small context margin should be kept before/after movement, and the
  exact default value.
- Whether robot-specific thresholds are required for UR, ARX, Aloha, and UMI.
- Whether edge standstill should be classified as `warning`, `needs_review`, or
  `trim_candidate` in the main QA report.
