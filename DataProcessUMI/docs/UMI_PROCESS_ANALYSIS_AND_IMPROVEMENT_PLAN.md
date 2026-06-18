# UMI Process Analysis and Improvement Plan

This document explains what the current `DataProcessUMI` code does, including
the new inverse-kinematics and executability code, and proposes how the parent
QA pipeline should split UMI processing into clearer phases.

## Executive Summary

`DataProcessUMI` is no longer only a validation/export helper. It now contains
four different types of work:

1. Raw UMI data assessment.
2. End-effector trajectory repair or rejection.
3. Coordinate-frame export to world-base EEF data.
4. Optional robot executability analysis using IK, collision checks, workspace
   sampling, and placement search.

The parent `QA_Pipeline` still exposes this as one Phase 6 named "UMI
processing", and its Phase 6 integration currently only runs assessment,
preprocess, and transform. It does not run the new IK/executability code. That
single phase is now too broad because it mixes fast data quality checks,
destructive derived-data generation, video transcoding, coordinate conversion,
and robot-specific feasibility analysis.

Recommended direction:

- Keep `DataProcessUMI/pipeline/run_pipeline.py` as the standalone UMI pipeline.
- Split the parent QA pipeline into separate UMI phases or subphases:
  - UMI applicability and trajectory-first gate.
  - UMI detection and raw-data assessment.
  - UMI trajectory preprocessing.
  - UMI world-frame export.
  - UMI IK/executability report generation.
  - UMI target-robot training readiness decision.
- Treat IK/executability as a robot-specific compatibility report, not as a
  basic data-quality pass/fail for the transformed dataset.
- For the initial stricter rollout, run the trajectory check before expensive
  video/assessment/export work and allow only `smooth` trajectories to continue.
  Treat `recoverable`, `middle_smooth`, `middle_recoverable`, and
  `unrecoverable` as not passing the automatic pipeline until the thresholds and
  repair policy are validated.

## Current Code Map

### Top-Level Orchestrator

`DataProcessUMI/pipeline/run_pipeline.py` streams each episode through:

```text
assessment -> preprocess -> transform [-> executability]
```

Important behavior:

- Episodes are processed end to end one at a time. The first episode can finish
  transform before the full input set is assessed.
- Assessment failures are converted into a gate decision.
- Preprocess can repair, crop, reject, or error.
- Transform writes final usable data under `output_root/data`.
- Optional executability writes detailed IK reports under
  `output_root/report/<class>/<episode>/executability`.
- The optional executability result is embedded in the per-episode report, but
  a non-executable robot result does not change the main `passed` status.

This is a good standalone shape, but the parent QA pipeline has not yet caught
up with all of it.

### Parent QA Phase 6 Integration

`QA_Pipeline/scripts/pipeline/phase6_umi_processing.py` currently imports only:

- `assessment/validate_raw_data.py`
- `preprocess/smooth_assessment.py`
- `preprocess/preprocess_trajectory.py`
- `transform/transform_episode_w_world_base.py`

It does not import or call:

- `DataProcessUMI/executability/solve_executability.py`
- `DataProcessUMI/solve/*`
- `DataProcessUMI/resources/*`

The root README also says Phase 6 does not run IK. That statement matches the
parent QA integration, but not the new standalone `DataProcessUMI` feature set.

## What Happens In UMI Processing

### 1. Raw Data Assessment

Main file: `assessment/validate_raw_data.py`

Purpose: detect raw episode problems before repair/export.

The assessment checks:

- Gripper mapping:
  - Compares `observation.state.gripper/data.csv` with
    `observation.state.raw_gripper_rotation/data.csv`.
  - Checks static/dynamic behavior, plausible gripper range, correlation,
    linear fit, and metadata calibration recomputation.
- Video integrity:
  - Checks frame counts, timestamps, duplicated frames, and readable streams.
- Wrist-view focus:
  - Samples wrist camera frames and uses Laplacian variance to detect blur.
- Cross-class label similarity:
  - Detects cases where wrist-view and tactile streams may have been mislabeled.
- Action/eef-pose plausibility:
  - Range-checks left/right end-effector xyz values.
