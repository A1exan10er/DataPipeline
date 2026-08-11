# Slow movement detection notes

## Data difference

The current sample set has 5 normal episodes and 7 slow episodes under `docs/`.
All episodes report stable `fps_actual = 30.0`, so the slow cases are not caused
by capture FPS drops.

The strongest separator is a long continuous segment where the primary end
effector is slow or near-stationary. The primary hand is the hand with the
larger total path in the episode; the other hand may be completely static in a
single-hand task and is ignored for the final flag.

| Group | Episode duration | Longest primary-hand <=3.0 cm/s segment |
| --- | ---: | ---: |
| normal | 28.97-43.57 s | 5.27-10.30 s |
| slow | 46.45-119.99 s | 22.93-55.53 s |

This matches the failure mode: the operator keeps moving for a long time, but
deliberately very slowly.

## Recommended QA rule

Flag an episode when the primary hand has a continuous segment satisfying:

- rolling path speed is at or below `3.0` cm/s
- slow segments separated by gaps up to `2.0` s are merged
- segment duration is at least `15.0` s

Near-stationary primary-hand periods are included by default because a task
that should be progressing but remains still for a long time is the same QA
failure class. The non-primary hand is not allowed to trigger the flag by
default, which prevents single-hand tasks from being marked slow just because
the unused hand is static.

Episode `0130` is an example of segmented slow movement. Its longest raw
continuous slow segment is only `7.57` s (`32.97-40.53` s), but the primary
hand repeatedly moves slowly with only short gaps. With `2.0` s gap merging,
the slow operation is `14.27-48.57` s, lasting `34.30` s, so it is classified
as slow.

## Threshold governance

Do not recompute the `3.0 cm/s` and `15.0 s` thresholds from the class balance
of whatever batch is being reviewed. A batch with too many slow examples would
push the threshold upward, and a batch with too many normal examples could make
legitimate slow-motion tasks fail.

Use fixed, versioned thresholds per task family instead:

- keep a global default for ordinary pick/place/open/close tasks
- allow task-level overrides for tasks that genuinely require slow controlled
  motion
- validate threshold changes against a frozen calibration set containing both
  known slow failures and known acceptable normal examples
- report score fields, such as `longest_slow_segment_s`, even when not flagged,
  so borderline cases can be reviewed without changing the threshold silently

## Script

Run:

```bash
python3 scripts/detect_slow_movement.py docs
```

QA integration with exit code:

```bash
python3 scripts/detect_slow_movement.py /path/to/episode_or_root --fail-on-detect
```

JSONL output for machine parsing:

```bash
python3 scripts/detect_slow_movement.py /path/to/episode_or_root --format jsonl
```

Exit code is `1` when `--fail-on-detect` is set and any episode is flagged.
