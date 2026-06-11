# NAS Sample Data Structure

Date inspected: 2026-05-27

---

## Overview

This document records the observed folder structure and file formats in the NAS sample task folders. It focuses on structure, schemas, and processing implications rather than numeric content analysis.

---

## Directory Layout

```
<task_name>/
└── <YYYYMMDD>/
    └── <operator_or_username>/
        └── <episode_folder>/
```

Episode folder names have two observed forms:

```
episode_####/
episode_####_<YYYYMMDD-HHMMSS>_<operator>_<robot>_<controller>/
```

Examples:

```
Bind_and_secure_the_socks_UMI/20260514/wuhao/episode_0000/
assemble_the_battery/20260428/wangyong/episode_0085_20260428-114939_wangyong_arx5_none/
Add_water_.../20260518/huodaoxing/episode_0000_20260518-172857_huodaoxing_ur_spacemouse/
```

---

## Episode Layout

### Minimal UMI episode

```
episode_0000/
├── .checksum_manifest
├── checksums.sha256
├── metadata.json
├── meta/
│   └── episode.json
├── actions.eef_pose/
│   └── data.csv
├── observation.image.left_wrist_view/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.image.right_wrist_view/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.image.left_wrist_left_tactile/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.image.left_wrist_right_tactile/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.image.right_wrist_left_tactile/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.image.right_wrist_right_tactile/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.state.eef_pose/
│   └── data.csv
├── observation.state.gripper/
│   └── data.csv
└── observation.state.raw_gripper_rotation/
    └── data.csv
```

### Richer newer episode (additional optional files)

```
episode_0000_.../
├── action.eef_pose/
│   ├── data.csv
│   └── data_raw.csv
├── actions.eef_pose/
│   ├── data.csv
│   └── data_raw.csv
├── observation.image.<camera>/
│   ├── config.csv
│   ├── timestamps.csv
│   ├── timestamps_raw.csv
│   ├── video.mp4
│   └── video.original.mp4
├── observation.image.flow_<camera>/
│   └── video.mp4
├── observation.state.<state>/
│   ├── data.csv
│   └── data_raw.csv
└── timing_log.jsonl
```

### Real-robot episode (ARX example, adds joint modalities and third_view)

```
episode_0000_.../
├── actions.joint_position/
│   └── data.csv
├── observation.image.third_view/
│   ├── timestamps.csv
│   └── video.mp4
├── observation.state.joint_position/
│   └── data.csv
└── observation.state.joint_velocity/
    └── data.csv
```

---

## Modality Naming Convention

Modalities follow a dotted namespace:

```
action.<type>
actions.<type>
observation.state.<type>
observation.image.<camera_or_sensor>
observation.image.flow_<camera_or_sensor>
```

### Observed modalities across all sample tasks

**Image modalities**
- `observation.image.left_wrist_view`
- `observation.image.right_wrist_view`
- `observation.image.left_wrist_left_tactile`
- `observation.image.left_wrist_right_tactile`
- `observation.image.right_wrist_left_tactile`
- `observation.image.right_wrist_right_tactile`
- `observation.image.third_view` — global environment camera, real-robot only
- `observation.image.second_third_view` — auxiliary viewpoint, uncommon
- `observation.image.flow_*` — optical-flow derived streams, newer episodes only

**State modalities**
- `observation.state.eef_pose`
- `observation.state.gripper`
- `observation.state.joint_position`
- `observation.state.joint_velocity`
- `observation.state.raw_gripper_rotation`
- `observation.state.left_wrist_left_tactile`
- `observation.state.left_wrist_right_tactile`
- `observation.state.right_wrist_left_tactile`
- `observation.state.right_wrist_right_tactile`

**Action modalities**
- `actions.eef_pose`
- `action.eef_pose`
- `actions.joint_position`

> Processing code must discover modalities dynamically from `metadata.json` and the episode directory. Do not hard-code a fixed modality set.

---

## File Formats

| File pattern | Format | Purpose |
|---|---|---|
| `metadata.json` | JSON | Main per-episode metadata and modality registry |
| `meta/episode.json` | JSON | Compact metadata, primarily modality names |
| `<modality>/data.csv` | CSV | Time-indexed action or robot state stream |
| `<modality>/data_raw.csv` | CSV | Raw stream, present in newer episodes |
| `observation.image.*/config.csv` | CSV | Camera configuration, present in newer episodes |
| `observation.image.*/timestamps.csv` | CSV | Frame timestamps for the video |
| `observation.image.*/timestamps_raw.csv` | CSV | Raw frame timestamps, newer episodes only |
| `observation.image.*/video.mp4` | MP4 | Camera stream |
| `observation.image.*/video.original.mp4` | MP4 | Original camera video, some newer episodes |
| `observation.image.flow_*/video.mp4` | MP4 | Optical-flow stream, newer episodes only |
| `timing_log.jsonl` | JSON Lines | Per-call timing log |
| `.checksum_manifest` | Text | Integrity manifest |
| `checksums.sha256` | Text | SHA-256 checksums, not present in all episodes |