- Motion consistency:
  - Compares wrist-view motion with action motion to detect static-device drift
    and possible left/right wrist-view swaps.

Current gate policy in both the standalone pipeline and parent Phase 6:

- Tolerated:
  - Gripper problems.
  - Video frame-drop-like problems, such as frame count mismatch, missing
    timestamps, or excessive duplicates.
- Blocking:
  - Defocus.
  - Mislabeling.
  - Left/right swap.
  - Missing files.
  - Non-monotonic timestamps.
  - Action/eef-pose problems.

This stage answers: "Is the raw episode structurally and semantically usable?"

### 2. Trajectory Smoothing Assessment

Main file: `preprocess/smooth_assessment.py`

Purpose: classify jumps in `actions.eef_pose/data.csv`.

It analyzes left and right device xyz trajectories separately. The logic is:

1. Compute windowed displacement over a short trailing time window.
2. Mark frames as "fast" if displacement exceeds `jump_displacement_m`.
3. Merge nearby fast runs into mutation segments.
4. For each mutation segment, decide whether it is:
   - `recoverable`: the pose jumps away and returns near the previous anchor
     within `recover_window_s`.
   - `unrecoverable`: the pose does not return quickly enough.
5. Combine left and right segment labels into one whole-episode label.

Whole-episode labels:

| Label | Meaning | Later action |
| --- | --- | --- |
| `smooth` | No jumps on either side. | Copy through. |
| `recoverable` | Only short out-and-back jumps. | Interpolate bad spans. |
| `middle_smooth` | Unrecoverable spans only at head/tail; middle is smooth. | Crop head/tail. |
| `middle_recoverable` | Head/tail unrecoverable spans plus repairable middle jumps. | Crop and interpolate. |
| `unrecoverable` | Middle unrecoverable span, or boundary span too long. | Reject. |

This stage answers: "What kind of trajectory defect exists, and is it
repairable?"

### 3. Trajectory Preprocessing

Main file: `preprocess/preprocess_trajectory.py`

Purpose: act on the smoothing assessment.

Behavior by category:

- `passthrough`:
  - Copy the episode unchanged.
- `interpolate`:
  - Repair recoverable jumps in both `actions.eef_pose` and
    `observation.state.eef_pose`.
  - Uses PCHIP interpolation per xyz and 6D rotation component.
  - Re-orthonormalizes repaired 6D rotations with Gram-Schmidt.
- `interpolate_crop`:
  - Repairs recoverable middle jumps.
  - Crops head/tail unrecoverable spans from every per-frame CSV, every video,
    and every timestamp file.
  - Re-zeroes timestamps after cropping.
- `reject`:
  - Writes no cleaned episode.

After writing an output episode, preprocessing updates:

- `metadata.json`
- `meta/episode.json`
- `checksums.sha256`
- `.checksum_manifest`

This stage answers: "Can we produce a cleaned episode, and exactly what was
changed?"

### 4. World-Base EEF Transform

Main files:

- `transform/ee_transform.py`
- `transform/transform_episode_w_world_base.py`

Purpose: convert UMI tracker-frame pose data into the world-base EEF coordinate
frame consumed by replay/training.

The transform applies:

1. Axis alignment from tracker coordinates into an intermediate transformed
   tracker frame.
2. Position offsets, including an optional right-arm offset.
3. Local end-effector projection from tracker to EEF point.
4. World projection into the configured world frame.
5. Rotation conversion from 6D representation to a rotation matrix and back.

Files transformed:

- `observation.state.eef_pose/data.csv`
- `actions.eef_pose/data.csv`

Additional behavior:

- Wrist-view videos are flipped in place.
- If a crop request is used, CSV/video/timestamp files can be cropped by frame
  range while wrist videos are flipped during crop.
- `metadata.json` is tagged with transform version/tag.
- `checksums.sha256` is recomputed.

This stage answers: "Can this cleaned UMI episode be exported into the replay
coordinate frame?"

## What The New IK / Executability Code Does

The new code is split into two layers:

- `solve/`: generic TCP trajectory -> IK / joints / feasibility tools.
- `executability/`: episode-level adapter for UMI-style `eef_pose` data.

