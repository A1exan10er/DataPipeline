import os
import csv
import glob

def fix_teleop_folders(base_dir):
    # Find all data.csv inside actions.joint_position directories
    csv_files = glob.glob(os.path.join(base_dir, "**", "actions.joint_position", "data.csv"), recursive=True)
    
    count = 0
    for csv_path in csv_files:
        try:
            with open(csv_path, 'r', newline='') as f:
                reader = csv.reader(f)
                try:
                    headers = next(reader)
                except StopIteration:
                    continue
                
            # Check if headers match the teleop structure identifying tcp data
            required_cols = ['timestamp_ms', 'tcp.x', 'tcp.y', 'tcp.z', 'tcp.r1', 'tcp.r2', 'tcp.r3', 'tcp.r4', 'tcp.r5', 'tcp.r6', 'gripper.pos']
            
            # We verify if at least 'tcp.x' is in the headers to recognize EEF pose data
            if 'tcp.x' in headers and 'tcp.r6' in headers:
                src_dir = os.path.dirname(csv_path)  # .../actions.joint_position
                parent_episode_dir = os.path.dirname(src_dir) # .../episode_XXXX
                dst_dir = os.path.join(parent_episode_dir, "actions.eef_pose")
                
                # Check if actions.eef_pose already exists to prevent overwrite conflicts
                if os.path.exists(dst_dir):
                    print(f"Warning: Destination {dst_dir} already exists. Skipping {src_dir}.")
                    continue
                
                # Handle permissions in case of read-only dataset structures (NAS)
                orig_dir_mode = os.stat(parent_episode_dir).st_mode
                try:
                    os.chmod(parent_episode_dir, orig_dir_mode | 0o755)
                except Exception:
                    pass
                
                try:
                    os.rename(src_dir, dst_dir)
                    print(f"Corrected folder: {src_dir} \n               -> {dst_dir}")
                    count += 1
                except Exception as e:
                    print(f"Failed to rename {src_dir}: {e}")
                finally:
                    try:
                        os.chmod(parent_episode_dir, orig_dir_mode)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Error processing {csv_path}: {e}")
            
    print(f"\nTotal folders corrected: {count}")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    print("Scanning for misplaced teleop commands...")
    fix_teleop_folders(base_dir)