---

## metadata.json Schema

### Top-level keys

```
cameras, created_at, duration_seconds, end_time,
episode_id, episode_index, fps_actual, fps_config,
frame_integrity, language_instruction, modalities,
nas_uploaded, quality, recording_session_id,
robot, robot_sn, session_id, start_time,
task_id, task_key, task_title, total_frames, username
```

### Key fields for QA

| Field | Purpose |
|---|---|
| `modalities` | Central map from modality name to type, row/frame count, and frequency |
| `cameras` | Image modality → width, height, fps, camera type, device path |
| `frame_integrity` | Dropped-frame summary per image modality |
| `quality.labels` | Filter labels, e.g. `完全正常` for usable recordings |
| `duration_seconds` | Episode length in seconds |
| `total_frames` | Total frame count |
| `fps_actual` | Actual recorded FPS |
| `fps_config` | Configured FPS |
| `robot` | Robot identifier, e.g. `arx5`, `flexiv`, `ur` |
| `username` | Operator identifier |

> `duration_seconds`, `total_frames`, `fps_actual`, and `fps_config` are sufficient for duration reporting without opening MP4 or CSV payload files.

---

## CSV Schemas

### `actions.eef_pose/data.csv` and `observation.state.eef_pose/data.csv`

```
timestamp_ms,
left_x,left_y,left_z,left_r1,left_r2,left_r3,left_r4,left_r5,left_r6,left_gripper,
right_x,right_y,right_z,right_r1,right_r2,right_r3,right_r4,right_r5,right_r6,right_gripper
```

### `observation.state.gripper/data.csv`

```
timestamp_ms,left_gripper,right_gripper
```

### `actions.joint_position/data.csv` — ARX bimanual

```
timestamp_ms,
left_j1,left_j2,left_j3,left_j4,left_j5,left_j6,left_gripper,
right_j1,right_j2,right_j3,right_j4,right_j5,right_j6,right_gripper
```

### `actions.joint_position/data.csv` — Flexiv single arm

```
timestamp_ms,joint_1.pos,joint_2.pos,joint_3.pos,joint_4.pos,joint_5.pos,joint_6.pos,joint_7.pos,gripper.pos
```

### `observation.state.joint_position/data.csv` — Flexiv

```
timestamp_ms,j1,j2,j3,j4,j5,j6,j7,gripper
```

### `observation.state.joint_velocity/data.csv` — ARX bimanual

```
timestamp_ms,left_v1,left_v2,left_v3,left_v4,left_v5,left_v6,right_v1,right_v2,right_v3,right_v4,right_v5,right_v6
```

### `observation.state.joint_velocity/data.csv` — Flexiv

```
timestamp_ms,v1,v2,v3,v4,v5,v6,v7
```

### `observation.image.*/timestamps.csv`

```
timestamp_ms,is_new
```

### `timing_log.jsonl` row keys

```
method, start, end, duration_ms, frame, timestamp
```

---

## Units

| Value type | Unit |
|---|---|
| Joint position | Radians |
| Gripper position or distance | Meters |
| Timestamps | Milliseconds |
| Duration | Seconds |

---

## Robot and Camera Configurations

### UMI (wrist-mounted cameras, no third_view)

Six image modalities: two wrist views and four tactile cameras.

### ARX real-robot (arx5)

Bimanual. Joint columns use `left_j*` / `right_j*` naming. Typically includes `observation.image.third_view`.

### Flexiv real-robot

Single arm, seven joints. Joint columns use `joint_*.pos` or `j1`–`j7` naming. Controller may be `gello`.

---

## Processing Rules

- Discover episodes by locating `metadata.json` files, not by matching exact folder name patterns.
- Read `modalities` from `metadata.json` to know which streams exist before touching any payload file.
- Do not assume `checksums.sha256` is present; check before relying on it.
- Do not assume all tasks share the same camera or modality set.
- Do not assume all joint CSVs share the same column names; detect robot type from metadata first.
- Timestamps for image modalities live in `timestamps.csv`; timestamps for state and action modalities live in the first column of `data.csv`.
- `observation.image.flow_*` streams are derived and optional; their absence is not a quality issue.
- Prefer metadata-first processing: read `metadata.json` fields before opening large CSV or MP4 files.
