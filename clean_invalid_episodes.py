import argparse
import os
import re
import shutil
import logging
from pathlib import Path

def setup_logger(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )

def notify_server_api(old_path, new_path):
    """
    Placeholder function for API integration.
    Currently simply records the intent. When run on a server, this function
    should be populated with the HTTP POST requests or internal tracking methods.
    """
    # e.g. requests.post('https://api.internal.com/dataStatus', json={...})
    pass

def scan_and_quarantine(root_dir, quarantine_dir, dry_run=False):
    root_path = Path(root_dir).resolve()
    quarantine_path = Path(quarantine_dir).resolve()
    
    # Regex to explicitly allow 'episode_' followed by ANY number of digits (\d+) lengths
    episode_pattern = re.compile(r'^episode_\d+$')
    
    if not root_path.exists():
        logging.error(f"Root directory {root_path} does not exist.")
        return
        
    logging.info(f"Scanning from: {root_path}")
    logging.info(f"Quarantine target folder: {quarantine_path}")
    
    if dry_run:
        logging.info("--- DRY RUN MODE ENABLED ---")
        
    for task_dir in root_path.iterdir():
        # Exclude hidden directories, known system directories, and the quarantine directory itself
        if not task_dir.is_dir() or task_dir.name.startswith('.') or task_dir == quarantine_path:
            continue
            
        for date_dir in task_dir.iterdir():
            if not date_dir.is_dir(): continue
            
            for operator_dir in date_dir.iterdir():
                if not operator_dir.is_dir(): continue
                
                for episode_dir in operator_dir.iterdir():
                    if not episode_dir.is_dir(): continue
                    
                    # Validate the format of the 4th level directory
                    if not episode_pattern.match(episode_dir.name):
                        handle_invalid_episode(root_path, episode_dir, quarantine_path, dry_run)

def handle_invalid_episode(root_path, episode_dir, quarantine_root, dry_run):
    rel_path = episode_dir.relative_to(root_path)
    target_path = quarantine_root / rel_path
    
    if dry_run:
        logging.info(f"[DRY-RUN] Would move {episode_dir} to {target_path}")
    else:
        # Create target parent directory structure if it doesn't exist
        target_path.parent.mkdir(parents=True, exist_ok=True)
        
        # When moving a directory across parents in Unix, the '..' entry inside it must be updated.
        # This requires the directory itself to refer to it as writable.
        original_mode = episode_dir.stat().st_mode
        try:
            # Temporarily grant write permission to the directory so `os.rename` succeeds
            os.chmod(str(episode_dir), original_mode | 0o200)
            
            shutil.move(str(episode_dir), str(target_path))
            
            # Restore the original permissions at the destination
            os.chmod(str(target_path), original_mode)
            
            logging.info(f"[MOVED] {episode_dir} -> {target_path}")
            # Log trace to server API if needed
            notify_server_api(episode_dir, target_path)
        except Exception as e:
            logging.error(f"[ERROR] Failed to move {episode_dir}: {e}")
            # Attempt to restore permissions upon failure
            try:
                os.chmod(str(episode_dir), original_mode)
            except:
                pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Data Storage Cleanup & Quarantine Tracker")
    parser.add_argument('--root', type=str, default='.', help='Root directory of the data (e.g. NAS Data folder or test DataPipeline folder)')
    parser.add_argument('--quarantine', type=str, default='./quarantine_data', help='Folder to store invalid/unrecognized data structures')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without actually moving files')
    parser.add_argument('--log', type=str, default='cleanup.log', help='Log file location')
    
    args = parser.parse_args()
    
    setup_logger(args.log)
    scan_and_quarantine(args.root, args.quarantine, args.dry_run)