### Important Concept: IK Is Not Just "Reachable Or Not"

Inverse kinematics asks:

> Given a target TCP pose in space, which robot joint angles can place the robot
> TCP at that pose?

The current code does more than solve IK. For each target pose it checks:

- IK convergence.
- Position and rotation residual.
- Joint limits.
- Singularities via Jacobian singular values.
- Self-collision.
- Collision clearance.
- Joint velocity between adjacent trajectory points.

So "executable" means the robot can follow the trajectory with acceptable pose
error, inside limits, away from singular/collision conditions, and without
exceeding velocity thresholds.

### Robot Model Loading

Main file: `solve/robots.py`

Supported robot registry:

- `franka_fr3v2`
- `ur5e`
- `ur7e`
- `flexiv_rizon4`
- `aloha_piper`
- `arx5_x5`

For each robot, the registry defines:

- URDF path under `resources/`.
- TCP frame name.
- Optional locked gripper joints.
- Optional SRDF.

Model loading uses Pinocchio and Coal:

1. Load URDF kinematic model.
2. Load collision geometry.
3. Lock gripper joints where needed so IK and Jacobian analysis use only arm
   degrees of freedom.
4. Add collision pairs.
5. Remove SRDF-disabled pairs, adjacent link pairs, and optionally always
   colliding pairs found by sampling.

### Single-Point IK And Metrics

Main file: `solve/core.py`

For one target pose:

1. `solve_ik` runs damped least-squares CLIK.
2. The solver uses the previous point's joint solution as a seed when following
   a trajectory, which helps maintain joint continuity.
3. It computes residuals:
   - `pos_err_mm`
   - `rot_err_deg`
4. It computes quality checks:
   - joint-limit margin.
   - Jacobian `sigma_min`, condition number, manipulability.
   - self-collision and clearance.
5. It marks the point executable only if all checks pass.

The simplified IK iteration is:

```text
error = log6(current_pose^-1 * target_pose)
J = frame Jacobian mapped into the error frame
dq = damped least-squares step that reduces error
q = integrate(q, dq), clipped to joint limits
```

### Batch Trajectory Validation

Main file: `solve/batch.py`

For a full trajectory:

- Single process mode follows all points sequentially with warm-started IK.
- Multi-process mode splits the trajectory into continuous chunks.
- Each chunk uses warm starts internally.
- Chunk boundaries cold-start with random restarts.
- Velocity checks are added after IK using adjacent joint differences and time.

This produces one `PointResult` per trajectory point.

### Generic TCP Tools

Main files:

- `solve/check_trajectory.py`
- `solve/tcp_to_joints.py`
- `solve/fit_trajectory.py`
- `solve/workspace_bounds.py`

What they do:

- `check_trajectory.py`: validate a TCP pose CSV on one robot.
- `tcp_to_joints.py`: convert a TCP pose CSV to a joint trajectory CSV.
- `workspace_bounds.py`: sample robot forward kinematics to estimate xyz
  workspace bounds.
- `fit_trajectory.py`: search for one constant xyz offset that moves the whole
  TCP trajectory into a robot-executable placement.

The placement search in `fit_trajectory.py` is important. It does not change the
shape or orientation of the trajectory. It only searches:

```text
shifted_position[i] = original_position[i] + constant_xyz_offset
shifted_rotation[i] = original_rotation[i]
```

The search is staged:

1. Geometric prune:
   - The trajectory bounding box must fit inside the robot workspace bounding
     box after some offset.
2. Seed:
   - Start near the robot workspace centroid.
3. Search:
   - Evaluate selected waypoints with an IK/collision/singularity penalty.
   - Refine the offset with Nelder-Mead.
4. Full validation:
   - Run the whole shifted trajectory through the batch validator.

### Episode-Level Executability

Main files:

- `executability/read_episode.py`
- `executability/solve_executability.py`

`read_episode.py` reads UMI episode pose CSVs:

- `actions.eef_pose/data.csv` for commanded target poses.
- `observation.state.eef_pose/data.csv` for observed actual poses.

For one arm, it extracts:

