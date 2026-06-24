"""Run an independent live dashboard updater for QA pipeline outputs."""

from __future__ import annotations

import argparse
import http.server
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from QA_Pipeline.scripts.generate_dashboard import generate_dashboard


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "dashboard.html"

    httpd = None
    if args.port:
        httpd = _start_http_server(output_dir, args.host, args.port)
        print(f"Serving dashboard: http://{args.host}:{args.port}/dashboard.html", flush=True)

    last_signature: tuple[int, int, int, int] | None = None
    try:
        while True:
            signature = _change_signature(db_path, output_dir)
            if args.force or signature != last_signature:
                try:
                    generate_dashboard(
                        db_path,
                        output_path,
                        args.interval,
                        max_episodes=_none_if_non_positive(args.max_episodes),
                        max_findings=_none_if_non_positive(args.max_findings),
                    )
                    last_signature = signature
                    print(f"Updated dashboard: {output_path}", flush=True)
                except Exception as exc:  # keep dashboard process alive during writer locks
                    print(f"Dashboard update skipped: {exc}", flush=True)
            if args.once:
                break
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("Stopping live dashboard.", flush=True)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update and optionally serve QA dashboard files without blocking the QA pipeline."
    )
    parser.add_argument("--db-path", required=True, help="QA pipeline SQLite DB path.")
    parser.add_argument("--output-dir", required=True, help="Directory containing dashboard.html/dashboard_data.json.")
    parser.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds. Default: 5.")
    parser.add_argument("--max-episodes", type=int, default=5000, help="Maximum episode detail rows. 0 means unlimited.")
    parser.add_argument("--max-findings", type=int, default=10000, help="Maximum finding detail rows. 0 means unlimited.")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host when --port is set. Default: 0.0.0.0.")
    parser.add_argument("--port", type=int, default=0, help="Serve output-dir on this port. 0 disables HTTP serving.")
    parser.add_argument("--once", action="store_true", help="Write dashboard once and exit.")
    parser.add_argument("--force", action="store_true", help="Regenerate every interval even if inputs look unchanged.")
    return parser.parse_args(argv)


def _start_http_server(output_dir: Path, host: str, port: int) -> socketserver.TCPServer:
    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(output_dir), **kwargs)
    httpd = socketserver.ThreadingTCPServer((host, port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _change_signature(db_path: Path, output_dir: Path) -> tuple[int, int, int, int]:
    db_stat = _safe_stat(db_path)
    run_status_stat = _safe_stat(_latest_run_file(output_dir, "run_status.json"))
    phase_status_stat = _safe_stat(_latest_run_file(output_dir, "phase_status.jsonl"))
    issue_events_stat = _safe_stat(_latest_run_file(output_dir, "issue_events.jsonl"))
    return (
        db_stat,
        run_status_stat,
        phase_status_stat,
        issue_events_stat,
    )


def _latest_run_file(output_dir: Path, name: str) -> Path:
    pointer = output_dir / "latest_run.txt"
    try:
        run_dir = Path(pointer.read_text(encoding="utf-8").strip())
    except OSError:
        return output_dir / "runs" / name
    return run_dir / name


def _safe_stat(path: Path) -> int:
    try:
        stat = path.stat()
    except OSError:
        return 0
    return int(stat.st_mtime_ns) ^ int(stat.st_size)


def _none_if_non_positive(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


if __name__ == "__main__":
    raise SystemExit(main())
