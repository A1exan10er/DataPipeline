# QA Pipeline Phase Decision Rules

This document explains what each QA phase checks and how findings affect the
episode status. It reflects the current source code in `QA_Pipeline/scripts`.

## Status Aggregation

Each phase produces findings with a `severity` and `status`. The phase status is
decided from all findings in that phase:

| Findings in phase | Phase status |
| --- | --- |
| any `critical` finding | `fail` |
| any `major` finding whose status is `fail` | `fail` |
| any `needs_review` finding | `needs_review` |
| any remaining `major` or `minor` finding | `warning` |
| no findings, or only `info/pass` findings | `pass` |

Episodes marked `fail` are not training-ready. `needs_review` means the data may
be usable, but needs human inspection before training.

## Phase 1: Structure And Metadata

Purpose: verify that the episode folder has the expected structure, metadata,
declared modalities, required files, checksum manifest, and quality labels.

| Check | Rule | Result |
| --- | --- | --- |
| `episode_folder_name` | folder name must start with `episode_` | fail |
| `checksum_manifest_missing` | `.checksum_manifest` is missing; default config requires it | fail |
| `checksum_manifest_invalid` | manifest is unreadable, invalid JSON, not an object, has unsafe paths, or unsupported checksum values | fail |
| `checksum_manifest_file_missing` | manifest lists files that do not exist | fail |
| `checksum_manifest_path_not_file` | manifest path is not a regular file | fail |
| `checksum_manifest_file_empty` | manifest-listed file exists but is empty | fail |
| `checksum_hash_unreadable` / `checksum_hash_mismatch` | only runs when hash verification is enabled; file cannot be read or digest does not match | fail |
| metadata load | `metadata.json` must exist and parse as valid JSON | fail |
| `parent_path_structure` | parent path should look like `<task>/<date>/<operator>/<episode>` | warning |
| `required_metadata_field` | `task_key`, `episode_index`, `duration_seconds`, `total_frames`, `modalities`, FPS, and `quality` must be valid | fail |
| `task_folder_metadata_mismatch` | task folder name differs from `metadata.task_key` | pass/info, reported for cleanup |
| `task_robot_mismatch` | task folder indicates one robot/source but metadata, episode name, or robot folder indicates another | fail |
| `modality_folder_missing` | known modality in metadata has no matching folder | fail |
| `required_modality_file_missing` | image modality lacks `video.mp4` or `timestamps.csv`; CSV modality lacks `data.csv` | fail |
| `required_modality_file_empty` | required modality file exists but is empty | fail |
| `action_modality_singular_name` | folder uses `action.*` instead of preferred `actions.*`; default config is pass/info | configurable |
| `unknown_modality_detected` | unknown modality names are detected but are not used for required-file failures; default config is pass/info | configurable |
| `quality_labels_missing` | `quality.labels` is missing or empty | warning |

Notes:

- Flow image modalities, `observation.image.flow_*`, are ignored by required
  file checks.
- The default checksum algorithms are `sha256` and `md5`.
- Hash contents are not verified by default; manifest presence and listed-file
  existence are verified.

## Phase 2: Duration, Counts, And Length Alignment

Purpose: verify that duration, frame counts, timestamp rows, state rows, and
task-level duration are internally consistent.

| Check | Rule | Result |
| --- | --- | --- |
| `duration_under_5s` | `duration_seconds < 5.0` | fail |
| `duration_not_positive` | `duration_seconds` is missing or not positive | fail |
| `total_frames_not_positive` | `total_frames` is missing or not positive | fail |
| `duration_frames_fps_inconsistent` | `abs(total_frames - duration_seconds * fps) / expected_frames > 0.10` | fail |
| `timestamps_unreadable` | image `timestamps.csv` cannot be read | fail |
| `timestamps_row_count_mismatch` | image timestamp row count differs from `total_frames` by more than 10% | fail |
| `state_csv_row_count_mismatch` | non-tactile state `data.csv` rows differ from `duration_seconds * fps` by more than 15% | warning |
| `modality_frame_count_misaligned` | metadata modality count spread is 4-10 frames | warning |
| `modality_frame_count_misaligned` | metadata modality count spread is more than 10 frames | needs_review |
| `duration_task_outlier` | task group size at least 5 and `abs(duration - median) / IQR > 3.0` | needs_review |
| `duration_absolute_too_short` | task group size at least 3 and duration is less than 20% of task median | fail |
| `duration_absolute_too_short` | duration is less than 40% of task median | needs_review |
| `duration_absolute_too_long` | duration is more than 250% of task median | needs_review |

Notes:

- Phase 2 group checks are task-based, so group-aware batching keeps complete
  task groups together.
