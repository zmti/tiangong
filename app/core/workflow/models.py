"""工作流编排模块 — 增强版。

支持真正的 DAG 执行（拓扑排序 + 并行）、循环子图、错误恢复策略、
结构化变量系统和会话上下文集成。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class NodeType(str, Enum):
    """节点类型枚举。"""
    START = "start"                    # 入口节点
    END = "end"                        # 结束节点
    AGENT = "agent"                    # Agent 执行节点
    TOOL = "tool"                      # 工具调用节点
    CONDITION = "condition"            # 条件分支节点
    LOOP = "loop"                      # 循环节点（可执行子图）
    HTTP = "http"                      # HTTP 请求节点
    SLEEP = "sleep"                    # 延时节点
    SCRIPT = "script"                  # 脚本执行节点


class NodeStatus(str, Enum):
    """节点状态。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ErrorStrategy(str, Enum):
    """节点错误处理策略。"""
    FAIL_FAST = "fail_fast"      # 默认：立即失败，中断整个工作流
    RETRY = "retry"              # 重试后仍失败则报错
    FALLBACK = "fallback"        # 失败时执行 fallback 节点
    CONTINUE = "continue"        # 跳过失败节点，继续执行其他节点


class LoopMode(str, Enum):
    """循环节点执行模式。"""
    SEQUENTIAL = "sequential"    # 顺序执行（默认）
    PARALLEL = "parallel"        # 并行执行所有迭代


@dataclass
class WorkflowNode:
    """工作流节点。"""
    id: str
    type: NodeType
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    # 执行配置
    tool_name: Optional[str] = None        # 工具调用节点
    agent_config: Optional[dict] = None    # Agent 节点配置
    condition_expr: Optional[str] = None   # 条件表达式
    loop_config: Optional[dict] = None     # 循环配置
    http_config: Optional[dict] = None     # HTTP 配置
    script_code: Optional[str] = None      # 脚本代码
    timeout: int = 60                      # 超时时间（秒）

    # 分支配置
    branches: dict[str, str] = field(default_factory=dict)  # {条件: 目标节点}

    # 错误恢复配置
    error_strategy: ErrorStrategy = ErrorStrategy.FAIL_FAST
    retry_count: int = 0                     # 重试次数
    retry_delay: float = 0.5                 # 重试间隔（秒）
    fallback_node_id: Optional[str] = None   # fallback 节点 ID

    # 循环子图配置
    loop_mode: LoopMode = LoopMode.SEQUENTIAL
    subgraph_ids: list[str] = field(default_factory=list)  # 循环内执行的节点 ID

    # 输出 schema 校验
    output_schema: Optional[dict] = None

    # 设计器手动排版位置(持久化,供前端恢复节点摆放)
    position: Optional[dict] = None  # {"x": float, "y": float}

    # 运行时状态
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None


@dataclass
class WorkflowEdge:
    """工作流边（连接）。"""
    id: str
    source: str          # 源节点 ID
    target: str          # 目标节点 ID
    condition: Optional[str] = None  # 条件表达式（可选）

    # 动态条件（运行时计算）
    condition_expr: Optional[str] = None


