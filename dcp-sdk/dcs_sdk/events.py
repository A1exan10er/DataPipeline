"""Event publishing and consuming SDK for DCS processes."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import aiormq
from pydantic import BaseModel, Field

from dcs_sdk.config import get_section

logger = logging.getLogger(__name__)


class BaseEvent(BaseModel):
    event: str
    version: int = 1
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    try_count: int = 0
    payload: dict[str, Any]


def get_rabbitmq_url() -> str:
    return str(get_section("rabbitmq").get("url", ""))


def get_exchange_name() -> str:
    return str(get_section("rabbitmq").get("exchange", "event_center.events"))


class EventClient:
    _instance: Optional["EventClient"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_inited"):
            return

        self._inited = True
        self._bus = None
        self._connected = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("EventClient background loop started")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self):
        if self._connected:
            return

        # Lazy import avoids a package init cycle with the compatibility
        # wrappers under event_center.
        from event_center.bus.event_bus import EventBus

        self._bus = EventBus(
            url=get_rabbitmq_url(),
            exchange_name=get_exchange_name(),
        )
        await self._bus.connect()
        self._connected = True
        logger.info("EventClient connected")

    async def _emit_msg(
        self,
        event: str,
        payload: dict[str, Any],
        context: Optional[dict[str, Any]] = None,
    ):
        if not self._connected:
            await self._connect()

        full_payload = payload.copy()
        if context:
            full_payload["_context"] = context

        event_obj = BaseEvent(event=event, payload=full_payload)

        try:
            await self._bus.publish(event_obj)
            logger.info("[PUBLISH-OK] %s", event)
        except aiormq.exceptions.ChannelInvalidStateError as e:
            logger.warning("[RECONNECT] %s", e)
            self._connected = False
            await self._connect()
            await self._bus.publish(event_obj)
        except Exception:
            logger.exception("[PUBLISH-FAIL] %s", event)
            raise

    def emit(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        context: Optional[dict[str, Any]] = None,
        wait: bool = False,
    ):
        future = asyncio.run_coroutine_threadsafe(
            self._emit_msg(event, payload, context),
            self._loop,
        )
        if wait:
            return future.result()
        return future

    def publish(self, event: str, payload: dict[str, Any], **kwargs):
        return self.emit(event, payload, **kwargs)


_event_client: Optional[EventClient] = None


def get_event_client() -> EventClient:
    global _event_client
    if _event_client is None:
        _event_client = EventClient()
    return _event_client


def emit_event(
    event: str,
    payload: dict[str, Any],
    *,
    context: Optional[dict[str, Any]] = None,
    wait: bool = False,
):
    return get_event_client().emit(event, payload, context=context, wait=wait)


async def subscribe_events(
    queue_name: str,
    routing_keys: list[str],
    handler: Callable[[BaseEvent], Awaitable[None]],
    *,
    max_retries: int = 10,
    retry_delay_ms: int = 30000,
):
    """Subscribe to events from the message bus.

    Creates a durable named queue bound to the given routing keys on the
    TOPIC exchange.  Because the exchange is ``topic``, every independent
    queue that matches a routing key receives its own copy of the message
    — other systems are never starved.

    Parameters
    ----------
    queue_name:
        Unique durable queue name for this consumer (e.g. ``"qms.events"``).
        Choose a name that identifies your system so messages are not
        competed away by other consumers.
    routing_keys:
        List of routing keys (event names or wildcard patterns like ``"#\"``
        for all messages).  Examples: ``["task.created", "task.status_changed"]``.
    handler:
        Async callback invoked with each :class:`BaseEvent`.
    max_retries:
        Max delivery attempts before the message is dead-lettered.
    retry_delay_ms:
        Delay in milliseconds between retries.

    Returns
    -------
    EventBus
        The connected bus instance.  Keep a reference to it and call
        ``await bus.close()`` on shutdown.

    Example
    -------
    .. code-block:: python

        from dcs_sdk.events import subscribe_events, BaseEvent

        async def on_task_created(event: BaseEvent):
            print(f"task created: {event.payload}")

        bus = await subscribe_events(
            "qms.consumer",
            ["task.created", "task.status_changed"],
            on_task_created,
        )
        # ... application runs ...
        await bus.close()
    """
    # Lazy import avoids a package init cycle.
    from event_center.bus.event_bus import EventBus

    bus = EventBus(
        url=get_rabbitmq_url(),
        exchange_name=get_exchange_name(),
    )
    await bus.connect()

    await bus.subscribe(
        handler,
        queue_name=queue_name,
        routing_keys=routing_keys,
        max_retries=max_retries,
        retry_delay_ms=retry_delay_ms,
    )

    logger.info(
        "subscribed queue=%s keys=%s", queue_name, routing_keys,
    )
    return bus
