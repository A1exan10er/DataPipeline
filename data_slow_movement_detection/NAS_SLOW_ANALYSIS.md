# NAS slow movement analysis

Script:

```bash
python3 scripts/analyze_nas_slow_movement.py
```

Default behavior:

- DSM URL: `http://192.168.50.2:5000`
- episode root: `/database/verified`
- max episodes per task key: `20`
- max task keys: `0`, meaning all discovered task keys
- episode sampling: `even`, meaning samples are spread across the full episode
  list instead of taking only the latest episodes. For 10 total and 5 selected,
  it chooses positions 2, 4, 6, 8, 10.
- output directory: `nas_slow_analysis/`
- max discovered episode candidates per task: `200`; set
  `--max-discovered-episodes-per-task 0` for exhaustive discovery.
- slow rule: by default both hands must be slow at the same time
  (`--flag-mode all-hands-slow`), speed `<=3.0 cm/s`, merge gaps `<=2.0 s`,
  duration `>=10.0 s`
- gripper gate: enabled by default. If an EEF slow candidate has clearly active
  gripper motion, it is downgraded; however, continuous one-direction gripper
  motion for `>=15.0 s` is still considered slow.
- dropper relaxed mode: disabled by default. Dropper tasks use the same slow
  rule as other tasks unless `--dropper-relaxed-mode` is explicitly enabled.
- rotation gate: if EEF is nearly stationary but a joint rotates clearly in the
  same slow window, the EEF slow candidate is downgraded. This handles actions
  such as pouring, where the wrist rotates in place while the end effector
  position barely changes.

Useful runs:

```bash
# Analyze one task key, such as PACK_SCHOOLBAG
python3 scripts/analyze_nas_slow_movement.py --task-key PACK_SCHOOLBAG

# Analyze at most 10 task keys, at most 20 episodes per task
python3 scripts/analyze_nas_slow_movement.py --max-task-keys 10 --max-episodes-per-task 20

# Analyze at most 30 task keys, 50 episodes per task
python3 scripts/analyze_nas_slow_movement.py --max-task-keys 30 --max-episodes-per-task 50

# Analyze 50 task keys, 20 evenly sampled episodes per task
python3 scripts/analyze_nas_slow_movement.py --max-task-keys 50 --max-episodes-per-task 20 --episode-sampling even

# Analyze only episodes on or after 2026-07-01
python3 scripts/analyze_nas_slow_movement.py --max-task-keys 50 --max-episodes-per-task 20 --date-from 20260701

# July smoke test: 50 task keys, 20 evenly sampled episodes per task, 10s slow threshold
python3 scripts/analyze_nas_slow_movement.py \
  --max-task-keys 50 \
  --max-episodes-per-task 20 \
  --episode-sampling even \
  --date-from 20260701 \
  --min-duration-s 10 \
  --output-dir nas_slow_analysis_20260701_min10

# Analyze specific NAS directories directly
python3 scripts/analyze_nas_slow_movement.py \
  --root-path /database/verified/tape_measure_pickup_UMI/UMI_Headless/collector-xinzhi-System-Product-Name-e5556daf/20260713/huangzhenyong \
  --root-path /database/verified/drawer_pull_open_UMI/UMI_Headless/collector-xinzhi-System-Product-Name-c7b6876d/20260714/wangliang \
  --root-path /database/verified/Add_water_to_the_test_tube_with_a_dropper_then_put_it_back_on_the_rack_UMI/UMI_Headless/collector-xinzhi-System-Product-Name-975f8dc6/20260703/huangzhenyong \
  --max-episodes-per-task 20 \
  --episode-sampling even \
  --output-dir nas_compare_tape_drawer_dropper

# Take the latest episodes instead of even sampling
python3 scripts/analyze_nas_slow_movement.py --episode-sampling latest

# Preserve the older primary-hand-only behavior
python3 scripts/analyze_nas_slow_movement.py --flag-mode primary

# Keep EEF-only behavior and report gripper metrics without using them to downgrade
python3 scripts/analyze_nas_slow_movement.py --no-gripper-activity-gate

# Discover episode paths without downloading eef_pose CSV
python3 scripts/analyze_nas_slow_movement.py --dry-run --max-task-keys 3
```

Login fallback:

- First tries environment variables: `DSM_ACCOUNT`/`DSM_PASSWORD`, or
  `NAS_ACCOUNT`/`NAS_PASSWORD`.
- If that fails and `--non-interactive` is not set, it prompts in terminal.
- If login fails, it keeps asking for credentials.
- During analysis, transient HTTP `502/503/504` errors are retried.
- DSM session errors trigger relogin with the same credentials.

Outputs:

```text
nas_slow_analysis/
  nas_slow_movement_results.jsonl
  nas_slow_movement_summary.json
  nas_slow_movement_report.md
```

The Markdown report lists task-level counts and all slow episodes with NAS
paths in `/volume1/...` form. In the "Slow Episodes" section, each task key
shows one shared NAS directory and then lists episode numbers such as `0001`
and `0014`; full per-episode paths are kept in a separate appendix and in the
JSONL output.

The task summary includes average slow-window speed, average gripper
bouts/min, and average gripper active fraction. Slow episode rows also include
the longest one-direction gripper segment and the final `flag_reason`.

The report files are refreshed after each completed task by default, so a long
run can be inspected while it is still running. Use `--no-incremental-output`
to write only once at the end.

If a task appears stuck during episode discovery, the script prints directory
scan progress every 25 scanned directories. Date directories outside
`--date-from/--date-to` are pruned before recursion, and empty task keys do not
count toward `--max-task-keys`. For exhaustive discovery, disable the default
candidate cap:

```bash
python3 scripts/analyze_nas_slow_movement.py \
  --max-task-keys 50 \
  --max-episodes-per-task 20 \
  --max-discovered-episodes-per-task 0
```

For two-hand alternating tasks, keep the default `--flag-mode all-hands-slow`.
This prevents false positives when one hand is intentionally holding an object
still while the other hand is actively operating.

For tasks that legitimately hold the arm still while operating the gripper,
keep the default gripper gate. The current defaults look at gripper motion
inside the EEF slow window, not across the full episode. If the slow window has
at least `--gripper-window-min-motion 0.03` total gripper motion and
`--gripper-window-min-avg-speed 0.002`, the EEF slow candidate is downgraded.
If the gripper itself moves in one direction for
`--gripper-slow-min-duration-s 15.0`, the episode remains flagged as slow.

For dropper tasks, the default is intentionally conservative:
`--dropper-relaxed-mode` is off, so EEF slow candidates remain flagged for
manual review. If you explicitly enable it, task keys containing `dropper` only
keep EEF slow flags when the slow window is extreme:
`--dropper-extreme-duration-s 45.0` and
`--dropper-extreme-max-speed-cm-s 1.0`.

For pouring-like tasks, the default rotation gate uses
`observation.state.joint_position/data.csv`. It suppresses an EEF slow candidate
when one joint moves at least `--joint-rotation-min-window-motion 1.5` rad in
the slow window and averages at least `--joint-rotation-min-avg-speed 0.1`
rad/s.

For UMI tasks without joint position, raw gripper rotation can also suppress an
EEF slow candidate, but only for task keys matching
`--raw-rotation-gate-task-pattern` substrings. The defaults are `rice`, `pour`,
`tea`, `cup`, and `water`, with slow-window thresholds
`--raw-rotation-window-min-motion 1.0` and
`--raw-rotation-window-min-avg-speed 0.07`. This handles tasks such as tipping a
paper cup to pour rice while keeping drawer and tape-measure tasks unaffected.
