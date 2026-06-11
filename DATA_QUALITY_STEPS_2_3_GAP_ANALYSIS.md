# Data Quality Steps 2-3 Gap Analysis

Last updated: 2026-06-09

Source requirement:

```text
Documents/数据质检.pdf
```

Current goal:

- Step 2: hard filtering / hard processing in batch.
- Step 3: static/idle frame filtering and final length alignment.

## Executive Summary

The current `QA_Pipeline` scripts partially satisfy Step 2 and Step 3. They can
already detect many structural, duration, FPS, timestamp, frame-drop, video, and
robot-state issues. However, they do not fully satisfy the PDF requirements yet
because several rules use different thresholds, some required actions are only
reported rather than processed, and quarantine/move behavior is not implemented.

The most important missing pieces are:

1. No quarantine planner or safe mover.
2. No absolute `duration <= 5s -> quarantine` rule.
3. RGB frame-drop rule uses 10% instead of required 15%, and consecutive drops
   use statistical/group logic instead of fixed `25 frames -> quarantine`.
4. Tactile frame-drop handling is not separated as `>20% -> mark tactile missing`.
5. Frame/action length mismatch now has an absolute `>3 timeframe` report-only
   fail finding, but trimming/alignment output is still not implemented.
6. Motion discontinuity and impossible robot values mostly become
   `needs_review`, not hard quarantine.
7. Start/end static frame trimming is not implemented.
8. Long idle segment detection exists, but uses a 4-second buffer and warning
   logic instead of the required `>5s -> remind + record`.
9. Length alignment to minimum when all modalities differ within `±3` is not
   implemented.

## Step 2 Requirement Coverage

### 2.1 Ultra-short episodes

PDF requirement:

```text
超短时间剔除（5秒以内，直接隔离，隔离理由：时间过短）
```

Current implementation:

- `QA_Pipeline/scripts/pipeline/phase2_duration.py` checks positive duration.
- It also flags duration relative to task median:
  - `< task_median * 0.20` -> fail;
  - `< task_median * 0.40` -> needs_review;
  - `> task_median * 2.50` -> needs_review.

Gap:

- There is no absolute `duration_seconds <= 5` hard fail rule.
- Existing median-relative rules may catch many short episodes, but they do not
  guarantee the PDF rule.

Fix:

- Add `duration_too_short_hard` in Phase 2.
- Rule:

```text
duration_seconds <= 5.0 -> severity=major or critical, status=fail,
reason="时间过短"
```

### 2.2 Abnormal FPS

PDF requirement:

```text
异常FPS（异常fps标注）
```

Current implementation:

- Phase 2 checks `duration_seconds`, `total_frames`, and FPS consistency.
- Phase 3 checks actual timestamp frequency deviation.
- Phase 3 also has group-level frequency outlier detection.
- `QA_Pipeline/configs/quality_rules.json` now provides the Step 2 abnormal-FPS
  threshold.

Gap:

- Duration/FPS consistency and timestamp-based FPS checks are still split across
  phases.
- The timestamp-based check now has explicit abnormal-FPS finding names, but the
  metadata-side FPS consistency check still uses code-level thresholds.

Fix:

- Keep current checks, and normalize report category:

```text
abnormal_fps_metadata
abnormal_fps_loss
abnormal_fps_gain
abnormal_fps_group_outlier
```

- Current configured default:

```text
phase3_timestamp.abnormal_fps.loss_fail_ratio = 0.10
```

- FPS loss above this threshold is `major/fail`.
- FPS gain above `gain_warning_ratio` is `minor/warning`.
- Remaining work: move Phase 2 metadata FPS consistency threshold into the same
  config.

### 2.3 Gripper data abnormality

PDF requirement:

```text
夹爪数据异常（每个机器人有一个定义，偏离定义则需要修复）
aloha 0 - 0.1, arx 0 - 0.082
```

Current implementation:

