# QA Pipeline Technical Summary

## Overview

The QA pipeline checks recorded robot episodes before they are used for training. It discovers `episode_*` folders, creates or loads one SQLite row per episode in `outputs/qa_pipeline.db`, then runs phases 1 through 5 in order. Before each phase, episodes that failed an earlier phase are skipped, so obvious structural or timing failures do not waste later video and robot-state work.

Each phase writes findings back to SQLite. At the end, the runner computes one final status per episode and exports `outputs/quality_report.csv`, `outputs/quality_findings.jsonl`, and `outputs/quality_summary.md`. The CSV is the compact per-episode view, JSONL is the detailed finding log, and the Markdown summary groups results by status, task, operator, robot, and issue type.

## Status and Severity System

The pipeline uses four statuses. `pass` means no meaningful issue was found. `warning` means the episode is probably usable but has a quality concern. `needs_review` means the data may be valid but should be inspected by a person before training. `fail` means the episode is considered unsafe or unusable for training.

Severity describes impact. `critical` always fails an episode, usually because a required file cannot be read or values are impossible. `major` may fail when the check itself marks failure, or become a warning or review item for serious but not always fatal anomalies. `minor` usually becomes warning or review. `info` records non-blocking observations.

## Phase 1 — Structure and Metadata Checks

Purpose: verify that an episode has the expected folder shape, metadata, and required modality files.

Input: the episode directory, `metadata.json`, `.checksum_manifest`, image modality folders, `video.mp4`, `timestamps.csv`, and state/action `data.csv` files.

Method: Phase 1 checks that the folder name starts with `episode_`, `metadata.json` exists and is valid JSON, and required metadata fields such as `task_key`, `episode_index`, `duration_seconds`, `total_frames`, `modalities`, FPS, and `quality` are present. It compares metadata modality names to actual folders, handles the special `actions` metadata key by accepting `action.*` and `actions.*` folders, and checks that required files exist and are non-empty. Image streams need `video.mp4` and `timestamps.csv`; flow image streams need only `video.mp4`; state and action streams need `data.csv`.

Classification: missing or invalid metadata, missing modality folders, and missing or empty required files fail. Missing checksum manifests, nonstandard parent paths, and missing quality labels are warnings. This phase is fast because it mostly reads metadata and filesystem attributes.

## Phase 2 — Duration and Count Consistency

Purpose: detect recordings whose duration or row/frame counts do not match the metadata.

Input: `metadata.json`, image `timestamps.csv`, state `data.csv`, and the metadata `modalities` count fields.

Method: Phase 2 checks that duration and total frame count are positive, that duration under 5 seconds fails, and that `duration_seconds * fps` roughly matches `total_frames`. A mismatch above 10% means the metadata and recording disagree. It compares image timestamp row counts against `total_frames` with a 10% tolerance and state CSV rows against expected duration times FPS with a 15% tolerance. It also checks modality count spread from metadata: counts within 3 frames are accepted, 4-10 frames warn, and larger spreads need review. Group checks compare duration within each task: extreme task outliers beyond 3 IQR need review, under 20% of task median fails, under 40% needs review, and over 250% needs review.

Classification: impossible duration/frame metadata fails. Large timestamp mismatches fail. State row mismatch warns. This phase is fast to medium: it reads CSV row counts but does not decode video.

## Phase 3 — Timestamp Synchronization

Purpose: verify that image timestamp streams are ordered, regular, and synchronized.

Input: image `timestamps.csv`, metadata `frame_integrity`, and optional `timestamps_raw.csv` for consistency checks.

Method: For image modalities except flow, Phase 3 reads `timestamp_ms` rows where `is_new == 1`. It checks strictly increasing timestamps, duplicates, actual frequency versus expected FPS, and start/end alignment across modalities. A start or end spread above 500 ms means cameras do not cover the same time interval. It uses `frame_integrity`, with a `timestamps.csv` fallback, to fail if RGB-like video frame drop ratio exceeds 10% or tactile video frame drop ratio exceeds 15%. It warns when `timestamps_raw.csv` and processed `timestamps.csv` differ by more than 2 rows. Group checks flag unusually long consecutive frame-drop runs within task+robot groups.

