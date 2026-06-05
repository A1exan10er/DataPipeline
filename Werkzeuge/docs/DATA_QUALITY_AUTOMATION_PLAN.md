# Data Quality Automation Plan

Last updated: 2026-05-27

Related structure reference:

```text
docs/NAS_SAMPLE_DATA_STRUCTURE.md
```

## Goal

Build automation features that keep high-quality robot and UMI data while identifying, quarantining, or eventually removing disqualified data. The process should be explainable, reversible, and robust across different tasks, robots, camera setups, and operator-control methods.

The current priority is detection and reporting. Deletion should not be automated until the quality rules have been validated on real samples.

## Guiding Principles

- Do not delete data first. Write reports and quarantine candidates before permanent removal.
- Separate detection from action. Quality checks should produce structured findings; another step may later move or remove failed episodes.
- Prefer metadata-first checks before opening large CSV or MP4 payloads.
- Make quality decisions explainable with concrete reasons.
- Keep thresholds task-aware and robot-aware where possible.
- Use physically meaningful checks for abnormal velocity, jitter, joint values, gripper distance, and episode length.
- For UMI demonstrations intended for real-robot training, verify that motion is compatible with target robot inverse kinematics before marking the data as training-ready.
- Treat borderline cases as `needs_review` instead of forcing pass/fail.
- Keep a manifest of every decision so downstream analysis can filter data without changing the original folder tree.

## Proposed Status Values

```text
pass
warning
fail
needs_review
```

Suggested meaning:

- `pass`: no critical or major issues found.
- `warning`: structurally usable, but minor quality issues exist.
- `fail`: disqualified by critical issues or repeated major issues.
- `needs_review`: statistically unusual or ambiguous; human review recommended.

## Proposed Severity Values

```text
critical
major
minor
info
```

Suggested meaning:

- `critical`: missing or unreadable essential files, invalid metadata, corrupt payloads, or impossible synchronization.
- `major`: severe frame drops, large timestamp gaps, large row/frame mismatch, or robot-state spikes.
- `minor`: small FPS drift, small duration deviation, optional files missing, or non-blocking schema differences.
- `info`: non-failing observations useful for summaries.

## Target Outputs

Primary report:

```text
quality_report.csv
```

Suggested columns:

```text
episode_path,task,date,operator,robot,controller,status,severity,reasons,checked_at
```

Detailed findings report:

```text
quality_findings.jsonl
```

Each line should describe one finding:

```text
episode_path,check_name,severity,status,message,details
```

Summary report:

```text
quality_summary.md
```

The summary should include:

- total episodes checked;
- pass/warning/fail/needs_review counts;
- issue counts by check;
- issue counts by task;
- issue counts by operator;
- issue counts by robot/controller type;
- examples of failed and borderline episodes.

## Phase 1: Structural And Metadata Checks

Status: not started

Purpose: catch reliable, cheap quality problems without reading large video payloads.

Checks:

- `metadata.json` exists.
- `metadata.json` is valid JSON.
- episode folder name starts with `episode_`.
- parent path follows `<task>/<date>/<operator>/<episode>`.
- required metadata fields exist where expected:
  - `task_key`;
  - `episode_index`;
  - `duration_seconds`;
  - `total_frames`;
  - `fps_actual` or `fps_config`;
  - `modalities`;
  - `quality`.
- `modalities` is readable and matches discovered modality folders.
- `.checksum_manifest` exists.
- required modality files exist:
  - CSV modality: `data.csv`;
  - image modality: `video.mp4` and `timestamps.csv`.
- required files are non-empty.
- `quality.labels` is present and can be used for filtering.

Expected first tool:

```text
quality_check_episodes.py
```

Initial behavior:

- walk one or more task folders;
- discover episodes through `metadata.json`;
- run structural and metadata checks;
- write `quality_report.csv`;
- write `quality_findings.jsonl`;
- write `quality_summary.md`;
- never move or delete data.

## Phase 2: Duration And Count Consistency

Status: not started

Purpose: identify suspicious recordings using metadata and lightweight CSV/timestamp counts.

Checks:

- `duration_seconds` is present and positive.
- `total_frames` is positive.
- `duration_seconds`, `total_frames`, and FPS are roughly consistent.
- episode duration is not a task-level outlier.
- modality row/frame counts are internally consistent.
- image `timestamps.csv` row counts are close to video frame counts when video frame counts are available.
- action/state CSV row counts are close to expected frame counts.

Recommended statistics:

