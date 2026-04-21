import os
import csv
import glob
import argparse
from typing import List, NamedTuple, Optional, Tuple

STANDSTILL_BUFFER_MS = 4000


class LongStopSegment(NamedTuple):
    """A detected long stop and the exact annotated sub-range."""
    stop_start_ms: int
    stop_end_ms: int
    duration_ms: int
    annotate_start_ms: int
    annotate_end_ms: int

def get_data_columns(headers: List[str]) -> List[int]:
    """Identify columns that represent movement data (ignoring timestamp and metadata)."""
    return [
        i for i, h in enumerate(headers)
        if 'gripper' not in h and h not in ('timestamp_ms', 'is_standstill')
    ]

def detect_long_stop_segments(csv_path: str, threshold: float) -> Optional[List[LongStopSegment]]:
    """Find standstill intervals from a CSV as (start_threshold_ms, end_ms).

    A row is annotated as standstill when timestamp_ms > start_threshold_ms and
    timestamp_ms <= end_ms. This allows short pauses to be ignored while only
    marking the excess duration beyond STANDSTILL_BUFFER_MS.
    
    Detects multiple stop-move-stop patterns in the episode.
    """
    if not os.path.exists(csv_path):
        return None

    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return None
        
        data_indices = get_data_columns(headers)
        if not data_indices:
            return None
            
        try:
            ts_idx = headers.index('timestamp_ms')
        except ValueError:
            return None

        long_stops: List[LongStopSegment] = []
        rows_list = []
        timestamps = []
        
        # Load all data
        for row in reader:
            if len(row) <= max(data_indices) or len(row) <= ts_idx:
                continue
            
            try:
                ts = int(row[ts_idx])
                timestamps.append(ts)
                rows_list.append(row)
            except ValueError:
                continue
        
        if len(rows_list) < 2:
            return None
        
        # Detect stillness segments by analyzing movement between consecutive rows
        still_start_idx: Optional[int] = None
        still_end_idx: Optional[int] = None
        
        for i in range(1, len(rows_list)):
            prev_row = rows_list[i - 1]
            curr_row = rows_list[i]
            
            moving = False
            for j_idx in data_indices:
                try:
                    if abs(float(curr_row[j_idx]) - float(prev_row[j_idx])) > threshold:
                        moving = True
                        break
                except ValueError:
                    continue
            
            if moving:
                # Movement detected - close any active still segment
                if still_start_idx is not None and still_end_idx is not None:
                    duration_ms = timestamps[still_end_idx] - timestamps[still_start_idx]
                    if duration_ms > STANDSTILL_BUFFER_MS:
                        start_threshold = timestamps[still_start_idx] + STANDSTILL_BUFFER_MS
                        end_ts = timestamps[still_end_idx]
                        long_stops.append(
                            LongStopSegment(
                                stop_start_ms=timestamps[still_start_idx],
                                stop_end_ms=timestamps[still_end_idx],
                                duration_ms=duration_ms,
                                annotate_start_ms=start_threshold,
                                annotate_end_ms=end_ts,
                            )
                        )
                    still_start_idx = None
                    still_end_idx = None
            else:
                # No movement - track stillness segment
                if still_start_idx is None:
                    still_start_idx = i - 1  # Start from previous row (last known position)
                still_end_idx = i  # Update end to current row
        
        # Handle final segment
        if still_start_idx is not None and still_end_idx is not None:
            duration_ms = timestamps[still_end_idx] - timestamps[still_start_idx]
            if duration_ms > STANDSTILL_BUFFER_MS:
                start_threshold = timestamps[still_start_idx] + STANDSTILL_BUFFER_MS
                end_ts = timestamps[still_end_idx]
                long_stops.append(
                    LongStopSegment(
                        stop_start_ms=timestamps[still_start_idx],
                        stop_end_ms=timestamps[still_end_idx],
                        duration_ms=duration_ms,
                        annotate_start_ms=start_threshold,
                        annotate_end_ms=end_ts,
                    )
                )

    return long_stops


def detect_standstill_intervals(csv_path: str, threshold: float) -> Optional[List[Tuple[int, int]]]:
    """Keep compatibility: return only the annotation intervals."""
    long_stops = detect_long_stop_segments(csv_path, threshold)
    if long_stops is None:
        return None
    return [(seg.annotate_start_ms, seg.annotate_end_ms) for seg in long_stops]

def is_timestamp_standstill(ts: int, intervals: List[Tuple[int, int]], idx: int) -> Tuple[bool, int]:
    """Check standstill membership with a moving interval pointer for sorted timestamps."""
    while idx < len(intervals) and ts > intervals[idx][1]:
        idx += 1

    if idx < len(intervals):
        start_threshold, end_ts = intervals[idx]
        if start_threshold < ts <= end_ts:
            return True, idx

    return False, idx

