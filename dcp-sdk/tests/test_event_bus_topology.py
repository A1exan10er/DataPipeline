import asyncio
import json
from types import SimpleNamespace

import aio_pika

from event_center.bus.event_bus import EventBus


class FakeExchange:
    def __init__(self, name: str):
        self.name = name
        self.published = []

    async def publish(self, message, routing_key: str):
        self.published.append((routing_key, message))


class FakeQueue:
    def __init__(
        self,
        name: str,
        *,
        durable: bool,
        auto_delete: bool,
        arguments: dict | None,
    ):
        self.name = name
        self.durable = durable
        self.auto_delete = auto_delete
        self.arguments = arguments or {}
        self.bindings = []
        self.consumer = None

    async def bind(self, exchange, routing_key: str):
        self.bindings.append((exchange.name, routing_key))

    async def consume(self, callback):
        self.consumer = callback


class FakeChannel:
    def __init__(self):
        self.queues = {}

    async def declare_queue(
        self,
        name: str,
        *,
        durable: bool,
        auto_delete: bool,
        arguments: dict | None = None,
    ):
        queue = FakeQueue(
            name,
            durable=durable,
            auto_delete=auto_delete,
            arguments=arguments,
        )
        self.queues[name] = queue
        return queue


class FakeIncomingMessage:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = headers
        self.content_type = "application/json"
        self.correlation_id = "test-correlation"
        self.acked = False
        self.nacked = False
        self.rejected = False

    async def ack(self):
        self.acked = True

    async def nack(self, requeue: bool = True):
        self.nacked = requeue

    async def reject(self, requeue: bool = False):
        self.rejected = requeue


def _fake_bus() -> tuple[EventBus, FakeChannel, FakeExchange, FakeExchange]:
    bus = EventBus("amqp://example")
    channel = FakeChannel()
    exchange = FakeExchange("event_center.events")
    retry_exchange = FakeExchange("event_center.events.retry")
    bus.channel = channel
    bus.exchange = exchange
    bus.retry_exchange = retry_exchange
    return bus, channel, exchange, retry_exchange


def test_reliable_queue_uses_private_retry_routes():
    bus, channel, _, _ = _fake_bus()
    queue_name = "consumer.events"
    routing_key = "collector_platform.episode_verified"

    asyncio.run(
        bus._declare_reliable_queue(
            queue_name,
            [routing_key],
            retry_delay_ms=12345,
        )
    )

    queue = channel.queues[queue_name]
    retry_queue = channel.queues[f"{queue_name}.retry"]
    dlq = channel.queues[f"{queue_name}.dlq"]
    retry_routing_key = EventBus._retry_routing_key(queue_name)
    retry_return_routing_key = EventBus._retry_return_routing_key(queue_name)

    assert "x-dead-letter-exchange" not in queue.arguments
    assert ("event_center.events", routing_key) in queue.bindings
    assert ("event_center.events.retry", retry_return_routing_key) in queue.bindings

    assert retry_queue.arguments == {
        "x-message-ttl": 12345,
        "x-dead-letter-exchange": "event_center.events.retry",
        "x-dead-letter-routing-key": retry_return_routing_key,
    }
    assert retry_queue.bindings == [("event_center.events.retry", retry_routing_key)]
    assert ("event_center.events.retry", routing_key) not in retry_queue.bindings

    assert dlq.bindings == [
        ("event_center.events.retry", EventBus._dlq_routing_key(queue_name))
    ]


def test_retry_count_uses_header_and_legacy_x_death_fallback():
    bus = EventBus("amqp://example")

    assert bus._retry_count(
        {
            "x-death": [
                {"queue": "consumer.events", "count": 3},
                {"queue": "consumer.events.retry", "count": 8},
            ],
        },
        "consumer.events",
    ) == 3

    assert bus._retry_count(
        {
            EventBus.RETRY_COUNT_HEADER: "2",
            "x-death": [{"queue": "consumer.events", "count": 3}],
        },
        "consumer.events",
    ) == 3


def test_handler_failure_publishes_to_private_retry_and_acks_original():
    async def run():
        bus, channel, _, retry_exchange = _fake_bus()

        async def handler(_event):
            raise RuntimeError("boom")

        await bus.subscribe(
            handler,
            queue_name="consumer.events",
            routing_keys=["collector_platform.episode_verified"],
            max_retries=3,
            retry_delay_ms=1000,
        )

        payload = {
            "event": "collector_platform.episode_verified",
            "payload": {"id": 1},
        }
        message = FakeIncomingMessage(json.dumps(payload).encode("utf-8"))

        await channel.queues["consumer.events"].consumer(message)

        assert message.acked is True
        assert message.nacked is False
        assert message.rejected is False
        assert len(retry_exchange.published) == 1

        routing_key, retry_message = retry_exchange.published[0]
        assert routing_key == EventBus._retry_routing_key("consumer.events")
        assert retry_message.headers[EventBus.RETRY_COUNT_HEADER] == 1
        assert retry_message.headers["retry_reason"] == "boom"
        assert retry_message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT

    asyncio.run(run())


def test_publish_to_retry_preserves_headers_and_uses_private_route():
    async def run():
        bus, _, _, retry_exchange = _fake_bus()
        message = SimpleNamespace(
            body=b"{}",
            headers={"source": "unit-test"},
            content_type="application/json",
            correlation_id="cid-1",
        )

        await bus._publish_to_retry(message, "consumer.events", 2, "failed")

        routing_key, retry_message = retry_exchange.published[0]
        assert routing_key == EventBus._retry_routing_key("consumer.events")
        assert retry_message.headers["source"] == "unit-test"
        assert retry_message.headers[EventBus.RETRY_COUNT_HEADER] == 2
        assert retry_message.headers["retry_reason"] == "failed"

    asyncio.run(run())
