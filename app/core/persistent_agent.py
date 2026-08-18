"""多代理持久化:子代理状态落盘,支持断点续跑。

对标 Claude Projects 的子任务持久化:
- 每个子代理创建时生成独立 session_id 和状态快照
- 状态包含:消息历史、工具调用记录、中间结果、checkpoint 指针
- 支持 resume(agent_id) 从断点继续执行
- 支持 list_agents / get_agent_status / cancel_agent
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from app.config import settings
from app.core.storage_backend import get_storage_backend


class AgentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SubAgentState:
    """子代理执行状态(可序列化到数据库)。"""
    agent_id: str = ""
    parent_run_id: str = ""
    session_id: str = ""
    role: str = ""                  # researcher/coder/...
    task: str = ""                  # 原始任务指令
    status: str = "queued"          # AgentStatus
    messages: list[dict] = field(default_factory=list)   # 消息历史
    tool_calls: list[dict] = field(default_factory=list) # 工具调用记录
    intermediate_results: list[dict] = field(default_factory=list)  # 中间结果
    current_step: int = 0           # 当前执行到第几步
    max_steps: int = 15             # 最大步数
    provider: str = ""              # LLM provider
    model: str = ""                 # LLM model
    checkpoint_node: str = ""       # checkpoint 中的节点名(断点续跑用)
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    result: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SubAgentState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class PersistentAgentStore:
    """子代理持久化存储:SQLite 记录子代理状态。"""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(
            settings.resolved_data_dir, "sub_agents.db"
        )
        self._backend = get_storage_backend(db_path=self._db_path)
        self._agents: dict[str, SubAgentState] = {}  # 内存缓存
        self._init_db()

    def _init_db(self) -> None:
        schema = {
            "sub_agents": [
                {"name": "agent_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "parent_run_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "session_id", "type": "TEXT", "constraints": ""},
                {"name": "role", "type": "TEXT", "constraints": ""},
                {"name": "task", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "status", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'queued'"},
                {"name": "messages", "type": "TEXT", "constraints": ""},
                {"name": "tool_calls", "type": "TEXT", "constraints": ""},
                {"name": "intermediate_results", "type": "TEXT", "constraints": ""},
                {"name": "current_step", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "max_steps", "type": "INTEGER", "constraints": "DEFAULT 15"},
                {"name": "provider", "type": "TEXT", "constraints": ""},
                {"name": "model", "type": "TEXT", "constraints": ""},
                {"name": "checkpoint_node", "type": "TEXT", "constraints": ""},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "started_at", "type": "REAL", "constraints": ""},
                {"name": "completed_at", "type": "REAL", "constraints": ""},
                {"name": "error", "type": "TEXT", "constraints": ""},
                {"name": "result", "type": "TEXT", "constraints": ""},
            ],
        }
        self._backend.init_db(schema)

    def create(
        self,
        parent_run_id: str,
        session_id: str,
        role: str,
        task: str,
        max_steps: int = 15,
        provider: str = "",
        model: str = "",
    ) -> SubAgentState:
        """创建子代理记录。"""
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        now = time.time()
        state = SubAgentState(
            agent_id=agent_id,
            parent_run_id=parent_run_id,
            session_id=session_id,
            role=role,
            task=task,
            max_steps=max_steps,
            provider=provider,
            model=model,
            created_at=now,
        )
        self._agents[agent_id] = state
        self._persist(state)
        return state

    def update(
        self,
        agent_id: str,
        **kwargs,
    ) -> Optional[SubAgentState]:
        """更新子代理状态。"""
        state = self._agents.get(agent_id)
        if state is None:
            return None
        for k, v in kwargs.items():
            if hasattr(state, k):
                setattr(state, k, v)
        self._persist(state)
        return state

    def get(self, agent_id: str) -> Optional[SubAgentState]:
        """获取子代理状态。"""
        return self._agents.get(agent_id)

    def list_active(self, parent_run_id: str = "") -> list[SubAgentState]:
        """列出活跃子代理。"""
        active_statuses = {"queued", "running", "paused"}
        results = []
        for state in self._agents.values():
            if state.status in active_statuses:
                if parent_run_id and state.parent_run_id != parent_run_id:
                    continue
                results.append(state)
        return results

    def list_by_session(self, session_id: str) -> list[SubAgentState]:
        """按会话列出所有子代理。"""
        return [s for s in self._agents.values() if s.session_id == session_id]

    def mark_running(self, agent_id: str) -> bool:
        """标记为运行中。"""
        return bool(self.update(agent_id, status="running", started_at=time.time()))

    def mark_done(self, agent_id: str, result: str = "") -> bool:
        """标记完成。"""
        return bool(self.update(
            agent_id, status="done", result=result, completed_at=time.time()
        ))

    def mark_failed(self, agent_id: str, error: str = "") -> bool:
        """标记失败。"""
        return bool(self.update(
            agent_id, status="failed", error=error, completed_at=time.time()
        ))

    def mark_paused(self, agent_id: str, checkpoint_node: str = "") -> bool:
        """标记暂停(保存断点)。"""
        return bool(self.update(
            agent_id, status="paused", checkpoint_node=checkpoint_node
        ))

    def mark_cancelled(self, agent_id: str) -> bool:
        """标记取消。"""
        return bool(self.update(
            agent_id, status="cancelled", completed_at=time.time()
        ))

    def add_message(self, agent_id: str, message: dict) -> None:
        """追加消息到历史。"""
        state = self._agents.get(agent_id)
        if state:
            state.messages.append(message)
            self._persist(state)

    def add_tool_call(self, agent_id: str, tool_call: dict) -> None:
        """追加工具调用记录。"""
        state = self._agents.get(agent_id)
        if state:
            state.tool_calls.append(tool_call)
            self._persist(state)

    def add_intermediate_result(self, agent_id: str, result: dict) -> None:
        """追加中间结果。"""
        state = self._agents.get(agent_id)
        if state:
            state.intermediate_results.append(result)
            self._persist(state)

    def increment_step(self, agent_id: str) -> int:
        """步数+1,返回新步数。"""
        state = self._agents.get(agent_id)
        if state:
            state.current_step += 1
            self._persist(state)
            return state.current_step
        return 0

    def _persist(self, state: SubAgentState) -> None:
        """持久化到数据库。"""
        self._backend.upsert(
            "sub_agents",
            {
                "agent_id": state.agent_id,
                "parent_run_id": state.parent_run_id,
                "session_id": state.session_id,
                "role": state.role,
                "task": state.task,
                "status": state.status,
                "messages": json.dumps(state.messages, ensure_ascii=False),
                "tool_calls": json.dumps(state.tool_calls, ensure_ascii=False),
                "intermediate_results": json.dumps(state.intermediate_results, ensure_ascii=False),
                "current_step": state.current_step,
                "max_steps": state.max_steps,
                "provider": state.provider,
                "model": state.model,
                "checkpoint_node": state.checkpoint_node,
                "created_at": state.created_at,
                "started_at": state.started_at,
                "completed_at": state.completed_at,
                "error": state.error,
                "result": state.result,
            },
            conflict_columns=["agent_id"],
        )

    def stats(self) -> dict:
        """代理池统计。"""
        statuses = {}
        for s in self._agents.values():
            statuses[s.status] = statuses.get(s.status, 0) + 1
        return {
            "total": len(self._agents),
            "by_status": statuses,
            "active": sum(1 for s in self._agents.values()
                         if s.status in ("queued", "running", "paused")),
        }


# 全局单例
_instance: Optional[PersistentAgentStore] = None


def get_agent_store() -> PersistentAgentStore:
    global _instance
    if _instance is None:
        _instance = PersistentAgentStore()
    return _instance