- group by `task_key`;
- calculate median duration and IQR;
- mark duration outliers as `needs_review` first;
- escalate to `fail` only after the thresholds are validated.

Existing useful script:

```text
check_episode_durations.py
```

Potential enhancement:

- reuse its duration extraction and per-task outlier logic inside the quality checker.

## Phase 3: Timestamp Synchronization Checks

Status: not started

Purpose: detect recordings where modalities exist but are not synchronized or have timing damage.

Checks:

- timestamps are monotonic.
- no duplicate timestamps unless explicitly allowed.
- no large gaps in `timestamp_ms`.
- per-modality observed rate is close to expected FPS or control rate.
- modality start/end times are reasonably aligned.
- `timestamps_raw.csv` and `timestamps.csv` differences are explainable when both exist.

Important notes:

- UMI and real-robot samples may have different camera sets.
- Real-robot samples may include `third_view`, `second_third_view`, and flow videos.
- The checker should discover available modalities instead of requiring a fixed set.

## Phase 4: Video Health Checks

Status: not started

Purpose: detect corrupt, blank, frozen, or inconsistent videos.

Checks:

- MP4 can be opened by `ffprobe`, OpenCV, or another reliable video reader.
- video frame count is available.
- video duration roughly matches metadata and timestamps.
- resolution matches metadata or `config.csv` when available.
- sampled frames are not all black, all white, or empty.
- sampled frames are not frozen for the full episode.
- optional blur/brightness checks can be added later.

Performance rule:

- sample frames instead of decoding full videos by default.
- run full video decoding only on suspicious or selected episodes.

## Phase 5: Robot-State Plausibility Checks

Status: not started

Purpose: detect recordings with impossible or damaged robot motion streams.

Checks:

- CSV numeric values parse correctly.
- no NaN or Inf values.
- timestamps are monotonic.
- joint positions are finite.
- gripper values are finite.
- joint values are inside robot-specific joint limits.
- gripper distance is inside robot-specific limits.
- episode duration is inside task-specific expected bounds.
- velocity values are finite and inside robot-specific limits.
- acceleration and jerk estimated from position streams are not extreme.
- joint jumps between adjacent frames are below robot-specific limits.
- gripper jumps are below robot-specific limits.
- end-effector pose jumps are below robot-specific limits.
- jitter score is below task-specific and robot-specific thresholds.
- movement is not fully static unless the task allows static periods.

Units:

- real-robot joint positions are in radians;
- real-robot gripper distance or position is in meters;
- thresholds should be robot-specific when possible.

Robot-specific column patterns:

- ARX-style bimanual samples use `left_j*`, `right_j*`, `left_gripper`, and `right_gripper`.
- Flexiv-style samples use `j1`-`j7`, `joint_*.pos`, and `gripper`.

Recommended abnormal-value method:

- derive `dt` from timestamps instead of assuming a fixed frame rate;
- compute velocity from adjacent positions when velocity streams are absent;
- compute acceleration from adjacent velocities;
- compute jerk from adjacent accelerations when needed;
- apply a median filter or Hampel filter to detect isolated spikes without hiding sustained abnormal motion;
- report both maximum values and robust percentiles such as p95, p99, and p99.9;
- fail on impossible values, for example NaN, Inf, values outside hard physical limits, or very large discontinuities;
- mark as `needs_review` when robust statistics are unusual but not clearly impossible.

Recommended jitter checks:

- detect high-frequency position noise by comparing raw motion to a smoothed trajectory;
- compute per-joint residuals after smoothing;
- compute end-effector residuals when FK is available;
- use task-relative thresholds first, then robot-specific thresholds once enough examples are labeled.

Suggested output fields:

```text
max_joint_abs,max_joint_step,max_velocity,max_acceleration,max_jerk,jitter_score,static_ratio
```

## Phase 6: Inverse Kinematics Compatibility Checks

Status: not started

Purpose: filter UMI episodes that cannot be executed by the target real robots. UMI data may look structurally valid, but if its end-effector movement cannot be solved by inverse kinematics for a target robot, then that episode is not suitable for training models intended to control that robot.

Target robots:

```text
UR
ARX5
Flexiv
Aloha
Piper
Franka
```

Core idea:

- Use the recorded UMI end-effector trajectory as the desired task-space trajectory.
- For each target robot, load the correct robot model, joint limits, gripper limits, base frame, tool frame, and IK solver.
- Solve IK along the trajectory.
- Check whether a continuous, joint-limit-valid, collision-aware or at least self-consistency-aware solution exists.
- Mark episodes as training-ready only for the target robots that pass IK compatibility.