- timestamp.
- xyz position.
- 6D rotation.
- original frame index.

It can optionally apply the tracker -> world EEF transform in memory. This is
why there are two safe usage modes:

- Raw UMI episode: use default transform-on behavior.
- Already transformed pipeline output: pass `--no-transform`.

`solve_executability.py` then:

1. Reads left, right, or both arms.
2. For each selected robot:
   - Samples or loads workspace information.
   - Searches for an xyz offset that maximizes the longest continuous
     executable segment.
   - Fully validates that offset.
3. Produces two threshold reports:
   - `strict`: 1 mm / 0.5 deg pose tolerance, stricter singularity and
     clearance limits.
   - `replay`: about 5 mm / 3 deg tolerance, relaxed limits closer to real
     replay tolerance.
4. Decides `executable = true` if the replay threshold finds a continuous
   executable segment at least `--min-segment` sampled points long.

Outputs per `(arm, robot)`:

- `placement.json`
- `tcp_shifted.csv`
- `joints.csv`
- `report.strict.csv`
- `report.replay.csv`
- strict/replay summary JSON files

It also writes a global `summary.json`.

## Current Weak Points

### 1. Parent QA Phase 6 Is Now Too Broad

Phase 6 currently contains:

- UMI detection.
- UMI raw-data assessment.
- Trajectory repair/crop/reject.
- World-frame export.

The new standalone UMI pipeline can also run:

- IK and collision analysis.
- Robot-specific executability classification.
- Joint trajectory output.

These are different risk levels and different dependency levels. Keeping them
as one "Phase 6" makes it hard to:

- Run only fast checks.
- Retry only transform.
- Retry only IK for a new robot.
- Cache expensive robot workspace samples.
- Explain why an episode is data-valid but not executable on a target robot.
- Mark training readiness per robot.

### 2. Standalone UMI Pipeline And Parent QA Phase 6 Are Diverging

`DataProcessUMI/pipeline/run_pipeline.py` supports `--run-executability`.
`QA_Pipeline/scripts/pipeline/phase6_umi_processing.py` does not.

This creates two sources of truth:

- Standalone UMI reports can contain `executability`.
- Parent QA Phase 6 reports cannot.

The parent QA pipeline also has duplicated assessment-gate and report-building
logic. This duplication will drift as `DataProcessUMI` evolves.

### 3. IK Results Are Not Yet Integrated Into Training Readiness

The standalone pipeline deliberately keeps `passed` independent of IK. That is
reasonable for generic data export. However, the parent QA pipeline has
`training_ready`, and IK compatibility is robot-specific. A dataset may be:

- Clean and transformed.
- Executable on `flexiv_rizon4`.
- Not executable on `ur5e`.

The current parent status model does not express this well.

### 4. Heavy Dependency Boundaries Are Blurry

Assessment/preprocess/transform need packages like NumPy, SciPy, OpenCV, and
FFmpeg. Executability also needs Pinocchio/Coal through `pin`, robot resources,
URDF meshes, and more CPU time.

The parent pipeline dependency check currently validates the first group, not
the IK group. If IK is added into the same phase without a separate dependency
gate, Phase 6 will become fragile.

### 5. Configuration Is Too Coarse

Current UMI configuration covers assessment/preprocess/transform. The standalone
pipeline has IK CLI flags, but the parent QA config has no matching structured
settings for:

- enable/disable executability.
- target robots.
- source: action vs state.
- arm selection.
- max points / stride.
- min executable segment.
- strict vs replay decision policy.
- workspace samples.
- jobs/restarts/seed.
- target-robot readiness policy.

### 6. Some IK Assumptions Need Explicit Data Provenance

Important assumptions should be recorded prominently:

- Whether poses were transformed in memory or read from transformed data.
- Which transform config was used.
- Which robot URDF and TCP frame were used.
- Whether a constant xyz offset was allowed.
- Whether mounted workspace constraints were used.
- Which source was used: action target or observed state.
- Which thresholds determined the final executable decision.

Some of these fields exist in outputs, but the parent QA summary should surface
them so a user can understand the decision without opening many nested files.