- Phase 2 skips `observation.state.*tactile` row-count and modality-alignment
  checks. Tactile state
  CSVs are derived from tactile image postprocessing, are not used by the
  current model training, and should not affect QA pass/fail results. Raw
  tactile quality is checked through `observation.image.*tactile` in Phase 3.

## Phase 3: Image Timestamps, FPS, And Frame Drops

Purpose: verify image timestamp quality, per-episode FPS, frame drops, raw versus
processed timestamp consistency, and multi-image-modality start/end alignment.

Scope:

- Checks `observation.image.*` modalities only.
- Excludes `observation.image.flow_*`.
- State/action timestamp checks are handled by Phase 5.
- Image timestamp checks read `timestamps.csv` and use only rows where
  `is_new == "1"`.
- Frame-drop checks use `metadata.frame_integrity` first for speed. If missing,
  Phase 3 falls back to `timestamps.csv` and counts `is_new == "0"` rows.

| Check | Rule | Result |
| --- | --- | --- |
| `timestamps_unreadable` | image `timestamps.csv` is missing or unreadable | fail |
| `timestamps_not_monotonic` | violation ratio `>= 5%` | fail |
| `timestamps_not_monotonic` | violation ratio `>= 1%` and `< 5%` | needs_review |
| `timestamps_not_monotonic` | violation ratio `< 1%` | warning |
| `duplicate_timestamps` | duplicate ratio `>= 5%` | fail |
| `duplicate_timestamps` | duplicate ratio `>= 1%` and `< 5%` | needs_review |
| `duplicate_timestamps` | duplicate ratio `< 1%` | warning |
| `frame_drop_ratio` | RGB-like video `total_drops / frame_count > 0.10` | fail |
| `frame_drop_ratio` | tactile video `total_drops / frame_count > 0.15` | fail |
| `frame_drop_consecutive` | `max_consecutive_drops >= 25` | fail |
| `abnormal_fps_loss` | actual FPS is more than 10% below expected FPS | fail |
| `abnormal_fps_gain` | actual FPS is more than 10% above expected FPS | warning |
| `timestamps_raw_inconsistency` | `timestamps_raw.csv` and `timestamps.csv` row counts differ by more than 2 | warning |
| `modality_alignment_start` / `modality_alignment_end` | image modality start or end timestamp spread exceeds 500 ms | fail |
| `consecutive_drops_outlier` | task+robot group size at least 5 and `max_consecutive_drops > median + 3 * IQR` | needs_review |
| `consecutive_drops_outlier` | group size less than 5 and `max_consecutive_drops >= 10` | warning |

Important decision:

- Phase 3 does not compare actual FPS across episodes in the same task. The old
  task-wise formula `abs(actual_fps - median) / IQR` is not used. FPS is judged
  only against the episode's own expected FPS.

## Phase 4: Video Health

Purpose: open each image `video.mp4`, read video properties, sample frames, and
detect obvious visual corruption.

| Check | Rule | Result |
| --- | --- | --- |
| dependency check | `opencv-python-headless` must be installed before Phase 4 starts | pipeline configuration error |
| `video_not_openable` | OpenCV cannot open `video.mp4` | fail |
| `video_frame_count_unreadable` | OpenCV frame count is not positive | fail |
| `video_frame_count_mismatch` | video frame count differs from metadata `total_frames` by more than 10% | fail |
| `video_duration_mismatch` | `video_frame_count / video_fps` differs from metadata duration by more than 10% | warning |
| `video_resolution_mismatch` | video resolution differs from metadata camera resolution or `config.csv`; taller stored video is accepted when width matches | warning |
| `video_black_frames` | sampled frame brightness below 5.0; if over half sampled frames are black/white combined | fail |
| `video_black_frames` | sampled black/white frames exist but are at most half of samples | needs_review |
| `video_white_frames` | sampled frame brightness above 250.0; same severity rule as black frames | fail / needs_review |
| `video_frozen` | all adjacent sampled grayscale frame differences are below 1.0 | fail |
| `both_wrist_views_still` | ARX5 only: both `left_wrist_view` and `right_wrist_view` are still in more than 80% of sampled adjacent pairs, using mean diff below 5.0 | fail |

Notes:

- Phase 4 samples up to eight positions: 0%, 15%, 30%, 45%, 60%, 75%, 90%, and
  100% of the video.
- Tactile cameras are not used for the ARX wrist-view stillness check.

## Phase 5: Robot State And Action Reasonableness

Purpose: inspect robot numeric CSVs for parseability, timestamp order, limits,
large jumps, velocity/acceleration, jitter, end-effector jumps, and operator
standstill.

Scope:

