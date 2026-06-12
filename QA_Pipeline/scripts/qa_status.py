"""Show live QA pipeline status without remembering the run directory."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main() -> int:
    args = _parse_args()
    output_dir = Path(args.output_dir)
    while True:
        run_dir = _resolve_run_dir(output_dir, args.run_id)
        if run_dir is None:
            print(f"No QA run found under {output_dir}.")
            return 1
        summary_path = run_dir / "live_summary.md"
        status_path = run_dir / "run_status.json"
        _clear_screen(args.no_clear)
        print(f"QA run: {run_dir}")
        print()
        if summary_path.is_file():
            print(summary_path.read_text(encoding="utf-8"))
        elif status_path.is_file():
            print(status_path.read_text(encoding="utf-8"))
        else:
            print("Run directory exists, but no live_summary.md or run_status.json is available yet.")
        if not args.watch:
            return 0
        time.sleep(args.interval)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show the latest QA pipeline live status.")
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Pipeline output directory. Defaults to outputs.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Specific run ID under <output-dir>/runs. Defaults to latest run.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Refresh continuously, similar to watch.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Refresh interval in seconds for --watch. Default: 2.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear the terminal between watch refreshes.",
    )
    return parser.parse_args()


def _resolve_run_dir(output_dir: Path, run_id: str | None) -> Path | None:
    if run_id:
        run_dir = output_dir / "runs" / run_id
        return run_dir if run_dir.is_dir() else None
    pointer = output_dir / "latest_run.txt"
    if pointer.is_file():
        run_dir = Path(pointer.read_text(encoding="utf-8").strip())
        if run_dir.is_dir():
            return run_dir
    runs_dir = output_dir / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = [path for path in runs_dir.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _clear_screen(no_clear: bool) -> None:
    if no_clear:
        return
    if sys.stdout.isatty():
        os.system("clear")


if __name__ == "__main__":
    raise SystemExit(main())
