#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/umi-val-env}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: $PYTHON_BIN is not available on this system" >&2
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    cat >&2 <<'EOF'
error: failed to create the virtual environment
on Ubuntu, install the venv package first, for example:
  sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
EOF
    exit 1
  fi
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

if [[ ! -d "$ROOT_DIR/test_sample/test_sample" ]]; then
  echo "warning: local test_sample data is missing; benchmark will not have sample episodes to validate" >&2
fi

exec "$VENV_DIR/bin/python" "$ROOT_DIR/ik_benchmark.py" "$@"