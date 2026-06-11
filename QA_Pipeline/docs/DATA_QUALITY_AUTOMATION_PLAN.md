# Data Quality Automation Plan

Last updated: 2026-05-27

---

## Goal

Develop an automated data quality checking pipeline to retain high-quality robot and UMI collection episodes, while identifying, isolating, or removing substandard data once rules are sufficiently validated. The entire process must be explainable, traceable, rollback-safe, and adaptable to different tasks, robots, camera configurations, and operator devices.

**The current focus is detection and reporting, not automatic deletion.** Rules must be validated against real samples before any data is moved or removed.

---

## Core Principles

- Do not delete data first. Generate reports first; isolate only when necessary.
- Separate detection from action. Quality checks only output structured findings; downstream tools move or process failed episodes based on the report.
- Prefer metadata and lightweight file checks. Avoid reading large CSV or MP4 files unless necessary.
- Every quality decision must include a specific reason.
- Thresholds should be configured per task, robot, and camera type where possible.
- Use physically meaningful checks for abnormal velocity, jitter, joint values, gripper distance, and episode duration.
- For UMI data intended for real-robot training, validate that motion trajectories satisfy the inverse kinematics of the target robot.
- Prefer marking edge cases as `needs_review` rather than forcing a pass or fail verdict.
- Write every decision into a manifest so downstream analysis can filter data without changing the original directory structure.

---

## Status Values

| Status | Meaning |
|---|---|
| `pass` | No critical or major issues found |
| `warning` | Structurally usable but has minor quality issues |
| `fail` | Has critical issues or multiple major issues; judged unusable |
| `needs_review` | Statistically anomalous or uncertain; requires human review |

---

## Severity Levels

| Severity | Meaning |
|---|---|
| `critical` | Key file missing or unreadable, invalid metadata, corrupted payload, severe sync failure |
| `major` | Severe frame drop, large timestamp gap, significant row/frame count mismatch, abnormal robot state jump |
| `minor` | Slight FPS deviation, slight duration anomaly, missing optional file, non-blocking schema difference |
| `info` | Does not affect pass/fail; useful for statistics and summaries |

---

## Target Outputs

### Primary report: `quality_report.csv`

One row per episode.

```
episode_path, task, date, operator, robot, controller,
status, severity, reasons, checked_at
```

### Detailed findings: `quality_findings.jsonl`

One line per specific issue.

```json
{
  "episode_path": "...",
  "check_name": "...",
  "phase": 1,
  "severity": "major",
  "status": "fail",
  "message": "...",
  "details": {}
}
```

### Summary report: `quality_summary.md`

Human-readable rollup including:
- Total episodes checked
- Count of pass, warning, fail, needs_review
- Issue counts by check name
- Issue counts by task
- Issue counts by operator
- Issue counts by robot and controller
- Examples of failed and borderline episodes

---

## Pipeline Phases

### Phase 1 — Structure and Metadata Checks

**Status: not started**

Capture stable, low-cost, reliable issues without reading large video files.

**Checks:**
- `metadata.json` exists
- `metadata.json` is valid JSON
- Episode folder name starts with `episode_`
- Parent path follows `<task>/<date>/<operator>/<episode>`
- Required metadata fields present: `task_key`, `episode_index`, `duration_seconds`, `total_frames`, `fps_actual` or `fps_config`, `modalities`, `quality`
- `modalities` is readable and matches actual modality folders
- `.checksum_manifest` exists
- Required modality files exist: `data.csv` for CSV modalities; `video.mp4` and `timestamps.csv` for image modalities
- Required files are not empty
- `quality.labels` exists and is usable for quality filtering

---

### Phase 2 — Duration and Count Consistency Checks

**Status: not started**

Use metadata and lightweight CSV/timestamp statistics to identify suspicious recordings.