- Phase 5 has robot-specific gripper limits:
  - `arx5`: `0.0 - 0.082`
  - `aloha`: `0.0 - 0.1`
  - `flexiv`: `0.0 - 0.10`
- It checks gripper bounds and gripper step spikes.
- `QA_Pipeline/configs/quality_rules.json` now includes central gripper config.
- Phase 5 now reports `gripper_mean_too_low_remap_needed` when mean gripper
  distance is below the configured threshold.

Gap:

- UR, UMI, and other robots are not clearly configured.
- The requirement says abnormal gripper data may need repair, but current
  pipeline only reports findings and does not yet generate a repair/remapping
  plan.

Fix:

- Continue expanding `QA_Pipeline/configs/quality_rules.json` with entries for:
  - `flexiv`
  - `ur`
  - `umi`
- Current configured remap rule:

```text
phase5_robot_state.gripper.mean_remap_threshold_m = 0.005
```

- Add optional repair/remapping-planning output, not automatic repair:

```text
repair_plan.csv
```

- Keep automatic mutation disabled until reviewed.

### 2.4 RGB frame-drop detection

PDF requirement:

```text
rgb丢帧检测：
相机总丢帧大于15%，或者连续丢帧25帧，
直接丢入隔离区，隔离理由：相机丢帧
```

Current implementation:

- Phase 3 reads `metadata.frame_integrity`.
- Current hard RGB drop rule is configured as `drop_ratio > 0.15 -> fail`.
- Current hard consecutive drop rule is configured as
  `max_consecutive_drops >= 25 -> fail`.
- Thresholds live in `QA_Pipeline/configs/quality_rules.json`.

Gap:

- The hard PDF thresholds are now implemented for normal image streams.
- Remaining work is mostly validation on larger samples and wiring fail reports
  into the future quarantine planner.

Fix:

- Current modality classification:

```text
rgb camera: normal image streams excluding tactile image streams
tactile camera: modality name contains tactile
flow video: optional separate class
```

- Current fixed rules:

```text
normal_video_drop_ratio_fail = 0.15
max_consecutive_fail = 25
```

- Keep group outlier checks as additional `needs_review`/warning context, not as
  replacement for the hard rule.

### 2.5 Tactile frame-drop detection

PDF requirement:

```text
触觉丢帧检测（触觉丢帧大于20%，视为缺触觉）
```

Current implementation:

- Phase 3 checks frame drops generically from metadata.
- Phase 1 can report modality/file missing.
- Phase 3 now uses a tactile-specific frame-drop threshold when `tactile` is in
  the image modality name.

Gap:

- Tactile frame-drop hard fail is now implemented as a direct `fail` finding.
- Remaining work is to connect fail findings to quarantine movement.

Fix:

- Current tactile-specific rule:

```text
tactile_video_drop_ratio_fail = 0.20
```

- The PDF now requests the same action for tactile threshold violations, so the
  current status is `fail`.
- Optional future task policy can still distinguish required/optional tactile
  data if training teams need it:

```yaml
modalities:
  tactile:
    required_for_training: false
    drop_ratio_missing: 0.20
```

### 2.6 Frame-action length severe mismatch

PDF requirement:

```text
帧-动作长度严重不匹配：
>3 timeframe difference, see as unusable.
```

Current implementation:

- Phase 2 checks image timestamp row count vs `total_frames` using ratio > 10%.
- Phase 2 checks state CSV rows vs expected duration/FPS using ratio > 15%.
- Phase 2 now compares each image `timestamps.csv` row count against the primary
  action `data.csv` row count. If the absolute difference is greater than the
  central config threshold
  `phase2_duration.length_alignment.max_video_action_difference` (default `3`),
  it emits `video_action_length_mismatch` with `status: fail`.
- Phase 3 checks cross-modality start/end alignment by time.

Gap:

- This is currently summary/report-only. It marks the episode unusable in QA
  findings, but deliberately does not move or quarantine files yet.
- There is no final alignment/trimming output.

Fix:

- Add absolute row-count checks:

