import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

try:
    from .ee_transform import EEF_POSE_DIR, load_config, transform_row
except ImportError:
    from ee_transform import EEF_POSE_DIR, load_config, transform_row

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


VIDEO_STREAMS = {
    "left": "observation.image.left_wrist_view",
    "right": "observation.image.right_wrist_view",
}
ACTION_EEF_POSE_DIR = "actions.eef_pose"
EXPORT_TRANSFORM_VERSION = "v1"
EXPORT_TRANSFORM_TAG = "world_eef_raw"


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Transform episode(s) into world-frame EEF pose, flip wrist-view videos, "
            "and optionally crop a single episode to a frame range."
        )
    )
    parser.add_argument(
        "input_path_arg",
        nargs="?",
        type=Path,
        help="Input class directory, or a class_name/episode_XXX directory.",
    )
    parser.add_argument("-i", "--input-path", type=Path, help="Input class or episode directory.")
    parser.add_argument(
        "-o",
        "--output-root",
        type=Path,
        default=Path("outputs"),
        help="Output root directory. Defaults to outputs.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="Transform config JSON. Defaults to ee_trajectory_config.json next to this script.",
    )
    parser.add_argument(
        "--start-frame",
        type=int,
        default=None,
        help="Inclusive start frame for crop. Only valid when input is a single episode_XXX directory.",
    )
    parser.add_argument(
        "--end-frame",
        type=int,
        default=None,
        help="Inclusive end frame for crop. Only valid when input is a single episode_XXX directory.",
    )
    return parser.parse_args()


def is_episode_dir(path):
    return path.is_dir() and path.name.startswith("episode_")


def find_episode_dirs(input_path):
    """Return a list of episode_* directories under ``input_path``.

    ``input_path`` must be either an ``episode_XXX`` directory itself, or a
    directory that contains at least one ``episode_XXX`` subdirectory.
    Anything else raises ``ValueError`` with a usage hint.
    """
    if not input_path.exists():
        raise FileNotFoundError(
            f"Input path does not exist: {input_path}\n"
            "Expected an episode_XXX directory, or a class directory containing episode_XXX subdirectories."
        )
    if is_episode_dir(input_path):
        return [input_path]
    if input_path.is_dir():
        episodes = sorted(p for p in input_path.iterdir() if is_episode_dir(p))
        if episodes:
            return episodes
    raise ValueError(
        f"Invalid input path: {input_path}\n"
        "Expected one of:\n"
        "  - an episode_XXX directory (e.g. .../class_name/episode_0001)\n"
        "  - a class directory containing at least one episode_XXX subdirectory "
        "(e.g. .../class_name)"
    )


