"""多智能体任务分解与拓扑排序:将复杂任务拆分为子任务并按依赖顺序排列。

仅保留 Agent 实际使用的能力:
- decompose_task: LLM 将复杂任务拆分为子任务
- _topological_sort: 按依赖关系对子任务拓扑排序
- register_agent / assign_task: 注册与分配执行模式

流程执行(sequential/hierarchical/consensual)由 Agent 内部实现,
本模块不涉及。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentCapability:
    """Agent 能力描述"""
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    expertise: list[str] = field(default_factory=list)  # 专业领域
    max_concurrent: int = 1  # 最大并发数


@dataclass
class SubTask:
    """子任务"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    description: str = ""
    assigned_agent: str = ""  # 分配给哪个 Agent
    status: str = "pending"  # pending/running/completed/failed
    result: Optional[dict] = None
    dependencies: list[str] = field(default_factory=list)  # 依赖的子任务ID
    flow_id: str = ""  # 所属流程ID
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "assigned_agent": self.assigned_agent,
            "status": self.status,
            "result": self.result,
            "dependencies": self.dependencies,
            "flow_id": self.flow_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class MultiAgentOrchestrator:
    """多智能体任务分解器:将复杂任务拆分为子任务并分配给合适的执行模式。"""

    def __init__(self):
        self._agents: dict[str, AgentCapability] = {}

    def register_agent(self, agent_id: str, capability: AgentCapability) -> None:
        """注册 Agent(幂等)"""
        self._agents[agent_id] = capability

    async def decompose_task(
        self,
        task: str,
        user_input: str,
        llm: Any,
        num_subtasks: int = 0,
    ) -> list[SubTask]:
        """任务分解: 将复杂任务分解为子任务。

        num_subtasks: UI 配置的子任务数量提示(0=不限制,由模型自行决定)。
        """
        count_hint = ""
        if num_subtasks and num_subtasks > 0:
            count_hint = (
                f"\n建议将任务分解为约 {num_subtasks} 个子任务"
                f"(可按实际复杂度在 ±1 范围内微调,但不要偏离太多)。"
            )
        prompt = f"""你是一个任务分解专家。请将以下任务分解为可执行的子任务。

任务: {task}
用户输入: {user_input}

可用的 Agent:
{self._build_agent_descriptions()}

请输出 JSON 格式的子任务列表，每个子任务包含:
- description: 子任务描述
- assigned_agent: 推荐的 Agent ID（从可用 Agent 中选择）
- dependencies: 依赖的子任务序号列表（从 0 开始）

示例输出:
[
  {{"description": "分析用户需求", "assigned_agent": "analyzer", "dependencies": []}},
  {{"description": "编写代码", "assigned_agent": "coder", "dependencies": [0]}},
  {{"description": "测试代码", "assigned_agent": "tester", "dependencies": [1]}}
]

只输出 JSON，不要其他内容。{count_hint}"""

        try:
            response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
            import json
            tasks_data = json.loads(response)
            tasks = []
            for t in tasks_data:
                task_obj = SubTask(
                    description=t.get("description", ""),
                    assigned_agent=t.get("assigned_agent", ""),
                    dependencies=[str(d) for d in t.get("dependencies", [])],
                )
                tasks.append(task_obj)
            return tasks
        except Exception as e:
            logger.error(f"任务分解失败: {e}")
            return [SubTask(description=task, assigned_agent="default")]

    def _build_agent_descriptions(self) -> str:
        """构建 Agent 描述文本"""
        lines = []
        for agent_id, cap in self._agents.items():
            lines.append(f"- {agent_id}: {cap.description}")
            if cap.tools:
                lines.append(f"  工具: {', '.join(cap.tools)}")
            if cap.expertise:
                lines.append(f"  专长: {', '.join(cap.expertise)}")
        return "\n".join(lines)

    def assign_task(self, task: SubTask) -> str:
        """自动分配子任务给最合适的 Agent"""
        if task.assigned_agent and task.assigned_agent in self._agents:
            return task.assigned_agent

        best_agent = None
        best_score = -1

        for agent_id, cap in self._agents.items():
            score = 0
            for tool in cap.tools:
                if tool.lower() in task.description.lower():
                    score += 2
            for exp in cap.expertise:
                if exp.lower() in task.description.lower():
                    score += 3
            if any(word in task.description.lower() for word in cap.description.lower().split()):
                score += 1

            if score > best_score:
                best_score = score
                best_agent = agent_id

        return best_agent or list(self._agents.keys())[0] if self._agents else "default"

    def _topological_sort(self, tasks: list[SubTask]) -> list[SubTask]:
        """拓扑排序(按依赖关系)"""
        task_map = {t.id: t for t in tasks}
        in_degree = {t.id: 0 for t in tasks}
        for t in tasks:
            for dep in t.dependencies:
                if dep in in_degree:
                    in_degree[t.id] += 1

        queue = [t for t in tasks if in_degree[t.id] == 0]
        result = []
        while queue:
            task = queue.pop(0)
            result.append(task)
            for t in tasks:
                if task.id in t.dependencies:
                    in_degree[t.id] -= 1
                    if in_degree[t.id] == 0:
                        queue.append(t)
        return result


# 单例
_orchestrator: Optional[MultiAgentOrchestrator] = None


def get_orchestrator() -> MultiAgentOrchestrator:
    """获取编排器单例"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = MultiAgentOrchestrator()
    return _orchestrator
