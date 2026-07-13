# UMI Post Pipeline

Unified post-processing entrypoint for curated UMI episodes.

```bash
cd /home/ubuntu/workspace/DIDP/tools/data
python3 umi_post/run_pipeline.py \
  --data-root /home/ubuntu/data/curated_samples/success \
  --output-root umi_post/reports \
  --robot-type umi \
  --workers 1
```

Default mode is dry-run. Add `--apply` to modify episode data.
When `--data-root` is a full curated tree, `--robot-type umi` only processes
paths shaped as `task_name/date/umi/name/episode_*`.
Use `--workers N` for independent episode-level parallelism on large curated
trees.

For large runs, prefer fast-first mode:

```bash
python3 umi_post/run_pipeline.py \
  --data-root /data/curated \
  --output-root /data/curated/umi_post_reports \
  --robot-type umi \
  --apply \
  --fast-first \
  --workers 96 \
  --repair-workers 16
```

Fast-first mode writes clean episodes to `success_detected.jsonl`, hard
failures to `failure_detected.jsonl`, and recoverable episodes to
`repair_queue.jsonl` before the repair queue is processed.

For trajectory/gripper-only continuation, add `--skip-prechecks`. This skips
focus, static-frame, and camera/trajectory matching checks, records those stages
as `skip` in each new report, and judges quality only from trajectory and
gripper results.

Pipeline order:

1. Focus check on left/right wrist views. Any defocused wrist view makes the
   episode quality false and stops later stages.
2. Cross-class label and left/right motion consistency check. If left/right data
   is reversed, the pipeline swaps left/right CSV column values for state/action
   data using the same column-swap rule as `robot-data-curation/ingest/relabel_lr.py`.
3. Wrist static check. Static side data is removed from original locations
   (moved under `.umi_post_deleted/` when `--apply`) and side columns are removed
   from action/derived CSVs. State CSVs are left intact.
4. Trajectory quality judgement. Recoverable/local jitter trajectories are
   smoothed into `observation.state.eef_pose_smooth` and
   `actions.eef_pose_smooth`.
5. Gripper quality judgement and recovery. Bad recoverable gripper sides are
   repaired using the gripper recovery model; original gripper data is preserved
   in `observation.state.gripper_original`.
6. Final EEF sync. Smoothed state EEF pose is copied to final
   `observation.state.eef_pose`, and `actions.eef_pose` is rebuilt with the
   one-frame state shift. Gripper values are synchronized from
   `observation.state.gripper`.
7. Unified report output. Each episode receives one report containing stage
   reports, final quality, reasons, and operations.

Outputs:

- `report/<relative_episode>/umi_post_report.json`
- `report/<relative_episode>/trajectory_report.json`
- `summary.csv`
- `success_failure.json`

`success_failure.json` has exactly:

```json
{
  "success": ["relative/episode/path"],
  "failure": ["relative/episode/path"]
}
```
