# SOP Upload Maintenance Notes

This document describes the current behavior of the SOP upload utilities for future maintainers and AI agents. It intentionally avoids environment-specific paths and credentials.

## Local Data Layout

The scripts expect local SOP video data under:

```text
data/
  YYYYMMDD/
    task_key/
      video_file.mov|mp4|avi|mkv|...
```

`task_key` is the local task directory name, including any suffix such as an arm/capture mode suffix. The scripts treat the directory name as the canonical task key for SOP upload metadata.

The `data/` directory is intentionally ignored by git.

## Remote NAS Layout

The scripts use Synology DSM File Station APIs and write SOP outputs under:

```text
/database/sop/
  task_key/
    sop_video.mp4
    sop_task.json
    *_lastframe.png
  sop_tasks.json
  reference_video_paths.json
  upload_log.txt
```

NAS absolute paths stored in JSON use the `/volume1/...` form when needed.

Note for maintainers: `sop_tasks.json` describes the SOP task metadata structure stored on the NAS, not a local-only cache. The `task_id` field is currently reserved and not fully implemented yet; future work should populate and reconcile it with the downstream task system.

## `upload_sop_videos.py`

This script uploads local task videos to the NAS.

Key behavior:

- Default source root is `data/`.
- It scans both direct task directories and date-scoped task directories under `data/YYYYMMDD/`.
- A task directory is considered valid when it contains exactly one supported video file.
- Non-MP4 input videos are transcoded to `sop_video.mp4` with ffmpeg before upload.
- Uploaded videos are stored at `/database/sop/{task_key}/sop_video.mp4`.
- Per-task metadata is stored at `/database/sop/{task_key}/sop_task.json`.
- Existing `sop_task.json` content is merged before upload so existing fields such as `task_id` are preserved.
- Upload logs are appended to `/database/sop/upload_log.txt`.

Important command modes:

```bash
python3 upload_sop_videos.py --task-key TASK_KEY
python3 upload_sop_videos.py --all
python3 upload_sop_videos.py --check-sop-tasks
python3 upload_sop_videos.py --sync-sop-tasks
```

`--check-sop-tasks` compares local task directories containing videos with remote `/database/sop/sop_tasks.json`.

`--sync-sop-tasks` updates remote `sop_tasks.json` from local `data/` without uploading or transcoding videos. It preserves existing remote fields, including `task_id`.

By default, sync only adds or updates local tasks and does not remove extra remote entries. Use `--prune-sop-tasks` with `--sync-sop-tasks` only when remote `sop_tasks.json` should strictly match local `data/`.

After normal video upload, the script automatically checks `sop_tasks.json`. If local video tasks are missing from the remote summary file, it prompts:

```text
Sync sop_tasks.json now? [y/N]
```

Only `y` or `yes` performs the sync.

## `extract_last_frames.py`

This script finds verified reference videos on the NAS, extracts the last frame from each discovered video, and uploads those frames into SOP task directories.

Key behavior:

- Default SOP root is `data/`.
- The operator enters a date; by default the script reads local task keys from `data/{date}`.
- It matches local task keys against `/database/verified`.
- It searches for a matching date/operator directory under each verified task.
- It selects the highest-numbered `episode_*` directory.
- It discovers videos under `observation.image.*` modality folders, skipping `flow_*` modalities.
- It downloads each discovered reference video temporarily, extracts the last frame using ffmpeg, and uploads PNG outputs to `/database/sop/{task_key}/`.
- It merges and uploads per-task `sop_task.json`.
- It merges and uploads `/database/sop/sop_tasks.json` for successfully processed tasks.
- It appends frame extraction records to `/database/sop/upload_log.txt`.

Reference video paths are tracked in:

```text
/database/sop/reference_video_paths.json
```

That file is merged rather than replaced. Records are keyed by `video_path` and include:

- `task_key`
- `date`
- `operator`
- `episode`
- `episode_path`
- `episode_nas_path`
- `modality`
- `video_path`
- `video_nas_path`
- `updated_at`

## JSON Merge Rules

The scripts avoid overwriting remote JSON with only the current run's data.

Per-task `sop_task.json`:

- Existing remote JSON is downloaded first.
- The script refreshes `task_key`, `nas_path`, and `replace_existing`.
- Existing `task_id` is preserved.
- Unknown existing fields are preserved.

Summary `sop_tasks.json`:

This file represents the task summary structure stored on the NAS at `/database/sop/sop_tasks.json`. It is not just an example generated from local state. The `task_id` field is currently a reserved placeholder and is not populated by the current scripts; future work should implement task ID assignment and reconciliation with the downstream task system.

Do not add JavaScript-style comments to the actual JSON file. Keep `sop_tasks.json` valid JSON because the scripts download, parse, merge, and upload it as a JSON array.

- Existing remote array is downloaded first.
- Entries are keyed by `task_key`.
- Existing entries are updated with current generated fields.
- Existing `task_id` values are preserved.
- New local tasks can be added by sync.
- Extra remote tasks are retained unless pruning is explicitly requested.

Reference `reference_video_paths.json`:

- Existing remote records are downloaded first.
- Records are keyed by `video_path`.
- New records are appended and existing records are updated.

## Safety Notes

- Do not hard-code personal local paths, credentials, or operator names.
- Keep `data/` ignored by git because it contains large local video inputs.
- Prefer merge/update behavior for remote JSON files. Avoid replacing summary files with only the current task list.
- Use `--dry-run` when available before large frame extraction runs.
- Use `--check-sop-tasks` after sync or upload changes to verify summary coverage.
