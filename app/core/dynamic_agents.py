"""动态多代理协作引擎:运行时创建临时专业代理,按需协作完成任务。

从 agents/roles.py 导入角色定义,避免重复。
提供并发控制(Semaphore)和流水线支持。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from app.config import settings
from app.core.llm import LLMClient
from app.tools.registry import ToolRegistry

# 从 roles.py 导入角色定义
from app.agents.roles import AgentRole as _RoleDef, get_role_registry  # noqa: F401


# ── 代理 ──────────────────────────────────────────────────────
@dataclass
class DynamicAgent:
    """动态创建的子代理。"""
    agent_id: str
    role: _RoleDef
    parent_session_id: str
    llm: LLMClient
    tool_executor: Callable
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    status: str = "running"  # running / completed / failed / timeout
    result: str = ""
    error: str = ""
    steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "role": self.role.name,
            "status": self.status,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "steps": self.steps[-5:],
            "duration_s": self.completed_at - self.created_at if self.completed_at else 0,
        }


class DynamicAgentPool:
    """动态代理池:管理和调度运行时创建的子代理。

    与 handoff_tool.py 的区别:
    - handoff_to_role: 主 Agent 在 ReAct 循环中委派,使用完整 Agent 生命周期
    - DynamicAgentPool: 外部 API 直接创建,轻量执行,支持并发控制
    """

    def __init__(self, max_concurrent: int = 5):
        self._agents: dict[str, DynamicAgent] = {}
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._registry: Optional[ToolRegistry] = None

    def set_registry(self, registry: ToolRegistry) -> None:
        self._registry = registry

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
        agent = DynamicAgent(
            agent_id=agent_id,
            role=role,
            parent_session_id=parent_session_id,
            llm=LLMClient(),
            tool_executor=self._make_tool_executor(role.available_tools or []),
        )
        self._agents[agent_id] = agent

        async with self._semaphore:
            try:
                result = await self._run_agent(agent, task, timeout)
                return result
            except Exception as e:
                agent.status = "failed"
                agent.error = str(e)
                return agent.to_dict()
            finally:
                agent.completed_at = time.time()
                if agent.status in ("completed", "failed", "timeout"):
                    self._agents.pop(agent_id, None)

    def _resolve_role(
        self, role_name: str, task: str, custom_tools: Optional[list[str]],
    ) -> Optional[_RoleDef]:
        """解析角色:优先从 registry,其次尝试自定义。"""
        reg = get_role_registry()
        role = reg.team.get(role_name)
        if role is not None:
            # 合并 available_tools
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

        # 自定义角色
        return _RoleDef(
            name=role_name,
            description=role_name,
            system_prompt=f"你是{role_name}。{task}",
            available_tools=custom_tools or [],
        )

    async def _run_agent(
        self, agent: DynamicAgent, task: str, timeout: int,
    ) -> dict:
        """运行单个代理的核心逻辑。"""
        timeout = timeout or 300
        messages = [
            {"role": "system", "content": agent.role.system_prompt},
            {"role": "user", "content": task},
        ]
        agent.messages = messages.copy()

        max_steps = agent.role.max_steps
        for step_i in range(max_steps):
            agent.steps.append({"step": step_i + 1, "type": "thinking", "content": "思考中..."})

            try:
                if hasattr(agent.llm, "chat_with_tools") and self._registry:
                    tools = self._registry.list(enabled_only=True)
                    if agent.role.available_tools:
                        tools = [t for t in tools if t.name in agent.role.available_tools]
                    schemas = [t.to_openai_schema() for t in tools]

                    resp = await asyncio.wait_for(
                        agent.llm.chat_with_tools(messages, tools=schemas, temperature=0.3),
                        timeout=timeout,
                    )
                else:
                    resp = await asyncio.wait_for(
                        agent.llm.chat(messages, temperature=0.3),
                        timeout=timeout,
                    )
            except asyncio.TimeoutError:
                agent.status = "timeout"
                agent.error = f"代理 {agent.agent_id} 执行超时({timeout}s)"
                agent.result = "[超时] 请在更长的时间内重试"
                return agent.to_dict()
            except Exception as e:
                agent.status = "failed"
                agent.error = str(e)
                return agent.to_dict()

            # 处理工具调用
            if hasattr(resp, 'tool_calls') and resp.tool_calls:
                for tc in resp.tool_calls:
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                        tool_result = await agent.tool_executor(tc.name, json.dumps(args))
                        messages.append({"role": "assistant", "content": None, "tool_calls": [tc.to_openai()]})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result[:4000],
                        })
                        agent.steps.append({
                            "step": step_i + 1,
                            "type": "tool_call",
                            "tool": tc.name,
                            "result_len": len(tool_result),
                        })
                    except Exception as e:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"[工具调用失败] {e}",
                        })
                        agent.steps.append({
                            "step": step_i + 1,
                            "type": "tool_error",
                            "tool": tc.name,
                            "error": str(e),
                        })
            else:
                agent.result = resp.content if hasattr(resp, 'content') else str(resp)
                agent.status = "completed"
                agent.steps.append({
                    "step": step_i + 1,
                    "type": "final_answer",
                    "content_preview": agent.result[:200],
                })
                break

        if agent.status != "completed":
            agent.status = "completed"
            agent.result = agent.messages[-1].get("content", "") if agent.messages else ""

        return agent.to_dict()

    def _make_tool_executor(self, allowed_tools: list[str]) -> Callable:
        """创建工具执行器闭包。"""
        registry = self._registry
        if registry is None:
            from app.tools.registry import get_registry
            registry = get_registry()

        async def _exec(tool_name: str, arguments: str) -> str:
            if allowed_tools and tool_name not in allowed_tools:
                return json.dumps({"error": f"工具 {tool_name} 不在允许列表中"})
            tool = registry.get(tool_name)
            if tool is None:
                return json.dumps({"error": f"工具 {tool_name} 不存在"})
            return await tool.run(arguments)
        return _exec

    def get_agent(self, agent_id: str) -> Optional[dict]:
        agent = self._agents.get(agent_id)
        return agent.to_dict() if agent else None

    def list_active(self) -> list[dict]:
        return [a.to_dict() for a in self._agents.values()]

    def cleanup(self) -> int:
        """清理所有已完成的代理,返回清理数量。"""
        to_remove = [
            aid for aid, a in self._agents.items()
            if a.status in ("completed", "failed", "timeout")
        ]
        for aid in to_remove:
            del self._agents[aid]
        return len(to_remove)


# ── 全局实例 ──────────────────────────────────────────────────────
_pool: Optional[DynamicAgentPool] = None


def get_agent_pool() -> DynamicAgentPool:
    global _pool
    if _pool is None:
        _pool = DynamicAgentPool()
    return _pool


# ── API 模型 ──────────────────────────────────────────────────────
class CreateAgentRequest(BaseModel):
    role: str = Field("researcher", description="代理角色: researcher/coder/analyst/writer/reviewer")
    task: str = Field(..., description="任务描述")
    session_id: str = Field("default", description="关联会话")
    custom_tools: list[str] = Field(default_factory=list, description="自定义工具列表")
    timeout: int = Field(0, description="超时秒数(0=使用角色默认)")


class PipelineTask(BaseModel):
    """流水线任务:按顺序执行多个代理。"""
    tasks: list[dict] = Field(..., description="任务列表 [{role, task}]")
    merge_strategy: str = Field("concat", description="合并策略: concat/sum/pipeline")
    session_id: str = Field("default", description="关联会话")