Recommended implementation stages:

1. Robot model registry

Create a versioned registry for every target robot:

```text
robot_id
urdf_or_description_path
base_frame
tool_frame
joint_names
joint_limits
velocity_limits
acceleration_limits
gripper_limits
ik_solver
solver_parameters
```

Do not hard-code robot limits inside the checker. Keep them in configuration files so they can be reviewed and updated.

2. Kinematics backend selection

Use a proven robotics kinematics library instead of implementing IK from scratch. Practical candidates:

- Pinocchio for FK, Jacobians, and model-based checks;
- IKPy or TRAC-IK style solvers for numerical IK;
- robot-vendor or internal SDK IK when available and reliable;
- MoveIt-style IK and planning checks if a ROS/ROS2 environment is available.

The quality checker should wrap the backend behind a small interface:

```text
solve_ik(robot_id, target_pose, seed_joint_state) -> ik_result
forward_kinematics(robot_id, joint_state) -> pose
check_joint_limits(robot_id, joint_state) -> result
```

3. Frame calibration and transform handling

IK validation is only meaningful if frames are correct. Required transforms:

```text
UMI/task frame -> robot base frame
UMI gripper/tool frame -> robot tool frame
camera/world frame -> robot base frame, if pose is camera-derived
```

These transforms should be stored per task, setup, or robot cell. Missing or uncertain transforms should produce `needs_review`, not `fail`.

4. Trajectory-level IK, not point-only IK

Single-frame IK success is not enough. The trajectory should be checked as a continuous motion:

- solve frame 0 with a neutral or configured seed;
- solve frame N using frame N-1 joint solution as the seed;
- prefer the solution closest to the previous joint state;
- reject large joint discontinuities;
- reject solutions outside joint limits;
- reject solutions exceeding velocity and acceleration limits;
- track IK failure ratio over the episode.

Suggested metrics:

```text
ik_success_ratio
ik_max_position_error
ik_max_rotation_error
ik_mean_position_error
ik_joint_limit_violation_count
ik_max_joint_step
ik_max_joint_velocity
ik_max_joint_acceleration
ik_failure_segments
```

5. Reachability pre-check

Before running expensive full IK, apply cheaper reachability checks:

- target position distance from robot base is inside approximate workspace radius;
- target height is inside plausible workspace bounds;
- target orientation is not obviously impossible for the target robot/tool;
- trajectory does not require extreme workspace boundary operation for most frames.

This is not a replacement for IK, but it can quickly identify impossible episodes and reduce compute cost.

6. Collision and environment checks

Initial IK validation can ignore environment collision if environment geometry is unavailable. However, the final high-confidence filter should eventually include:

- self-collision checks;
- table or workspace collision checks;
- tool and gripper collision checks;
- task-specific obstacle checks when geometry is known.

Until these are implemented, label IK-passing episodes as `ik_reachable_no_collision_check` rather than fully executable.

7. Robot-specific training eligibility

One episode may pass for one robot and fail for another. Store eligibility per target robot:

```text
episode_path,target_robot,ik_status,ik_reasons,ik_metrics
```

Suggested statuses:

```text
ik_pass
ik_warning
ik_fail
ik_needs_review
ik_not_applicable
```

Recommended first-pass decision rules:

- `ik_fail`: no valid IK solution for a significant continuous segment.
- `ik_fail`: joint limits are violated after IK.
- `ik_fail`: required joint velocity or acceleration exceeds hard robot limits.
- `ik_needs_review`: missing or uncertain calibration transform.
- `ik_warning`: IK succeeds but operates near joint or workspace limits.
- `ik_pass`: IK succeeds continuously with acceptable pose error and smooth joint motion.

8. UMI-to-real-robot mapping validation

UMI gripper motion may not map one-to-one to every real robot gripper. Validate:

- left/right hand mapping;
- single-arm versus bimanual mapping;
- gripper opening range conversion;
- tool center point convention;
- whether the task requires one arm or both arms.

If the mapping is unknown, report `ik_needs_review`.

Suggested first IK tool:

```text
quality_check_ik.py
```

Initial behavior:

- read the structural quality report;
- select UMI episodes that passed structural and motion checks;
- load target robot config;
- run reachability pre-check;
- run trajectory IK on sampled frames first;
- optionally run full-frame IK for candidates;
- write `ik_quality_report.csv`;
- write `ik_quality_findings.jsonl`;
- do not move or delete data.

