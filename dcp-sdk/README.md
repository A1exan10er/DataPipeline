# dcs-sdk

DCS 事件总线 SDK，提供 RabbitMQ 消息的**发布**和**订阅**能力。

## 安装

```bash
pip install git+http://git-host/group/dcp-sdk.git
```

## 配置

拷贝 `dcs_config.json.example` 为 `dcs_config.json`，放到项目根目录，然后修改 `rabbitmq.url`。

也可以通过环境变量指定：

```bash
export DCS_CONFIG_FILE=/path/to/dcs_config.json
```

## 使用

### 消费事件

```python
import asyncio
from dcs_sdk.events import subscribe_events, BaseEvent

async def on_episode_finalized(event: BaseEvent):
    print(f"收到: {event.event}, payload: {event.payload}")

async def main():
    bus = await subscribe_events(
        queue_name="my_system.events",
        routing_keys=["collector.episode_finalize"],
        handler=on_episode_finalized,
    )
    await asyncio.Event().wait()

asyncio.run(main())
```

### 发布事件

```python
from dcs_sdk.events import emit_event
emit_event("task.created", {"task_id": 1})
```

## 架构

TOPIC 交换机，不同队列名各自收到完整消息副本，互不抢占。
