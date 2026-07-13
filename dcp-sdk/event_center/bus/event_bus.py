import hashlib
import json
from typing import Awaitable, Callable, Optional

import aio_pika

from event_center.events.base import BaseEvent


class EventBus:
    RETRY_COUNT_HEADER = "x-dcs-retry-count"
    RELIABLE_EPISODE_QUEUE = "collector.episode_finalize.workflow"
    RELIABLE_EPISODE_ROUTING_KEY = "collector.episode_finalize"
    PLATFORM_EPISODE_VERIFIED_ROUTING_KEY = "collector_platform.episode_verified"

    def __init__(self, url: str, exchange_name: str = "app.events"):
        self.url = url
        self.exchange_name = exchange_name
        self.connection: Optional[aio_pika.RobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self.exchange: Optional[aio_pika.Exchange] = None
        self.retry_exchange: Optional[aio_pika.Exchange] = None

    async def connect(self):
        self.connection = await aio_pika.connect_robust(self.url)
        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

        self.exchange = await self.channel.declare_exchange(
            self.exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self.retry_exchange = await self.channel.declare_exchange(
            f"{self.exchange_name}.retry",
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        await self._declare_reliable_queue(
            self.RELIABLE_EPISODE_QUEUE,
            [self.RELIABLE_EPISODE_ROUTING_KEY],
        )

        print("EventBus connected")

    async def close(self):
        if self.connection:
            await self.connection.close()
            print("EventBus closed")

    async def publish(self, event: BaseEvent):
        if not self.exchange:
            raise RuntimeError("EventBus not connected")

        if event.event == self.RELIABLE_EPISODE_ROUTING_KEY:
            await self._declare_reliable_queue(
                self.RELIABLE_EPISODE_QUEUE,
                [self.RELIABLE_EPISODE_ROUTING_KEY],
            )

        message = aio_pika.Message(
            body=event.model_dump_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )

        await self.exchange.publish(
            message,
            routing_key=event.event,
        )

        print(f"published event: {event.event}")

    async def _declare_reliable_queue(
        self,
        queue_name: str,
        routing_keys: list[str],
        *,
        retry_delay_ms: int = 30000,
    ) -> aio_pika.Queue:
        if not self.channel or not self.exchange or not self.retry_exchange:
            raise RuntimeError("EventBus not connected")

        queue = await self.channel.declare_queue(
            name=queue_name,
            durable=True,
            auto_delete=False,
        )

        retry_routing_key = self._retry_routing_key(queue_name)
        retry_return_routing_key = self._retry_return_routing_key(queue_name)

        retry_queue = await self.channel.declare_queue(
            name=f"{queue_name}.retry",
            durable=True,
            auto_delete=False,
            arguments={
                "x-message-ttl": retry_delay_ms,
                "x-dead-letter-exchange": self.retry_exchange.name,
                "x-dead-letter-routing-key": retry_return_routing_key,
            },
        )

        dlq = await self.channel.declare_queue(
            name=f"{queue_name}.dlq",
            durable=True,
            auto_delete=False,
        )

        for key in routing_keys:
            await queue.bind(self.exchange, routing_key=key)

        await retry_queue.bind(self.retry_exchange, routing_key=retry_routing_key)
        await queue.bind(self.retry_exchange, routing_key=retry_return_routing_key)
        await dlq.bind(
            self.retry_exchange,
            routing_key=self._dlq_routing_key(queue_name),
        )
        return queue

    @staticmethod
    def _queue_route_id(queue_name: str) -> str:
        return hashlib.sha256(queue_name.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _retry_routing_key(queue_name: str) -> str:
        return f"dcs.retry.{EventBus._queue_route_id(queue_name)}"

    @staticmethod
    def _retry_return_routing_key(queue_name: str) -> str:
        return f"dcs.retry.{EventBus._queue_route_id(queue_name)}.return"

    @staticmethod
    def _dlq_routing_key(queue_name: str) -> str:
        return f"dcs.retry.{EventBus._queue_route_id(queue_name)}.dlq"

    @staticmethod
    def _death_count(headers: dict | None, queue_name: str) -> int:
        deaths = (headers or {}).get("x-death") or []
        count = 0
        for item in deaths:
            if not isinstance(item, dict):
                continue
            if item.get("queue") == queue_name:
                try:
                    count += int(item.get("count", 0))
                except (TypeError, ValueError):
                    pass
        return count

    def _retry_count(self, headers: dict | None, queue_name: str) -> int:
        headers = headers or {}
        dead_letter_count = self._death_count(headers, queue_name)
        if self.RETRY_COUNT_HEADER not in headers:
            return dead_letter_count

        try:
            return max(int(headers[self.RETRY_COUNT_HEADER]), dead_letter_count)
        except (TypeError, ValueError):
            return dead_letter_count

    async def _publish_to_retry(
        self,
        message: aio_pika.IncomingMessage,
        queue_name: str,
        retry_count: int,
        reason: str,
    ) -> None:
        if not self.retry_exchange:
            raise RuntimeError("EventBus not connected")

        headers = dict(message.headers or {})
        headers[self.RETRY_COUNT_HEADER] = retry_count
        headers["retry_reason"] = reason

        await self.retry_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers=headers,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type=message.content_type,
                correlation_id=message.correlation_id,
            ),
            routing_key=self._retry_routing_key(queue_name),
        )

    async def _dead_letter_to_dlq(
        self,
        message: aio_pika.IncomingMessage,
        queue_name: str,
        reason: str,
    ) -> None:
        if not self.retry_exchange:
            raise RuntimeError("EventBus not connected")

        headers = dict(message.headers or {})
        headers["dlq_reason"] = reason

        await self.retry_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers=headers,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                content_type=message.content_type,
                correlation_id=message.correlation_id,
            ),
            routing_key=self._dlq_routing_key(queue_name),
        )

    async def subscribe(
        self,
        handler: Callable[[BaseEvent], Awaitable[None]],
        queue_name: Optional[str] = None,
        routing_keys: Optional[list[str]] = None,
        *,
        max_retries: int = 10,
        retry_delay_ms: int = 30000,
    ):
        if not self.channel or not self.exchange:
            raise RuntimeError("EventBus not connected")

        if not routing_keys:
            routing_keys = ["#"]

        if queue_name:
            queue = await self._declare_reliable_queue(
                queue_name,
                routing_keys,
                retry_delay_ms=retry_delay_ms,
            )
        else:
            queue = await self.channel.declare_queue(
                name="",
                durable=False,
                auto_delete=True,
            )
            for key in routing_keys:
                await queue.bind(self.exchange, routing_key=key)

        print(f"subscribed queue={queue.name}, keys={routing_keys}")

        async def _on_message(message: aio_pika.IncomingMessage):
            retry_count = self._retry_count(message.headers, queue.name)
            if queue_name and retry_count >= max_retries:
                await self._dead_letter_to_dlq(
                    message,
                    queue.name,
                    f"max retries exceeded ({retry_count}/{max_retries})",
                )
                await message.ack()
                print(f"moved to DLQ queue={queue.name} retries={retry_count}")
                return

            try:
                data = json.loads(message.body)
                event = BaseEvent(**data)
                await handler(event)
            except Exception as exc:
                print(f"message failed queue={queue.name} exc={exc}")
                if queue_name:
                    next_retry_count = retry_count + 1
                    if next_retry_count >= max_retries:
                        await self._dead_letter_to_dlq(
                            message,
                            queue.name,
                            f"max retries exceeded ({next_retry_count}/{max_retries})",
                        )
                        print(
                            f"moved to DLQ queue={queue.name} "
                            f"retries={next_retry_count}"
                        )
                    else:
                        await self._publish_to_retry(
                            message,
                            queue.name,
                            next_retry_count,
                            str(exc),
                        )
                        print(
                            f"sent to retry queue={queue.name} "
                            f"retries={next_retry_count}"
                        )
                    await message.ack()
                else:
                    await message.nack(requeue=True)
                return

            await message.ack()

        await queue.consume(_on_message)
