# NAS Sample Data Structure Notes

Date inspected: 2026-05-27

Sample root:

```text
/home/tianyu/Downloads/NAS_Sample_Data
```

This document records the observed folder structure and file formats in the downloaded NAS sample task folders. It intentionally focuses on structure, schemas, and processing implications instead of analyzing the full numeric contents of the recorded data.

## Sample Sets

The sample root currently contains task folders only. Previously downloaded ZIP files have been removed.

```text
NAS_Sample_Data/
|-- Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_UMI/
|-- Bind_and_secure_the_socks_UMI/
|-- Folding_trousers_ARX/
|-- assemble_bactory_umi/
|-- assemble_the_battery/
|-- classify_the_battery_ARX/
`-- put_cups_in_line_flexiv/
```

Observed episode counts, using one `metadata.json` per episode:

| Task folder | Episodes |
| --- | ---: |
| `Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_UMI` | 2,128 |
| `Bind_and_secure_the_socks_UMI` | 460 |
| `Folding_trousers_ARX` | 16 |
| `assemble_bactory_umi` | 1,027 |
| `assemble_the_battery` | 508 |
| `classify_the_battery_ARX` | 53 |
| `put_cups_in_line_flexiv` | 75 |
| **Total** | **4,267** |

The folder pattern is:

```text
<task_name>/
`-- <YYYYMMDD>/
    `-- <operator_or_username>/
        `-- <episode_folder>/
```

The second-level date folders currently include:

```text
Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_UMI/
|-- 20260514
|-- 20260515
|-- 20260516
|-- 20260517
|-- 20260518
|-- 20260519
|-- 20260520
|-- 20260521
|-- 20260522
|-- 20260523
|-- 20260524
`-- 20260526

Bind_and_secure_the_socks_UMI/
|-- 20260514
|-- 20260519
|-- 20260521
`-- 20260522

Folding_trousers_ARX/
`-- 20260526

assemble_bactory_umi/
|-- 20260427
|-- 20260429
|-- 20260506
|-- 20260507
|-- 20260508
|-- 20260521
|-- 20260522
`-- 20260523

assemble_the_battery/
|-- 20260421
|-- 20260422
|-- 20260423
|-- 20260428
|-- 20260520
`-- 20260525

classify_the_battery_ARX/
`-- 20260526

put_cups_in_line_flexiv/
`-- 20260526
```

Episode folder names have at least two observed forms:

```text
episode_####/
episode_####_<YYYYMMDD-HHMMSS>_<operator>_<robot>_<controller>/
```

Examples:

```text
Bind_and_secure_the_socks_UMI/20260514/wuhao/episode_0000/
assemble_the_battery/20260428/wangyong/episode_0085_20260428-114939_wangyong_arx5_none/
Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_UMI/20260518/huodaoxing/episode_0000_20260518-172857_huodaoxing_ur_spacemouse/
```

## Episode Layout

A representative UMI episode has this structure:

```text
episode_0000/
|-- .checksum_manifest
|-- actions.eef_pose/
|   `-- data.csv
|-- checksums.sha256
|-- meta/
|   `-- episode.json
|-- metadata.json
|-- observation.image.left_wrist_left_tactile/
|   |-- timestamps.csv
|   `-- video.mp4
|-- observation.image.left_wrist_right_tactile/
|   |-- timestamps.csv
|   `-- video.mp4
|-- observation.image.left_wrist_view/
|   |-- timestamps.csv
|   `-- video.mp4
|-- observation.image.right_wrist_left_tactile/
|   |-- timestamps.csv
|   `-- video.mp4
|-- observation.image.right_wrist_right_tactile/
|   |-- timestamps.csv
|   `-- video.mp4
|-- observation.image.right_wrist_view/
|   |-- timestamps.csv
|   `-- video.mp4
|-- observation.state.eef_pose/
|   `-- data.csv
|-- observation.state.gripper/
|   `-- data.csv
`-- observation.state.raw_gripper_rotation/
    `-- data.csv