### 7. Known Tool-Frame Risk For ARX UMI

`executability/README.md` documents that `arx5_x5` currently uses
`X5.urdf`, while UMI data may correspond to `X5_umi.urdf` with a longer UMI
finray gripper TCP offset. This can shift feasibility by about 80 mm along the
tool axis. This should be treated as a known limitation until the registry can
select the correct UMI TCP model.

## Proposed Phase Split

The exact phase numbers can be adjusted to match the parent QA roadmap, but the
responsibilities should be split like this.

### Phase 6a: UMI Applicability And Trajectory-First Gate

Inputs:

- Original episode directory.
- Metadata and path context.

Responsibilities:

- Decide whether an episode is UMI.
- Run the smoothing assessment on `actions.eef_pose/data.csv`.
- In strict mode, only allow the `smooth` label to continue.
- Record non-smooth labels as rejected or held for review before doing heavier
  checks.

Outputs:

- `umi_applicable`: true/false.
- `trajectory_label`.
- `trajectory_gate_status`: `pass`, `needs_review`, `reject`, or
  `not_applicable`.
- Per-side segment summaries and mutation events.

Training readiness effect:

- `smooth` can proceed to later checks.
- `recoverable`, `middle_smooth`, and `middle_recoverable` should initially be
  marked `needs_review` or `fail`, depending on how strict the rollout should
  be.
- `unrecoverable` should be marked not ready.

Why this should be first:

- It is cheaper than video transcoding and IK.
- It catches the core UMI failure mode: unusable end-effector motion.
- If the recorded EEF trajectory is not acceptable, later checks cannot make the
  episode training-ready.
- It reduces wasted work on episodes that would be rejected anyway.

### Phase 6b: UMI Raw Assessment

Inputs:

- Original episode directory.
- Metadata and path context.

Responsibilities:

- Decide whether an episode is UMI.
- Run raw UMI assessment.
- Apply assessment gate.
- Write assessment report.

Outputs:

- `umi_applicable`: true/false.
- `umi_assessment_status`: passed/rejected/error/not_applicable.
- Blocking and tolerated problems.

Training readiness effect:

- Blocking raw assessment problems should mark the episode not ready.
- Non-UMI episodes remain not applicable for UMI phases.

### Phase 6c: UMI Trajectory Preprocessing

Inputs:

- Original episode if the trajectory-first gate and raw assessment passed, or if
  the operator explicitly runs a repair/review mode.

Responsibilities:

- Run smoothing assessment.
- Interpolate repairable jumps.
- Crop boundary-only unrecoverable spans.
- Reject unrecoverable trajectories.
- Record exact operations.

Outputs:

- Cleaned intermediate episode.
- Label/category/operations.
- Preprocess report.

Training readiness effect:

- Rejected/unrecoverable means not ready.
- Repaired/cropped may be pass, warning, or needs review depending on policy.

Strict rollout policy:

- In the first production rollout, this stage should not automatically pass
  repaired or cropped episodes into training output.
- It may still write repair candidates to a review output folder so thresholds
  can be audited.
- After enough manual validation, `recoverable` could be allowed as
  `warning/pass`, then boundary-cropped labels can be evaluated separately.

### Phase 6d: UMI World-Frame Export

Inputs:

- Cleaned intermediate episode.
- Transform config.

Responsibilities:

- Transform `actions.eef_pose` and `observation.state.eef_pose`.
- Flip wrist videos.
- Update metadata and checksums.
- Write final transformed episode.

Outputs:

- `data/<task>_w_world_base/.../episode_XXXX`.
- Transform report.

Training readiness effect:

- Transform failure means not ready.
- Transform success means the episode is data-ready, before robot-specific IK.

### Phase 6e: UMI IK / Executability Analysis

Inputs:

- Transformed episode from Phase 6c, or raw episode with transform-on behavior.
- Target robot list.
- IK/executability config.

Responsibilities:

- Read action or observed EEF trajectory.
- Solve each selected arm and robot.
- Search allowed constant xyz placement offset.
- Run strict and replay validation.
- Write joint trajectories and executable segment reports.

Outputs:

