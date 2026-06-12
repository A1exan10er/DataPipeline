# data/scripts

Utilities for inspecting, transforming, and visualizing UMI episode data.

All scripts accept either a single episode directory (`.../class_name/episode_XXXX`)
or a class directory containing one or more `episode_*` subdirectories. They reject
any other path with a usage hint.

## Scripts

### `validate_raw_data.py`
Checks raw gripper readings and image-stream timestamps in each episode and writes
structured JSON reports.

- **Gripper check** — aligns `observation.state.gripper/data.csv` against
  `observation.state.raw_gripper_rotation/data.csv` for both sides. Flags
  out-of-range distances, static/dynamic mismatches, weak correlation, and
  divergence from the metadata `mag_calibration` recompute.
- **Video check** — for each `observation.image.*` stream, compares the
  `timestamps.csv` row count against `ffprobe` frame count, detects
  non-monotonic timestamps, missing frames inferred from FPS, and duplicate
  frames (millisecond-collision timestamps).
- **Duplicate-frame policy** — controlled by `validate_raw_data_config.json`:
  a stream fails if duplicate proportion ≥ `max_duplicate_frame_proportion`
  (default 0.1) **or** any two consecutive duplicate rows are fewer than
  `max_near_duplicate_frame` frames apart (default 10).
- **Output** — by default writes to `outputs/<class_name>/`:
  - `episode_XXXX.validation.json` per episode (full structured result)
  - `summary.validation.json` per class (rollup + per-episode problem index)
  - Use `--no-reports` to suppress, `-o/--output-root` to redirect, or
    `--json PATH` for an extra copy of the summary.
- Class-level runs show a `tqdm` progress bar.

```
python validate_raw_data.py path/to/class
python validate_raw_data.py path/to/class/episode_0001
python validate_raw_data.py path/to/class --validate-config custom.json
```

### `transform_episode_w_world_base.py`
Transforms tracker poses into the world-frame EEF pose used downstream and
prepares wrist-view videos for replay.

- Copies each episode to `<output-root>/<class_name>/<episode>/`.
- Applies `ee_trajectory_config.json` to rewrite `observation.state.eef_pose`
  and `actions.eef_pose` CSVs in place.
- Flips wrist-view videos (`hflip,vflip` via ffmpeg) so they match the
  transformed coordinate frame.
- Optional single-episode crop (`--start-frame`, `--end-frame`) trims every
  `data.csv` and `video.mp4` to the matching timestamp window; wrist-view
  videos are flipped during the same ffmpeg pass.
- Updates `metadata.json` (`umi_transform_*` fields) and rewrites
  `checksums.sha256` for the destination episode.
- Class-level runs show a `tqdm` progress bar with the active step in the
  postfix.

```
python transform_episode_w_world_base.py path/to/class
python transform_episode_w_world_base.py path/to/class/episode_0001 --start-frame 30 --end-frame 480
```

### `visualize_episode_w_world_base.py`
Launches a local web visualizer for tracker and world-frame EEF trajectories.
The browser UI renders both the original tracker pose and the transformed
world EEF pose side by side, syncs wrist-view videos to the slider, and lets
you tweak the transform config or export an episode/class through the server.

```
python visualize_episode_w_world_base.py path/to/class
python visualize_episode_w_world_base.py --no-open path/to/class/episode_0001
```

### `add_embodied_absolute_offset.py`
Copies an episode (or every episode in a class) into a new output directory
and adds a fixed XYZ offset to the absolute TCP positions in
`observation.state.eef_pose/data.csv` and `actions.eef_pose/data.csv`. Useful
for shifting a recorded trajectory to a different workspace anchor without
rotating it.

```
python add_embodied_absolute_offset.py path/to/class --offset 0.02 0 -0.05
python add_embodied_absolute_offset.py path/to/class/episode_0001 --x 0.02 --y 0 --z -0.05
```

### `add_embodied_ee_relative_offset.py`
Computes the EE-local `[x, y, z]` offset from a 2-D `tracker_umi_local_position`
using the `local_ee_projection` block in `ee_trajectory_config.json`. With
`--write-config` it writes the result back into the config under
`local_ee_projection.tracker_based_ee_local_position`, which is what the
trajectory transform consumes when projecting tracker pose to EEF pose.

```
python add_embodied_ee_relative_offset.py 0.012 0.242
python add_embodied_ee_relative_offset.py --plain
python add_embodied_ee_relative_offset.py 0.012 0.242 --write-config
```

## Shared modules and configs

| File | Purpose |
|---|---|
| `ee_transform.py` | Tracker → world EEF math used by `transform_episode_w_world_base.py`, `visualize_episode_w_world_base.py`, and `add_embodied_ee_relative_offset.py`. |
| `ee_trajectory_config.json` | Default transform config: rotation sequence, position offsets, `local_ee_projection`, `world_projection`. Edit (or override per-script with `-c`) to retune the world frame. |
| `validate_raw_data_config.json` | Thresholds for the video duplicate-frame quality gate consumed by `validate_raw_data.py`. |

## Default output layout

```
outputs/
  <class_name>/
    summary.validation.json
    episode_0001.validation.json
    episode_0001/                 # produced by transform_episode_w_world_base.py
      observation.state.eef_pose/data.csv
      observation.image.left_wrist_view/video.mp4
      ...
```