```

Newer or richer episodes may include additional files and modality folders:

```text
episode_0000_20260518-172857_huodaoxing_ur_spacemouse/
|-- action.eef_pose/
|   |-- data.csv
|   `-- data_raw.csv
|-- actions.eef_pose/
|   |-- data.csv
|   `-- data_raw.csv
|-- observation.image.<camera_name>/
|   |-- config.csv
|   |-- timestamps.csv
|   |-- timestamps_raw.csv
|   |-- video.mp4
|   `-- video.original.mp4
|-- observation.image.flow_<camera_name>/
|   `-- video.mp4
|-- observation.state.<state_name>/
|   |-- data.csv
|   `-- data_raw.csv
`-- timing_log.jsonl
```

Some robot datasets add or replace modalities. For example, `assemble_the_battery` includes:

```text
actions.joint_position/data.csv
observation.image.third_view/timestamps.csv
observation.image.third_view/video.mp4
observation.state.joint_position/data.csv
observation.state.joint_velocity/data.csv
```

ARX and Flexiv samples in the current folder use the same general episode structure but show real-robot variants:

```text
Folding_trousers_ARX/20260526/wangshuai/episode_0000_20260527-030620_wangshuai_arx5_none/
classify_the_battery_ARX/20260526/lipengfei/episode_0000_20260527-011624_lipengfei_arx5_none/
put_cups_in_line_flexiv/20260526/yangwenzhe/episode_0000_20260527-005554_yangwenzhe_flexiv_gello/
```

These examples include `robot` values such as `arx5` and `flexiv`. The episode folder suffix also records the controller source, for example `none` for direct pull/push style control and `gello` for GELLO.

## Robot And Camera Setup

UMI samples have left and right wrist observation cameras plus two tactile cameras on each wrist:

```text
observation.image.left_wrist_view
observation.image.right_wrist_view
observation.image.left_wrist_left_tactile
observation.image.left_wrist_right_tactile
observation.image.right_wrist_left_tactile
observation.image.right_wrist_right_tactile
```

Real-robot samples, such as UR, ARX, Flexiv, and other robots not all represented in this sample folder, can have additional environment cameras:

- `observation.image.third_view` records a global view of the manipulation environment.
- `observation.image.second_third_view` appears in some special tasks that need another global or auxiliary viewpoint. It is not common across all tasks.

Some newer real-robot episodes also include optical-flow style visual streams derived from tactile cameras:

```text
observation.image.flow_left_wrist_left_tactile
observation.image.flow_left_wrist_right_tactile
observation.image.flow_right_wrist_left_tactile
observation.image.flow_right_wrist_right_tactile
```

## Operator Control And Robot Motion

Real-robot operators may control or guide the robot with devices such as GELLO, SpaceMouse, or direct pull/push operation. These devices tell the robot where to go and what to do, for example moving to a target point, picking up an object, or executing a task-specific action.

For real robots, recorded motion is commonly stored as joint angles and gripper distance:

- Joint position columns represent joint angles. Unit: radians.
- Gripper position or gripper distance columns represent gripper opening distance. Unit: meters.
- Joint velocity columns represent joint velocity streams when present.

ARX-style bimanual headers use left/right arm naming, for example:

```text
timestamp_ms,left_j1,left_j2,left_j3,left_j4,left_j5,left_j6,left_gripper,right_j1,right_j2,right_j3,right_j4,right_j5,right_j6,right_gripper
```

Flexiv-style headers use single-arm seven-joint naming, for example:

```text
timestamp_ms,joint_1.pos,joint_2.pos,joint_3.pos,joint_4.pos,joint_5.pos,joint_6.pos,joint_7.pos,gripper.pos
```

## File Formats

| File or folder pattern | Format | Purpose |
| --- | --- | --- |
| `metadata.json` | JSON | Main per-episode metadata and modality registry. |
| `meta/episode.json` | JSON | Compact metadata, primarily modality names. |
| `<modality>/data.csv` | CSV | Time-indexed action or robot state stream. |
| `<modality>/data_raw.csv` | CSV | Raw version of an action or state stream, present in newer episodes. |
| `observation.image.*/config.csv` | CSV | Camera or stream configuration, present in newer episodes. |
| `observation.image.*/timestamps.csv` | CSV | Timestamp rows for the corresponding video frames. |
| `observation.image.*/timestamps_raw.csv` | CSV | Raw timestamp rows, present in newer episodes. |
| `observation.image.*/video.mp4` | MP4 video | Camera stream for one visual modality. |
| `observation.image.*/video.original.mp4` | MP4 video | Original camera video, present in some newer episodes. |
| `observation.image.flow_*/video.mp4` | MP4 video | Optical-flow or derived visual stream, present in some newer episodes. |
| `timing_log.jsonl` | JSON Lines | Per-call timing log with one JSON object per line. |
| `.checksum_manifest` | Text | Manifest used for integrity checking. |
| `checksums.sha256` | Text | SHA-256 checksums. Present in some observed episodes. |

The current task-folder samples contain these file type counts:

| Extension/type | Count |
| --- | ---: |
| `csv` | 59,222 |
| `mp4` | 30,495 |
| `json` | 8,534 |
| `.checksum_manifest` | 4,267 |
| `sha256` | 3,432 |
| `jsonl` | 574 |

## Metadata Schema

Observed top-level keys in `metadata.json`:

```text
cameras
created_at
duration_seconds
end_time
episode_id
episode_index
fps_actual
fps_config
frame_integrity
language_instruction
modalities
nas_uploaded
quality
recording_session_id
robot
robot_sn
session_id
start_time
task_id
task_key
task_title
total_frames
username
```

Important schema-level observations:

- `modalities` is the central map from modality name to format details such as `type`, row or frame count, and nominal frequency.
- `cameras` maps image modality names to camera configuration such as width, height, fps, camera type, and device path.
- `frame_integrity` records dropped-frame summary values per image modality.
- `quality.labels` can be used to filter usable recordings. The existing `check_episode_durations.py` script already filters for the normal label `完全正常`.
- `duration_seconds`, `total_frames`, `fps_actual`, and `fps_config` are enough for duration reporting without opening the large MP4 or CSV payloads.

Observed `meta/episode.json` top-level key:

```text
modalities
```

## Modality Naming

Observed modality names use a dotted namespace convention:

```text
action.<action_type>
actions.<action_type>
observation.state.<state_type>
observation.image.<camera_or_sensor_name>
observation.image.flow_<camera_or_sensor_name>
```

Observed image, action, and state modalities across current samples:

```text
actions.eef_pose
action.eef_pose
observation.image.left_wrist_left_tactile
observation.image.left_wrist_right_tactile
observation.image.left_wrist_view
observation.image.right_wrist_left_tactile
observation.image.right_wrist_right_tactile
observation.image.right_wrist_view
observation.image.third_view
observation.image.second_third_view
observation.image.flow_left_wrist_left_tactile
observation.image.flow_left_wrist_right_tactile
observation.image.flow_right_wrist_left_tactile
observation.image.flow_right_wrist_right_tactile
observation.state.eef_pose
observation.state.gripper
observation.state.joint_position
observation.state.joint_velocity
observation.state.left_wrist_left_tactile
observation.state.left_wrist_right_tactile
observation.state.right_wrist_left_tactile
observation.state.right_wrist_right_tactile
observation.state.raw_gripper_rotation
```

Observed robot joint modalities:

```text
actions.joint_position
observation.image.third_view
observation.image.second_third_view
observation.state.joint_position
observation.state.joint_velocity
```

Processing code should discover modalities from `metadata.json` and the episode directory contents instead of hard-coding one fixed episode layout, because different tasks, dates, and robot setups may include different action, state, camera, raw, original-video, flow-video, and timing-log streams.

## CSV Schemas

Representative CSV headers:

`actions.eef_pose/data.csv` and `observation.state.eef_pose/data.csv`:

```text
timestamp_ms,left_x,left_y,left_z,left_r1,left_r2,left_r3,left_r4,left_r5,left_r6,left_gripper,right_x,right_y,right_z,right_r1,right_r2,right_r3,right_r4,right_r5,right_r6,right_gripper
```

`observation.state.gripper/data.csv`:

```text
timestamp_ms,left_gripper,right_gripper
```

`observation.state.raw_gripper_rotation/data.csv`:

```text
timestamp_ms,left_raw_gripper_rotation,right_raw_gripper_rotation
```

`actions.joint_position/data.csv`:

```text
timestamp_ms,left_j1,left_j2,left_j3,left_j4,left_j5,left_j6,left_gripper,right_j1,right_j2,right_j3,right_j4,right_j5,right_j6,right_gripper
```

`actions.joint_position/data.csv` in Flexiv samples:

```text
timestamp_ms,joint_1.pos,joint_2.pos,joint_3.pos,joint_4.pos,joint_5.pos,joint_6.pos,joint_7.pos,gripper.pos
```

`observation.state.joint_position/data.csv` in Flexiv samples:

```text
timestamp_ms,j1,j2,j3,j4,j5,j6,j7,gripper
```

`observation.state.joint_velocity/data.csv`:

```text
timestamp_ms,left_v1,left_v2,left_v3,left_v4,left_v5,left_v6,right_v1,right_v2,right_v3,right_v4,right_v5,right_v6
```

`observation.state.joint_velocity/data.csv` in Flexiv samples:

```text
timestamp_ms,v1,v2,v3,v4,v5,v6,v7
```

`observation.image.*/timestamps.csv`:

```text
timestamp_ms,is_new
```

Common rule: CSV streams are timestamped by `timestamp_ms`. Video frame timing is represented separately from the MP4 payload via `timestamps.csv`.

`timing_log.jsonl` rows are JSON objects. A representative row contains keys like:

```text
method,start,end,duration_ms,frame,timestamp
```

## Processing Implications

- Avoid downloading complete NAS datasets when only structure, metadata, or duration summaries are required.
- Prefer metadata-first processing:
  - scan `metadata.json` files to discover episodes;
  - read `modalities` to know what streams exist;
  - read row/frame counts and duration fields before opening large CSV or MP4 files;
  - open payload files only for selected episodes or selected modalities.
- The current local sample root is folder-based, not ZIP-based. Tools should walk directories directly.
- Episode discovery should key off `metadata.json` under folders whose names start with `episode_`, not just exact `episode_####` folder names.
- Do not assume every episode contains `checksums.sha256`; verify from the actual sample or manifest before relying on it.
- Do not assume all tasks have the same camera set. Some samples have six wrist/tactile views; other samples include `third_view`, `second_third_view`, flow videos, or original-video files.
- Do not assume all joint CSVs share the same column names. ARX-style bimanual samples use `left_j*` and `right_j*`; Flexiv samples use `j1`-`j7` or `joint_*.pos`.
- Interpret real-robot joint position values as radians and gripper position or distance values as meters unless task-specific metadata says otherwise.
- Existing duration analysis can remain based on `metadata.json`, because duration and frame/fps values are metadata fields.

## What Was Added

This document was added and then updated to capture the current folder-only NAS sample structure:

```text
docs/NAS_SAMPLE_DATA_STRUCTURE.md
```

No raw recorded values were analyzed in depth. Only folder names, file names, file types, metadata keys, modality names, CSV headers, one representative JSONL key pattern, and user-provided robot/camera/control context were inspected or documented.

## Suggested Next Enhancements

- Add a lightweight manifest generator that walks the task folders and emits one row per episode with task, date, operator, episode id, modalities, duration, frame count, and quality labels.
- Extend `check_episode_durations.py` or add a sibling tool so it emits a folder-based structural manifest for the current task folders.
- Add schema validation checks for required files per discovered modality, for example `video.mp4` plus `timestamps.csv` for every `observation.image.*` modality.
- Add a small README section explaining how to run structure-only scans against selected NAS task folders without copying full payloads locally.
