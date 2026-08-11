#!/usr/bin/env python3
"""Apply wrist-view static-check results to curated UMI episodes.

This removes static arm-side data according to a view-level static report:

* both existing wrist views static -> delete the episode;
* one side static and the other non-static -> convert to single-arm data;
* if the removed side is left, rename the kept right side back to left.

The script defaults to dry-run. Pass ``--apply`` to modify data.
"""

import argparse
import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


SIDES = ("left", "right")
VIEW_TO_SIDE = {
    "left_wrist_view": "left",
    "right_wrist_view": "right",
}

SIDE_DIR_TEMPLATES = (
    "observation.image.{side}_wrist_view",
    "observation.image.{side}_wrist_left_tactile",
    "observation.image.{side}_wrist_right_tactile",
    "observation.state.{side}_wrist_left_tactile",
    "observation.state.{side}_wrist_right_tactile",
)

CSV_DIRS = (
    "actions.eef_pose",
    "actions.eef_pose_raw",
    "actions.eef_pose_smooth",
    "episode.eef_pose_smooth",
    "observation.state.eef_pose",
    "observation.state.eef_pose_raw",
    "observation.state.eef_pose_smooth",
    "observation.state.gripper",
    "observation.state.raw_gripper_rotation",
)

ACTION_LIKE = {
    "actions.eef_pose",
    "actions.eef_pose_raw",
    "actions.eef_pose_smooth",
    "episode.eef_pose_smooth",
}
STATE_EEF = {
    "observation.state.eef_pose",
    "observation.state.eef_pose_raw",
    "observation.state.eef_pose_smooth",
}

POSE_SUFFIXES = ("x", "y", "z", "r1", "r2", "r3", "r4", "r5", "r6", "gripper")
ACTION_OUTPUT = {
    "x": "tcp.x",
    "y": "tcp.y",
    "z": "tcp.z",
    "r1": "tcp.r1",
    "r2": "tcp.r2",
    "r3": "tcp.r3",
    "r4": "tcp.r4",
    "r5": "tcp.r5",
    "r6": "tcp.r6",
    "gripper": "gripper.pos",
}
STATE_OUTPUT = {suffix: suffix for suffix in POSE_SUFFIXES}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("tools/data/static_check/reports/curated_samples_success/view_static_summary.csv"),
        help="view_static_summary.csv produced by the static checker.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tools/data/static_check/reports/curated_samples_success/static_filter_summary"),
        help="Output directory for operation summary.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify/delete episodes. Without this, only write a dry-run plan.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create .single_arm_backup for converted episodes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process at most this many non-noop episode operations.",
    )
    parser.add_argument(
        "--only-sample",
        action="append",
        default=[],
        help="Restrict to one sample path from the report. May be repeated.",
    )
    return parser.parse_args()


def read_report(path):
    by_sample = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            sample = row["sample"]
            entry = by_sample.setdefault(sample, {
                "sample": sample,
                "episode_path": Path(row["episode_path"]),
                "views": {},
            })
            side = VIEW_TO_SIDE.get(row["view"])
            if side:
                entry["views"][side] = row["view_verdict"]
    return by_sample


def side_dirs(side):
    return [template.format(side=side) for template in SIDE_DIR_TEMPLATES]


def existing_sides(episode):
    out = []
    for side in SIDES:
        if (episode / f"observation.image.{side}_wrist_view" / "video.mp4").exists():
            out.append(side)
    return out


def decide_operation(entry):
    views = entry["views"]
    sides = set(views)
    static = {side for side, verdict in views.items() if verdict == "static"}
    moving = {side for side, verdict in views.items() if verdict == "non-static"}
    if not static:
        return {"action": "noop", "reason": "no_static_view", "keep": "", "dead": ""}
    if not moving:
        return {"action": "delete_episode", "reason": "all_existing_views_static", "keep": "", "dead": ",".join(sorted(sides))}
    if len(moving) > 1:
        return {"action": "noop", "reason": "has_static_but_multiple_non_static_views", "keep": ",".join(sorted(moving)), "dead": ",".join(sorted(static))}
    keep = next(iter(moving))
    dead = "right" if keep == "left" else "left"
    if dead not in static:
        return {"action": "noop", "reason": "opposite_side_not_static", "keep": keep, "dead": dead}
    return {"action": "convert_single_arm", "reason": f"remove_static_{dead}", "keep": keep, "dead": dead}


def read_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def backup_file(src, dst):
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def backup_convert_inputs(episode, backup_dir, keep, dead, csv_dirs, del_dirs):
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file(episode / "metadata.json", backup_dir / "metadata.json.bak")
    backup_file(episode / "meta" / "episode.json", backup_dir / "meta__episode.json.bak")
    backup_file(episode / ".checksum_manifest", backup_dir / "checksum_manifest.bak")
    for name in csv_dirs:
        src = episode / name / "data.csv"
        if src.exists():
            backup_file(src, backup_dir / "csv" / f"{name}.csv")
    for name in del_dirs:
        src = episode / name
        dst = backup_dir / "dirs" / name
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst)


