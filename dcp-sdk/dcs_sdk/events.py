"""Event publishing and consuming SDK for DCS processes."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Generic, Literal, Optional, TypeVar

import aiormq
from pydantic import BaseModel, ConfigDict, Field

from dcs_sdk.config import get_section

logger = logging.getLogger(__name__)


QA_EPISODE_ABNORMAL_DETECTED = "qa.episode_abnormal.detected"
"""QA pipeline detected an abnormal episode that may require operator action."""


class QAEpisodeAbnormalPayload(BaseModel):
    """Payload schema for ``qa.episode_abnormal.detected`` messages."""

    model_config = ConfigDict(extra="forbid")

    event_schema: Literal["qa_episode_abnormal_detected.v1"] = "qa_episode_abnormal_detected.v1"
    event_key: str
    run_id: str
    finding_id: int
    episode_path: str
    episode_name: str
    task: str
    date: str
    operator: str
    robot: str
    controller: str
    collector_id: str
    phase: int
    check_name: str
    severity: str
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    detected_at: str
    source: Literal["qa_pipeline"] = "qa_pipeline"
    recommended_action: str


PayloadT = TypeVar("PayloadT")


class BaseEvent(BaseModel, Generic[PayloadT]):
    event: str
    version: int = 1
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    try_count: int = 0
    payload: PayloadT


class QAEpisodeAbnormalEvent(BaseEvent[QAEpisodeAbnormalPayload]):
    """Typed event envelope for QA abnormal episode notifications."""

    event: Literal["qa.episode_abnormal.detected"] = QA_EPISODE_ABNORMAL_DETECTED
    payload: QAEpisodeAbnormalPayload


def get_rabbitmq_url() -> str:
    return str(get_section("rabbitmq").get("url", ""))


def get_exchange_name() -> str:
    return str(get_section("rabbitmq").get("exchange", "event_center.events"))


# ---------------------------------------------------------------------------
# Well-known event routing keys
# ---------------------------------------------------------------------------

EPISODE_FINALIZED = "collector.episode_finalize"
"""采集 episode 已落盘，待后处理（转码 / 光流 / 上传）。"""

EPISODE_VERIFIED = "collector_platform.episode_verified"
"""采集结果已通过验证并入库。"""

TASK_CREATED = "task.created"
"""审批任务已创建。"""

TASK_STATUS_CHANGED = "task.status_changed"
"""审批任务状态变更。"""


def build_event(event: str, payload: dict[str, Any]) -> BaseEvent[Any]:
    if event == QA_EPISODE_ABNORMAL_DETECTED:
        return QAEpisodeAbnormalEvent(payload=QAEpisodeAbnormalPayload.model_validate(payload))
    return BaseEvent[dict[str, Any]](event=event, payload=payload)


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

        event_obj = build_event(event, full_payload)

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


class EventListener:
    """同步友好的事件监听器，在后台守护线程中运行 asyncio event loop。

    支持同步和异步 handler。handler 接收 :class:`BaseEvent` 对象，
    可通过 ``event.payload`` 访问消息字段。

    用法::

        from dcs_sdk.events import EventListener, EPISODE_VERIFIED

        def on_episode_verified(event):
            print(f"episode verified: {event.payload}")

        listener = EventListener(
            routing_keys=[EPISODE_VERIFIED],
            handler=on_episode_verified,
        )
        listener.start()
        # ... 应用运行 ...
        listener.stop()

    也可以使用便捷函数 :func:`listen_event` 一步完成::

        from dcs_sdk import listen_event, EPISODE_VERIFIED

        listener = listen_event(EPISODE_VERIFIED, on_episode_verified)
    """

    def __init__(
        self,
        routing_keys: list[str],
        handler: Callable[["BaseEvent"], Any],
        *,
        queue_name: Optional[str] = None,
        max_retries: int = 10,
        retry_delay_ms: int = 30000,
    ):
        self._routing_keys = list(routing_keys)
        self._handler = handler
        self._queue_name = queue_name or f"dcs_sdk.listener.{uuid.uuid4().hex[:8]}"
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._bus: Optional[Any] = None
        self._started = False

    def start(self) -> None:
        """启动后台监听线程。

        可重复调用 —— 已启动时直接返回，不会重复创建。
        """
        if self._started:
            return

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._started = True
        logger.info("EventListener started queue=%s keys=%s", self._queue_name, self._routing_keys)

    def stop(self, timeout: float = 5.0) -> None:
        """停止监听，关闭连接并回收线程。

        Parameters
        ----------
        timeout:
            等待后台线程退出的最大秒数。
        """
        if not self._started:
            return

        if self._loop is not None:
            # 在 event loop 中安排关闭
            async def _shutdown():
                if self._bus is not None:
                    await self._bus.close()
                    logger.info("EventListener bus closed queue=%s", self._queue_name)

            try:
                future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
                future.result(timeout=timeout)
            except Exception:
                logger.exception("EventListener shutdown error queue=%s", self._queue_name)

            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread is not None:
            self._thread.join(timeout=timeout)

        self._started = False
        logger.info("EventListener stopped queue=%s", self._queue_name)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._subscribe_and_run())
        # 处理完 _subscribe_and_run 返回后继续跑 loop 以完成 stop 中的回调
        if self._loop is not None:
            self._loop.run_forever()

    async def _subscribe_and_run(self) -> None:
        # Lazy import avoids a package init cycle.
        from event_center.bus.event_bus import EventBus

        self._bus = EventBus(
            url=get_rabbitmq_url(),
            exchange_name=get_exchange_name(),
        )
        await self._bus.connect()

        # 包装 handler：自动检测 async/sync
        async def _dispatch(raw_event: BaseEvent) -> None:
            try:
                if inspect.iscoroutinefunction(self._handler):
                    await self._handler(raw_event)
                else:
                    self._handler(raw_event)
            except Exception:
                logger.exception(
                    "EventListener handler error queue=%s event=%s",
                    self._queue_name,
                    raw_event.event,
                )

        await self._bus.subscribe(
            _dispatch,
            queue_name=self._queue_name,
            routing_keys=self._routing_keys,
            max_retries=self._max_retries,
            retry_delay_ms=self._retry_delay_ms,
        )

        logger.info(
            "EventListener subscribed queue=%s keys=%s",
            self._queue_name,
            self._routing_keys,
        )


def listen_event(
    event_name: str,
    handler: Callable[["BaseEvent"], Any],
    *,
    queue_name: Optional[str] = None,
    max_retries: int = 10,
    retry_delay_ms: int = 30000,
) -> EventListener:
    """监听单个事件，收到消息后自动调用 ``handler``。

    ``handler`` 接收一个 :class:`BaseEvent` 对象，从中可获取
    ``event.payload``（消息字段）、``event.event``（事件名）等。
    ``handler`` 可以是同步或异步函数。

    返回 :class:`EventListener`，可调用 ``.stop()`` 停止监听。

    用法::

        from dcs_sdk import listen_event, EPISODE_VERIFIED

        def on_episode_verified(event):
            print(f"record_id: {event.payload['record_id']}")

        listener = listen_event(EPISODE_VERIFIED, on_episode_verified)
        # ... 应用运行 ...
        listener.stop()
    """
    listener = EventListener(
        routing_keys=[event_name],
        handler=handler,
        queue_name=queue_name,
        max_retries=max_retries,
        retry_delay_ms=retry_delay_ms,
    )
    listener.start()
    return listener


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
