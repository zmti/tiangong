"""工作流编排模块。

支持可视化流程设计、条件分支、循环节点、错误处理与状态持久化。
"""
from __future__ import annotations

import asyncio
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
    LOOP = "loop"                      # 循环节点
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
    timeout: int = 60                      # 超时时间
    
    # 分支配置
    branches: dict[str, str] = field(default_factory=dict)  # {条件: 目标节点}
    
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
    
    def get_next_nodes(self, node_id: str, context: dict[str, Any] = None) -> list[str]:
        """获取下一个节点 ID 列表（支持条件分支）。"""
        next_nodes = []
        context = context or {}
        
        for edge in self.edges:
            if edge.source != node_id:
                continue
            
            # 检查条件
            if edge.condition:
                try:
                    if not eval(edge.condition, {"__builtins__": {}}, context):
                        continue
                except Exception as e:
                    logger.warning(f"条件评估失败: {e}")
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
                # 非入口节点必须有入边
                incoming = [e for e in self.edges if e.target == node_id]
                if not incoming:
                    errors.append(f"节点 {node_id} 没有入边")
        
        return errors


@dataclass
class WorkflowRun:
    """工作流运行实例。"""
    run_id: str
    workflow_id: str
    status: str = "running"  # running/completed/failed/cancelled
    
    # 上下文
    context: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    
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
    
    @property
    def duration(self) -> float:
        """执行时长。"""
        end = self.finished_at or time.time()
        return end - self.started_at
