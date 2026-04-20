import os
import csv
import glob
import argparse
from typing import List, Optional, Tuple

STANDSTILL_BUFFER_MS = 4000

def get_data_columns(headers: List[str]) -> List[int]:
    """Identify columns that represent movement data (ignoring timestamp and metadata)."""
    return [
        i for i, h in enumerate(headers)
        if 'gripper' not in h and h not in ('timestamp_ms', 'is_standstill')
    ]

def find_last_movement(csv_path: str, threshold: float) -> Optional[int]:
    """Scan the CSV file and return the timestamp of the last detected movement."""
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

        t_last_move = None
        prev_row = None
        
        for row in reader:
            if len(row) <= max(data_indices) or len(row) <= ts_idx:
                continue
            
            try:
                ts = int(row[ts_idx])
            except ValueError:
                continue
            
            if prev_row is None:
                t_last_move = ts
            else:
                for j_idx in data_indices:
                    try:
                        if abs(float(row[j_idx]) - float(prev_row[j_idx])) > threshold:
                            t_last_move = ts
                            break
                    except ValueError:
                        pass
                        
            prev_row = row

    return t_last_move

def annotate_csv_file(csv_path: str, t_cutoff: float) -> None:
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

            for row in reader:
                is_standstill = False
                if ts_idx != -1 and len(row) > ts_idx:
                    try:
                        if int(row[ts_idx]) > t_cutoff:
                            is_standstill = True
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

def process_episode(episode_dir: str, target_csv_path: Optional[str] = None, threshold: float = 0.01) -> None:
    """Process an episode: calculate its standstill cutoff and rewrite its CSV files."""
    csv_path = target_csv_path or os.path.join(episode_dir, "observation.state.joint_position", "data.csv")
    
    t_last_move = find_last_movement(csv_path, threshold)
    if t_last_move is None:
        return

    # Determine final timestamp from the file to calculate full duration
    t_last_overall = t_last_move
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            t_last_overall = int(list(csv.reader(f))[-1][0])
    except Exception:
        pass

    duration = t_last_overall - t_last_move
    if duration > STANDSTILL_BUFFER_MS:
        t_cutoff = t_last_move + STANDSTILL_BUFFER_MS
        annotated_duration = t_last_overall - t_cutoff
        print(f"Episode {episode_dir}: Total physical standstill {duration}ms. "
              f"Waiting {STANDSTILL_BUFFER_MS}ms buffer -> Annotating {annotated_duration}ms as True.")
    else:
        t_cutoff = float('inf')
        if duration > 0:
            print(f"Episode {episode_dir}: Standstill duration {duration}ms (<= {STANDSTILL_BUFFER_MS}ms threshold). "
                  "Annotating as entirely active.")

    # Annotate all CSVs in the episode
    for f in glob.glob(os.path.join(episode_dir, "**", "*.csv"), recursive=True):
        annotate_csv_file(f, t_cutoff)

def main():
    parser = argparse.ArgumentParser(description="Annotate robot continuous standstill data.")
    parser.add_argument("path", nargs="?", default=".", help="Path to a specific episode directory, data.csv, or base directory.")
    parser.add_argument("--threshold", type=float, default=0.05, help="Movement delta threshold per frame.")
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
        process_episode(episode_dir, target_csv_path=target_path, threshold=args.threshold)
        
    elif os.path.isdir(target_path):
        if os.path.basename(target_path).startswith("episode_"):
            print(f"Testing episode directory: {target_path} (threshold: {args.threshold})")
            process_episode(target_path, threshold=args.threshold)
        else:
            print(f"Scanning base repository: {target_path} (threshold: {args.threshold})")
            episode_dirs = glob.glob(os.path.join(target_path, "*", "*", "*", "episode_*"))
            count = 0
            for d in sorted(episode_dirs):
                if os.path.isdir(d):
                    process_episode(d, threshold=args.threshold)
                    count += 1
            print(f"\nProcessed {count} episodes.")
    else:
        print(f"Invalid path provided: {target_path}")

if __name__ == "__main__":
    main()
