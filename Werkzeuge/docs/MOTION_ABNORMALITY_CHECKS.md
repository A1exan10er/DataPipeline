# Motion Abnormality Checks

Last updated: 2026-06-04

## Purpose

This document describes a practical, safety-first design for detecting abnormal
motion values in robot and UMI episodes. The goal is to identify episodes with
large velocity spikes, jitter, unwanted joint positions, impossible state jumps,
or suspicious motion behavior before the final NAS quarantine pipeline is
enabled.

The checks here are read-only. They must produce evidence and reports first;
they must not move or delete data.

## Sample Analysis

A read-only prototype checker was added:

```text
Werkzeuge/analyze_motion_abnormalities.py
```

Command used for the first bounded NAS sample run:

```bash
python3 Werkzeuge/analyze_motion_abnormalities.py NAS_Sample_Data \
  --output /tmp/motion_abnormality_report_v2 \
  --max-episodes-per-task 8
```

This checked 64 representative episodes from 8 task folders.

Observed output:

```text
Episodes checked: 64
Findings: 251
Status counts:
- pass: 47
- needs_review: 15
- fail_candidate: 2
```

Finding types:

| Check | Count | Meaning |
| --- | ---: | --- |
| `eef_velocity_derived` | 152 | Cartesian EEF velocity derived from adjacent pose rows exceeded threshold. |
| `eef_position_step` | 88 | Cartesian EEF frame-to-frame position jump exceeded threshold. |
| `joint_velocity_reported` | 11 | Reported joint velocity exceeded threshold. |

Important correction from the first prototype run:

Tactile state folders also use `x,y,z` columns. A naive column-name-only checker
misclassified tactile data as EEF pose data and produced noisy false positives.
The checker was changed to use modality folder names:

- EEF rules only apply to folders containing `eef_pose`.
- Joint position rules only apply to folders containing `joint_position`.
- Joint velocity rules only apply to folders containing `joint_velocity`.
- Tactile state streams are not treated as robot EEF motion.

This reduced the same bounded run from 143,622 noisy findings to 251 motion
findings.

## Representative Findings

The strongest fail candidates came from
`Bind_and_secure_the_socks_UMI/20260514/wuhao`.

Examples:

```text
episode_0002:
left_y EEF velocity reached about -5.5 m/s.

episode_0006:
left_y EEF step reached about 0.30 m/frame.
left_y EEF velocity reached about 8.9 m/s.
left_y EEF velocity reached about -12.4 m/s.
left_z EEF velocity reached about 12.8 m/s.
```

These are strong abnormal-motion candidates, but they should still be reviewed
before turning these rules into automatic quarantine decisions. The values are
large enough that they may indicate tracking jumps, coordinate discontinuities,
timestamp issues, or a real but unsafe demonstration segment.

## Detection Strategy

Motion checks should be grouped by modality type.

### Joint Position Checks

Inputs:

```text
observation.state.joint_position/data.csv
actions.joint_position/data.csv
```

Checks:

- absolute joint position outside physical robot limits;
- frame-to-frame joint position jump;
- derived joint velocity from joint position and `timestamp_ms`;
- repeated constant joint position for too long;
- non-numeric, NaN, Inf, or missing values.

Decision policy:

- Physical joint limit violation: `fail` after robot limits are confirmed.
- Extreme jump or derived velocity spike: `needs_review` first, then `fail`
  after thresholds are validated.
- Small isolated spike: `warning` or `needs_review`.

### Joint Velocity Checks

Inputs:

```text
observation.state.joint_velocity/data.csv
```

Checks:

- reported joint velocity above robot-specific limit;
- acceleration derived from adjacent velocity rows;
- jerk derived from acceleration changes;
- non-numeric, NaN, Inf, or missing values.

Decision policy:

- Reported velocity above known hard physical limit: `fail`.
- Velocity above provisional/statistical limit: `needs_review`.
- Acceleration/jerk spikes: `needs_review` until enough examples are reviewed.

### EEF Pose Checks

Inputs:

```text
observation.state.eef_pose/data.csv
actions.eef_pose/data.csv
action.eef_pose/data.csv
```

Checks:

