#!/usr/bin/env python3
"""Read-only production health check for the verified-episode listener."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def dashboard_listener_status(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.load(response)
    status = payload.get("event_listener", payload)
    if not isinstance(status, dict):
        raise RuntimeError("dashboard 返回的 listener 状态格式无效")
    return status


async def rabbitmq_queue_status(dc_root: Path, queue_name: str) -> dict[str, int]:
    sys.path.insert(0, str(dc_root))
    import aio_pika
    from dcs_sdk.config import get_section

    rabbitmq = get_section("rabbitmq")
    url = str(rabbitmq.get("url") or "").strip()
    if not url:
        raise RuntimeError("DCS 配置中缺少 rabbitmq.url")
    connection = await aio_pika.connect_robust(url, timeout=10)
    try:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, passive=True)
        declaration = queue.declaration_result
        return {
            "messages": int(declaration.message_count),
            "consumers": int(declaration.consumer_count),
        }
    finally:
        await connection.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查实时 episode listener 和 RabbitMQ consumer。")
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:4131/api/event-listener/status",
    )
    parser.add_argument("--queue-name", default="qa_pipeline.listen_episode_verified.v2")
    parser.add_argument(
        "--dc-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "dcp-sdk",
    )
    parser.add_argument("--timeout-seconds", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    deadline = time.monotonic() + max(1, args.timeout_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            listener = dashboard_listener_status(args.api_url)
            queue = asyncio.run(rabbitmq_queue_status(args.dc_root, args.queue_name))
            if listener.get("running") and queue["consumers"] >= 1:
                print(
                    json.dumps(
                        {"ok": True, "listener": listener, "queue": queue},
                        ensure_ascii=False,
                    )
                )
                return 0
            last_error = (
                f"listener_running={listener.get('running')}, "
                f"queue_consumers={queue['consumers']}"
            )
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    print(f"实时检测服务健康检查失败：{last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
