# Gripper recovery

This toolchain avoids relying on per-episode manual magnetic calibration. It uses
known-good samples to learn:

- unwrapped `raw_gripper_rotation` angle change -> gripper width change
- `wrist_view` visual gripper pixel gap -> gripper motion / optional absolute width

The visual extractor looks at the top part of each wrist-view frame and measures
the gap between the two dark gripper fingers. Raw rotation is unwrapped before
fitting and recovery, so `359 -> 0` and `0 -> 359` crossings are treated as
continuous rotation.

## 1. Assess failed gripper sides

```bash
python3 tools/data/gripper_recovery/gripper_recovery.py assess \
  --report tools/data/gripper_outputs/all_samples/gripper_compact_report.csv \
  --out tools/data/gripper_recovery/reports
```

Recoverability classes:

- recoverable: mapping/calibration issue, or both visual and raw are nearly static.
- unrecoverable: visual gripper moves but raw rotation is static, indicating a hardware/readout issue.

The detector distinguishes the requested cases this way:

- video gripper moves, raw rotation moves, old gripper check failed:
  `mapping_or_calibration_recoverable`
- video gripper barely moves, raw rotation barely moves:
  `static_visual_and_raw`
- video gripper moves by at least `0.03` normalized gap, but raw rotation range
  is at most `30` degrees:
  `raw_invalid_visual_recoverable`

Sides without the corresponding wrist-view video are skipped by default and are
not counted as recovery failures.

## 2. Fit data-driven calibration

```bash
python3 tools/data/gripper_recovery/gripper_recovery.py fit \
  --report tools/data/gripper_outputs/all_samples/gripper_compact_report.csv \
  --model-out tools/data/gripper_recovery/gripper_recovery_model.json \
  --out tools/data/gripper_recovery/reports
```

The model is learned only from sides marked correct by the existing gripper check.
The JSON records the learned raw delta coefficient, visual fit quality, gripper
range, and whether the visual absolute width fit is good enough to use as an
absolute anchor. If not, recovery uses vision only as a motion consistency check.

## 3. Recover gripper widths

Dry run:

```bash
python3 tools/data/gripper_recovery/gripper_recovery.py recover \
  --model tools/data/gripper_recovery/gripper_recovery_model.json \
  --assessment tools/data/gripper_recovery/reports/gripper_recoverability.csv \
  --out tools/data/gripper_recovery/reports
```

Apply:

```bash
python3 tools/data/gripper_recovery/gripper_recovery.py recover \
  --model tools/data/gripper_recovery/gripper_recovery_model.json \
  --assessment tools/data/gripper_recovery/reports/gripper_recoverability.csv \
  --out tools/data/gripper_recovery/reports \
  --apply
```

With `--apply`, every recoverable side first preserves the pre-repair gripper
directory as `observation.state.gripper_original/` if it does not already
exist. The repair strategy is:

- raw and visual both move clearly, and their motion relation is strong:
  repair from unwrapped raw rotation.
- raw and visual both move clearly, but their motion relation is weak:
  repair from the visual gap trajectory.
- visual moves clearly and raw rotation barely changes:
  mark raw invalid and repair from the visual gap trajectory.
- neither source is reliable:
  leave it unapplied and report quality failure.

Applied repairs write values to `observation.state.gripper/data.csv` and all
gripper-bearing EEF/action CSVs. Touched files are also backed up under
`.gripper_recovery_backup/`, and checksum files are refreshed.

Current fitted model summary:

- correct sides used: 600
- visual measurements: 14397
- dynamic raw slope samples: 565
- median unwrapped raw delta coefficient: `0.0004619735385349535 m/deg`
- visual absolute fit: `visual_absolute_usable=false`, so vision is used for
  motion consistency rather than as the primary absolute width source

Latest recovery apply summary:

- candidate sides with corresponding wrist-view video: 419
- applied repairs: 419
- skipped missing-video sides: not counted as failures
- modes:
  - `raw_primary_high_raw_visual_correlation`: 367
  - `raw_primary_visual_gate`: 27
  - `visual_primary_raw_invalid`: 20
  - `visual_primary_low_raw_visual_correlation`: 1
  - `static`: 4