def csv_rows(csv_path):
    with csv_path.open("r", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(csv_path, fieldnames, rows):
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_timestamp_ms(value, path):
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid timestamp_ms in {path}: {value!r}") from exc


def format_timestamp_ms(value):
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return str(int(rounded))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def json_timestamp_ms(value):
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return int(rounded)
    return float(f"{value:.6f}")


def transform_pose_csv(csv_path, config):
    if not csv_path.exists():
        return False
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [transform_row(row, config=config) for row in reader]
    write_csv_rows(csv_path, fieldnames, rows)
    return True


def crop_timestamped_csv(csv_path, start_timestamp_ms, end_timestamp_ms, timestamp_offset_ms):
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "timestamp_ms" not in fieldnames:
        raise ValueError(f"Cannot crop {csv_path}: missing timestamp_ms column.")

    kept_rows = []
    for row in rows:
        timestamp_ms = parse_timestamp_ms(row.get("timestamp_ms"), csv_path)
        if start_timestamp_ms <= timestamp_ms <= end_timestamp_ms:
            out = dict(row)
            out["timestamp_ms"] = format_timestamp_ms(timestamp_ms - timestamp_offset_ms)
            kept_rows.append(out)
    write_csv_rows(csv_path, fieldnames, kept_rows)
    return {"path": str(csv_path), "input_rows": len(rows), "output_rows": len(kept_rows)}


def estimate_frame_delta_ms(timestamps):
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    if not deltas:
        return 33.333333
    deltas = sorted(deltas)
    return deltas[len(deltas) // 2]


def wrist_video_paths(episode_dir):
    return [
        episode_dir / stream_dir / "video.mp4"
        for stream_dir in VIDEO_STREAMS.values()
        if (episode_dir / stream_dir / "video.mp4").exists()
    ]


def is_wrist_video_path(video_path):
    return video_path.parent.name in set(VIDEO_STREAMS.values())


def run_video_transcode(video_path, temp_path, clip_start_sec=None, clip_end_sec=None, flip=False):
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    if clip_start_sec is not None:
        command.extend(["-ss", f"{clip_start_sec:.6f}"])
    if clip_end_sec is not None:
        command.extend(["-to", f"{clip_end_sec:.6f}"])
    command.extend(["-i", str(video_path), "-an"])
    if flip:
        command.extend(["-vf", "hflip,vflip"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
    )
    subprocess.run(command, check=True)
    temp_path.replace(video_path)


def flip_video_in_place(video_path):
    temp_path = video_path.with_name(f"{video_path.stem}.flip.tmp{video_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()
    try:
        run_video_transcode(video_path, temp_path, flip=True)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return str(video_path)


def flip_wrist_videos(episode_dir):
    return [flip_video_in_place(p) for p in wrist_video_paths(episode_dir)]


def crop_video_with_timestamps(video_path, start_timestamp_ms, end_timestamp_ms, timestamp_offset_ms, flip=False):
    timestamp_path = video_path.parent / "timestamps.csv"
    if not timestamp_path.exists():
        raise ValueError(f"Cannot crop {video_path}: missing timestamps.csv.")
    with timestamp_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "timestamp_ms" not in fieldnames:
        raise ValueError(f"Cannot crop {timestamp_path}: missing timestamp_ms column.")
    timestamps = [parse_timestamp_ms(row.get("timestamp_ms"), timestamp_path) for row in rows]
    kept_indices = [
        index
        for index, timestamp_ms in enumerate(timestamps)
        if start_timestamp_ms <= timestamp_ms <= end_timestamp_ms
    ]
    if not kept_indices:
        raise ValueError(f"Cannot crop {video_path}: no video timestamps overlap the requested frame range.")

    first_video_timestamp = timestamps[0]
    first_index = kept_indices[0]
    last_index = kept_indices[-1]
    clip_start_sec = max(0.0, (start_timestamp_ms - first_video_timestamp) / 1000.0)
    if last_index + 1 < len(timestamps):
        clip_end_timestamp = timestamps[last_index + 1]
    else:
        clip_end_timestamp = timestamps[last_index] + estimate_frame_delta_ms(timestamps)
    clip_end_sec = max(clip_start_sec + 0.001, (clip_end_timestamp - first_video_timestamp) / 1000.0)

    output_rows = []
    for index in kept_indices:
        out = dict(rows[index])
        out["timestamp_ms"] = format_timestamp_ms(timestamps[index] - timestamp_offset_ms)
        output_rows.append(out)
    write_csv_rows(timestamp_path, fieldnames, output_rows)

    temp_path = video_path.with_name(f"{video_path.stem}.crop.tmp{video_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()
    try:
        run_video_transcode(video_path, temp_path, clip_start_sec=clip_start_sec, clip_end_sec=clip_end_sec, flip=flip)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return {
        "path": str(video_path),
        "input_frames": len(rows),
        "output_frames": len(output_rows),
        "clip_start_sec": clip_start_sec,
        "clip_end_sec": clip_end_sec,
        "flipped": flip,
    }


def crop_exported_episode(episode_dir, start_frame, end_frame):
    primary_csv = episode_dir / EEF_POSE_DIR / "data.csv"
    if not primary_csv.exists():
        raise ValueError(f"Cannot crop episode: missing {EEF_POSE_DIR}/data.csv.")
    primary_rows = csv_rows(primary_csv)
    if not primary_rows:
        raise ValueError(f"Cannot crop episode: {EEF_POSE_DIR}/data.csv has no rows.")

    max_frame = len(primary_rows) - 1
    if start_frame is None:
        start_frame = 0
    if end_frame is None:
        end_frame = max_frame
    if start_frame < 0 or end_frame < 0 or start_frame > max_frame or end_frame > max_frame or start_frame > end_frame:
        raise ValueError(f"Crop frame range must be between 0 and {max_frame}, with start <= end.")

    start_timestamp_ms = parse_timestamp_ms(primary_rows[start_frame].get("timestamp_ms"), primary_csv)
    end_timestamp_ms = parse_timestamp_ms(primary_rows[end_frame].get("timestamp_ms"), primary_csv)
    if start_timestamp_ms > end_timestamp_ms:
        raise ValueError("Crop frame timestamps are not monotonic: start timestamp is after end timestamp.")

    csv_results = []
    for data_csv in sorted(episode_dir.rglob("data.csv")):
        csv_results.append(crop_timestamped_csv(data_csv, start_timestamp_ms, end_timestamp_ms, start_timestamp_ms))

    video_results = []
    for video_path in sorted(episode_dir.rglob("video.mp4")):
        video_results.append(
            crop_video_with_timestamps(
                video_path,
                start_timestamp_ms,
                end_timestamp_ms,
                start_timestamp_ms,
                flip=is_wrist_video_path(video_path),
            )
        )

    return {
        "enabled": True,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": end_frame - start_frame + 1,
        "start_timestamp_ms": json_timestamp_ms(start_timestamp_ms),
        "end_timestamp_ms": json_timestamp_ms(end_timestamp_ms),
        "timestamp_offset_ms": json_timestamp_ms(start_timestamp_ms),
        "csv_files": len(csv_results),
        "video_files": len(video_results),
        "flipped_video_files": sum(1 for r in video_results if r.get("flipped")),
    }


def update_export_metadata(episode_dir, crop_info=None):
    metadata_path = episode_dir / "metadata.json"
    if not metadata_path.exists():
        return False
    with metadata_path.open("r", encoding="utf-8") as f:
        metadata = json.load(f)
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["umi_transform_version"] = EXPORT_TRANSFORM_VERSION
    metadata["umi_transform_tag"] = EXPORT_TRANSFORM_TAG
    metadata["umi_transform_cropped"] = bool(crop_info)
    if crop_info:
        metadata["umi_transform_crop"] = crop_info
    with metadata_path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return True


def recompute_checksum_manifest(episode_dir):
    manifest_path = episode_dir / "checksums.sha256"
    if not manifest_path.exists():
        return False
    entries = []
    for path in sorted(episode_dir.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(f"{digest.hexdigest()}  {path.relative_to(episode_dir).as_posix()}")
    manifest_path.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    return True


def resolve_output_root(output_dir):
    raw = (str(output_dir) or "outputs").strip() or "outputs"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def transform_episode(source_episode_dir, destination_episode_dir, config, crop_request=None, status=None):
    def step(message):
        if status is not None:
            status(message)
        else:
            print(f"  {message}")

    source_episode_dir = Path(source_episode_dir).resolve()
    destination_episode_dir = Path(destination_episode_dir).resolve()
    if source_episode_dir == destination_episode_dir:
        raise ValueError("Destination cannot be the source episode directory.")
    if destination_episode_dir.exists():
        step(f"removing existing {destination_episode_dir.name}")
        shutil.rmtree(destination_episode_dir)
    destination_episode_dir.parent.mkdir(parents=True, exist_ok=True)
    step(f"copying {source_episode_dir.name}")
    shutil.copytree(source_episode_dir, destination_episode_dir)

    crop_info = None
    flipped_videos = []
    if crop_request and crop_request.get("enabled"):
        step("cropping CSV/video files (wrist views flipped during crop)")
        crop_info = crop_exported_episode(
            destination_episode_dir,
            crop_request["start_frame"],
            crop_request["end_frame"],
        )
    else:
        step("flipping wrist-view videos")
        flipped_videos = [
            str(Path(p).relative_to(destination_episode_dir))
            for p in flip_wrist_videos(destination_episode_dir)
        ]

    step("transforming pose CSV files")
    transformed_files = []
    for rel_dir in (EEF_POSE_DIR, ACTION_EEF_POSE_DIR):
        csv_path = destination_episode_dir / rel_dir / "data.csv"
        if transform_pose_csv(csv_path, config):
            transformed_files.append(str(csv_path.relative_to(destination_episode_dir)))

    step("updating metadata.json and checksums.sha256")
    update_export_metadata(destination_episode_dir, crop_info)
    recompute_checksum_manifest(destination_episode_dir)

    return {
        "source": str(source_episode_dir),
        "destination": str(destination_episode_dir),
        "transformed_files": transformed_files,
        "crop": crop_info,
        "flipped_videos": flipped_videos,
    }


def main():
    args = parse_args()
    input_path = args.input_path or args.input_path_arg
    if input_path is None:
        raise ValueError(
            "Input path is required. Expected an episode_XXX directory "
            "(e.g. .../class_name/episode_0001) or a class directory containing "
            "episode_XXX subdirectories (e.g. .../class_name)."
        )

    config = load_config(args.config)
    episode_dirs = find_episode_dirs(input_path)
    single_episode = is_episode_dir(input_path)
    crop_request = None
    if args.start_frame is not None or args.end_frame is not None:
        if not single_episode:
            raise ValueError("--start-frame/--end-frame are only valid when input is a single episode_XXX directory.")
        crop_request = {
            "enabled": True,
            "start_frame": args.start_frame,
            "end_frame": args.end_frame,
        }

    class_name = input_path.parent.name if single_episode else input_path.name
    output_root = resolve_output_root(args.output_root)
    output_class_dir = output_root / class_name

    print(f"Transform target: {class_name} ({len(episode_dirs)} episode(s))")
    print(f"Output: {output_class_dir}")

    use_bar = tqdm is not None and len(episode_dirs) > 1
    bar = tqdm(total=len(episode_dirs), unit="episode", desc=class_name) if use_bar else None

    def log(message):
        if bar is not None:
            tqdm.write(message)
        else:
            print(message)

    processed = 0
    skipped = 0
    try:
        for episode_dir in episode_dirs:
            if not (episode_dir / EEF_POSE_DIR / "data.csv").exists():
                log(f"Skipping {episode_dir.name}: missing {EEF_POSE_DIR}/data.csv")
                skipped += 1
                if bar is not None:
                    bar.update(1)
                continue

            if bar is not None:
                bar.set_description(f"{class_name} | {episode_dir.name}")
                status_callback = lambda message, _bar=bar: _bar.set_postfix_str(message, refresh=True)
            else:
                print(f"Episode: {episode_dir.name}")
                status_callback = None

            transform_episode(
                episode_dir,
                output_class_dir / episode_dir.name,
                config,
                crop_request=crop_request,
                status=status_callback,
            )
            processed += 1
            if bar is not None:
                bar.set_postfix_str("done", refresh=False)
                bar.update(1)
    finally:
        if bar is not None:
            bar.close()

    print(f"Done. Processed {processed} episode(s), skipped {skipped}.")


if __name__ == "__main__":
    main()