@dataclass
class Workflow:
    """工作流定义。"""
    id: str
    name: str
    description: str = ""
    version: str = "1.0.0"

    nodes: dict[str, WorkflowNode] = field(default_factory=dict)
    edges: list[WorkflowEdge] = field(default_factory=list)
    entry_node: Optional[str] = None   # 入口节点 ID
    output_schema: Optional[dict] = None  # 输出 schema

    # 全局配置
    error_mode: str = "fail_fast"      # fail_fast / continue_on_error
    timeout: float = 1800.0            # 全局超时（秒）

    # 元数据
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_node(self, node: WorkflowNode) -> None:
        """添加节点。"""
        self.nodes[node.id] = node
        self.updated_at = time.time()

    def add_edge(self, edge: WorkflowEdge) -> None:
        """添加边。"""
        self.edges.append(edge)
        self.updated_at = time.time()

    def get_next_nodes(self, node_id: str, context: dict[str, Any] | None = None) -> list[str]:
        """获取下一个节点 ID 列表（支持条件分支）。"""
        next_nodes = []
        context = context or {}

        for edge in self.edges:
            if edge.source != node_id:
                continue

            # 检查动态条件
            if edge.condition_expr:
                try:
                    from app.core.workflow.safe_eval import safe_eval
                    if not safe_eval(edge.condition_expr, context):
                        continue
                except (Exception, ):
                    logger.warning("条件评估失败: %s", edge.condition_expr)
                    continue

            # 检查静态条件（branches 兼容）
            if edge.condition:
                try:
                    from app.core.workflow.safe_eval import safe_eval
                    if not safe_eval(edge.condition, context):
                        continue
                except (Exception, ):
                    logger.warning("条件评估失败: %s", edge.condition)
                    continue

            next_nodes.append(edge.target)

        return next_nodes

    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        """获取节点。"""
        return self.nodes.get(node_id)

    def validate(self) -> list[str]:
        """验证工作流合法性。"""
        errors = []

        # 检查入口节点
        if not self.entry_node:
            errors.append("未设置入口节点")
        elif self.entry_node not in self.nodes:
            errors.append(f"入口节点 {self.entry_node} 不存在")

        # 检查节点连接
        for node_id in self.nodes:
            outgoing = [e for e in self.edges if e.source == node_id]
            if not outgoing and node_id != self.entry_node:
                incoming = [e for e in self.edges if e.target == node_id]
                if not incoming:
                    errors.append(f"节点 {node_id} 没有入边")

        # 检查边引用: 边只能连接真实存在的节点
        for edge in self.edges:
            if edge.source not in self.nodes:
                errors.append(f"边 {edge.id} 的源节点 {edge.source} 不存在")
            if edge.target not in self.nodes:
                errors.append(f"边 {edge.id} 的目标节点 {edge.target} 不存在")

        # 检查节点坐标: 防止脏数据导致前端 SVG path 渲染出现 NaN
        for node in self.nodes.values():
            if node.position is not None:
                try:
                    x = float(node.position.get("x", 0))
                    y = float(node.position.get("y", 0))
                except (TypeError, ValueError):
                    errors.append(f"节点 {node.id} 的坐标不是有效数字")
                    continue
                if x != x or y != y or x in (float("inf"), float("-inf")) or y in (float("inf"), float("-inf")):
                    errors.append(f"节点 {node.id} 的坐标不是有限数值")

        # 检查循环引用（简单检测）
        try:
            self._check_cycles()
        except CyclicWorkflowError as e:
            errors.append(str(e))

        return errors

    def _check_cycles(self) -> None:
        """检测循环引用。"""
        visited = set()
        rec_stack = set()

        def dfs(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)

            for edge in self.edges:
                if edge.source == node_id:
                    if edge.target not in visited:
                        if dfs(edge.target):
                            return True
                    elif edge.target in rec_stack:
                        return True

            rec_stack.remove(node_id)
            return False

        for node_id in self.nodes:
            if node_id not in visited:
                if dfs(node_id):
                    raise CyclicWorkflowError("工作流存在循环依赖")

    def build_subgraph(self, node_ids: list[str]) -> "Workflow":
        """构建子图（用于循环节点）。"""
        sub_nodes = {nid: self.nodes[nid] for nid in node_ids if nid in self.nodes}
        sub_edges = [
            e for e in self.edges
            if e.source in sub_nodes and e.target in sub_nodes
        ]

        subgraph = Workflow(
            id=f"{self.id}_sub",
            name=f"{self.name}_sub",
            nodes=sub_nodes,
            edges=sub_edges,
            entry_node=node_ids[0] if node_ids else None,
        )
        return subgraph


class CyclicWorkflowError(Exception):
    """工作流循环依赖异常。"""
    pass


@dataclass
class WorkflowRun:
    """工作流运行实例。"""
    run_id: str
    workflow_id: str
    status: str = "running"  # running/completed/failed/cancelled

    # 上下文
    context: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)

    # 会话 ID（用于记忆和上下文注入）
    session_id: str = ""

    # 节点执行状态
    node_states: dict[str, dict] = field(default_factory=dict)

    # 执行历史
    history: list[dict] = field(default_factory=list)

    # 错误信息
    error: Optional[str] = None

    # 时间信息
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    # 输出结果
    output: Optional[dict] = None

    # 取消标记
    _cancelled: bool = False

    def set_variable(self, name: str, value: Any) -> None:
        """设置变量（保持 JSON 类型，不转字符串）。"""
        self.variables[name] = value

    def get_variable(self, name: str, default: Any = None) -> Any:
        """获取变量（返回原始值，非字符串）。"""
        return self.variables.get(name, default)

    def update_node_state(self, node_id: str, state: dict) -> None:
        """更新节点状态。"""
        self.node_states[node_id] = state
        self.history.append({
            "node_id": node_id,
            "state": state,
            "timestamp": time.time()
        })

    def set_output(self, output: dict) -> None:
        """设置输出。"""
        self.output = output
        self.finished_at = time.time()
        self.status = "completed"

    def set_error(self, error: str) -> None:
        """设置错误。"""
        self.error = error
        self.status = "failed"
        self.finished_at = time.time()

    def cancel(self) -> None:
        """取消运行。"""
        self._cancelled = True
        self.status = "cancelled"
        self.finished_at = time.time()

    @property
    def cancelled(self) -> bool:
        """是否已取消。"""
        return self._cancelled

    @property
    def duration(self) -> float:
        """执行时长。"""
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "session_id": self.session_id,
            "context": self.context,
            "variables": {k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                         for k, v in self.variables.items()},
            "node_states": self.node_states,
            "history": self.history,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": self.output,
            "duration": self.duration,
        }