```text
abs(image_timestamps_rows - action_rows) > 3 -> fail or needs_review
abs(state_rows - action_rows) > 3 -> fail or needs_review
abs(video_frames - action_rows) > 3 -> fail or needs_review
```

- If all lengths differ by `<=3`, mark as alignable and plan trimming to the
  minimum length in Step 3.

### 2.7 Motion discontinuity

PDF requirement:

```text
运动学不连续（直接隔离，隔离理由：机器人异常）
```

Current implementation:

- Phase 5 checks joint steps, gripper steps, velocity, acceleration, jitter,
  EEF pose steps, and timestamp monotonicity.
- Many of these currently produce `warning` or `needs_review`, not `fail`.

Gap:

- There is no clear hard `motion_discontinuity -> fail` rule.
- Thresholds are not externalized.
- EEF velocity/step logic from `Werkzeuge/analyze_motion_abnormalities.py` is not
  fully integrated into `QA_Pipeline` Phase 5.

Fix:

- Add explicit hard discontinuity rules:

```text
joint_step > hard threshold -> fail, reason="机器人异常"
eef_step > hard threshold -> fail, reason="机器人异常"
reported_velocity > hard threshold -> fail, reason="机器人异常"
derived_velocity > hard threshold -> fail, reason="机器人异常"
acceleration/jerk extreme -> fail or needs_review depending on calibration
```

- Use statistical calibration for warning/review thresholds, and physical limits
  for hard fail thresholds.

### 2.8 Impossible joint / EEF values and reachable workspace

PDF requirement:

```text
joint 和 eef 是不可能的值，重定向后超出可达工作空间
（直接隔离，隔离理由：机器人异常，但应该真机比较少）
```

Current implementation:

- Phase 5 has joint limits for `arx5` and `flexiv`.
- Phase 5 checks EEF step size but not EEF workspace reachability.
- `UMI_Data_Validation/ik_benchmark.py` can validate UMI EEF trajectories through
  inverse kinematics, but it is not integrated into `QA_Pipeline`.

Gap:

- No complete robot physical limit config for all robots.
- No EEF workspace check in Phase 5.
- No integrated IK phase.
- No redirected/converted coordinate workspace validation in the QA pipeline.

Fix:

- Add `phase6_umi_ik.py` using `UMI_Data_Validation/ik_benchmark.py` logic.
- Add workspace bounds per robot/task/controller.
- Add hard finding:

```text
unreachable_eef_pose -> fail, reason="机器人异常"
joint_out_of_physical_limits -> fail, reason="机器人异常"
```

- Start with `needs_review` until robot models, coordinate transforms, and
  tolerances are validated.

## Step 3 Requirement Coverage

### 3.1 Trim static frames at beginning and end

PDF requirement:

```text
首尾静止剔除
```

Current implementation:

- Phase 5 detects standstill segments inside joint-position data.
- `annotate_standstill.py` can annotate standstill rows with `is_standstill`.

Gap:

- No script trims/removes start/end static frames.
- No trim manifest exists.
- No video/state/action synchronized trimming exists.

Fix:

- Add a non-destructive trim planner:

```text
trim_plan.csv
trim_plan.jsonl
```

- For each episode, compute:

```text
trim_start_ms
trim_end_ms
first_kept_timestamp_ms
last_kept_timestamp_ms
affected_modalities
new_expected_rows
```

- Do not rewrite data initially. Generate a plan and review it.
- Later add safe materialization to a new cleaned dataset root, not in-place
  mutation.

### 3.2 Idle segment over 5 seconds

PDF requirement:

```text
查看整个视频内，是否存在静止大于 5s 的片段，
如果存在需要提醒 + 记录
```

Current implementation:

- Phase 5 detects standstill segments with `STANDSTILL_BUFFER_MS = 4000`.
- It records `operator_standstill` warnings.
- It marks excessive standstill when total excess idle time is over 20% of the
  episode duration.

Gap:

- Required threshold is `>5s`, not `>4s`.
- Current logic reports every segment beyond the 4s buffer rather than the exact
  `>5s` rule.
