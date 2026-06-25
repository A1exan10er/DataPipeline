"""Integration test for EventListener / listen_event() — requires RabbitMQ.

Run:
    cd dcs-sdk
    pytest tests/test_event_listener.py -v

Skip when RabbitMQ is unreachable:
    pytest tests/test_event_listener.py -v -k "not mq"
"""

import time
import uuid

import pytest

from dcs_sdk.events import (
    BaseEvent,
    EventListener,
    emit_event,
    get_rabbitmq_url,
    listen_event,
)


def _unique_queue(label: str) -> str:
    return f"test.event_listener.{label}.{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_event_name() -> str:
    return f"test.event_listener.{uuid.uuid4().hex[:6]}"


@pytest.fixture
def test_payload() -> dict:
    return {"msg": "hello from EventListener test", "n": 99}


# ---------------------------------------------------------------------------
# Connectivity probe
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mq_reachable():
    """Return True when RabbitMQ is reachable, False otherwise."""
    import socket
    from urllib.parse import urlparse

    url = get_rabbitmq_url()
    if not url:
        return False

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5672

    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def require_mq(mq_reachable, request):
    if not mq_reachable:
        pytest.skip("RabbitMQ not reachable")


# ---------------------------------------------------------------------------
# Tests: EventListener (manual start/stop)
# ---------------------------------------------------------------------------

def test_event_listener_receives_message(test_event_name, test_payload):
    """EventListener with sync handler receives a published message."""
    received: list[BaseEvent] = []

    def handler(event: BaseEvent):
        received.append(event)

    listener = EventListener(
        routing_keys=[test_event_name],
        handler=handler,
        queue_name=_unique_queue("recv1"),
    )
    listener.start()

    try:
        # Give the subscription a moment to settle.
        time.sleep(0.5)

        emit_event(test_event_name, test_payload, wait=True)

        # Poll for delivery.
        for _ in range(30):
            if received:
                break
            time.sleep(0.1)

        assert len(received) == 1, f"Expected 1 event, got {len(received)}"
        ev = received[0]
        assert ev.event == test_event_name
        assert ev.payload["msg"] == "hello from EventListener test"
        assert ev.payload["n"] == 99
    finally:
        listener.stop()


def test_event_listener_only_matches_bound_keys(test_event_name):
    """Handler must only receive events whose routing key was bound."""
    received: list[BaseEvent] = []

    def handler(event: BaseEvent):
        received.append(event)

    listener = EventListener(
        routing_keys=[test_event_name],
        handler=handler,
        queue_name=_unique_queue("recv2"),
    )
    listener.start()

    try:
        time.sleep(0.5)

        # Publish an event with a DIFFERENT routing key.
        emit_event("test.some_other_event", {"x": 1}, wait=True)
        # Publish the matching event.
        emit_event(test_event_name, {"x": 2}, wait=True)

        for _ in range(30):
            if received:
                break
            time.sleep(0.1)

        assert len(received) == 1, (
            f"Expected only 1 matching event, got {len(received)}"
        )
        assert received[0].event == test_event_name
    finally:
        listener.stop()


def test_event_listener_stop_ignores_subsequent(test_event_name, test_payload):
    """After stop() the listener should not receive new messages."""
    received: list[BaseEvent] = []

    def handler(event: BaseEvent):
        received.append(event)

    listener = EventListener(
        routing_keys=[test_event_name],
        handler=handler,
        queue_name=_unique_queue("recv3"),
    )
    listener.start()
    time.sleep(0.5)

    # Send one message, verify it arrives, then stop.
    emit_event(test_event_name, test_payload, wait=True)

    for _ in range(30):
        if received:
            break
        time.sleep(0.1)

    assert len(received) == 1

    listener.stop()
    time.sleep(0.5)

    # Send another message — should NOT be received.
    emit_event(test_event_name, {"msg": "after stop"}, wait=True)
    time.sleep(1.0)

    assert len(received) == 1  # still only the first message


# ---------------------------------------------------------------------------
# Tests: listen_event() convenience function
# ---------------------------------------------------------------------------

def test_listen_event_convenience(test_event_name, test_payload):
    """listen_event() one-liner receives messages."""
    received: list[BaseEvent] = []

    def handler(event: BaseEvent):
        received.append(event)

    listener = listen_event(
        test_event_name,
        handler,
        queue_name=_unique_queue("conv1"),
    )

    try:
        time.sleep(0.5)
        emit_event(test_event_name, test_payload, wait=True)

        for _ in range(30):
            if received:
                break
            time.sleep(0.1)

        assert len(received) == 1
        assert received[0].payload == test_payload
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Tests: event constants
# ---------------------------------------------------------------------------

def test_event_constants_are_importable():
    """Verify well-known event constants exist and are usable."""
    from dcs_sdk.events import (
        EPISODE_FINALIZED,
        EPISODE_VERIFIED,
        TASK_CREATED,
        TASK_STATUS_CHANGED,
    )

    assert EPISODE_FINALIZED == "collector.episode_finalize"
    assert EPISODE_VERIFIED == "collector_platform.episode_verified"
    assert TASK_CREATED == "task.created"
    assert TASK_STATUS_CHANGED == "task.status_changed"


# ---------------------------------------------------------------------------
# Tests: EventListener double-start is safe
# ---------------------------------------------------------------------------

def test_event_listener_double_start_is_idempotent(test_event_name, test_payload):
    """Calling start() twice should not crash or create duplicates."""
    received: list[BaseEvent] = []

    def handler(event: BaseEvent):
        received.append(event)

    listener = EventListener(
        routing_keys=[test_event_name],
        handler=handler,
        queue_name=_unique_queue("recv4"),
    )
    listener.start()
    listener.start()  # second call should be a no-op
    listener.start()  # third too

    try:
        time.sleep(0.5)
        emit_event(test_event_name, test_payload, wait=True)

        for _ in range(30):
            if received:
                break
            time.sleep(0.1)

        assert len(received) == 1, (
            f"Expected exactly 1 event, got {len(received)} "
            "(duplicate subscription on double start?)"
        )
    finally:
        listener.stop()
