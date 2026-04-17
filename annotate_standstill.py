import os
import csv
import glob

def get_joint_columns(headers):
    # Returns the indices of the joint columns
    joint_indices = []
    for i, h in enumerate(headers):
        if h.endswith('gripper'):
            continue
        if h == 'timestamp_ms':
            continue
        # Check if it satisfies joint name pattern (e.g. j1, left_j1, right_j1)
        if 'j' in h and any(c.isdigit() for c in h):
            joint_indices.append(i)
    return joint_indices

def annotate_episode(episode_dir):
    joint_csv_path = os.path.join(episode_dir, "observation.state.joint_position", "data.csv")
    if not os.path.exists(joint_csv_path):
        return

    with open(joint_csv_path, 'r', newline='') as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return
        
        joint_indices = get_joint_columns(headers)
        if not joint_indices:
            print(f"Skipping {episode_dir}: Could not identify joint columns in {headers}")
            return
        
        try:
            ts_idx = headers.index('timestamp_ms')
        except ValueError:
            print(f"Skipping {episode_dir}: No timestamp_ms column.")
            return

        prev_row = None
        t_last_move = None
        t_last = None
        first_ts = None
        
        for row in reader:
            if len(row) <= max(joint_indices) or len(row) <= ts_idx:
                continue
            try:
                ts = int(row[ts_idx])
            except ValueError:
                continue
            
            if first_ts is None:
                first_ts = ts
                t_last_move = ts

            t_last = ts
            
            if prev_row is not None:
                moved = False
                for j_idx in joint_indices:
                    try:
                        diff = abs(float(row[j_idx]) - float(prev_row[j_idx]))
                        if diff > 0.05:
                            moved = True
                            break
                    except ValueError:
                        pass
                if moved:
                    t_last_move = ts
            
            prev_row = row

    if t_last_move is None or t_last is None:
        return

    duration = t_last - t_last_move
    if duration > 4000:
        t_cutoff = t_last_move + 4000
        print(f"Episode {episode_dir}: Standstill duration {duration}ms. Annotating cutoff at {t_cutoff}ms.")
    else:
        t_cutoff = float('inf')
        if duration > 0:
            print(f"Episode {episode_dir}: Standstill duration {duration}ms (<= 4000ms threshold). Annotating as entirely active.")
    
    # Always run annotation to overwrite false positives from previous runs
    annotate_all_csvs(episode_dir, t_cutoff)

def annotate_all_csvs(episode_dir, t_cutoff):
    csv_files = glob.glob(os.path.join(episode_dir, "**", "*.csv"), recursive=True)
    for csv_file in csv_files:
        temp_file = csv_file + ".tmp"
        
        # In case the file or directory is read-only, try to change permissions
        orig_mode = os.stat(csv_file).st_mode
        parent_dir = os.path.dirname(csv_file)
        orig_dir_mode = os.stat(parent_dir).st_mode
        try:
            os.chmod(csv_file, orig_mode | 0o644)
            os.chmod(parent_dir, orig_dir_mode | 0o755)
        except Exception:
            pass

        try:
            with open(csv_file, 'r', newline='') as infile, open(temp_file, 'w', newline='') as outfile:
                reader = csv.reader(infile)
                writer = csv.writer(outfile)
                
                try:
                    headers = next(reader)
                except StopIteration:
                    continue
                
                # Remove existing is_standstill if it was previously annotated
                if 'is_standstill' in headers:
                    standstill_idx = headers.index('is_standstill')
                    new_headers = headers
                    has_existing = True
                else:
                    new_headers = headers + ['is_standstill']
                    has_existing = False
                    standstill_idx = len(new_headers) - 1

                writer.writerow(new_headers)
                
                if 'timestamp_ms' in headers:
                    ts_idx = headers.index('timestamp_ms')
                else:
                    ts_idx = -1

                for row in reader:
                    is_standstill = False
                    if ts_idx != -1 and len(row) > ts_idx:
                        try:
                            ts = int(row[ts_idx])
                            if ts > t_cutoff:
                                is_standstill = True
                        except ValueError:
                            pass
                    
                    if has_existing:
                        if len(row) > standstill_idx:
                            row[standstill_idx] = str(is_standstill)
                        else:
                            row.append(str(is_standstill))
                        writer.writerow(row)
                    else:
                        writer.writerow(row + [str(is_standstill)])

            os.replace(temp_file, csv_file)
        except PermissionError:
            print(f"  Warning: Permission denied for writing {csv_file}. Skipping.")
        finally:
            try:
                os.chmod(csv_file, orig_mode)
                os.chmod(parent_dir, orig_dir_mode)
            except Exception:
                pass
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    episode_dirs = glob.glob(os.path.join(base_dir, "*", "*", "*", "episode_*"))
    
    count = 0
    for d in sorted(episode_dirs):
        if os.path.isdir(d):
            annotate_episode(d)
            count += 1
    
    print(f"\nProcessed {count} episodes.")
