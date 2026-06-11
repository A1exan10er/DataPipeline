# UMI Data Validation

This repository contains the inverse-kinematics benchmark for validating UMI `eef_pose` streams.

## What is in this repo

- `ik_benchmark.py`: loads UMI pose streams and runs inverse-kinematics validation.
- `requirements.txt`: dependency list used to recreate the Python environment.
- `deploy_ubuntu.sh`: one-command setup and run script for Ubuntu.
- `test_sample/`: local-only UMI sample recordings for validation. This folder stays ignored by git.

## Local-only data

The `test_sample/` folder is intentionally ignored by git and should remain local.

## Deploy on a new Ubuntu PC

1. Clone the repository.
2. Copy the local `test_sample/` folder into the same relative path if you want to reproduce the sample validation results.
3. Run the one-command deployment script.

```bash
git clone <your-github-repo-url>
cd UMI_Data_Validation
bash deploy_ubuntu.sh
```

The script creates `umi-val-env` if needed, installs the pinned dependencies, and runs the benchmark.

## Expected behavior

- The benchmark reads `observation.state.eef_pose/data_raw.csv` when available, otherwise falls back to `data.csv`.
- The current robot pool validates the right stream against `Universal_Robots_UR5e`.
- If the `test_sample/` folder is absent on a new machine, the benchmark script will not have data to validate.
- To use a different dataset, keep the same `test_sample/test_sample/episode_*` structure or pass a different `--sample-root` value.

## GitHub workflow

When you are ready to publish only the code and deployment instructions, add just these files:

```bash
git add .gitignore README.md requirements.txt deploy_ubuntu.sh ik_benchmark.py
```

Keep `test_sample/` and `umi-val-env/` untracked so local recordings and machine-specific environment files do not go to GitHub.