- Per `(arm, robot)` placement and reports.
- Per-episode executability summary.
- Parent QA metrics such as:
  - `p6d_ik_<robot>_<arm>_executable`
  - `p6d_ik_<robot>_<arm>_segment_start`
  - `p6d_ik_<robot>_<arm>_segment_end`
  - `p6d_ik_<robot>_<arm>_found_offset`
  - `p6d_ik_<robot>_<arm>_failure_reasons`

Training readiness effect:

- Do not overwrite generic data readiness.
- Produce robot-specific readiness:
  - `training_ready_by_robot.flexiv_rizon4 = true/false`
  - `training_ready_by_robot.ur5e = true/false`

### Phase 6f: UMI Final Decision Aggregation

Inputs:

- Raw assessment result.
- Preprocess result.
- Transform result.
- Optional IK results.

Responsibilities:

- Produce a concise final UMI decision.
- Separate generic data readiness from robot-specific executability.
- Generate a human-readable summary for reports and dashboards.

Recommended statuses:

| Status | Meaning |
| --- | --- |
| `umi_not_applicable` | Episode is not UMI. |
| `umi_raw_rejected` | Raw assessment blocked the episode. |
| `umi_unrecoverable` | Preprocess rejected the trajectory. |
| `umi_exported` | Cleaned and transformed data was produced. |
| `umi_exported_with_repairs` | Data was produced after interpolation/crop. |
| `umi_ik_executable` | At least one target robot/arm has a valid segment. |
| `umi_ik_not_executable` | Data is valid, but selected robot(s) cannot execute it. |
| `umi_ik_error` | IK stage failed due to dependency/model/runtime error. |

## Implementation Plan

### Step 1: Make `DataProcessUMI` The Single Library API

Currently, `run_pipeline.py` and parent `phase6_umi_processing.py` duplicate
gate/report logic. Extract shared functions into a small internal module, for
example:

```text
DataProcessUMI/pipeline/umi_stages.py
```

Suggested functions:

- `build_assessment_context(...)`
- `run_assessment_stage(...)`
- `run_preprocess_stage(...)`
- `run_transform_stage(...)`
- `run_executability_stage(...)`
- `build_episode_report(...)`

Then:

- `DataProcessUMI/pipeline/run_pipeline.py` calls this module.
- `QA_Pipeline/scripts/pipeline/phase6_umi_processing.py` calls the same module.

Benefit: one source of truth for gates, reports, and future changes.

### Step 2: Add Parent QA Configuration For IK

Add a structured config block such as:

```json
{
  "phase6_umi_processing": {
    "enabled": true,
    "trajectory_first_gate": true,
    "trajectory_pass_labels": ["smooth"],
    "trajectory_nonpass_status": "fail",
    "run_assessment": true,
    "run_preprocess": true,
    "run_transform": true,
    "run_executability": false,
    "ik": {
      "robots": ["flexiv_rizon4"],
      "arm": "both",
      "source": "action",
      "max_points": 200,
      "min_segment": 5,
      "jobs": 1,
      "samples": 80000,
      "extra_args": ""
    }
  }
}
```

Keep `run_executability` default false because IK is slower and requires heavier
dependencies.

For the initial strict policy, keep `trajectory_pass_labels` as only
`["smooth"]`. Later, after manual review of repair quality, this can be relaxed
to include `recoverable` or boundary-cropped labels.

### Step 3: Add A Separate Dependency Check For IK

Do not make base UMI processing fail just because IK dependencies are missing.
Add a separate function:

```text
validate_executability_dependencies()
```

It should check:

- Python import: `pinocchio`.
- Python import: `scipy`.
- Robot resources directory exists.
- Required URDF files exist for selected robots.
- Mesh/package paths resolve enough for `robots.load(robot)` to succeed.

Only call it when `run_executability` is enabled.

### Step 4: Store IK Outputs Beside Existing UMI Reports

Keep the standalone layout:

```text
report/<task>/<date>/<operator>/<episode>/executability/
```

Embed a small summary in the parent QA finding details. Do not embed every
per-point CSV in the database; store paths and important metrics.

Suggested summary fields:

