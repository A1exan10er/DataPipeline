#!/bin/bash
# Wrapper to execute the Data Cleanup script.
# This script is ideal for cron job scheduling.

# Resolve the directory where this bash script is located so it can be run from anywhere
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Activate virtual environment if necessary (uncomment and adjust path if using one on the server)
# source "$DIR/venv/bin/activate"

echo "=====================================" >> "$DIR/cleanup_cron.log"
echo "Starting cleanup job at $(date)" >> "$DIR/cleanup_cron.log"

# You can modify the --root and --quarantine parameters when moving this to your central Server / NAS.
# e.g. --root "/mnt/nas_storage/DataPipeline" --quarantine "/mnt/nas_storage/DataPipeline_Quarantine"
python3 "$DIR/clean_invalid_episodes.py" \
    --root "$DIR" \
    --quarantine "$DIR/quarantine_data" \
    --log "$DIR/cleanup_python.log" >> "$DIR/cleanup_cron.log" 2>&1

echo "Completed cleanup job at $(date)" >> "$DIR/cleanup_cron.log"