def annotate_csv_file(csv_path: str, standstill_intervals: List[Tuple[int, int]]) -> None:
    """Rewrite a single CSV file, appending or updating the is_standstill column."""
    temp_path = f"{csv_path}.tmp"
    parent_dir = os.path.dirname(csv_path)
    
    # Store original permissions
    orig_file_mode = os.stat(csv_path).st_mode
    orig_dir_mode = os.stat(parent_dir).st_mode
    
    try:
        os.chmod(csv_path, orig_file_mode | 0o644)
        os.chmod(parent_dir, orig_dir_mode | 0o755)
        
        with open(csv_path, 'r', newline='', encoding='utf-8') as infile, \
             open(temp_path, 'w', newline='', encoding='utf-8') as outfile:
            
            reader = csv.reader(infile)
            writer = csv.writer(outfile)
            
            try:
                headers = next(reader)
            except StopIteration:
                return

            has_existing = 'is_standstill' in headers
            standstill_idx = headers.index('is_standstill') if has_existing else len(headers)
            ts_idx = headers.index('timestamp_ms') if 'timestamp_ms' in headers else -1

            if not has_existing:
                headers.append('is_standstill')
            writer.writerow(headers)

            interval_idx = 0

            for row in reader:
                is_standstill = False
                if ts_idx != -1 and len(row) > ts_idx:
                    try:
                        ts = int(row[ts_idx])
                        is_standstill, interval_idx = is_timestamp_standstill(ts, standstill_intervals, interval_idx)
                    except ValueError:
                        pass
                
                # Update or append the boolean flag
                if has_existing and len(row) > standstill_idx:
                    row[standstill_idx] = str(is_standstill)
                else:
                    row.append(str(is_standstill))
                    
                writer.writerow(row)

        os.replace(temp_path, csv_path)
        
    except PermissionError:
        print(f"  Warning: Permission denied for writing {csv_path}. Skipping.")
    finally:
        for p, mode in [(csv_path, orig_file_mode), (parent_dir, orig_dir_mode)]:
            try:
                os.chmod(p, mode)
            except Exception:
                pass
        if os.path.exists(temp_path):
            os.remove(temp_path)

def process_episode(
    episode_dir: str,
    target_csv_path: Optional[str] = None,
    threshold: float = 0.01,
    show_stop_log: bool = False,
) -> None:
    """Process an episode: detect standstill intervals and rewrite its CSV files."""
    csv_path = target_csv_path or os.path.join(episode_dir, "observation.state.joint_position", "data.csv")

    long_stops = detect_long_stop_segments(csv_path, threshold)
    if long_stops is None:
        return

    standstill_intervals = [(seg.annotate_start_ms, seg.annotate_end_ms) for seg in long_stops]
    if standstill_intervals is None:
        return

    if standstill_intervals:
        annotated_duration = sum(end_ms - start_threshold for start_threshold, end_ms in standstill_intervals)
        print(
            f"Episode {episode_dir}: Detected {len(standstill_intervals)} long standstill segments. "
            f"Annotating {annotated_duration}ms beyond {STANDSTILL_BUFFER_MS}ms buffer as True."
        )
        if show_stop_log:
            for i, seg in enumerate(long_stops, start=1):
                print(
                    f"  Long stop {i}: stop {seg.stop_start_ms}ms -> {seg.stop_end_ms}ms "
                    f"({seg.duration_ms}ms). Annotated range: {seg.annotate_start_ms}ms -> "
                    f"{seg.annotate_end_ms}ms."
                )

    # Annotate all CSVs in the episode
    for f in glob.glob(os.path.join(episode_dir, "**", "*.csv"), recursive=True):
        annotate_csv_file(f, standstill_intervals)

def main():
    parser = argparse.ArgumentParser(description="Annotate robot continuous standstill data.")
    parser.add_argument("path", nargs="?", default=".", help="Path to a specific episode directory, data.csv, or base directory.")
    parser.add_argument("--threshold", type=float, default=0.05, help="Movement delta threshold per frame.")
    parser.add_argument(
        "--show-stop-log",
        action="store_true",
        help="Print detailed long-stop ranges (start/end/duration and annotated sub-range).",
    )
    args = parser.parse_args()

    target_path = os.path.abspath(args.path)
    
    if os.path.isfile(target_path) and target_path.endswith('.csv'):
        # Resolve episode directory assuming standard structure
        parts = target_path.split(os.sep)
        episode_dir = target_path
        for i in range(len(parts)-1, -1, -1):
            if parts[i].startswith("episode_"):
                episode_dir = os.sep.join(parts[:i+1])
                break
        else:
            episode_dir = os.path.dirname(target_path)
            
        print(f"Testing specific CSV: {target_path} (threshold: {args.threshold})")
        process_episode(
            episode_dir,
            target_csv_path=target_path,
            threshold=args.threshold,
            show_stop_log=args.show_stop_log,
        )
        
    elif os.path.isdir(target_path):
        if os.path.basename(target_path).startswith("episode_"):
            print(f"Testing episode directory: {target_path} (threshold: {args.threshold})")
            process_episode(target_path, threshold=args.threshold, show_stop_log=args.show_stop_log)
        else:
            print(f"Scanning base repository: {target_path} (threshold: {args.threshold})")
            episode_dirs = glob.glob(os.path.join(target_path, "*", "*", "*", "episode_*"))
            count = 0
            for d in sorted(episode_dirs):
                if os.path.isdir(d):
                    process_episode(d, threshold=args.threshold, show_stop_log=args.show_stop_log)
                    count += 1
            print(f"\nProcessed {count} episodes.")
    else:
        print(f"Invalid path provided: {target_path}")

if __name__ == "__main__":
    main()