Practical rollout:

- start with one target robot whose model and IK solver are trusted;
- validate the checker against known good and known bad episodes;
- then add UR, ARX5, Flexiv, Aloha, Piper, and Franka one by one;
- keep per-robot thresholds separate.

## Phase 7: Combined Quality Decision

Status: not started

Purpose: combine structural, timing, video, robot-state, abnormal-value, and IK results into one final decision.

Suggested logic:

- structural `critical` issue -> `fail`;
- unreadable required video or CSV -> `fail`;
- impossible abnormal values -> `fail`;
- severe timestamp synchronization damage -> `fail`;
- task-level length outlier -> `needs_review` unless clearly impossible;
- UMI episode without valid IK for target robot -> not training-ready for that robot;
- IK pass for one robot does not imply IK pass for every robot;
- warnings should not remove data automatically.

Suggested final eligibility outputs:

```text
episode_path,general_quality_status,training_ready,target_robot,ik_status,reasons
```

## Phase 8: Review And Quarantine Workflow

Status: not started

Purpose: separate confirmed disqualified data from usable data without irreversible deletion.

Suggested quarantine layout:

```text
<task>/_quarantine/<date>/<operator>/<episode>
```

Suggested tool:

```text
quality_quarantine.py
```

Behavior:

- read `quality_report.csv`;
- move only episodes with `status=fail`;
- optionally skip episodes with `needs_review`;
- write a move manifest before moving;
- support dry-run by default;
- never permanently delete.

Suggested move manifest:

```text
original_path,new_path,status,reasons,moved_at
```

## Phase 9: Human Review Support

Status: not started

Purpose: make borderline decisions efficient and auditable.

Suggested review artifacts:

- sampled thumbnails from key cameras;
- optional short preview clips;
- CSV summary statistics;
- duration and frame-drop summary;
- top findings per episode;
- suggested status and reason.

This phase should focus on `needs_review` and high-impact `warning` episodes.

## Current Implementation Progress

Completed:

- Basic NAS sample data structure has been documented in `docs/NAS_SAMPLE_DATA_STRUCTURE.md`.
- Current task-folder layout, modalities, cameras, file formats, and CSV headers have been documented.
- UMI versus real-robot camera setup notes have been documented.
- Real-robot control device and unit notes have been documented.
- This quality automation plan has been created.
- Abnormal velocity, jitter, joint, gripper, and episode-length filtering requirements have been added to the plan.
- UMI-to-real-robot inverse kinematics validation has been added as a dedicated implementation phase.
- Target IK robot families have been recorded: UR, ARX5, Flexiv, Aloha, Piper, and Franka.

Not started:

- No automated quality checker has been implemented yet.
- No report schema has been written by code yet.
- No quarantine or delete behavior has been implemented.
- No thresholds have been validated.
- No robot model registry has been created yet.
- No IK backend has been selected or integrated yet.
- No UMI-to-target-robot frame calibration transforms have been provided yet.
- No robot-specific IK thresholds have been validated yet.

Next implementation step:

```text
Implement Phase 1 in quality_check_episodes.py with dry-run report-only behavior.
```

Parallel preparation for IK:

```text
Collect robot models, joint limits, gripper limits, tool frames, base frames, and UMI-to-robot calibration transforms for UR, ARX5, Flexiv, Aloha, Piper, and Franka.
```

## Open Questions

- Which `quality.labels` values should be treated as pass, warning, fail, or needs-review?
- Should task-level duration outliers start as `needs_review` or `fail`?
- What are acceptable frame-drop thresholds for each camera type?
- What are acceptable timestamp gap thresholds for UMI, ARX, Flexiv, and UR?
- What robot-specific joint and gripper limits should be used?
- Should `_quarantine` live inside each task folder or in a separate top-level quarantine root?
- Which robot should be used as the first IK implementation target?
- Which kinematics backend is preferred in the local environment: Pinocchio, IKPy, TRAC-IK, MoveIt, vendor SDKs, or an internal solver?
- Where are the URDF or robot-description files for UR, ARX5, Flexiv, Aloha, Piper, and Franka?
- What are the correct base frame and tool frame names for each robot?
- What transforms map UMI task-space poses into each target robot base frame?
- How should UMI left/right gripper poses map to single-arm robots?
- Should IK validation use every frame or a sampled trajectory first?
- What pose error tolerance is acceptable for training eligibility?
- Should collision checking be required before data is considered training-ready, or should initial IK validation only check reachability and joint feasibility?
