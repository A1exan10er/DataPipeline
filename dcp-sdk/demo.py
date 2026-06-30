"""
demo.py — 使用 EventListener / listen_event 监听采集结果验证完成事件

运行前确保 RabbitMQ 可达且 dcs_config.json 可被加载:
    conda activate datacollector
    set DCS_CONFIG_FILE=D:/code/dcp/dcs_config.json   (Windows)
    python demo.py
"""

import asyncio
import time

# 从 SDK 导入新 API
from dcs_sdk import (
    EventListener,
    listen_event,
    EPISODE_VERIFIED,
    BaseEvent,
    emit_event,
)


# ---------------------------------------------------------------------------
# 同步 handler（演示用）
# ---------------------------------------------------------------------------

def on_episode_verified_sync(event: BaseEvent):
    """同步回调 —— 收到采集验证完成事件时被调用。"""
    payload = event.payload or {}
    print(f"\n[sync handler] 事件: {event.event}")
    print(f"  event_id    : {event.event_id}")
    print(f"  record_id   : {payload.get('record_id')}")
    print(f"  session_id  : {payload.get('session_id')}")
    print(f"  verified_path: {payload.get('verified_path')}")
    print(f"  完整 payload: {payload}")


# ---------------------------------------------------------------------------
# 异步 handler（演示用）
# ---------------------------------------------------------------------------

async def on_episode_verified_async(event: BaseEvent):
    """异步回调 —— 可以做 await 操作（如写数据库、调 HTTP）。"""
    payload = event.payload or {}
    print(f"\n[async handler] 事件: {event.event}")
    print(f"  event_id    : {event.event_id}")
    print(f"  record_id   : {payload.get('record_id')}")

    # 模拟异步操作
    await asyncio.sleep(0.1)
    print(f"  [async 处理完成] session_id={payload.get('session_id')}")


# ---------------------------------------------------------------------------
# 方式一: listen_event() 便捷函数（推荐）
# ---------------------------------------------------------------------------

def demo_listen_event():
    """一键启动监听，自动在后台线程运行。"""
    print("=" * 60)
    print("方式一: listen_event() 便捷函数")
    print("=" * 60)

    listener = listen_event(EPISODE_VERIFIED, on_episode_verified_sync)
    print(f"已启动监听: {EPISODE_VERIFIED}")
    print("等待消息中... (按 Ctrl+C 退出)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        listener.stop()
        print("监听已停止")


# ---------------------------------------------------------------------------
# 方式二: EventListener 类（更灵活，支持多个 routing key）
# ---------------------------------------------------------------------------

def demo_event_listener():
    """手动创建 EventListener，支持多个事件和精细控制。"""
    print("=" * 60)
    print("方式二: EventListener 类（多事件 + 异步 handler）")
    print("=" * 60)

    listener = EventListener(
        routing_keys=[EPISODE_VERIFIED],
        handler=on_episode_verified_async,  # 异步 handler
        queue_name="demo.episode_verified",
    )
    listener.start()
    print(f"已启动监听 routing_keys={[EPISODE_VERIFIED]}")
    print("等待消息中... (按 Ctrl+C 退出)\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        listener.stop()
        print("监听已停止")


# ---------------------------------------------------------------------------
# 方式三: 模拟发送一条测试消息，验证监听器能收到
# ---------------------------------------------------------------------------

def demo_send_and_receive():
    """发送一条测试事件，验证监听器能收到。"""
    print("=" * 60)
    print("方式三: 发送测试消息验证")
    print("=" * 60)

    received_events = []

    def handler(event: BaseEvent):
        received_events.append(event)

    listener = listen_event(EPISODE_VERIFIED, handler)
    print("已启动监听，发送测试消息...")

    # 等待订阅就绪
    time.sleep(0.5)

    # 发送一条模拟事件
    emit_event(
        EPISODE_VERIFIED,
        {
            "record_id": "demo-record-001",
            "session_id": "demo-session-20260625",
            "verified_path": "/nas/staging/demo/episode_001",
        },
        wait=True,
    )

    # 等待投递
    for _ in range(30):
        if received_events:
            break
        time.sleep(0.1)

    if received_events:
        event = received_events[0]
        print(f"\n[OK] 收到事件!")
        print(f"  event       : {event.event}")
        print(f"  event_id    : {event.event_id}")
        print(f"  payload     : {event.payload}")
    else:
        print("\n[FAIL] 未收到事件（可能 RabbitMQ 未连接）")

    listener.stop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # python demo.py test  → 发送一条测试消息验证
        demo_send_and_receive()
    elif len(sys.argv) > 1 and sys.argv[1] == "class":
        # python demo.py class → 使用 EventListener 类
        demo_event_listener()
    else:
        # python demo.py → 使用 listen_event 便捷函数
        demo_listen_event()