- Joint position: `actions.joint_position` and `observation.state.joint_position`.
- Velocity: `observation.state.joint_velocity` if present; otherwise estimated
  from joint positions.
- EEF pose: `actions.eef_pose`, `action.eef_pose`, and
  `observation.state.eef_pose`.
- Robot-specific configs currently exist for `arx5`, `flexiv`, and `aloha`.
  Unknown robots use ARX5 defaults and get an info/pass finding.

| Check | Rule | Result |
| --- | --- | --- |
| `csv_not_parseable` | `data.csv` exists but cannot be read or parsed | fail |
| `joint_nan_inf` | selected numeric columns contain NaN, Inf, or unparseable values | fail |
| `timestamps_missing_or_unparseable` | `timestamp_ms` missing or has no parseable values | fail |
| `timestamps_not_monotonic` | same ratio tiers as Phase 3: `>=5%` fail, `>=1%` needs_review, otherwise warning | fail / needs_review / warning |
| `joint_out_of_limits` | joint value exceeds robot joint limits plus tolerance | needs_review |
| `gripper_out_of_limits` | gripper value exceeds robot gripper limits plus tolerance | needs_review |
| `gripper_mean_too_low_remap_needed` | mean gripper distance is below configured threshold, default 0.005 m | needs_review |
| `joint_step_too_large` | adjacent joint position step exceeds robot threshold | needs_review |
| `gripper_step_too_large` | adjacent gripper step exceeds robot threshold | needs_review |
| `joint_velocity_exceeded` | measured or estimated p99 joint velocity exceeds robot threshold | needs_review |
| `joint_acceleration_high` | measured or estimated p99 acceleration exceeds robot threshold | warning |
| `jitter_high` | jitter score `>= 0.05` by default | fail |
| `jitter_high` | jitter score `>= 0.01` by default | warning |
| `operator_standstill` | continuous stillness exceeds 5 second buffer | warning |
| `operator_standstill_excessive` | total standstill excess time is more than 20% of episode duration | needs_review |
| `eef_position_step_too_large` | adjacent EEF position step exceeds robot threshold, default 0.05 m | needs_review |
| `joint_columns_not_detected` | no recognized joint columns in a joint-position CSV | pass/info |

Important default robot thresholds:

| Robot | Joint limits | Gripper limits | Max joint step | Max gripper step | Max p99 velocity |
| --- | --- | --- | --- | --- | --- |
| `arx5` | `[-3.46, 4.25]` rad | `[0.0, 0.082]` m | `0.15` rad | `0.015` m | `4.5` rad/s |
| `flexiv` | `[-6.2832, 6.2832]` rad | `[0.0, 0.10]` m | `0.3` rad | `0.05` m | `2.5` rad/s |
| `aloha` | `[-6.2832, 6.2832]` rad | `[0.0, 0.10]` m | `0.3` rad | `0.05` m | `2.5` rad/s |

## Phase 6: UMI Validation, Preprocessing, And World-Frame Export

Purpose: run UMI-specific validation, trajectory preprocessing, optional repair
or crop, and export derived data in world-base coordinates.

Scope and gating:

- Runs only when `phase6_umi_processing.enabled` is true.
- Non-UMI episodes produce `umi_not_applicable` as info/pass.
- UMI detection uses robot metadata, robot tokens in the episode name/path/task,
  and required modality presence.
- Required runtime dependencies are `numpy`, `scipy`, `cv2`, `ffmpeg`, and
  `ffprobe`.

| Check | Rule | Result |
| --- | --- | --- |
| dependency check | required Python packages, FFmpeg binaries, or DataProcessUMI modules are unavailable | pipeline configuration error |
| `umi_processing_disabled` | Phase 6 disabled in config | pass/info |
| `umi_not_applicable` | episode does not look like UMI data | pass/info |
| `umi_processing_error` | Phase 6 raises an exception or cannot process the episode | fail |
| `umi_processing_rejected` | assessment or preprocessing rejects the episode | fail |
| `umi_processed_repaired` | episode passes after preprocessing repair or crop | configurable, default `warning` |
| `umi_processed_passed` | UMI validation, preprocessing, and transform pass without repair beyond passthrough | pass |

Assessment gate:

- Assessment video problems `video_frame_count_mismatch`, `missing_timestamps`,
  and `duplicate_frames_exceed_thresholds` are tolerated by Phase 6 because
  other QA phases and preprocessing handle them.
- Gripper assessment problems are tolerated.
- Action assessment problems block the episode and produce rejection.

Outputs:

- Derived UMI data goes under `outputs/umi_processed/data` by default.
- Reports go under `outputs/umi_processed/report`.
- Intermediate preprocessing work is removed unless `keep_intermediate` is true.