**Checks:**
- `duration_seconds` exists and is positive
- `total_frames` is positive
- `duration_seconds`, `total_frames`, and FPS are roughly consistent
- Episode duration is not a task-level outlier
- Modality row or frame counts are internally consistent
- Image `timestamps.csv` row count is close to video frame count
- Action/state CSV row count is close to expected frame count

**Statistics approach:**
- Group by `task_key`
- Compute median and IQR
- Flag duration outliers as `needs_review` first
- Only escalate to `fail` after thresholds are validated as stable

---

### Phase 3 — Timestamp Synchronization Checks

**Status: not started**

Detect files that exist but have unsynchronized modalities or corrupted time sequences.

**Checks:**
- Timestamps are monotonically increasing
- No duplicate timestamps
- No large gaps in `timestamp_ms`
- Actual frequency of each modality is close to expected FPS or control frequency
- Start and end times of each modality are roughly aligned
- If both `timestamps_raw.csv` and `timestamps.csv` exist, the difference is explainable

**Notes:**
- UMI and real-robot episodes may have different camera configurations
- Real-robot episodes may include `third_view`, `second_third_view`, and flow videos
- The checker must discover modalities automatically; do not require a fixed camera set

---

### Phase 4 — Video Health Checks

**Status: not started**

Detect corrupted, blank, frozen, or metadata-inconsistent videos.

**Checks:**
- MP4 can be opened by ffprobe, OpenCV, or another reliable reader
- Frame count is retrievable
- Video duration roughly matches metadata and timestamps
- Resolution matches metadata or `config.csv`
- Sampled frames are not all black, all white, or empty
- Sampled frames are not frozen across the entire episode
- Future: blur, brightness checks

**Performance principle:**
- Sample frames by default; do not fully decode all videos
- Full decoding only for suspicious or explicitly specified episodes

---

### Phase 5 — Robot State Reasonableness Checks

**Status: not started**

Detect impossible or corrupted robot motion data.

**Checks:**
- CSV values parse correctly
- No NaN or Inf values
- Timestamps are monotonically increasing
- Joint positions are finite
- Gripper values are finite
- Joint values are within robot-specific joint limits
- Gripper distance is within robot-specific limits
- Episode duration is within task-expected range
- Velocity is finite and within robot velocity limits
- Acceleration and jerk estimated from position are not anomalous
- Per-frame joint step is below robot threshold
- Per-frame gripper step is below robot threshold
- End-effector pose step is below robot threshold
- Jitter score is below task and robot threshold
- Motion is not abnormally static, unless the task allows static phases

**Units:**
- Real-robot joint positions: radians
- Real-robot gripper distance or position: meters
- Thresholds should be configured per robot where possible

**Robot column patterns:**
- ARX bimanual: `left_j*`, `right_j*`, `left_gripper`, `right_gripper`
- Flexiv: `j1`–`j7` or `joint_*.pos`, `gripper`

**Recommended anomaly approach:**
- Compute `dt` from timestamps; do not assume fixed frame rate
- Compute velocity from adjacent positions when no velocity stream exists
- Compute acceleration from velocity
- Compute jerk from acceleration when necessary
- Use median filter or Hampel filter to detect isolated spikes without masking sustained anomalies
- Report both maximum and robust quantiles: p95, p99, p99.9
- Directly fail impossible values: NaN, Inf, beyond physical hard limits, large discontinuous jumps
- Mark statistically anomalous but not clearly impossible cases as `needs_review`

**Suggested output metrics:**
```
max_joint_abs, max_joint_step, max_velocity,
max_acceleration, max_jerk, jitter_score, static_ratio
```

---

### Phase 6 — Inverse Kinematics Compatibility Checks

**Status: not started**

Filter UMI episodes whose trajectories cannot be executed by the target real robot.

**Target robots:** UR, ARX5, Flexiv, Aloha, Piper, Franka

**Core idea:**
- Use the UMI-recorded end-effector trajectory as the desired task-space trajectory
- For each target robot, load the robot model, joint limits, gripper limits, base frame, tool frame, and IK solver
- Solve IK along the entire trajectory
- Check for continuous, joint-limit-satisfying, self-consistent solutions
- Only episodes passing IK compatibility for a target robot are marked as training-ready for that robot

