"""动态多代理协作引擎:运行时创建临时专业代理,按需协作完成任务。

复用 Agent.run() 作为执行引擎,不重复实现 ReAct 循环。
提供并发控制(Semaphore)和生命周期管理。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Optional

from app.agents.roles import AgentRole as _RoleDef, get_role_registry
from app.config import settings
from app.core.llm import LLMClient

logger = logging.getLogger(__name__)


class DynamicAgentPool:
    """动态代理池:管理和调度运行时创建的子代理。

    与 handoff_tool.py 的区别:
    - handoff_to_role: 主 Agent 在 ReAct 循环中委派,使用完整 Agent 生命周期
    - DynamicAgentPool: 外部 API 直接创建,轻量执行,支持并发控制
    """

    def __init__(self, max_concurrent: int = 5):
        self._agents: dict[str, dict] = {}  # agent_id -> status info
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def create_and_run(
        self,
        role_name: str,
        task: str,
        parent_session_id: str = "",
        custom_tools: Optional[list[str]] = None,
        timeout: int = 0,
    ) -> dict:
        """创建并运行一个动态子代理。"""
        role = self._resolve_role(role_name, task, custom_tools)
        if role is None:
            return {"error": f"未知角色: {role_name}"}

        agent_id = f"{role_name}_{uuid.uuid4().hex[:8]}"
        self._agents[agent_id] = {"role": role_name, "status": "running"}
        timeout = timeout or 300

        async with self._semaphore:
            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    self._execute(role, task, parent_session_id),
                    timeout=timeout,
                )
                result["agent_id"] = agent_id
                result["role"] = role_name
                result["duration_s"] = round(time.time() - t0, 2)
                self._agents[agent_id] = result
                return result
            except asyncio.TimeoutError:
                result = {
                    "agent_id": agent_id,
                    "role": role_name,
                    "status": "timeout",
                    "error": f"代理执行超时({timeout}s)",
                    "duration_s": round(time.time() - t0, 2),
                }
                self._agents[agent_id] = result
                return result
            except Exception as e:
                result = {
                    "agent_id": agent_id,
                    "role": role_name,
                    "status": "failed",
                    "error": str(e),
                    "duration_s": round(time.time() - t0, 2),
                }
                self._agents[agent_id] = result
                return result
            finally:
                self._cleanup_done(agent_id)

    async def _execute(
        self, role: _RoleDef, task: str, parent_session_id: str,
    ) -> dict:
        """使用 Agent.run() 执行任务。"""
        from app.core.agent import Agent

        # 初始化模型
        if role.model:
            provider, _, model = role.model.partition(":")
            llm = LLMClient(provider or None, model or None)
        else:
            llm = LLMClient()

        agent = Agent(
            llm=llm,
            max_steps=role.max_steps,
            system_prompt=role.system_prompt,
            session_id=parent_session_id,
            temperature=0.4,
        )

        # 工具白名单
        if role.available_tools:
            agent._active_tools = list(role.available_tools)
            agent._schemas_cache = None

        # 收紧上下文预算
        agent.context_budget = settings.agent_subagent_context_budget
        agent.token_budget = settings.agent_subagent_token_budget

        result = await agent.run(task, history=[])
        tool_steps = [s for s in result.steps if s.get("type") == "tool"]

        return {
            "status": "ok" if result.answer else "failed",
            "answer": result.answer or "",
            "tool_calls": len(tool_steps),
            "tools_used": list(dict.fromkeys(
                s.get("name", "") for s in tool_steps
            )),
            "model": result.model,
        }

    def _resolve_role(
        self, role_name: str, task: str, custom_tools: Optional[list[str]],
    ) -> Optional[_RoleDef]:
        """解析角色:优先从 registry,其次尝试自定义。"""
        reg = get_role_registry()
        role = reg.team.get(role_name)
        if role is not None:
            if custom_tools:
                role = _RoleDef(
                    name=role.name,
                    system_prompt=role.system_prompt,
                    description=role.description,
                    tools=role.tools,
                    model=role.model,
                    max_steps=role.max_steps,
                    available_tools=custom_tools,
                )
            return role

        return _RoleDef(
            name=role_name,
            description=role_name,
            system_prompt=f"你是{role_name}。{task}",
            available_tools=custom_tools or [],
        )

    def _cleanup_done(self, agent_id: str) -> None:
        """清理已完成的代理。"""
        info = self._agents.get(agent_id, {})
        if info.get("status") in ("ok", "completed", "failed", "timeout", None):
            self._agents.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> Optional[dict]:
        return self._agents.get(agent_id)

    def list_active(self) -> list[dict]:
        return [v for v in self._agents.values() if v.get("status") == "running"]

    def cleanup(self) -> int:
        """清理所有已完成的代理。"""
        to_remove = [
            aid for aid, info in self._agents.items()
            if info.get("status") in ("ok", "completed", "failed", "timeout")
        ]
        for aid in to_remove:
            del self._agents[aid]
        return len(to_remove)


# 全局单例
_pool: Optional[DynamicAgentPool] = None
_pool_lock = threading.Lock()


def get_agent_pool() -> DynamicAgentPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = DynamicAgentPool()
    return _pool
