"""Export an existing QA SQLite database to a human-readable Excel workbook."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pipeline.qa_core import PipelineConfigurationError, export_excel_report


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        export_excel_report(Path(args.db_path), Path(args.output))
    except PipelineConfigurationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote Excel report: {args.output}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export QA pipeline results to Excel.")
    parser.add_argument("--db-path", required=True, help="QA pipeline SQLite database path.")
    parser.add_argument("--output", required=True, help="Excel .xlsx output path.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