- `source`
- `arm`
- `robots`
- `transform_mode`: `already_transformed_no_transform` or `raw_transform_in_memory`
- `summary_path`
- `per_robot_arm` with:
  - `executable`
  - `found_offset`
  - `strict.segment_len`
  - `replay.segment_len`
  - `replay.executable_frame_start`
  - `replay.executable_frame_end`
  - `replay.failure_reasons`
  - `outputs`

### Step 5: Add Robot-Specific Readiness

Keep existing `state.training_ready` for generic data readiness if that is the
current database contract, but add robot-specific metrics. For example:

```json
{
  "umi_data_ready": true,
  "umi_training_ready_by_robot": {
    "flexiv_rizon4": {
      "left": true,
      "right": false,
      "any_arm": true
    },
    "ur5e": {
      "left": false,
      "right": false,
      "any_arm": false
    }
  }
}
```

This avoids the incorrect implication that an episode is universally
training-ready for all robots.

### Step 6: Cache Workspace Sampling

`solve_executability.py` samples workspace points per robot during solving.
For large datasets this can dominate runtime.

Add optional cache files keyed by:

- robot name.
- URDF path and file hash or modification time.
- TCP frame.
- sample count.
- seed.
- mounted/free-space constraints.

Cache location:

```text
outputs/umi_processed/.cache/workspace/<key>.npz
```

This can significantly reduce repeated IK runs.

### Step 7: Add Tests Around Stage Boundaries

Recommended focused tests:

- Trajectory-first gate:
  - `smooth` passes.
  - `recoverable` is held or rejected in strict mode.
  - `middle_smooth` and `middle_recoverable` are held or rejected in strict
    mode.
  - `unrecoverable` is rejected.
- Assessment gate:
  - gripper-only problem tolerated.
  - frame-drop-like video problem tolerated.
  - focus/mislabel/action problem blocked.
- Preprocess:
  - smooth episode passes through.
  - recoverable jump is interpolated.
  - boundary-only unrecoverable span crops all modalities.
  - middle unrecoverable is rejected.
- Transform:
  - both action and observation eef_pose are transformed.
  - wrist videos are flipped.
  - metadata transform tags are written.
- Executability:
  - raw episode uses transform-on.
  - transformed episode uses `--no-transform`.
  - summary is embedded correctly.
  - solver return code 1 means "ran, no executable result", not infrastructure
    error.

### Step 8: Resolve ARX UMI TCP Model Selection

Add one of:

- A separate robot registry entry for `arx5_x5_umi`.
- Metadata-based TCP selection.
- A config option mapping source robot/task to TCP frame and URDF.

Until then, reports for ARX UMI should surface a warning when `arx5_x5` is used
on UMI data.

## Recommended User-Facing Explanation

For a user who does not know IK, explain the UMI pipeline like this:

1. First we check whether the raw recording is internally consistent: files,
   videos, timestamps, gripper readings, labels, wrist motion, and EEF pose
   values.
2. Then we inspect the hand/controller trajectory for tracker jumps. Short
   jumps that return are repaired by interpolation. Bad parts only at the start
   or end can be cropped. Bad jumps in the middle reject the episode.
3. Then we export the cleaned pose data into the world-base EEF frame expected
   by replay/training, and flip wrist videos to match that convention.
4. Optionally, we ask a robot-specific question: could a real robot reproduce
   this end-effector path?
5. The IK stage loads the robot model, tries to find joint angles for each
   target pose, checks limits/collisions/singularities/speed, and searches a
   constant xyz placement offset for the whole trajectory.
6. The result is robot-specific. A valid UMI episode may be executable on one
   robot but not another.

## Suggested Near-Term Milestone

The highest-value next change is not to rewrite IK. It is to split orchestration
and reporting:

1. Extract shared `DataProcessUMI` stage functions.
2. Let parent QA Phase 6 call those functions.
3. Add optional parent-QA executability config and dependency gating.
4. Store IK results as robot-specific readiness, separate from generic UMI data
   readiness.

That will make the new features understandable, resumable, and usable from the
main QA pipeline without hiding everything inside one overloaded Phase 6.
