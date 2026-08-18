"""多智能体自动编排:Sequential/Hierarchical/Consensual 流程

对标主流 Agent:
- CrewAI: Sequential/Hierarchical 流程
- AutoGen: GroupChat 群聊
- LangGraph: Subgraph 子图

核心能力:
1. Sequential: 顺序执行多个 Agent
2. Hierarchical: 主 Agent 分配子任务给子 Agent
3. Consensual: 多个 Agent 并行执行，投票决定结果
4. 动态任务分配: 基于 Agent 能力自动分配子任务
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


class FlowType(str, Enum):
    """流程类型"""
    SEQUENTIAL = "sequential"  # 顺序执行
    HIERARCHICAL = "hierarchical"  # 层级分配
    CONSENSUAL = "consensual"  # 共识投票


class AgentRole(str, Enum):
    """Agent 角色"""
    COORDINATOR = "coordinator"  # 协调者
    EXECUTOR = "executor"  # 执行者
    REVIEWER = "reviewer"  # 审查者
    SPECIALIST = "specialist"  # 专家


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


@dataclass
class FlowResult:
    """流程执行结果"""
    flow_id: str = ""
    flow_type: FlowType = FlowType.SEQUENTIAL
    success: bool = False
    results: list[dict] = field(default_factory=list)
    final_answer: str = ""
    duration: float = 0.0
    agent_stats: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "flow_id": self.flow_id,
            "flow_type": self.flow_type.value,
            "success": self.success,
            "results": self.results,
            "final_answer": self.final_answer,
            "duration": self.duration,
            "agent_stats": self.agent_stats,
            "created_at": self.created_at,
        }


class MultiAgentOrchestrator:
    """多智能体编排器"""

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 MultiAgentOrchestrator
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "orchestrator.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        
        self._lock = threading.RLock()
        self._agents: dict[str, AgentCapability] = {}
        self._flows: dict[str, FlowResult] = {}
        self._tasks: dict[str, SubTask] = {}
        self._task_assignments: dict[str, str] = {}  # task_id -> agent_id
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        """初始化数据库表"""
        schema = {
            "orchestrator_flows": [
                {"name": "flow_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "flow_type", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "success", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "results", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "final_answer", "type": "TEXT", "constraints": "DEFAULT ''"},
                {"name": "duration", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "agent_stats", "type": "TEXT", "constraints": "DEFAULT '{}'"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
            "orchestrator_tasks": [
                {"name": "task_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "flow_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "description", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "assigned_agent", "type": "TEXT", "constraints": "DEFAULT ''"},
                {"name": "status", "type": "TEXT", "constraints": "DEFAULT 'pending'"},
                {"name": "result", "type": "TEXT", "constraints": ""},
                {"name": "dependencies", "type": "TEXT", "constraints": "DEFAULT '[]'"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "completed_at", "type": "REAL", "constraints": ""},
            ],
        }
        with self._lock:
            self._backend.init_db(schema)

    def _load(self) -> None:
        """从数据库加载数据"""
        with self._lock:
            # 加载流程
            rows = self._backend.select("orchestrator_flows", "1 = 1", order_by="created_at DESC")
            for row in rows:
                flow = FlowResult(
                    flow_id=row["flow_id"],
                    flow_type=FlowType(row["flow_type"]),
                    success=bool(row["success"]),
                    results=json.loads(row["results"] or "[]"),
                    final_answer=row["final_answer"],
                    duration=row["duration"],
                    agent_stats=json.loads(row["agent_stats"] or "{}"),
                    created_at=row["created_at"],
                )
                self._flows[flow.flow_id] = flow

            # 加载任务
            rows = self._backend.select("orchestrator_tasks", "1 = 1")
            for row in rows:
                task = SubTask(
                    id=row["task_id"],
                    description=row["description"],
                    assigned_agent=row["assigned_agent"],
                    status=row["status"],
                    result=json.loads(row["result"]) if row["result"] else None,
                    dependencies=json.loads(row["dependencies"] or "[]"),
                    flow_id=row["flow_id"],
                    created_at=row["created_at"],
                    completed_at=row["completed_at"],
                )
                self._tasks[task.id] = task

    def register_agent(self, agent_id: str, capability: AgentCapability) -> None:
        """注册 Agent"""
        self._agents[agent_id] = capability
        logger.info(f"注册 Agent: {agent_id}, tools={capability.tools}")

    async def decompose_task(
        self,
        task: str,
        user_input: str,
        llm: Any,
    ) -> list[SubTask]:
        """任务分解: 将复杂任务分解为子任务"""
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

只输出 JSON，不要其他内容。"""

        try:
            response = await llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
            tasks_data = json.loads(response)
            tasks = []
            for i, t in enumerate(tasks_data):
                task = SubTask(
                    description=t.get("description", ""),
                    assigned_agent=t.get("assigned_agent", ""),
                    dependencies=[f"dep_{d}" for d in t.get("dependencies", [])],
                )
                tasks.append(task)
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

        # 基于任务描述匹配 Agent 能力
        best_agent = None
        best_score = -1

        for agent_id, cap in self._agents.items():
            score = 0
            # 工具匹配
            for tool in cap.tools:
                if tool.lower() in task.description.lower():
                    score += 2
            # 专长匹配
            for exp in cap.expertise:
                if exp.lower() in task.description.lower():
                    score += 3
            # 描述匹配
            if any(word in task.description.lower() for word in cap.description.lower().split()):
                score += 1

            if score > best_score:
                best_score = score
                best_agent = agent_id

        return best_agent or list(self._agents.keys())[0] if self._agents else "default"

    async def execute_sequential(
        self,
        tasks: list[SubTask],
        executor_fn: Callable,
    ) -> FlowResult:
        """顺序执行流程"""
        flow_id = uuid.uuid4().hex
        result = FlowResult(flow_id=flow_id, flow_type=FlowType.SEQUENTIAL)
        start_time = time.time()

        for task in tasks:
            # 分配任务
            agent_id = self.assign_task(task)
            task.assigned_agent = agent_id
            task.status = "running"
            task.flow_id = flow_id

            try:
                # 执行任务
                task_result = await executor_fn(agent_id, task)
                task.result = task_result
                task.status = "completed"
                task.completed_at = time.time()
                result.results.append(task.to_dict())
            except Exception as e:
                task.status = "failed"
                task.result = {"error": str(e)}
                result.results.append(task.to_dict())
                result.success = False
                break
        else:
            result.success = True

        result.duration = time.time() - start_time
        
        # 保存到数据库
        self._save_flow(result)
        
        self._flows[flow_id] = result
        return result

    async def execute_hierarchical(
        self,
        main_task: str,
        user_input: str,
        llm: Any,
        executor_fn: Callable,
    ) -> FlowResult:
        """层级分配流程: 主 Agent 分配，子 Agent 执行"""
        flow_id = uuid.uuid4().hex
        result = FlowResult(flow_id=flow_id, flow_type=FlowType.HIERARCHICAL)
        start_time = time.time()

        # 1. 分解任务
        sub_tasks = await self.decompose_task(main_task, user_input, llm)
        if not sub_tasks:
            result.success = False
            result.final_answer = "任务分解失败"
            return result

        # 2. 按依赖拓扑排序
        ordered_tasks = self._topological_sort(sub_tasks)

        # 3. 执行
        completed_tasks: dict[str, SubTask] = {}
        for task in ordered_tasks:
            # 检查依赖
            deps_met = all(
                dep_id in completed_tasks and completed_tasks[dep_id].status == "completed"
                for dep_id in task.dependencies
            )
            if not deps_met:
                task.status = "failed"
                task.result = {"error": "依赖任务未完成"}
                continue

            # 分配任务
            agent_id = self.assign_task(task)
            task.assigned_agent = agent_id
            task.status = "running"
            task.flow_id = flow_id

            try:
                # 执行任务
                task_result = await executor_fn(agent_id, task)
                task.result = task_result
                task.status = "completed"
                task.completed_at = time.time()
                completed_tasks[task.id] = task
                result.results.append(task.to_dict())
            except Exception as e:
                task.status = "failed"
                task.result = {"error": str(e)}
                result.results.append(task.to_dict())

        result.success = all(t.status == "completed" for t in sub_tasks)
        result.duration = time.time() - start_time
        
        # 保存到数据库
        self._save_flow(result)
        
        self._flows[flow_id] = result
        return result

    async def execute_consensual(
        self,
        tasks: list[SubTask],
        executor_fn: Callable,
        voter_fn: Optional[Callable] = None,
    ) -> FlowResult:
        """共识投票流程: 多 Agent 并行执行，投票决定结果"""
        flow_id = uuid.uuid4().hex
        result = FlowResult(flow_id=flow_id, flow_type=FlowType.CONSENSUAL)
        start_time = time.time()

        # 并行执行
        async def run_task(task: SubTask) -> dict:
            agent_id = self.assign_task(task)
            task.assigned_agent = agent_id
            task.status = "running"
            task.flow_id = flow_id
            try:
                task_result = await executor_fn(agent_id, task)
                task.status = "completed"
                return {"agent": agent_id, "result": task_result, "success": True}
            except Exception as e:
                task.status = "failed"
                return {"agent": agent_id, "error": str(e), "success": False}

        # 执行所有任务
        results = await asyncio.gather(*[run_task(t) for t in tasks])
        result.results = [r for r in results]

        # 投票决定最终结果
        if voter_fn:
            final = await voter_fn(results)
            result.final_answer = final.get("answer", "")
        else:
            # 简单多数投票
            successful = [r for r in results if r.get("success")]
            if successful:
                result.final_answer = str(successful[0].get("result", ""))
            else:
                result.final_answer = "所有 Agent 执行失败"

        result.success = any(r.get("success") for r in results)
        result.duration = time.time() - start_time
        
        # 保存到数据库
        self._save_flow(result)
        
        self._flows[flow_id] = result
        return result

    def _save_flow(self, flow: FlowResult) -> None:
        """保存流程到数据库"""
        with self._lock:
            self._backend.upsert(
                "orchestrator_flows",
                {
                    "flow_id": flow.flow_id,
                    "flow_type": flow.flow_type.value,
                    "success": int(flow.success),
                    "results": json.dumps(flow.results, ensure_ascii=False),
                    "final_answer": flow.final_answer,
                    "duration": flow.duration,
                    "agent_stats": json.dumps(flow.agent_stats, ensure_ascii=False),
                    "created_at": flow.created_at,
                },
                conflict_columns=["flow_id"]
            )

    def _topological_sort(self, tasks: list[SubTask]) -> list[SubTask]:
        """拓扑排序（按依赖关系）"""
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

    def get_flow(self, flow_id: str) -> Optional[FlowResult]:
        """获取流程结果"""
        return self._flows.get(flow_id)

    def list_flows(self, limit: int = 20) -> list[dict]:
        """列出流程"""
        flows = list(self._flows.values())
        flows.sort(key=lambda f: f.created_at, reverse=True)
        return [f.to_dict() for f in flows[:limit]]

    def list_agents(self) -> list[dict]:
        """列出所有注册的 Agent"""
        return [
            {"id": aid, "tools": cap.tools, "expertise": cap.expertise}
            for aid, cap in self._agents.items()
        ]


# 单例
_orchestrator: Optional[MultiAgentOrchestrator] = None


def get_orchestrator() -> MultiAgentOrchestrator:
    """获取编排器单例"""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = MultiAgentOrchestrator()
    return _orchestrator
