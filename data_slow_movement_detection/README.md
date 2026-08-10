# Data Slow Movement Detection

Utilities for detecting long slow-motion or hanging behavior in robot episode data.

This directory is maintained as part of the DataPipeline repository. It was
originally imported from
`https://github.com/oldyemeister/Data_Slow_movement_detection.git` at commit
`78a0b01`. The tools here are for standalone scans, NAS investigation, and
threshold calibration. The production Phase 7 entry point remains
`QA_Pipeline/scripts/pipeline/slow_motion.py`, which also attributes findings
to operator commands, equipment response, or insufficient evidence.

Main scripts:

- `scripts/analyze_nas_slow_movement.py`: analyze sampled episodes on Synology NAS via DSM FileStation API.
- `scripts/detect_slow_movement.py`: analyze local episode directories.

See `NAS_SLOW_ANALYSIS.md` and `SLOW_MOVEMENT_ANALYSIS.md` for usage and detection logic.

Raw episodes, NAS downloads, credentials, and generated analysis output must
not be committed.