Classification: frequent non-monotonic or duplicate timestamps can fail; smaller ratios warn or need review. Large gaps over 5x median interval warn, over 20x fail. This phase is medium cost because it reads timestamp rows and performs group statistics.

## Phase 4 — Video Health

Purpose: ensure each camera video can be decoded and contains plausible visual data.

Input: every non-flow `observation.image.*/video.mp4`, metadata `total_frames`, `duration_seconds`, camera metadata, and optional `config.csv`.

Method: Phase 4 opens each video with OpenCV, reads frame count, FPS, width, and height, and compares frame count and duration to metadata with 10% tolerance. It checks resolution against metadata or camera config, allowing taller stored frames when they appear to be padded. It samples up to eight frames across the video, measures brightness, and compares neighboring sampled grayscale frames. Very dark or very bright sampled frames indicate black/white video. If all sampled frame differences are below 1.0, the video appears frozen. For ARX5 only, it compares sampled `left_wrist_view` and `right_wrist_view`; if both are still for more than 80% of sampled pairs, the episode fails as possible operator idle.

Classification: unopenable video, unreadable frame count, frame-count mismatch, frozen video, or both ARX wrist views still fail. Resolution or duration mismatch warns. Bad sampled frames need review or fail if more than half are bad. This phase is slow because it seeks and decodes video frames, but it supports multiprocessing.

## Phase 5 — Robot State Reasonableness

Purpose: detect physically implausible robot motion and long operator idle periods.

Input: `actions.joint_position/data.csv`, `observation.state.joint_position/data.csv`, optional `observation.state.joint_velocity/data.csv`, and EEF pose CSVs.

Method: Phase 5 parses numeric columns and detects ARX bimanual columns (`left_j*`, `right_j*`) or Flexiv columns (`j1`, `joint_*.pos`). It checks NaN/Inf values, monotonic timestamps, joint and gripper limits, per-frame joint/gripper steps, measured or estimated velocity, acceleration, jitter, and EEF position jumps. ARX5 and Flexiv have separate limits; unknown robots fall back to ARX defaults. Standstill detection uses joint position rows: if all non-gripper joints move less than a tiny threshold for more than a 5 second buffer, the extra idle time is reported, and more than 20% idle excess needs review.

Classification: parse failures, NaN/Inf, frequent timestamp violations, or high jitter can fail. Joint limits, large steps, high velocity, and EEF jumps usually need review; high acceleration warns. This phase is medium to slow because it loads numeric CSVs and can run in parallel.

## Supplementary Tool: Frame Alignment (align_frames.py)

This standalone script rechecks frame/row alignment from actual files rather than only metadata. It reads image `timestamps_raw.csv` or `timestamps.csv`, and state/action `data_raw.csv` or `data.csv`. It chooses one file strategy per episode: raw only if all checked modalities have raw files, otherwise processed, so raw and processed counts are not mixed. It excludes flow streams and tactile state streams because those behave differently and often differ by one row.

If all counts match, status is `pass`. If spread is 1-3, status is `needs_trim`; with `--trim`, it writes `timestamps_trimmed.csv` or `data_trimmed.csv` beside originals without overwriting them. Spread above 3 is `fail`. The tool also suggests head/tail trim points from the two wrist view videos. It samples every 30 frames, computes an adaptive motion threshold from up to 10 full-video samples, then scans the first and last region with a five-sample sliding window where at least 80% must be still for at least 5 seconds. This is slower than count-only checks because it decodes wrist video, but it supports `--workers`.

## Supplementary Tool: Camera Focus Check (check_tactile_focus.py)

This standalone script checks focus for every non-flow image camera, including RGB and tactile cameras. For each video, it samples three middle frames around 40%, 50%, and 60% of the video and three ending frames. It computes Laplacian variance, which measures how sharp image edges are: a blurry frame has soft edges and scores low; a sharp frame has crisp edges and scores high. The current threshold is 50.0.

The CSV records `camera_type`, frame position, score, blur flag, width, and height. The summary separates RGB and tactile cameras because tactile low scores can mean no contact rather than optical blur. Blurry episode counts are based on RGB only. This tool is medium to slow because it decodes six frames per camera and supports multiprocessing.
