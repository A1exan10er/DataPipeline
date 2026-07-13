# Trajectory Smooth

Smooth local jitter and short abrupt-change windows detected by UMI trajectory
judgment reports.

The tool reads report JSON files, selects episodes whose report has
`trajectory_quality=true`, then smooths side-specific `minor_jitter` segments
from the raw EEF pose streams. The original trajectories are not overwritten.
With `--source auto`, output is written next to every present raw stream:

```text
actions.eef_pose_smooth/data.csv
observation.state.eef_pose_smooth/data.csv
```

Each output CSV keeps the same header and shape as its matching `eef_pose_raw`
source.

## Usage

```bash
cd ~/workspace/DIDP/tools/data
python traj_smooth/smooth_trajectory.py \
  /data/curated \
  /data/curated/umi_report/2026-06-24/report
```

Dry run on a small number of selected reports:

```bash
python traj_smooth/smooth_trajectory.py \
  /data/curated \
  /data/curated/umi_report/2026-06-24/report \
  --dry-run \
  --limit 20
```

Overwrite existing smooth outputs:

```bash
python traj_smooth/smooth_trajectory.py \
  /data/curated \
  /data/curated/umi_report/2026-06-24/report \
  --overwrite
```

## Selection Rules

By default a report is processed only when:

- `ok=true`
- `trajectory_quality=true`
- a side report has local jitter or short abrupt-change segments

Segment reason types considered smoothable:

- `jitter_window`
- `cluster_jitter`
- `micro_jitter`
- `abrupt_step`
- `abrupt_acceleration`
- `abrupt_jerk`
- `merged_short_gap`

Use `--include-bad-quality` to also process reports whose trajectory quality is
false.

## Algorithm

For every selected side and segment:

1. Merge nearby segments.
2. Expand the edit window by `--padding-frames`.
3. Replace the detected core window by interpolation between clean boundary
   frames.
4. Apply a Savitzky-Golay low-pass filter to the expanded window.
5. Blend the filter result back into the original trajectory with cosine
   tapers at the window boundaries.
6. Re-normalize 6D rotation columns with Gram-Schmidt when rotation columns are
   present.

The script smooths XYZ and 6D rotation columns for the affected side. Gripper
columns and timestamps are preserved.

For long in-place jitter clusters (`cluster_jitter`), the script uses a
separate rolling-median despike step plus a wider Savitzky-Golay low-pass
window. This preserves slow drift inside the region while removing repeated
sharp points. Relevant knobs:

```bash
--cluster-savgol-window 151
--cluster-median-window 31
--cluster-despike-threshold-m 0.004
```

For smaller local sharp-point jitter (`micro_jitter`), the script uses the same
despike + low-pass path with lighter defaults:

```bash
--micro-savgol-window 101
--micro-median-window 21
--micro-despike-threshold-m 0.0025
```
