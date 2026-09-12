"""增强型工具编排引擎:并行/条件分支/循环依赖 + 错误恢复。

对标 LangGraph / CrewAI 的编排能力:
- 并行扇出:多个工具同时执行
- 条件分支:根据中间结果选择不同路径
- 错误恢复:单步失败不影响整体流程
- 进度追踪:实时报告每个节点的执行状态
- ASCII 可视化:编排图可直观展示
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
from app.tools.registry import tool


class NodeType(str, Enum):
    TOOL_CALL = "tool_call"
    CONDITIONAL = "condition"
    PARALLEL = "parallel"
    SEQUENCE = "sequence"
    FINAL = "final"
    ERROR = "error"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"


@dataclass
class OrchestrationNode:
    """编排图节点。"""
    node_id: str
    node_type: NodeType
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    condition_expr: str = ""   # 条件分支表达式
    depends_on: list[str] = field(default_factory=list)
    parallel_group: str = ""   # 同组并行执行
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: str = ""
    start_time: float = 0.0
    end_time: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.node_id,
            "type": self.node_type.value,
            "tool": self.tool_name,
            "args": self.tool_args,
            "depends_on": self.depends_on,
            "parallel_group": self.parallel_group,
            "status": self.status.value,
            "result_preview": str(self.result)[:200] if self.result else None,
            "error": self.error,
            "duration_s": round(self.end_time - self.start_time, 2) if self.end_time and self.start_time else 0,
        }


@dataclass
class OrchestrationPlan:
    """工具编排计划。"""
    plan_id: str = ""
    description: str = ""
    nodes: list[OrchestrationNode] = field(default_factory=list)
    status: str = "pending"
    created_at: float = 0.0
    completed_at: float = 0.0
    metadata: dict = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "status": self.status,
            "node_count": len(self.nodes),
            "completed_nodes": sum(1 for n in self.nodes if n.status == NodeStatus.DONE),
            "failed_nodes": sum(1 for n in self.nodes if n.status == NodeStatus.FAILED),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class ToolOrchestrator:
    """工具编排引擎:执行编排计划,支持并行/条件/错误恢复。"""

    def __init__(
        self,
        tool_executor: Callable,
        max_parallel: int = 5,
        retry_on_error: bool = True,
        fallback_strategy: str = "skip",  # skip / default_value / rethrow
    ):
        self.tool_executor = tool_executor
        self.max_parallel = max_parallel
        self.retry_on_error = retry_on_error
        self.fallback_strategy = fallback_strategy

    async def execute(self, plan: OrchestrationPlan) -> OrchestrationPlan:
        """执行编排计划,返回最终状态。"""
        if not plan.nodes:
            plan.status = "completed"
            return plan

        plan.status = "running"
        plan.created_at = plan.created_at or time.time()

        ordered = self._topological_sort(plan.nodes)
        groups: dict[str, list[OrchestrationNode]] = {}
        for node in ordered:
            key = node.parallel_group or node.node_id
            groups.setdefault(key, []).append(node)

        semaphore = asyncio.Semaphore(self.max_parallel)

        async def _exec_one(node: OrchestrationNode) -> None:
            async with semaphore:
                node.status = NodeStatus.RUNNING
                node.start_time = time.time()
                try:
                    if node.node_type == NodeType.TOOL_CALL:
                        result = await self._call_tool(node)
                        node.result = result
                        node.status = NodeStatus.DONE
                    elif node.node_type == NodeType.CONDITIONAL:
                        if not self._eval_condition(node.condition_expr, plan):
                            node.status = NodeStatus.SKIPPED
                        else:
                            node.status = NodeStatus.DONE
                    else:
                        node.status = NodeStatus.DONE
                except Exception as e:
                    node.error = str(e)
                    if self.retry_on_error:
                        try:
                            result = await self._call_tool(node)
                            node.result = result
                            node.status = NodeStatus.DONE
                            node.error = ""
                        except Exception:
                            pass
                    if node.status != NodeStatus.DONE:
                        node.status = NodeStatus.FAILED
                        if self.fallback_strategy == "default_value":
                            node.result = "{}"
                finally:
                    node.end_time = time.time()

        for group_nodes in groups.values():
            if len(group_nodes) == 1:
                await _exec_one(group_nodes[0])
            else:
                await asyncio.gather(*[_exec_one(n) for n in group_nodes])

        failed = [n for n in plan.nodes if n.status == NodeStatus.FAILED]
        plan.status = "failed" if failed else "completed"
        plan.completed_at = time.time()
        if failed:
            plan.metadata["failure_nodes"] = [n.node_id for n in failed]
        return plan

    async def _call_tool(self, node: OrchestrationNode) -> str:
        if not node.tool_name:
            return ""
        return await self.tool_executor(node.tool_name, json.dumps(node.tool_args))

    def _eval_condition(self, expr: str, plan: OrchestrationPlan) -> bool:
        if not expr:
            return True
        expr = expr.strip()
        for node in plan.nodes:
            prefix = f"node_{node.node_id}_"
            if expr.startswith(prefix):
                suffix = expr[len(prefix):]
                if suffix == "success":
                    return node.status == NodeStatus.DONE
                elif suffix.startswith("contains "):
                    text = suffix[len("contains "):]
                    return text in str(node.result or "")
        return False

    def _resolve_conditions(self, plan: OrchestrationPlan) -> None:
        for node in plan.nodes:
            if node.status == NodeStatus.PENDING and node.condition_expr:
                if not self._eval_condition(node.condition_expr, plan):
                    node.status = NodeStatus.SKIPPED

    def _topological_sort(self, nodes: list[OrchestrationNode]) -> list[OrchestrationNode]:
        node_map = {n.node_id: n for n in nodes}
        visited = set()
        ordered = []

        def _visit(nid: str):
            if nid in visited:
                return
            visited.add(nid)
            node = node_map.get(nid)
            if node:
                for dep in node.depends_on:
                    _visit(dep)
                ordered.append(node)

        for n in nodes:
            _visit(n.node_id)
        return ordered

    def visualize(self, plan: OrchestrationPlan) -> str:
        lines = [f"=== 工具编排: {plan.description} ==="]
        lines.append(f"状态: {plan.status} | 节点: {len(plan.nodes)} | 完成: {sum(1 for n in plan.nodes if n.status == NodeStatus.DONE)}")
        icons = {
            NodeStatus.PENDING: "○", NodeStatus.RUNNING: "◷", NodeStatus.DONE: "✓",
            NodeStatus.FAILED: "✗", NodeStatus.SKIPPED: "-", NodeStatus.WAITING: "→",
        }
        for node in plan.nodes:
            icon = icons.get(node.status, "?")
            deps = f"  ← {', '.join(node.depends_on)}" if node.depends_on else ""
            parallel = f" [并行:{node.parallel_group}]" if node.parallel_group else ""
            lines.append(f"  {icon} [{node.node_id}] {node.node_type.value}{parallel}{deps}")
            lines.append(f"      工具: {node.tool_name} | 参数: {json.dumps(node.tool_args, ensure_ascii=False)[:80]}")
            if node.result:
                lines.append(f"      结果: {str(node.result)[:100]}")
            if node.error:
                lines.append(f"      错误: {node.error}")
            lines.append("")
        return "\n".join(lines)


# ── 工具注册 ──────────────────────────────────────────────────────
@tool(
    "orchestrate_tools",
    "工具编排:并行/条件分支执行多个工具,返回结构化结果和可视化",
    timeout=120,
)
def orchestrate_tools(
    tasks: list[dict] = Field(description="任务列表 [{tool, args, depends_on, parallel_group}]"),
    max_parallel: int = Field(5, description="最大并行数"),
) -> dict:
    """工具编排入口。"""
    from app.tools.registry import get_registry
    from app.core.tool_executor import ToolExecutor

    registry = get_registry()
    from app.tools.project_context import get_project_path
    executor = ToolExecutor(registry, project_path=get_project_path())

    async def _tool_exec(tool_name: str, args_str: str) -> str:
        return await executor.execute_tool(tool_name, args_str)

    engine = ToolOrchestrator(_tool_exec, max_parallel=max_parallel)

    nodes = []
    for i, task in enumerate(tasks):
        nodes.append(OrchestrationNode(
            node_id=f"node_{i}",
            node_type=NodeType.TOOL_CALL,
            tool_name=task.get("tool", ""),
            tool_args=task.get("args", {}),
            depends_on=task.get("depends_on", []),
            parallel_group=task.get("parallel_group", ""),
        ))

    plan = OrchestrationPlan(
        plan_id=f"plan_{int(time.time())}",
        description=f"编排 {len(tasks)} 个任务",
        nodes=nodes,
        created_at=time.time(),
    )

    result = asyncio.run(engine.execute(plan))
    return {
        "plan_id": result.plan_id,
        "status": result.status,
        "visualization": engine.visualize(result),
        "results": {n.node_id: n.result for n in result.nodes if n.status == NodeStatus.DONE},
        "errors": {n.node_id: n.error for n in result.nodes if n.status == NodeStatus.FAILED},
    }
