# dcs-sdk

DCS 事件总线 SDK，提供 RabbitMQ 消息的**发布**和**订阅**能力。

## 安装

```bash
# 本地可编辑安装（推荐开发时使用，源码修改即时生效）
pip install -e /path/to/dcs-sdk

# 或从 Git 仓库安装
pip install git+http://git-host/group/dcp-sdk.git
```

## 配置

在项目根目录创建 `dcs_config.json`：

```json
{
  "rabbitmq": {
    "url": "amqp://username:password@rabbitmq-host:5672/",
    "exchange": "event_center.events"
  }
}
```

也可以通过环境变量指定配置路径（Windows）：

```powershell
$env:DCS_CONFIG_FILE = "D:\code\dcp\dcs_config.json"
```

Linux / macOS：

```bash
export DCS_CONFIG_FILE=/path/to/dcs_config.json
```

## 使用

### 消费事件（推荐：同步风格）

```python
from dcs_sdk import listen_event, EPISODE_VERIFIED, BaseEvent

def on_episode_verified(event: BaseEvent):
    """收到采集验证完成事件"""
    print(f"record_id: {event.payload['record_id']}")
    print(f"session_id: {event.payload['session_id']}")

listener = listen_event(EPISODE_VERIFIED, on_episode_verified)
# ... 应用运行 ...
listener.stop()
```

handler 也支持异步函数：

```python
async def on_episode_verified(event: BaseEvent):
    await save_to_db(event.payload)

listener = listen_event(EPISODE_VERIFIED, on_episode_verified)
```

### 消费事件（多事件 + 精细控制）

```python
from dcs_sdk import EventListener, EPISODE_FINALIZED, EPISODE_VERIFIED, BaseEvent

def on_collection_event(event: BaseEvent):
    print(f"[{event.event}] {event.payload}")

listener = EventListener(
    routing_keys=[EPISODE_FINALIZED, EPISODE_VERIFIED],
    handler=on_collection_event,
    queue_name="my_system.events",  # 可选，不指定自动生成
)
listener.start()
# ...
listener.stop()
```

### 消费事件（旧版 async 方式，仍可使用）

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
from dcs_sdk import emit_event

emit_event("task.created", {"task_id": 1})
```

## 已知事件

| 常量 | 值 | 说明 |
|------|-----|------|
| `EPISODE_FINALIZED` | `collector.episode_finalize` | 采集 episode 已落盘，待后处理 |
| `EPISODE_VERIFIED` | `collector_platform.episode_verified` | 采集结果已验证入库 |
| `TASK_CREATED` | `task.created` | 审批任务已创建 |
| `TASK_STATUS_CHANGED` | `task.status_changed` | 审批任务状态变更 |

## API 对比

| | `listen_event()` | `EventListener` | `subscribe_events()` |
|------|------|------|------|
| handler 类型 | sync / async | sync / async | 仅 async |
| 启动方式 | 自动 | 手动 `.start()` | `await` |
| 多事件 | 单个 | `routing_keys: [...]` | `routing_keys: [...]` |
| 线程模型 | 后台守护线程 | 后台守护线程 | 调用方 event loop |

## 架构

TOPIC 交换机，不同队列名各自收到完整消息副本，互不抢占。发布和监听均通过 `event_center.events` exchange 进行。