**IK status values:**
- `ik_pass` — IK solved continuously, pose error acceptable, joint motion smooth
- `ik_warning` — IK succeeded but trajectory stays near joint limits or workspace boundary
- `ik_fail` — Extended segments with no valid IK solution, or joint limit violations, or velocity/acceleration exceeds hard limits
- `ik_needs_review` — Calibration transform missing or uncertain
- `ik_not_applicable` — Episode is not UMI data

**Per-robot output fields:**
```
episode_path, target_robot, ik_status, ik_reasons,
ik_success_ratio, ik_max_position_error, ik_max_rotation_error,
ik_mean_position_error, ik_joint_limit_violation_count,
ik_max_joint_step, ik_max_joint_velocity, ik_max_joint_acceleration,
ik_failure_segments
```

---

### Phase 7 — Aggregate Quality Verdict

**Status: not started**

Combine structure, timestamp, video, robot state, outlier, and IK results into a final quality conclusion.

**Decision logic:**
- Structural `critical` issue → `fail`
- Required video or CSV unreadable → `fail`
- Impossible outlier values → `fail`
- Severe timestamp sync corruption → `fail`
- Task-level duration outlier → `needs_review` unless clearly impossible
- UMI episode fails IK for a target robot → not `training_ready` for that robot
- IK pass for one robot does not imply pass for all robots
- `warning` must not trigger automatic deletion

**Final training readiness output:**
```
episode_path, general_quality_status, training_ready,
target_robot, ik_status, reasons
```

---

### Phase 8 — Quarantine Workflow

**Status: not started**

Separate confirmed substandard data from usable data before any irreversible deletion.

**Quarantine directory:**
```
<task>/_quarantine/<date>/<operator>/<episode>
```

**Behavior:**
- Read `quality_report.csv`
- Only move episodes with `status=fail`
- Skip `needs_review` episodes by default
- Write a move manifest before moving anything
- Default to dry-run mode
- Never permanently delete

**Move manifest fields:**
```
original_path, new_path, status, reasons, moved_at
```

---

### Phase 9 — Human Review Support

**Status: not started**

Make human judgment of borderline episodes faster and more traceable.

**Review materials per episode:**
- Key camera sampled thumbnails
- Optional short video preview
- CSV statistics summary
- Duration and dropped-frame summary
- Main issues per episode
- Suggested status and reason

This phase focuses on `needs_review` and high-impact `warning` episodes.

---

## State Storage

All intermediate pipeline state is stored in a single SQLite file:

```
outputs/qa_pipeline.db
```

### Table: `episodes`

```sql
CREATE TABLE episodes (
    episode_path      TEXT PRIMARY KEY,
    task              TEXT,
    date              TEXT,
    operator          TEXT,
    robot             TEXT,
    controller        TEXT,
    phases_completed  TEXT,  -- JSON array of completed phase numbers
    phase_status      TEXT,  -- JSON dict: {phase_number: status}
    metrics           TEXT,  -- JSON dict: all numeric metrics from all phases
    final_status      TEXT,
    training_ready    INTEGER,
    last_updated      TEXT
);
```

### Table: `findings`

```sql
CREATE TABLE findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_path  TEXT,
    phase         INTEGER,
    check_name    TEXT,
    severity      TEXT,
    status        TEXT,
    message       TEXT,
    details       TEXT   -- JSON
);
```

**All SQL queries must use parameterized placeholders (`?`), never f-string interpolation.**

---

## Final Report Export

After the pipeline completes, export three files from SQLite:

| File | Purpose |
|---|---|
| `quality_report.csv` | One row per episode, used for programmatic filtering |
| `quality_findings.jsonl` | One line per finding, used for debugging and human review |
| `quality_summary.md` | Human-readable rollup with counts by task, operator, and robot |