- Cartesian position outside plausible workspace;
- frame-to-frame position jump;
- derived Cartesian velocity;
- derived acceleration;
- rotation representation discontinuity;
- gripper value range when gripper columns are embedded in EEF pose files.

Decision policy:

- Extreme EEF velocity or step: `fail_candidate` in reports, not immediate
  quarantine until reviewed.
- Moderate EEF velocity or step: `needs_review`.
- Position outside confirmed workspace: `fail` after robot/task workspace config
  is available.

### Timestamp Checks

Inputs:

All action/state CSVs with `timestamp_ms`.

Checks:

- timestamp is missing or non-numeric;
- timestamps are not strictly increasing;
- duplicate timestamps;
- large timestamp gaps;
- inconsistent sampling rate.

Decision policy:

- Non-monotonic or duplicate timestamps in essential motion streams: `fail`.
- Large gaps: `needs_review` or `fail` depending on duration and affected
  modality.

## Threshold Design

Do not hardcode final thresholds in Python code. Use a versioned config file,
for example:

```text
configs/quality_rules.yaml
```

Suggested structure:

```yaml
default:
  timestamp_gap_warn_ms: 200
  timestamp_gap_major_ms: 1000

robots:
  arx5:
    joint_position_min: []
    joint_position_max: []
    joint_velocity_warn: []
    joint_velocity_fail: []
  flexiv:
    joint_position_min: []
    joint_position_max: []
  ur:
    joint_position_min: []
    joint_position_max: []
  umi:
    eef_step_warn_m: 0.08
    eef_step_fail_m: 0.25
    eef_velocity_warn_mps: 2.0
    eef_velocity_fail_mps: 5.0

tasks:
  Bind_and_secure_the_socks_UMI:
    eef_velocity_warn_mps: 2.0
    eef_velocity_fail_mps: 5.0
```

Thresholds need three levels:

1. **Hard physical limits**: robot limits, impossible workspace, invalid
   timestamp ordering. These can produce `fail`.
2. **Task/robot configured limits**: expected safe ranges for a task. These
   should start as `needs_review` until validated.
3. **Statistical limits**: learned from normal episodes by task, robot, and
   operator. These should initially produce `needs_review`, not `fail`.

## Evidence Requirements

Every finding must include enough information to debug and review the decision:

```text
episode_path
csv_path
check_name
severity
message
column
value
threshold
timestamp_ms
unit
row_number when available
```

For NAS-scale operation, reports should also include per-episode top evidence:

- maximum absolute value;
- timestamp of maximum;
- number of threshold crossings;
- first several examples;
- whether the same spike appears in both action and observation streams.

## Safety-First Decision Rules

Initial rule mapping:

```text
critical timestamp parse/order issue -> fail
hard physical joint limit violation -> fail
extreme EEF/joint velocity spike -> fail_candidate
moderate EEF/joint velocity spike -> needs_review
jitter/acceleration/jerk anomaly -> needs_review
small isolated anomaly -> warning
```

Important:

`fail_candidate` in the prototype should not directly move data. In the final
pipeline it should map to either `needs_review` or `fail` only after thresholds
are reviewed against representative samples.

## Efficient NAS-Scale Implementation

The production checker should stream CSV rows and keep only rolling state:

- previous timestamp;
- previous position;
- previous velocity;
- current maxima;
- threshold-crossing counts;
- first N evidence examples.

It should not load full CSV files into memory.

For huge NAS data:

- process episode-by-episode;
- write JSONL findings incrementally;
- cap evidence examples per check to avoid enormous reports;
- support resume;
- support task/date/operator filters;
- limit worker count to avoid overloading NAS I/O;
- run read-only first.

## Next Implementation Steps

1. Move prototype thresholds into `configs/quality_rules.yaml`.
2. Add robot-specific physical joint limits for ARX, Flexiv, UR, and other
   supported robots.
3. Add task-specific EEF workspace and duration rules.
4. Add per-check evidence caps and aggregate counts.
5. Add statistical baseline reports from episodes labeled `完全正常`.
6. Review fail candidates manually before allowing any quarantine decision.
7. Integrate this checker into the main quality pipeline decision engine.