def rewrite_csv(path, keep, logical_name):
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
        old_fields = f.readline if False else None
    if not rows:
        # Preserve empty CSVs with just a header by handling them separately.
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, [])
        if not any(col.startswith(("left_", "right_")) for col in header):
            return False
        rows = []

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if not any(col.startswith(("left_", "right_")) for col in fieldnames):
        return False

    if logical_name in ACTION_LIKE:
        mapping = ACTION_OUTPUT
        out_fields = ["timestamp_ms"] + [mapping[s] for s in POSE_SUFFIXES]
        source = {mapping[s]: f"{keep}_{s}" for s in POSE_SUFFIXES}
    elif logical_name in STATE_EEF:
        mapping = STATE_OUTPUT
        out_fields = ["timestamp_ms"] + [mapping[s] for s in POSE_SUFFIXES]
        source = {mapping[s]: f"{keep}_{s}" for s in POSE_SUFFIXES}
    elif logical_name == "observation.state.gripper":
        out_fields = ["timestamp_ms", "gripper"]
        source = {"gripper": f"{keep}_gripper"}
    elif logical_name == "observation.state.raw_gripper_rotation":
        out_fields = ["timestamp_ms", "raw_gripper_rotation"]
        source = {"raw_gripper_rotation": f"{keep}_raw_gripper_rotation"}
    else:
        return False

    missing = [src for src in source.values() if src not in fieldnames]
    if missing:
        raise RuntimeError(f"{path} missing columns for {keep}: {missing}")

    new_rows = []
    for row in rows:
        new_row = {"timestamp_ms": row.get("timestamp_ms", "")}
        for out_name, src_name in source.items():
            new_row[out_name] = row.get(src_name, "")
        new_rows.append(new_row)

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(new_rows)
    return True


def map_side_key(key, keep, dead):
    dead_prefix = f"observation.image.{dead}_wrist"
    dead_state_prefix = f"observation.state.{dead}_wrist"
    if key.startswith(dead_prefix) or key.startswith(dead_state_prefix):
        return None
    if keep == "right":
        if key.startswith("observation.image.right_wrist"):
            return key.replace("observation.image.right_wrist", "observation.image.left_wrist", 1)
        if key.startswith("observation.state.right_wrist"):
            return key.replace("observation.state.right_wrist", "observation.state.left_wrist", 1)
    return key


def map_side_dict(obj, keep, dead):
    if not isinstance(obj, dict):
        return obj
    out = {}
    for key, value in obj.items():
        new_key = map_side_key(key, keep, dead)
        if new_key is not None:
            out[new_key] = value
    return out


def single_robot_name(robot):
    if not robot:
        return "single_arm"
    if robot.endswith("_single"):
        return robot
    return f"{robot}_single"


def update_metadata(episode, keep, dead, rewritten_csv_dirs):
    metadata_path = episode / "metadata.json"
    meta_episode_path = episode / "meta" / "episode.json"
    meta_changed = []

    if metadata_path.exists():
        data = read_json(metadata_path)
        robot_orig = data.get("robot", "")
        data["robot"] = single_robot_name(robot_orig)
        if "modalities" in data:
            data["modalities"] = map_side_dict(data["modalities"], keep, dead)
        if "cameras" in data:
            data["cameras"] = map_side_dict(data["cameras"], keep, dead)
        if "frame_integrity" in data:
            data["frame_integrity"] = map_side_dict(data["frame_integrity"], keep, dead)
        if isinstance(data.get("coordinate_reference"), dict):
            sides = data["coordinate_reference"].get("sides")
            if isinstance(sides, dict):
                kept = sides.get(keep)
                data["coordinate_reference"]["sides"] = {"left": kept} if kept is not None else {}
        write_json(metadata_path, data)
        meta_changed.append("metadata.json")
    else:
        robot_orig = ""

    if meta_episode_path.exists():
        data = read_json(meta_episode_path)
        if "modalities" in data:
            data["modalities"] = map_side_dict(data["modalities"], keep, dead)
        write_json(meta_episode_path, data)
        meta_changed.append("meta/episode.json")

    return robot_orig, meta_changed


def remove_and_rename_dirs(episode, keep, dead):
    del_dirs = side_dirs(dead)
    ren_dirs = []
    for name in del_dirs:
        path = episode / name
        if path.exists():
            shutil.rmtree(path)

    if keep == "right":
        for template in SIDE_DIR_TEMPLATES:
            src_name = template.format(side="right")
            dst_name = template.format(side="left")
            src = episode / src_name
            dst = episode / dst_name
            if src.exists():
                if dst.exists():
                    raise RuntimeError(f"rename target already exists: {dst}")
                src.rename(dst)
                ren_dirs.append([src_name, dst_name])
    return del_dirs, ren_dirs


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def include_in_manifest(rel):
    parts = rel.parts
    if any(part.startswith(".") for part in parts):
        return False
    if rel.name in {"checksums.sha256"}:
        return False
    return True


