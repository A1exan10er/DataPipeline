"""
Demo for dcs-sdk consumption patterns.

Run:
    conda activate datacollector
    set DCS_CONFIG_FILE=D:/code/dcp/dcs_config.json   (Windows)
    python demo.py [mode]
"""

import argparse
import asyncio
import time
from urllib.parse import urlparse

from dcs_sdk import (
    EPISODE_VERIFIED,
    BaseEvent,
    EventListener,
    emit_event,
    listen_event,
)
from dcs_sdk.events import get_exchange_name, get_rabbitmq_url

PLATFORM_EPISODE_VERIFIED = "platform.episode.verified"
VERIFIED_ROUTING_KEYS = [EPISODE_VERIFIED, PLATFORM_EPISODE_VERIFIED]


def on_episode_verified_sync(event: BaseEvent):
    """Simple sync handler."""
    payload = event.payload or {}
    print(f"\n[sync handler] event: {event.event}")
    print(f"  event_id     : {event.event_id}")
    print(f"  record_id    : {payload.get('record_id')}")
    print(f"  session_id   : {payload.get('session_id')}")
    print(f"  verified_path: {payload.get('verified_path')}")
    print(f"  payload      : {payload}")


async def on_episode_verified_async(event: BaseEvent):
    """Async handler sample."""
    payload = event.payload or {}
    print(f"\n[async handler] event: {event.event}")
    print(f"  event_id   : {event.event_id}")
    print(f"  record_id  : {payload.get('record_id')}")

    await asyncio.sleep(0.1)
    print(f"  [done] session_id={payload.get('session_id')}")


def _print_connection_context():
    """Print MQ endpoint used by this process, with secret masked."""
    url = get_rabbitmq_url()
    exchange = get_exchange_name()
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        masked_host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        user = parsed.username or ""
        vhost = parsed.path or "/"
        masked_url = f"{parsed.scheme}://{user}:***@{masked_host}{port}{vhost}"
    else:
        masked_url = url or "<empty>"

    print(f"MQ URL    : {masked_url}")
    print(f"Exchange  : {exchange}")


def _run_until_keyboard_interrupt():
    """Block until user hits Ctrl+C."""
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")


def demo_listen_event():
    """
    Method 1 (recommended for multi-system): anonymous queue, listen both keys.

    No queue_name is passed, so every process gets its own auto-delete queue.
    Different services can run this independently without queue-name collision.
    """
    print("=" * 64)
    print("Method 1: EventListener with two routing keys (anonymous, multi-system-safe)")
    print("=" * 64)

    listener = EventListener(
        routing_keys=VERIFIED_ROUTING_KEYS,
        handler=on_episode_verified_sync,
        max_retries=10,
        retry_delay_ms=15_000,
    )
    listener.start()
    print(f"Subscribed keys={VERIFIED_ROUTING_KEYS}")
    _print_connection_context()
    print("Waiting for messages... (Ctrl+C to stop)\n")
    try:
        _run_until_keyboard_interrupt()
    finally:
        listener.stop()
        print("Listener stopped")


def demo_listen_event_named():
    """
    Method 1.1: named queue.

    Use this only when you explicitly want a durable queue for restart recovery.
    """
    print("=" * 64)
    print("Method 1.1: listen_event() with named queue")
    print("=" * 64)

    listener = listen_event(
        EPISODE_VERIFIED,
        on_episode_verified_sync,
        queue_name="demo.episode_verified",
        max_retries=10,
        retry_delay_ms=15_000,
    )
    print("Subscribed queue=demo.episode_verified")
    _print_connection_context()
    print("Waiting for messages... (Ctrl+C to stop)\n")
    try:
        _run_until_keyboard_interrupt()
    finally:
        listener.stop()
        print("Listener stopped")


def demo_event_listener():
    """
    Method 2: EventListener with explicit named queue.
    """
    print("=" * 64)
    print("Method 2: EventListener with named queue")
    print("=" * 64)

    listener = EventListener(
        routing_keys=VERIFIED_ROUTING_KEYS,
        handler=on_episode_verified_async,
        queue_name="demo.episode_verified",
        max_retries=10,
        retry_delay_ms=15_000,
    )
    listener.start()
    print(f"Subscribed routing_keys={VERIFIED_ROUTING_KEYS}")
    _print_connection_context()
    print("Waiting for messages... (Ctrl+C to stop)\n")
    try:
        _run_until_keyboard_interrupt()
    finally:
        listener.stop()
        print("Listener stopped")


def demo_event_listener_anonymous():
    """
    Method 3: EventListener with anonymous queue.

    Same collision-safe behavior as anonymous listen_event, with async handler.
    """
    print("=" * 64)
    print("Method 3: EventListener without queue_name (anonymous)")
    print("=" * 64)

    listener = EventListener(
        routing_keys=VERIFIED_ROUTING_KEYS,
        handler=on_episode_verified_async,
        max_retries=10,
        retry_delay_ms=15_000,
    )
    listener.start()
    print(f"Subscribed routing_keys={VERIFIED_ROUTING_KEYS}")
    _print_connection_context()
    print("Waiting for messages... (Ctrl+C to stop)\n")
    try:
        _run_until_keyboard_interrupt()
    finally:
        listener.stop()
        print("Listener stopped")


def demo_send_and_receive():
    """Method 4: send an event and verify it is received by the listener."""
    print("=" * 64)
    print("Method 4: send + receive smoke test")
    print("=" * 64)

    received_events: list[BaseEvent] = []

    def handler(event: BaseEvent):
        received_events.append(event)

    listener = listen_event(EPISODE_VERIFIED, handler)
    _print_connection_context()
    print("Listener started. Sending one test event...")

    time.sleep(0.5)
    emit_event(
        EPISODE_VERIFIED,
        {
            "record_id": "demo-record-001",
            "session_id": "demo-session-20260625",
            "verified_path": "/nas/staging/demo/episode_001",
        },
        wait=True,
    )

    for _ in range(30):
        if received_events:
            break
        time.sleep(0.1)

    if received_events:
        event = received_events[0]
        print("\n[OK] message received")
        print(f"  event     : {event.event}")
        print(f"  event_id  : {event.event_id}")
        print(f"  payload   : {event.payload}")
    else:
        print("\n[FAIL] no message received (is RabbitMQ reachable?)")

    listener.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="dcs-sdk demo")
    parser.add_argument(
        "mode",
        nargs="?",
        default="listen",
        choices=[
            "listen",
            "listen-named",
            "class",
            "class-anon",
            "test",
        ],
        help=(
            "listen: anonymous EventListener for both episode_verified keys (default), "
            "listen-named: named listen_event, "
            "class: EventListener named queue, "
            "class-anon: EventListener anonymous queue, "
            "test: send + receive smoke test"
        ),
    )
    args = parser.parse_args()

    if args.mode == "listen-named":
        demo_listen_event_named()
    elif args.mode == "class":
        demo_event_listener()
    elif args.mode == "class-anon":
        demo_event_listener_anonymous()
    elif args.mode == "test":
        demo_send_and_receive()
    else:
        demo_listen_event()