- It is based on joint-position stillness, not video-based static analysis.

Fix:

- Change or add a Step 3-specific check:

```text
standstill_segment_duration_ms > 5000 -> warning, record segment
```

- Keep the current 20% excessive idle metric as additional context.
- Optionally add sampled video motion detection later, but state/action-based
  stillness should be the first implementation.

### 3.3 Confirm all frame lengths align; if within ±3, align to minimum

PDF requirement:

```text
确认所有帧长度对齐，如果±3内按照最小的对齐
```

Current implementation:

- Phase 2 checks row-count mismatches by ratio.
- Phase 4 checks video frame count vs metadata.
- No alignment plan or trimming materialization exists.

Gap:

- No absolute `±3` alignment policy.
- No `align_to_min_length` operation.
- No cleaned-output writer.

Fix:

- Add `phase3_length_alignment.py` or extend Phase 2/3 with alignment metrics:

```text
min_length
max_length
length_spread
alignable = length_spread <= 3
unusable = length_spread > 3
```

- Add `alignment_plan.csv`.
- Later add a safe writer that creates a cleaned copy under:

```text
cleaned_data/<task>/<date>/<operator>/<episode>/
```

- Do not modify NAS source data in place.

## Quarantine Gap

The PDF repeatedly says "直接隔离" for several Step 2 failures.

Current implementation:

- `QA_Pipeline` produces reports and statuses.
- `clean_invalid_episodes.py` can move invalid episode folder names, but it is
  not suitable for the new NAS episode naming rules or quality-based moves.

Gap:

- No quality-report-based quarantine planner.
- No safe mover.
- No rollback plan.

Fix:

- Implement:

```text
QA_Pipeline/scripts/plan_quarantine.py
QA_Pipeline/scripts/move_to_quarantine.py
```

- Plan from `quality_report.csv`.
- Move only `status == fail` by default.
- Preserve relative paths.
- Refuse overwrites.
- Write:

```text
move_plan.csv
move_log.jsonl
rollback_plan.csv
```

## Recommended Fix Order

1. Add explicit Step 2/3 config file:

```text
QA_Pipeline/configs/data_quality_steps_2_3.yaml
```

2. Update Step 2 rules:
   - duration <= 5s hard fail;
   - RGB drop ratio > 15% hard fail;
   - RGB max consecutive drops >= 25 hard fail;
   - tactile drop ratio > 20% marks tactile missing;
   - frame/action length difference > 3 hard unusable;
   - robot gripper ranges from config;
   - hard motion discontinuity rules.

3. Update Step 3 rules:
   - long standstill > 5s warning + record;
   - start/end static trim planner;
   - length spread <= 3 align-to-min planner;
   - length spread > 3 unusable.

4. Integrate UMI IK/workspace validation as Phase 6.

5. Add quarantine planner and safe mover.

6. Run validation on:

```text
Test_Data/
Test_Folder_For_DataPipeline/
NAS_Sample_Data/
```

7. Review false positives manually before allowing NAS quarantine.

## Current Fulfillment Summary

| Requirement | Current Status |
| --- | --- |
| Duration checks | Partial; no absolute 5s hard quarantine |
| Abnormal FPS | Implemented as configurable report fail for FPS loss |
| Gripper abnormality | Partial; configured ranges and remap-needed reporting exist, repair execution is not implemented |
| RGB frame drops | Implemented as configurable report fail; quarantine mover not implemented |
| Tactile frame drops | Implemented as separate configurable report fail |
| Frame-action mismatch >3 | Implemented as configurable report fail; quarantine mover not implemented |
| Motion discontinuity | Partial; not hard isolation yet |
| Impossible joint/EEF/workspace | Partial; IK/workspace not integrated |
| Start/end static trimming | Missing |
| Idle segment >5s record | Partial; current threshold is 4s |
| Align lengths within ±3 to minimum | Missing |
| Quarantine movement | Missing for QA status-based filtering |