def refresh_checksums(episode):
    files = sorted(
        p for p in episode.rglob("*")
        if p.is_file() and include_in_manifest(p.relative_to(episode))
    )
    manifest = {str(p.relative_to(episode)): sha256_file(p) for p in files}
    write_json(episode / ".checksum_manifest", manifest)
    with (episode / "checksums.sha256").open("w", encoding="utf-8") as f:
        for rel, digest in manifest.items():
            f.write(f"{digest}  {rel}\n")
    return len(manifest)


def convert_episode(episode, keep, dead, make_backup=True):
    del_dirs = side_dirs(dead)
    csv_rewrite = []
    if make_backup:
        backup_convert_inputs(episode, episode / ".single_arm_backup", keep, dead, CSV_DIRS, del_dirs)

    for name in CSV_DIRS:
        path = episode / name / "data.csv"
        if rewrite_csv(path, keep, name):
            csv_rewrite.append(name)

    del_dirs, ren_dirs = remove_and_rename_dirs(episode, keep, dead)
    robot_orig, meta_changed = update_metadata(episode, keep, dead, csv_rewrite)
    embodiment_single = single_robot_name(robot_orig)
    config = {
        "robot_orig": robot_orig,
        "embodiment_single": embodiment_single,
        "keep": keep,
        "dead": dead,
    }
    write_json(episode / ".single_arm.json", config)

    manifest = {
        **config,
        "csv_rewrite": csv_rewrite,
        "del_dirs": del_dirs,
        "ren_dirs": ren_dirs,
        "meta_changed": meta_changed,
        "generated_by": "tools/data/static_check/apply_static_filter.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(episode / ".single_arm_backup" / "manifest.json", manifest)
    checksum_entries = refresh_checksums(episode)
    return {
        "csv_rewrite": csv_rewrite,
        "del_dirs": del_dirs,
        "ren_dirs": ren_dirs,
        "meta_changed": meta_changed,
        "checksum_entries": checksum_entries,
    }


def write_summary(out_dir, rows, apply):
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / ("apply_summary.csv" if apply else "dry_run_summary.csv")
    fields = [
        "sample", "episode_path", "action", "reason", "keep", "dead",
        "applied", "details", "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    counts = {}
    for row in rows:
        key = row["action"] if not row["error"] else "error"
        counts[key] = counts.get(key, 0) + 1
    json_path = out_dir / ("apply_summary.json" if apply else "dry_run_summary.json")
    write_json(json_path, {
        "applied": apply,
        "counts": counts,
        "rows": rows,
    })
    md_path = out_dir / ("APPLY_REPORT.md" if apply else "DRY_RUN_REPORT.md")
    lines = [
        "# Static Filter Summary",
        "",
        f"- Applied: {apply}",
        *[f"- {k}: {v}" for k, v in sorted(counts.items())],
        "",
        "| action | sample | keep | dead | reason | error |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows[:300]:
        lines.append(
            f"| {row['action']} | {row['sample']} | {row['keep']} | "
            f"{row['dead']} | {row['reason']} | {row['error']} |"
        )
    if len(rows) > 300:
        lines.append(f"| ... | {len(rows) - 300} more rows in {csv_path.name} | | | | |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, json_path, md_path


def main():
    args = parse_args()
    samples = read_report(args.report)
    only = set(args.only_sample)
    rows = []
    processed_ops = 0

    for sample, entry in sorted(samples.items()):
        if only and sample not in only:
            continue
        episode = entry["episode_path"]
        decision = decide_operation(entry)
        action = decision["action"]
        if action == "noop":
            continue
        if args.limit is not None and processed_ops >= args.limit:
            continue
        processed_ops += 1
        row = {
            "sample": sample,
            "episode_path": str(episode),
            "action": action,
            "reason": decision["reason"],
            "keep": decision["keep"],
            "dead": decision["dead"],
            "applied": str(args.apply),
            "details": "",
            "error": "",
        }
        try:
            if args.apply:
                if action == "delete_episode":
                    shutil.rmtree(episode)
                    row["details"] = "episode_deleted"
                elif action == "convert_single_arm":
                    details = convert_episode(
                        episode,
                        decision["keep"],
                        decision["dead"],
                        make_backup=not args.no_backup,
                    )
                    row["details"] = json.dumps(details, ensure_ascii=False, sort_keys=True)
            else:
                if action == "convert_single_arm":
                    row["details"] = json.dumps({
                        "would_rewrite_csv": [name for name in CSV_DIRS if (episode / name / "data.csv").exists()],
                        "would_delete_dirs": side_dirs(decision["dead"]),
                        "would_rename_right_to_left": decision["keep"] == "right",
                    }, ensure_ascii=False, sort_keys=True)
                elif action == "delete_episode":
                    row["details"] = "would_delete_episode"
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)

    csv_path, json_path, md_path = write_summary(args.out, rows, args.apply)
    print(f"operations: {len(rows)} apply={args.apply}")
    print(f"wrote {csv_path}")
    print(f"      {json_path}")
    print(f"      {md_path}")
    errors = sum(1 for row in rows if row["error"])
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
