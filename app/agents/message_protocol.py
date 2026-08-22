"""AgentMessage: 多智能体间结构化消息协议。

对标 AutoGen / CrewAI 的 Agent-to-Agent 通信:
- 每条消息携带 sender/receiver/task_id/status 元数据
- 支持 task 级并发委派,父 Agent 等待所有子任务完成
- 支持子 Agent 之间横向消息传递

使用方式:
    msg = AgentMessage(
        sender="main", receiver="coder",
        task_id="task_abc",
        content="请实现这个功能...",
        expect_schema='{"result": "str"}'
    )
    result = await msg.wait(timeout=300)
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class AgentStatus(str, Enum):
    """子任务状态。"""
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class AgentMessage:
    """Agent 间结构化消息。"""
    sender: str = ""
    receiver: str = ""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    content: str = ""
    expect_schema: str = ""  # JSON Schema 字符串,非空时要求结构化输出
    status: AgentStatus = AgentStatus.PENDING
    result: Optional[str] = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "task_id": self.task_id,
            "content": self.content,
            "expect_schema": self.expect_schema,
            "status": self.status.value if isinstance(self.status, AgentStatus) else self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentMessage":
        m = cls(
            sender=data.get("sender", ""),
            receiver=data.get("receiver", ""),
            task_id=data.get("task_id", ""),
            content=data.get("content", ""),
            expect_schema=data.get("expect_schema", ""),
            status=AgentStatus(data.get("status", "pending")),
            result=data.get("result"),
            error=data.get("error", ""),
            created_at=data.get("created_at", time.time()),
            completed_at=data.get("completed_at", 0.0),
            metadata=data.get("metadata", {}) or {},
        )
        return m

    async def wait(self, timeout: float = 300.0) -> "AgentMessage":
        """阻塞等待任务完成(由外部任务队列填充 result)。"""
        from app.agents.message_protocol import get_task_queue
        queue = get_task_queue()
        return await queue.wait_for(self.task_id, timeout=timeout)

    def mark_done(self, result: str = "", error: str = "") -> None:
        self.result = result
        self.error = error
        self.status = AgentStatus.FAILED if error else AgentStatus.DONE
        self.completed_at = time.time()


class AgentMessageQueue:
    """Agent 间消息队列:支持 task_id 等待和多 receiver 广播。"""

    def __init__(self):
        self._messages: dict[str, AgentMessage] = {}  # task_id -> message
        self._locks: dict[str, asyncio.Event] = {}  # task_id -> event
        self._broadcasts: list[dict] = []  # 广播历史(供审计)

    def send(self, message: AgentMessage) -> AgentMessage:
        """发送消息到队列,返回引用。"""
        self._messages[message.task_id] = message
        if message.status == AgentStatus.PENDING:
            self._locks[message.task_id] = asyncio.Event()
        return message

    def complete(self, task_id: str, result: str = "", error: str = "") -> None:
        """标记任务完成,唤醒等待者。"""
        msg = self._messages.get(task_id)
        if msg is None:
            return
        msg.mark_done(result=result, error=error)
        evt = self._locks.get(task_id)
        if evt is not None:
            evt.set()

    async def wait_for(
        self, task_id: str, timeout: float = 300.0,
    ) -> AgentMessage:
        """阻塞等待指定 task_id 完成。"""
        msg = self._messages.get(task_id)
        if msg is None:
            raise KeyError(f"未找到任务: {task_id}")
        if msg.status in (AgentStatus.DONE, AgentStatus.FAILED):
            return msg
        evt = self._locks.get(task_id)
        if evt is None:
            return msg
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            msg.status = AgentStatus.TIMEOUT
            msg.error = f"任务 {task_id} 等待超时({timeout}s)"
            return msg
        return self._messages.get(task_id, msg)

    def get(self, task_id: str) -> Optional[AgentMessage]:
        return self._messages.get(task_id)

    def list_pending(self) -> list[AgentMessage]:
        return [m for m in self._messages.values() if m.status == AgentStatus.PENDING]

    def list_all(self) -> list[AgentMessage]:
        return list(self._messages.values())

    def clear_done(self) -> int:
        """清理已完成的任务,返回清理数。"""
        to_remove = [tid for tid, m in self._messages.items()
                     if m.status in (AgentStatus.DONE, AgentStatus.FAILED, AgentStatus.TIMEOUT)]
        for tid in to_remove:
            self._messages.pop(tid, None)
            self._locks.pop(tid, None)
        return len(to_remove)


# 模块级单例
_message_queue: Optional[AgentMessageQueue] = None
_message_queue_lock = threading.Lock()


def get_task_queue() -> AgentMessageQueue:
    global _message_queue
    if _message_queue is None:
        with _message_queue_lock:
            if _message_queue is None:
                _message_queue = AgentMessageQueue()
    return _message_queue


def reset_message_queue() -> None:
    global _message_queue
    with _message_queue_lock:
        _message_queue = None
