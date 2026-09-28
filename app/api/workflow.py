"""工作流 API 端点 (增强版) — 支持 DAG 执行、错误恢复、会话集成。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.security import require_api_key
from app.config import settings
from app.core.workflow.models import (
    Workflow, WorkflowNode, WorkflowEdge, WorkflowRun,
    NodeType, NodeStatus,
    ErrorStrategy, LoopMode
)
from app.core.workflow.executor import WorkflowExecutor
from app.core.workflow.db import get_db, init_db, shutdown_db
from app.core.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workflows", tags=["工作流"])


# ============================================================
# 请求 / 响应模型
# ============================================================

class WorkflowCreateRequest(BaseModel):
    name: str = Field(..., description="工作流名称")
    description: str = Field("", description="工作流描述")
    nodes: list[dict] = Field(..., description="节点列表")
    edges: list[dict] = Field(..., description="边列表")
    entry_node: str = Field(..., description="入口节点 ID")
    error_mode: str = Field("fail_fast", description="错误处理模式: fail_fast / continue_on_error")


class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    nodes: list[dict] = Field(default_factory=list, description="完整替换")
    edges: list[dict] = Field(default_factory=list, description="完整替换")
    entry_node: Optional[str] = None
    error_mode: Optional[str] = None
    timeout: Optional[float] = None


class NodeCreateRequest(BaseModel):
    node_id: str = Field(..., description="节点 ID")
    type: str = Field(..., description="节点类型")
    name: str = Field("", description="节点名称")
    config: dict = Field(default_factory=dict, description="节点配置")


class NodeUpdateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    config: Optional[dict] = None
    tool_name: Optional[str] = None
    agent_config: Optional[dict] = None
    condition_expr: Optional[str] = None
    branches: Optional[dict] = None
    loop_config: Optional[dict] = None
    http_config: Optional[dict] = None
    script_code: Optional[str] = None
    timeout: Optional[int] = None
    # 错误恢复配置
    error_strategy: Optional[str] = None
    retry_count: Optional[int] = None
    retry_delay: Optional[float] = None
    fallback_node_id: Optional[str] = None
    # 循环配置
    loop_mode: Optional[str] = None
    subgraph_ids: Optional[list[str]] = None
    # 手动排版位置(设计器拖拽后持久化)
    position: Optional[dict] = None


class EdgeCreateRequest(BaseModel):
    source: Optional[str] = Field(None, description="源节点 ID")
    target: Optional[str] = Field(None, description="目标节点 ID")
    from_node: Optional[str] = Field(None, description="源节点 ID(前端兼容字段)")
    to_node: Optional[str] = Field(None, description="目标节点 ID(前端兼容字段)")
    condition: Optional[str] = None
    condition_expr: Optional[str] = None

    def resolve_source(self) -> str:
        return self.source or self.from_node or ""

    def resolve_target(self) -> str:
        return self.target or self.to_node or ""


class WorkflowGenerateRequest(BaseModel):
    """AI 描述性工作流生成请求。"""
    name: str = Field(..., description="工作流名称")
    description: str = Field(..., description="自然语言描述,如'先用搜索获取天气,再根据天气推荐穿衣'")
    input_variables: dict = Field(default_factory=dict, description="可选输入变量说明")


class WorkflowRunRequest(BaseModel):
    inputs: dict = Field(default_factory=dict, description="输入变量")
    session_id: str = Field("default", description="关联的会话 ID")


class WorkflowRunStreamRequest(BaseModel):
    inputs: dict = Field(default_factory=dict, description="输入变量")
    session_id: str = Field("default", description="关联的会话 ID")
    stream_detail: bool = Field(True, description="是否流式返回节点状态变化")


# ============================================================
# 模块级共享实例
# ============================================================

_agent: Optional[Agent] = None
_workflows: dict[str, Workflow] = {}
_executors: dict[str, WorkflowExecutor] = {}
_runs: dict[str, dict[str, WorkflowRun]] = {}


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


# 共享的 Checkpoint 管理器:所有工作流运行都持久化到 data/workflow_checkpoints.db。
# 这是"断点续跑 / 跳过已成功节点"功能真正生效的前提——早期实现全传
# WorkflowCheckpointManager("") (空路径),导致所有 save/load 都是 no-op。
_checkpoint_mgr: Optional["WorkflowCheckpointManager"] = None


def _get_checkpoint_mgr() -> "WorkflowCheckpointManager":
    """返回(懒初始化)共享的节点级 Checkpoint 管理器。"""
    global _checkpoint_mgr
    if _checkpoint_mgr is None:
        from app.core.workflow.checkpoint import WorkflowCheckpointManager
        data_dir = Path(__file__).resolve().parents[2] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _checkpoint_mgr = WorkflowCheckpointManager(str(data_dir / "workflow_checkpoints.db"))
    return _checkpoint_mgr


def _make_executor(on_node_event=None) -> "WorkflowExecutor":
    """构造带 Checkpoint 持久化的执行器。

    注意:agent 不通过构造器传入(早期代码误把 agent 当 on_node_event 传,
    导致非流式运行每次发事件都调用 Agent 对象并抛出被吞掉的异常)。
    agent 由 run() 显式接收。
    """
    return WorkflowExecutor(on_node_event=on_node_event, checkpoint_mgr=_get_checkpoint_mgr())


# ============================================================
# 生命周期
# ============================================================

# 说明: 工作流路由挂在 app.main 的 app.include_router(..., dependencies=_auth) 上,
# 鉴权由 include_router 统一处理, 此处不再重复 import/绑定, 避免与 main.py 冲突。
# 启动/关闭: FastAPI 新版推荐用 lifespan 钩子; 这里保留 on_event 兼容旧调用方,
# 实际生效的生命周期见 app.main.lifespan(它会调用 workflow.router 的 startup/shutdown)。

@router.on_event("startup")
async def startup():
    init_db()
    db = get_db()
    workflows = db.load_all_workflows()
    for wf in workflows.values():
        _workflows[wf.id] = wf
        _executors[wf.id] = _make_executor()
    logger.info(f"已加载 {len(workflows)} 个工作流")


@router.on_event("shutdown")
async def shutdown():
    db = get_db()
    for wf in _workflows.values():
        db.save_workflow(wf)
    shutdown_db()
    logger.info("工作流已持久化到数据库")


# ============================================================
# 辅助函数
# ============================================================

def _load_workflow(workflow_id: str) -> Optional[Workflow]:
    """从内存或数据库加载工作流。"""
    wf = _workflows.get(workflow_id)
    if wf:
        return wf
    db = get_db()
    wf = db.load_workflow(workflow_id)
    if wf:
        _workflows[workflow_id] = wf
        if workflow_id not in _executors:
            _executors[workflow_id] = _make_executor()
    return wf


def _ensure_executor(workflow_id: str) -> WorkflowExecutor:
    if workflow_id not in _executors:
        _executors[workflow_id] = _make_executor()
    return _executors[workflow_id]


def _safe_coordinate(value: Any, default: float = 0.0) -> float:
    """把坐标值转换为有限 float, 异常/NaN/Inf 时回退默认值, 防止前端渲染 NaN。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float("inf"), float("-inf")):
        return float(default)
    return number


def _normalize_position(raw_position: Any) -> Optional[dict]:
    """规范化节点坐标, 保证保存进数据库的 position 始终可用。"""
    if raw_position is None:
        return None
    if not isinstance(raw_position, dict):
        return None
    return {"x": _safe_coordinate(raw_position.get("x", 0), 0.0), "y": _safe_coordinate(raw_position.get("y", 0), 0.0)}


def _parse_node(n: dict) -> WorkflowNode:
    """从请求字典解析节点。"""
    position = _normalize_position(n.get("position"))
    return WorkflowNode(
        id=n["id"],
        type=NodeType(n["type"]),
        name=n.get("name", n["id"]),
        config=n.get("config", {}),
        tool_name=n.get("tool_name"),
        agent_config=n.get("agent_config"),
        condition_expr=n.get("condition_expr"),
        loop_config=n.get("loop_config"),
        http_config=n.get("http_config"),
        script_code=n.get("script_code"),
        timeout=n.get("timeout", 60),
        branches=n.get("branches", {}),
        # 错误恢复配置
        error_strategy=ErrorStrategy(n.get("error_strategy", "fail_fast")),
        retry_count=n.get("retry_count", 0),
        retry_delay=n.get("retry_delay", 0.5),
        fallback_node_id=n.get("fallback_node_id"),
        # 循环配置
        loop_mode=LoopMode(n.get("loop_mode", "sequential")),
        subgraph_ids=n.get("subgraph_ids", []),
        position=position,
    )


def _parse_edge(e: dict, fallback_seq: int = 0) -> WorkflowEdge:
    """从请求字典解析边。优先保留调用方传入的稳定 id, 否则用序号兜底。"""
    edge_id = e.get("id")
    if not edge_id:
        edge_id = f"edge_{fallback_seq}"
    return WorkflowEdge(
        id=edge_id,
        source=e["source"],
        target=e["target"],
        condition=e.get("condition"),
        condition_expr=e.get("condition_expr"),
    )


# ============================================================
# CRUD
# ============================================================

@router.get("")
async def list_workflows():
    """列出所有工作流。"""
    return {
        "workflows": [
            {
                "id": wf.id,
                "name": wf.name,
                "description": wf.description,
                "version": wf.version,
                "nodes_count": len(wf.nodes),
                "edges_count": len(wf.edges),
                "entry_node": wf.entry_node,
                "error_mode": wf.error_mode,
                "created_at": wf.created_at,
                "updated_at": wf.updated_at,
            }
            for wf in _workflows.values()
        ]
    }


@router.post("")
async def create_workflow(req: WorkflowCreateRequest):
    """创建工作流。"""
    node_ids = {n["id"] for n in req.nodes}
    if req.entry_node not in node_ids:
        raise HTTPException(400, f"入口节点 {req.entry_node} 不存在")

    nodes = {n["id"]: _parse_node(n) for n in req.nodes}
    edges = [_parse_edge(e, i) for i, e in enumerate(req.edges)]

    now = time.time()
    wf_id = f"wf_{int(now * 1000)}"
    workflow = Workflow(
        id=wf_id,
        name=req.name,
        description=req.description,
        nodes=nodes,
        edges=edges,
        entry_node=req.entry_node,
        error_mode=req.error_mode,
        created_at=now,
        updated_at=now,
    )

    # 验证
    errors = workflow.validate()
    if errors:
        raise HTTPException(400, "; ".join(errors))

    _workflows[wf_id] = workflow
    _executors[wf_id] = _make_executor()
    _runs[wf_id] = {}
    get_db().save_workflow(workflow)

    logger.info(f"工作流已创建: {wf_id}")
    return {"id": wf_id, "message": "工作流创建成功"}


# ============================================================
# AI 描述性工作流生成
# ============================================================

_WORKFLOW_GEN_SYSTEM_PROMPT = """你是一个工作流设计专家。根据用户的自然语言描述,生成一个结构化的工作流定义。

## 节点类型说明
- **start**: 入口节点(必须有且仅有1个)
- **end**: 结束节点(必须有且仅有1个)
- **agent**: AI 智能体节点,调用 LLM 处理文本任务
- **tool**: 工具调用节点,执行具体的工具(如搜索、计算等)
- **condition**: 条件分支节点,根据表达式决定走向
- **loop**: 循环节点,遍历集合执行子操作
- **http**: HTTP 请求节点,调用外部 API
- **script**: Python 脚本节点,执行数据处理
- **sleep**: 延时节点

## 节点配置说明
- agent 节点: config.input 是提示词,支持 {{变量名}} 模板
- tool 节点: tool_name 是工具名,config.params 是参数
- condition 节点: condition_expr 是 Python 表达式,branches 定义 true/false 分支的下游节点 ID
- loop 节点: loop_config.iterable 是可迭代对象(JSON数组),subgraph_ids 是循环内执行的节点
- http 节点: http_config.url 和 http_config.method

## 设计原则
1. 从 start 开始,到 end 结束,确保所有节点可达
2. 尽量保持线性流程,只有真正需要分支/循环时才用 condition/loop
3. 使用已有工具名称(不要虚构不存在的工具)
4. 节点名称用中文,简洁明了
5. 边不需要 id 字段,只需 source 和 target
6. 只输出 JSON,不要包裹 markdown

## 输出格式
```json
{
  "name": "工作流名称",
  "description": "工作流描述",
  "nodes": [
    {"id": "start_1", "type": "start", "name": "开始"},
    {"id": "agent_1", "type": "agent", "name": "处理", "config": {"input": "提示词"}}
  ],
  "edges": [
    {"source": "start_1", "target": "agent_1"}
  ],
  "entry_node": "start_1"
}
```
"""


@router.post("/generate")
async def generate_workflow(req: WorkflowGenerateRequest):
    """根据自然语言描述 AI 自动生成工作流。"""
    llm = _get_agent().llm

    user_prompt = (
        f"请根据以下描述生成工作流定义:\n"
        f"名称: {req.name}\n"
        f"描述: {req.description}\n"
        f"输入变量: {json.dumps(req.input_variables, ensure_ascii=False) if req.input_variables else '(无)'}\n\n"
        f"请严格按照格式要求输出 JSON。"
    )

    try:
        result = await llm.chat_json(
            [
                {"role": "system", "content": _WORKFLOW_GEN_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
    except Exception as e:
        raise HTTPException(500, f"AI 生成失败: {e}")

    # 解析生成结果
    nodes = result.get("nodes", [])
    edges = result.get("edges", [])
    entry_node = result.get("entry_node", "")
    wf_name = req.name or result.get("name", "生成工作流")
    wf_desc = req.description or result.get("description", "")

    # 确保有 start 和 end 节点
    node_types = {n.get("type") for n in nodes}
    if "start" not in node_types:
        nodes.insert(0, {"id": "start_1", "type": "start", "name": "开始"})
        if entry_node and entry_node != "start_1":
            edges.append({"source": "start_1", "target": entry_node})
        entry_node = "start_1"
    if "end" not in node_types:
        nodes.append({"id": "end_1", "type": "end", "name": "结束"})
        # 找到没有出边的节点作为新的入口
        source_ids = {e["source"] for e in edges}
        target_ids = {e["target"] for e in edges}
        leaf_nodes = [n["id"] for n in nodes if n["id"] not in source_ids and n["id"] != "end_1"]
        if leaf_nodes:
            edges.append({"source": leaf_nodes[-1], "target": "end_1"})

    # 构建工作流
    node_ids = {n["id"] for n in nodes}
    if entry_node not in node_ids:
        entry_node = "start_1"

    now = time.time()
    wf_id = f"wf_{int(now * 1000)}"
    workflow = Workflow(
        id=wf_id,
        name=wf_name,
        description=wf_desc,
        nodes={n["id"]: _parse_node(n) for n in nodes},
        edges=[_parse_edge(e, i) for i, e in enumerate(edges)],
        entry_node=entry_node,
        created_at=now,
        updated_at=now,
    )

    errors = workflow.validate()
    if errors:
        raise HTTPException(400, "; ".join(errors))

    _workflows[wf_id] = workflow
    _executors[wf_id] = _make_executor()
    _runs[wf_id] = {}
    get_db().save_workflow(workflow)

    logger.info(f"AI 生成工作流: {wf_id} ({wf_name})")
    return {"id": wf_id, "name": wf_name, "message": "工作流生成成功"}


@router.put("/{workflow_id}")
async def update_workflow(workflow_id: str, req: WorkflowUpdateRequest):
    """更新工作流(完整替换 nodes/edges)。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    if req.name is not None:
        wf.name = req.name
    if req.description is not None:
        wf.description = req.description
    if req.entry_node is not None:
        wf.entry_node = req.entry_node
    if req.error_mode is not None:
        wf.error_mode = req.error_mode
    if req.timeout is not None:
        wf.timeout = req.timeout

    if req.nodes:
        new_nodes = {}
        for n in req.nodes:
            nid = n["id"]
            existing = wf.nodes.get(nid)
            if existing:
                existing.name = n.get("name", existing.name)
                existing.config = n.get("config", existing.config)
                existing.tool_name = n.get("tool_name", existing.tool_name)
                existing.agent_config = n.get("agent_config", existing.agent_config)
                existing.condition_expr = n.get("condition_expr", existing.condition_expr)
                existing.branches = n.get("branches", existing.branches)
                existing.timeout = n.get("timeout", existing.timeout)
                # 完整保留编辑器需要的节点字段, 避免编辑保存后读取不到 loop/http/script 配置
                if "loop_config" in n:
                    existing.loop_config = n.get("loop_config")
                if "http_config" in n:
                    existing.http_config = n.get("http_config")
                if "script_code" in n:
                    existing.script_code = n.get("script_code")
                # 错误恢复配置
                if "error_strategy" in n:
                    existing.error_strategy = ErrorStrategy(n["error_strategy"])
                if "retry_count" in n:
                    existing.retry_count = n["retry_count"]
                if "retry_delay" in n:
                    existing.retry_delay = n["retry_delay"]
                if "fallback_node_id" in n:
                    existing.fallback_node_id = n["fallback_node_id"]
                # 循环配置
                if "loop_mode" in n:
                    existing.loop_mode = LoopMode(n["loop_mode"])
                if "subgraph_ids" in n:
                    existing.subgraph_ids = n["subgraph_ids"]
                if "position" in n:
                    existing.position = _normalize_position(n.get("position"))
                new_nodes[nid] = existing
            else:
                node = _parse_node(n)
                new_nodes[nid] = node
        wf.nodes = new_nodes

    if req.edges:
        new_edges = []
        for i, e in enumerate(req.edges):
            # 保留请求方传入的稳定 id(前端据此做边的选中/删除),缺失时回退为序号 id
            edge_id = e.get("id") or f"edge_{i}"
            new_edges.append(WorkflowEdge(
                id=edge_id,
                source=e["source"],
                target=e["target"],
                condition=e.get("condition"),
                condition_expr=e.get("condition_expr"),
            ))
        wf.edges = new_edges

    errors = wf.validate()
    if errors:
        raise HTTPException(400, "; ".join(errors))

    wf.updated_at = time.time()
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    return {"id": workflow_id, "message": "工作流已更新"}


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str):
    """获取工作流详情。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")
    return {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description,
        "version": wf.version,
        "entry_node": wf.entry_node,
        "error_mode": wf.error_mode,
        "nodes": [
            {
                "id": n.id, "type": n.type.value, "name": n.name,
                "config": n.config, "description": n.description,
                "tool_name": n.tool_name, "agent_config": n.agent_config,
                "condition_expr": n.condition_expr, "branches": n.branches,
                "timeout": n.timeout,
                "error_strategy": n.error_strategy.value,
                "retry_count": n.retry_count,
                "retry_delay": n.retry_delay,
                "fallback_node_id": n.fallback_node_id,
                "loop_config": n.loop_config,
                "http_config": n.http_config,
                "script_code": n.script_code,
                "loop_mode": n.loop_mode.value,
                "subgraph_ids": n.subgraph_ids,
                "position": n.position,
            }
            for n in wf.nodes.values()
        ],
        "edges": [
            {"id": e.id, "source": e.source, "target": e.target,
             "condition": e.condition, "condition_expr": e.condition_expr}
            for e in wf.edges
        ],
        "created_at": wf.created_at,
        "updated_at": wf.updated_at,
    }


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str):
    """删除工作流。"""
    if workflow_id not in _workflows:
        raise HTTPException(404, "工作流不存在")
    del _workflows[workflow_id]
    if workflow_id in _executors:
        del _executors[workflow_id]
    if workflow_id in _runs:
        del _runs[workflow_id]
    get_db().delete_workflow(workflow_id)
    return {"message": "工作流已删除"}


# ============================================================
# 节点管理
# ============================================================

@router.post("/{workflow_id}/nodes")
async def add_node(workflow_id: str, req: NodeCreateRequest):
    """添加节点。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")
    if req.node_id in wf.nodes:
        raise HTTPException(400, f"节点 ID 已存在: {req.node_id}")

    # 解析错误恢复配置
    error_strategy = ErrorStrategy(req.config.get("error_strategy", "fail_fast")) if req.config else ErrorStrategy.FAIL_FAST
    retry_count = req.config.get("retry_count", 0) if req.config else 0
    retry_delay = req.config.get("retry_delay", 0.5) if req.config else 0.5
    fallback_node_id = req.config.get("fallback_node_id") if req.config else None
    loop_mode = LoopMode(req.config.get("loop_mode", "sequential")) if req.config else LoopMode.SEQUENTIAL
    subgraph_ids = req.config.get("subgraph_ids", []) if req.config else []

    pos = _normalize_position(req.config.get("position") if req.config else None)

    node = WorkflowNode(
        id=req.node_id,
        type=NodeType(req.type),
        name=req.name or req.node_id,
        config=req.config or {},
        tool_name=req.config.get("tool_name") if req.config else None,
        agent_config=req.config.get("agent_config") if req.config else None,
        condition_expr=req.config.get("condition_expr") if req.config else None,
        branches=req.config.get("branches") if req.config else None,
        timeout=req.config.get("timeout", 60) if req.config else 60,
        error_strategy=error_strategy,
        retry_count=retry_count,
        retry_delay=retry_delay,
        fallback_node_id=fallback_node_id,
        loop_mode=loop_mode,
        subgraph_ids=subgraph_ids,
        position=pos,
    )
    wf.add_node(node)
    wf.updated_at = time.time()
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    return {"id": req.node_id, "message": "节点已添加"}


@router.patch("/{workflow_id}/nodes/{node_id}")
async def update_node(workflow_id: str, node_id: str, req: NodeUpdateRequest):
    """更新节点配置。"""
    wf = _load_workflow(workflow_id)
    if not wf or node_id not in wf.nodes:
        raise HTTPException(404, "工作流或节点不存在")

    node = wf.nodes[node_id]
    if req.name is not None:
        node.name = req.name
    if req.type is not None:
        node.type = NodeType(req.type)
    if req.config is not None:
        node.config = req.config
    if req.tool_name is not None:
        node.tool_name = req.tool_name
    if req.agent_config is not None:
        node.agent_config = req.agent_config
    if req.condition_expr is not None:
        node.condition_expr = req.condition_expr
    if req.branches is not None:
        node.branches = req.branches
    if req.loop_config is not None:
        node.loop_config = req.loop_config
    if req.http_config is not None:
        node.http_config = req.http_config
    if req.script_code is not None:
        node.script_code = req.script_code
    if req.timeout is not None:
        node.timeout = req.timeout
    # 错误恢复配置
    if req.error_strategy is not None:
        node.error_strategy = ErrorStrategy(req.error_strategy)
    if req.retry_count is not None:
        node.retry_count = req.retry_count
    if req.retry_delay is not None:
        node.retry_delay = req.retry_delay
    if req.fallback_node_id is not None:
        node.fallback_node_id = req.fallback_node_id
    # 循环配置
    if req.loop_mode is not None:
        node.loop_mode = LoopMode(req.loop_mode)
    if req.subgraph_ids is not None:
        node.subgraph_ids = req.subgraph_ids
    # 手动排版位置
    if req.position is not None:
        node.position = _normalize_position(req.position)

    wf.updated_at = time.time()
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    return {"id": node_id, "message": "节点已更新"}


@router.delete("/{workflow_id}/nodes/{node_id}")
async def delete_node(workflow_id: str, node_id: str):
    """删除节点及关联边。"""
    wf = _load_workflow(workflow_id)
    if not wf or node_id not in wf.nodes:
        raise HTTPException(404, "工作流或节点不存在")
    if wf.entry_node == node_id:
        raise HTTPException(400, "不能删除入口节点")

    removed_edges = 0
    kept_edges = []
    for edge in wf.edges:
        if edge.source == node_id or edge.target == node_id:
            removed_edges += 1
            continue
        kept_edges.append(edge)
    wf.edges = kept_edges

    # 节点删除后, 清掉其他节点对它的残留引用, 避免校验/渲染时再出现悬空节点
    for remaining_node in wf.nodes.values():
        if remaining_node.fallback_node_id == node_id:
            remaining_node.fallback_node_id = None
        if remaining_node.branches:
            remaining_node.branches = {
                k: v for k, v in remaining_node.branches.items() if v != node_id
            }
        if remaining_node.subgraph_ids:
            remaining_node.subgraph_ids = [nid for nid in remaining_node.subgraph_ids if nid != node_id]

    del wf.nodes[node_id]

    wf.updated_at = time.time()
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    return {"deleted_node": node_id, "removed_edges": removed_edges, "message": f"节点 {node_id} 已删除"}


# ============================================================
# 边管理
# ============================================================

@router.post("/{workflow_id}/edges")
async def add_edge(workflow_id: str, req: EdgeCreateRequest):
    """添加边。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    source = req.resolve_source()
    target = req.resolve_target()
    if not source:
        raise HTTPException(400, "缺少源节点: source/from_node 至少传一个")
    if not target:
        raise HTTPException(400, "缺少目标节点: target/to_node 至少传一个")
    if source == target:
        raise HTTPException(400, "源节点和目标节点不能相同")
    if source not in wf.nodes:
        raise HTTPException(400, f"源节点不存在: {source}")
    if target not in wf.nodes:
        raise HTTPException(400, f"目标节点不存在: {target}")

    # 编辑器里两个节点之间允许双向连接(A->B 和 B->A 是两条不同的边),
    # 只有完全相同的同向重复边才幂等返回, 否则会导致“两个节点不能连接”。
    existing = next((e for e in wf.edges if e.source == source and e.target == target), None)
    if existing:
        return {"id": existing.id, "message": "边已存在", "existing": True}

    # 新边 id:避免与现有边 id 撞车(旧实现直接用 len 做 id, 删除中间边后会产生重复 id)
    used_ids = {e.id for e in wf.edges}
    seq = len(wf.edges)
    while f"edge_{seq}" in used_ids:
        seq += 1
    edge = WorkflowEdge(
        id=f"edge_{seq}",
        source=source,
        target=target,
        condition=req.condition,
        condition_expr=req.condition_expr,
    )
    wf.add_edge(edge)
    wf.updated_at = time.time()
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    return {"id": edge.id, "message": "边已添加", "existing": False}


@router.delete("/{workflow_id}/edges/{edge_id}")
async def delete_edge(workflow_id: str, edge_id: str):
    """删除边。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    before = len(wf.edges)
    wf.edges = [e for e in wf.edges if e.id != edge_id]
    if len(wf.edges) == before:
        raise HTTPException(404, f"边不存在: {edge_id}")

    wf.updated_at = time.time()
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    return {"message": "边已删除"}


# ============================================================
# 运行工作流
# ============================================================

@router.get("/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: str):
    """列出工作流运行记录。"""
    db = get_db()
    db_runs = db.load_runs(workflow_id, limit=50)
    memory_runs = list(_runs.get(workflow_id, {}).values())
    all_runs = memory_runs + db_runs
    return {"runs": [
        {
            "run_id": r.run_id,
            "status": r.status,
            "session_id": r.session_id,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration": r.duration,
            "output": r.output,
            "error": r.error,
            "node_states": r.node_states,
        }
        for r in all_runs
    ], "total": len(all_runs)}


@router.post("/{workflow_id}/run/stream")
async def run_workflow_stream(workflow_id: str, req: WorkflowRunStreamRequest):
    """运行工作流(SSE 流式返回节点状态变化)。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")
    if not wf.entry_node or wf.entry_node not in wf.nodes:
        raise HTTPException(400, "入口节点不存在")

    run_id = f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    if workflow_id not in _runs:
        _runs[workflow_id] = {}

    # 同一时刻只允许一个流式运行,避免并发跑同一个工作流导致事件缓冲互相串台
    for existing_run_id, existing_run in list(_runs.get(workflow_id, {}).items()):
        if existing_run.status == "running":
            raise HTTPException(409, "该工作流已有正在进行的运行,请等待完成或取消后重试")

    # 实时事件队列(替代原来的 list 缓冲)。
    # 原实现把事件 append 到 list,再用 `await wait_for(run_task)` 等整个工作流跑完才统一
    # 吐出 —— async generator 在 await 期间一个字节都不会 yield,前端全程收不到任何节点事件,
    # 只能在最后一刻哗地全涌出来(实测 8 条节点事件集中在 1ms 内到达,而运行耗时 6s)。
    # 现改为队列驱动:节点事件一产生就入队,生成器边等边推。
    event_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _enqueue(evt: dict) -> None:
        """节点事件入队。

        回调可能来自非事件循环线程(如 script 节点跑在 to_thread 里),统一走
        call_soon_threadsafe 保证线程安全;它的 FIFO 顺序同时保证了下面"终止负载
        一定排在所有节点事件之后",不会丢事件。
        """
        try:
            loop.call_soon_threadsafe(event_queue.put_nowait, evt)
        except RuntimeError:
            pass  # 事件循环已关闭(客户端断开),丢弃即可

    # 创建带回调的执行器
    realtime_executor = _make_executor(
        on_node_event=lambda rid, nid, status, result=None, error=None: _enqueue({
            "event": f"node_{status}",
            "node_id": nid,
            "status": status,
            "result": result,
            "error": error,
        })
    )

    _TERMINAL = "__terminal__"

    async def _event_stream():
        # 启动运行任务
        run_task = asyncio.create_task(
            realtime_executor.run(wf, req.inputs or {}, run_id=run_id, session_id=req.session_id, agent=_get_agent())
        )

        async def _await_run() -> dict:
            """在后台等待运行结束并算出终止负载。

            成功/超时/异常三条路径都会返回一个 dict,调用方把它放进队列。
            它同时充当"收尾哨兵":因为它在 await 结束之后才入队,而所有节点事件在
            await 结束之前就已请求入队,call_soon_threadsafe 的 FIFO 保证节点事件
            一定排在它前面被取出,因此不需要 sleep 轮询,也不会丢最后的事件。
            """
            try:
                run = await asyncio.wait_for(run_task, timeout=wf.timeout or 1800)
                _runs.setdefault(workflow_id, {})[run_id] = run
                get_db().save_run(run)
                return {
                    "event": "run_done",
                    "data": {
                        "run_id": run_id,
                        "status": run.status,
                        "duration": run.duration,
                        "output": run.output,
                        "error": run.error,
                        "node_states": run.node_states,
                        "history": run.history,
                        "terminal": True,
                    },
                }
            except asyncio.TimeoutError:
                # 运行任务因超时被 wait_for 取消,把超时也记录进 run,便于前端/历史面板看到真实状态
                run_task.cancel()
                failed_run = WorkflowRun(
                    run_id=run_id,
                    workflow_id=wf.id,
                    status="failed",
                    session_id=req.session_id,
                    error=f"运行超时({int(wf.timeout or 1800)}s)",
                    started_at=time.time(),
                    finished_at=time.time(),
                )
                _runs.setdefault(workflow_id, {})[run_id] = failed_run
                get_db().save_run(failed_run)
                return {
                    "event": "run_error",
                    "data": {"run_id": run_id, "error": "运行超时", "status": "failed", "terminal": True},
                }
            except Exception as e:
                run_task.cancel()
                failed_run = WorkflowRun(
                    run_id=run_id,
                    workflow_id=wf.id,
                    status="failed",
                    session_id=req.session_id,
                    error=str(e),
                    started_at=time.time(),
                    finished_at=time.time(),
                )
                _runs.setdefault(workflow_id, {})[run_id] = failed_run
                get_db().save_run(failed_run)
                return {
                    "event": "run_error",
                    "data": {"run_id": run_id, "error": str(e), "status": "failed", "terminal": True},
                }

        async def _pump_terminal() -> None:
            """等待运行结束后把终止负载放入队列,让主循环能收尾。"""
            terminal = await _await_run()
            await event_queue.put({_TERMINAL: True, "terminal": terminal})

        awaiter = asyncio.create_task(_pump_terminal())

        try:
            # 发送运行开始事件
            yield f"id: {run_id}\n"
            yield f"event: run_start\n"
            yield f"data: {json.dumps({'run_id': run_id, 'status': 'running'})}\n\n"

            # 主泵:节点事件随到随推,直到拿到终止负载
            while True:
                evt = await event_queue.get()
                if evt.get(_TERMINAL):
                    terminal = evt["terminal"]
                    break
                # 跳过运行级 __run__ 事件,前端只关心具体节点状态,
                # 避免历史面板把每次运行级事件也当成"节点失败"重复入栈
                if evt.get("node_id") == "__run__":
                    continue
                yield f"event: {evt['event']}\n"
                yield f"data: {json.dumps({
                    'run_id': run_id,
                    'node_id': evt['node_id'],
                    'status': evt['status'],
                    'result': evt.get('result'),
                    'error': evt.get('error'),
                })}\n\n"

            # 发送最终状态(带 terminal=true 标记,前端据此只入栈一次运行历史)
            yield f"event: {terminal['event']}\n"
            yield f"data: {json.dumps(terminal['data'])}\n\n"
        finally:
            # 客户端断开/生成器被取消时清理后台任务,避免留下孤儿 task 继续跑工作流
            awaiter.cancel()
            if not run_task.done():
                run_task.cancel()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, req: WorkflowRunRequest):
    """运行工作流(同步，等待完成)。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")
    if not wf.entry_node or wf.entry_node not in wf.nodes:
        raise HTTPException(400, "入口节点不存在")

    run_id = f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    if workflow_id not in _runs:
        _runs[workflow_id] = {}

    executor = _ensure_executor(workflow_id)
    # 同步路径也必须传 agent,否则 Agent 节点会因拿不到 agent 实例报"Agent 未初始化"
    # (与流式 /run/stream 一致)。这是此前遗留的 bug:脚本/第三方走同步接口调用工作流必挂。
    run = await executor.run(wf, req.inputs or {}, run_id=run_id, session_id=req.session_id, agent=_get_agent())

    if workflow_id not in _runs:
        _runs[workflow_id] = {}
    _runs[workflow_id][run_id] = run
    get_db().save_run(run)

    return {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "status": run.status,
        "duration": run.duration,
        "output": run.output,
        "error": run.error,
        "node_states": run.node_states,
        "history": run.history,
    }


@router.get("/{workflow_id}/runs/{run_id}")
async def get_run_detail(workflow_id: str, run_id: str):
    """获取单次运行详情。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    # 先从内存查
    if workflow_id in _runs and run_id in _runs[workflow_id]:
        run = _runs[workflow_id][run_id]
        return {
            "run_id": run.run_id,
            "workflow_id": run.workflow_id,
            "status": run.status,
            "session_id": run.session_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration": run.duration,
            "output": run.output,
            "error": run.error,
            "node_states": run.node_states,
            "history": run.history,
        }

    # 再从数据库查
    db_runs = get_db().load_runs(workflow_id, limit=100)
    for r in db_runs:
        if r.run_id == run_id:
            return {
                "run_id": r.run_id,
                "workflow_id": r.workflow_id,
                "status": r.status,
                "session_id": r.session_id,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "duration": r.duration,
                "output": r.output,
                "error": r.error,
                "node_states": r.node_states,
                "history": r.history,
            }

    raise HTTPException(404, f"运行记录不存在: {run_id}")


@router.delete("/{workflow_id}/runs")
async def clear_workflow_runs(workflow_id: str):
    """清空工作流运行记录。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    if workflow_id in _runs:
        _runs[workflow_id].clear()
    get_db().delete_runs(workflow_id)
    return {"message": "运行记录已清空"}


# ============================================================
# 运行状态查询
# ============================================================

@router.get("/{workflow_id}/runs/running")
async def list_running_workflows():
    """查询当前正在运行的工作流。"""
    active = []
    for wf_id, runs in _runs.items():
        for run_id, run in runs.items():
            if run.status == "running":
                active.append({
                    "workflow_id": wf_id,
                    "run_id": run_id,
                    "session_id": run.session_id,
                    "started_at": run.started_at,
                })
    return {"active_runs": active}


# ============================================================
# 高级 DAG 编排功能
# ============================================================

# ============================================================
# DAG 可视化
# ============================================================

@router.get("/{workflow_id}/visualize")
async def visualize_workflow(workflow_id: str):
    """生成 DAG 可视化数据(JSON 格式,供前端渲染)。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    from app.core.workflow.visualization import generate_dag_visualization
    return generate_dag_visualization(wf)


@router.get("/{workflow_id}/runs/{run_id}/snapshot")
async def get_run_snapshot(workflow_id: str, run_id: str):
    """获取运行快照(用于实时可视化)。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    # 从内存或数据库查找运行记录
    run = None
    if workflow_id in _runs and run_id in _runs[workflow_id]:
        run = _runs[workflow_id][run_id]
    else:
        for r in get_db().load_runs(workflow_id, limit=100):
            if r.run_id == run_id:
                run = r
                break

    if not run:
        raise HTTPException(404, f"运行记录不存在: {run_id}")

    from app.core.workflow.visualization import generate_execution_snapshot
    return generate_execution_snapshot(run)


@router.get("/{workflow_id}/runs/{run_id}/performance")
async def get_run_performance(workflow_id: str, run_id: str):
    """获取运行性能报告。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    run = None
    if workflow_id in _runs and run_id in _runs[workflow_id]:
        run = _runs[workflow_id][run_id]
    else:
        for r in get_db().load_runs(workflow_id, limit=100):
            if r.run_id == run_id:
                run = r
                break

    if not run:
        raise HTTPException(404, f"运行记录不存在: {run_id}")

    from app.core.workflow.visualization import generate_performance_report
    return generate_performance_report(run)


# ============================================================
# 工作流模板
# ============================================================

class TemplateListResponse(BaseModel):
    templates: list[dict]
    total: int


class TemplateInstantiateRequest(BaseModel):
    instance_id: str = Field(..., description="实例 ID(唯一标识)")
    inputs: dict = Field(default_factory=dict, description="输入变量")


class TemplateSearchRequest(BaseModel):
    keyword: str = ""
    limit: int = Field(20, ge=1, le=100)


@router.get("/templates")
async def list_templates() -> TemplateListResponse:
    """列出所有可用模板。"""
    from app.core.workflow.templates import get_template_registry
    registry = get_template_registry()
    templates = [t.to_dict() for t in registry.list_all()]
    return TemplateListResponse(templates=templates, total=len(templates))


@router.get("/templates/search")
async def search_templates(q: str = "", limit: int = 20):
    """搜索模板。"""
    from app.core.workflow.templates import get_template_registry
    registry = get_template_registry()
    templates = registry.search(q, limit=limit)
    return {
        "query": q,
        "templates": [t.to_dict() for t in templates],
        "total": len(templates),
    }


@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    """获取模板详情。"""
    from app.core.workflow.templates import get_template_registry
    registry = get_template_registry()
    template = registry.get(template_id)
    if not template:
        raise HTTPException(404, f"模板不存在: {template_id}")
    return template.to_dict()


@router.post("/templates/{template_id}/instantiate")
async def instantiate_template(template_id: str, req: TemplateInstantiateRequest):
    """从模板实例化工作流。"""
    from app.core.workflow.templates import get_template_registry
    registry = get_template_registry()
    template = registry.get(template_id)
    if not template:
        raise HTTPException(404, f"模板不存在: {template_id}")

    try:
        workflow = registry.instantiate(
            template_id=template_id,
            instance_id=req.instance_id,
            inputs=req.inputs,
        )
    except Exception as e:
        raise HTTPException(500, f"实例化失败: {e}")

    # 验证并保存
    errors = workflow.validate()
    if errors:
        raise HTTPException(400, "; ".join(errors))

    now = time.time()
    wf_id = f"wf_inst_{req.instance_id}"
    workflow.id = wf_id
    workflow.created_at = now
    workflow.updated_at = now

    _workflows[wf_id] = workflow
    _executors[wf_id] = _make_executor()
    _runs[wf_id] = {}
    get_db().save_workflow(workflow)

    logger.info(f"从模板实例化工作流: {template_id} -> {wf_id}")
    return {
        "id": wf_id,
        "template_id": template_id,
        "name": workflow.name,
        "message": "工作流实例化成功",
    }


# ============================================================
# 工作流编排
# ============================================================

class ChainExecuteRequest(BaseModel):
    workflow_ids: list[str] = Field(..., description="工作流 ID 列表(按顺序)")
    inputs: dict = Field(default_factory=dict)
    session_id: str = Field("default")


class ParallelExecuteRequest(BaseModel):
    workflow_ids: list[str] = Field(..., description="工作流 ID 列表")
    inputs: dict = Field(default_factory=dict)
    session_id: str = Field("default")
    max_concurrency: int = Field(5, ge=1, le=20)


class DependencyRequest(BaseModel):
    workflow_id: str = Field(..., description="工作流 ID")
    depends_on: list[str] = Field(default_factory=list, description="依赖的工作流 ID 列表")


@router.post("/orchestrate/chain")
async def execute_chain(req: ChainExecuteRequest):
    """顺序执行工作流链。"""
    from app.core.workflow.orchestrator import get_orchestrator
    orchestrator = get_orchestrator(_executors.get(req.workflow_ids[0]) if req.workflow_ids else None)

    results = await orchestrator.execute_chain(
        workflow_ids=req.workflow_ids,
        inputs=req.inputs,
        session_id=req.session_id,
    )
    return {
        "type": "chain",
        "results": results,
        "total_workflows": len(req.workflow_ids),
    }


@router.post("/orchestrate/parallel")
async def execute_parallel(req: ParallelExecuteRequest):
    """并行执行多个工作流。"""
    from app.core.workflow.orchestrator import get_orchestrator
    orchestrator = get_orchestrator()

    results = await orchestrator.execute_parallel(
        workflow_ids=req.workflow_ids,
        inputs=req.inputs,
        session_id=req.session_id,
        max_concurrency=req.max_concurrency,
    )
    return {
        "type": "parallel",
        "results": results,
        "total_workflows": len(req.workflow_ids),
    }


@router.post("/orchestrate/dependencies")
async def execute_with_dependencies(req: ChainExecuteRequest):
    """按依赖关系执行工作流。"""
    from app.core.workflow.orchestrator import get_orchestrator
    orchestrator = get_orchestrator()

    results = await orchestrator.execute_with_dependencies(
        workflow_ids=req.workflow_ids,
        inputs=req.inputs,
        session_id=req.session_id,
    )
    return {
        "type": "dependencies",
        "results": results,
        "total_workflows": len(req.workflow_ids),
    }


@router.post("/orchestrate/dependencies/add")
async def add_dependency(req: DependencyRequest):
    """添加工作流依赖关系。"""
    from app.core.workflow.orchestrator import get_orchestrator
    orchestrator = get_orchestrator()
    orchestrator.add_dependency(req.workflow_id, req.depends_on)
    return {"message": f"依赖已添加: {req.workflow_id} <- {req.depends_on}"}


@router.get("/orchestrate/status")
async def get_orchestration_status():
    """获取编排器状态。"""
    from app.core.workflow.orchestrator import get_orchestrator
    orchestrator = get_orchestrator()
    return {
        "registered_executors": list(orchestrator._executors.keys()),
        "dependencies": orchestrator._dependencies,
        "active_runs": len(orchestrator._running_runs),
    }


# ============================================================
# Checkpoint 管理
# ============================================================

@router.get("/{workflow_id}/runs/{run_id}/checkpoints")
async def list_run_checkpoints(workflow_id: str, run_id: str):
    """获取运行的检查点列表。"""
    mgr = _get_checkpoint_mgr()

    checkpoints = mgr.get_all_checkpoints(run_id)
    return {
        "run_id": run_id,
        "checkpoints": [cp.to_dict() for cp in checkpoints],
        "total": len(checkpoints),
    }


@router.post("/{workflow_id}/runs/{run_id}/resume")
async def resume_workflow_run(workflow_id: str, run_id: str):
    """从检查点恢复运行(跳过已成功的节点)。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    mgr = _get_checkpoint_mgr()
    skippable = mgr.get_skippable_nodes(run_id)

    if not skippable:
        raise HTTPException(400, "该运行没有可恢复的检查点")

    executor = _ensure_executor(workflow_id)
    run = await executor.run(
        workflow=wf,
        inputs={},  # 输入已保存在 checkpoint 中
        run_id=run_id,
        skip_nodes=skippable,
    )
    return {
        "run_id": run.run_id,
        "status": run.status,
        "skipped_nodes": list(skippable),
        "output": run.output,
        "duration": run.duration,
    }


# ============================================================
# SAGA 补偿管理
# ============================================================

@router.get("/saga/status/{run_id}")
async def get_saga_status(run_id: str):
    """获取 SAGA 补偿状态。"""
    from app.core.workflow.saga import get_compensation_registry
    registry = get_compensation_registry()
    saga = registry.get_or_create(run_id)
    return saga.to_dict()


# ============================================================
# 定时清理
# ============================================================

@router.post("/cleanup/checkpoints")
async def cleanup_checkpoints(max_age_hours: float = 24.0):
    """清理过期的检查点。"""
    mgr = _get_checkpoint_mgr()
    deleted = mgr.cleanup_old_checkpoints(max_age_hours=max_age_hours)
    return {"deleted": deleted, "max_age_hours": max_age_hours}


# ============================================================
# 版本管理
# ============================================================

class VersionCreateRequest(BaseModel):
    version: str = Field(..., description="版本号,如 v1.1.0")
    comment: str = Field("", description="版本说明")


@router.post("/{workflow_id}/versions")
async def save_workflow_version(workflow_id: str, req: VersionCreateRequest):
    """保存工作流版本快照。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    nodes_json = json.dumps([
        {
            "id": n.id, "type": n.type.value, "name": n.name, "config": n.config,
            "tool_name": n.tool_name, "agent_config": n.agent_config,
            "condition_expr": n.condition_expr, "branches": n.branches,
            "timeout": n.timeout, "error_strategy": n.error_strategy.value,
            "retry_count": n.retry_count, "retry_delay": n.retry_delay,
            "fallback_node_id": n.fallback_node_id, "loop_mode": n.loop_mode.value,
            "subgraph_ids": n.subgraph_ids, "position": n.position,
        }
        for n in wf.nodes.values()
    ])
    edges_json = json.dumps([
        {"id": e.id, "source": e.source, "target": e.target,
         "condition": e.condition, "condition_expr": e.condition_expr}
        for e in wf.edges
    ])

    version_id = get_db().save_version(
        workflow_id=workflow_id,
        version=req.version,
        nodes_json=nodes_json,
        edges_json=edges_json,
        metadata={"comment": req.comment, "node_count": len(wf.nodes), "edge_count": len(wf.edges)},
    )
    logger.info(f"工作流版本已保存: {workflow_id} -> {req.version} ({version_id})")
    return {"version_id": version_id, "version": req.version, "message": "版本已保存"}


@router.get("/{workflow_id}/versions")
async def list_workflow_versions(workflow_id: str, limit: int = 50):
    """列出工作流版本历史。"""
    versions = get_db().load_workflow_versions(workflow_id, limit=limit)
    return {"workflow_id": workflow_id, "versions": versions, "total": len(versions)}


@router.get("/{workflow_id}/versions/{version_id}")
async def get_workflow_version(workflow_id: str, version_id: str):
    """获取指定版本的完整内容。"""
    content = get_db().load_version_content(version_id)
    if not content:
        raise HTTPException(404, f"版本不存在: {version_id}")
    return {"version_id": version_id, "nodes": content["nodes"], "edges": content["edges"]}


@router.post("/{workflow_id}/versions/{version_id}/rollback")
async def rollback_to_version(workflow_id: str, version_id: str):
    """回滚到指定版本。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    content = get_db().load_version_content(version_id)
    if not content:
        raise HTTPException(404, f"版本不存在: {version_id}")

    # 保存当前版本作为备份
    now = time.time()
    backup_version = f"backup_{int(now * 1000)}"
    nodes_json = json.dumps([{"id": n.id, "type": n.type.value, "name": n.name, "config": n.config}
                             for n in wf.nodes.values()])
    edges_json = json.dumps([{"id": e.id, "source": e.source, "target": e.target}
                             for e in wf.edges])
    get_db().save_version(workflow_id, backup_version, nodes_json, edges_json,
                          {"comment": f"自动备份 before rollback to {version_id}"})

    # 加载目标版本
    nodes_data = content["nodes"]
    edges_data = content["edges"]
    nodes = {}
    for n in nodes_data:
        error_strategy_str = n.get("error_strategy", "fail_fast")
        try:
            error_strategy = ErrorStrategy(error_strategy_str)
        except ValueError:
            error_strategy = ErrorStrategy.FAIL_FAST
        loop_mode_str = n.get("loop_mode", "sequential")
        try:
            loop_mode = LoopMode(loop_mode_str)
        except ValueError:
            loop_mode = LoopMode.SEQUENTIAL
        nodes[n["id"]] = WorkflowNode(
            id=n["id"], type=NodeType(n["type"]), name=n.get("name", n["id"]),
            config=n.get("config", {}), tool_name=n.get("tool_name"),
            agent_config=n.get("agent_config"), condition_expr=n.get("condition_expr"),
            branches=n.get("branches", {}), timeout=n.get("timeout", 60),
            error_strategy=error_strategy, retry_count=n.get("retry_count", 0),
            retry_delay=n.get("retry_delay", 0.5), fallback_node_id=n.get("fallback_node_id"),
            loop_mode=loop_mode, subgraph_ids=n.get("subgraph_ids", []),
            position=_normalize_position(n.get("position")),
        )
    edges = [
        WorkflowEdge(id=e.get("id") or f"edge_{i}", source=e["source"], target=e["target"],
                     condition=e.get("condition"), condition_expr=e.get("condition_expr"))
        for i, e in enumerate(edges_data)
    ]

    wf.nodes = nodes
    wf.edges = edges
    wf.updated_at = now
    _workflows[workflow_id] = wf
    _executors[workflow_id] = _make_executor()
    get_db().save_workflow(wf)
    logger.info(f"工作流已回滚到版本: {version_id}")
    return {"message": f"已回滚到版本 {version_id}", "backup_version": backup_version}


# ============================================================
# 导出 / 导入
# ============================================================

class WorkflowImportRequest(BaseModel):
    workflow: dict = Field(..., description="完整工作流定义")
    import_as: str = Field("new", description="导入方式: new=新建 / overwrite=覆盖现有")
    workflow_id: Optional[str] = Field(None, description="覆盖时的目标 ID")


@router.get("/{workflow_id}/export")
async def export_workflow(workflow_id: str):
    """导出工作流为完整 JSON。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")
    return {
        "id": wf.id, "name": wf.name, "description": wf.description, "version": wf.version,
        "nodes": [
            {
                "id": n.id, "type": n.type.value, "name": n.name, "config": n.config,
                "description": n.description, "tool_name": n.tool_name,
                "agent_config": n.agent_config, "condition_expr": n.condition_expr,
                "branches": n.branches, "timeout": n.timeout,
                "error_strategy": n.error_strategy.value,
                "retry_count": n.retry_count, "retry_delay": n.retry_delay,
                "fallback_node_id": n.fallback_node_id, "loop_mode": n.loop_mode.value,
                "subgraph_ids": n.subgraph_ids, "position": n.position,
            }
            for n in wf.nodes.values()
        ],
        "edges": [
            {"id": e.id, "source": e.source, "target": e.target,
             "condition": e.condition, "condition_expr": e.condition_expr}
            for e in wf.edges
        ],
        "entry_node": wf.entry_node, "error_mode": wf.error_mode, "timeout": wf.timeout,
        "created_at": wf.created_at, "updated_at": wf.updated_at,
    }


@router.post("/import")
async def import_workflow(req: WorkflowImportRequest):
    """从 JSON 导入工作流。"""
    wf_data = req.workflow
    if req.import_as == "overwrite" and req.workflow_id:
        wf_id = req.workflow_id
        wf = _load_workflow(wf_id)
        if not wf:
            raise HTTPException(404, "目标工作流不存在")
    else:
        wf_id = f"wf_{int(time.time() * 1000)}"
        wf = None

    nodes = {}
    for n in wf_data.get("nodes", []):
        error_strategy_str = n.get("error_strategy", "fail_fast")
        try:
            error_strategy = ErrorStrategy(error_strategy_str)
        except ValueError:
            error_strategy = ErrorStrategy.FAIL_FAST
        loop_mode_str = n.get("loop_mode", "sequential")
        try:
            loop_mode = LoopMode(loop_mode_str)
        except ValueError:
            loop_mode = LoopMode.SEQUENTIAL
        node = WorkflowNode(
            id=n["id"], type=NodeType(n["type"]), name=n.get("name", n["id"]),
            config=n.get("config", {}), description=n.get("description", ""),
            tool_name=n.get("tool_name"), agent_config=n.get("agent_config"),
            condition_expr=n.get("condition_expr"), loop_config=n.get("loop_config"),
            http_config=n.get("http_config"), script_code=n.get("script_code"),
            timeout=n.get("timeout", 60), branches=n.get("branches", {}),
            error_strategy=error_strategy, retry_count=n.get("retry_count", 0),
            retry_delay=n.get("retry_delay", 0.5), fallback_node_id=n.get("fallback_node_id"),
            loop_mode=loop_mode, subgraph_ids=n.get("subgraph_ids", []),
            position=_normalize_position(n.get("position")),
        )
        nodes[node.id] = node

    edges = [
        WorkflowEdge(id=e.get("id") or f"edge_{i}", source=e["source"], target=e["target"],
                     condition=e.get("condition"), condition_expr=e.get("condition_expr"))
        for i, e in enumerate(wf_data.get("edges", []))
    ]

    now = time.time()
    if wf:
        wf.nodes = nodes
        wf.edges = edges
        wf.entry_node = wf_data.get("entry_node", wf.entry_node)
        wf.error_mode = wf_data.get("error_mode", wf.error_mode)
        wf.timeout = wf_data.get("timeout", wf.timeout)
        wf.updated_at = now
    else:
        wf = Workflow(
            id=wf_id, name=wf_data.get("name", "导入工作流"),
            description=wf_data.get("description", ""), version=wf_data.get("version", "1.0.0"),
            nodes=nodes, edges=edges, entry_node=wf_data.get("entry_node"),
            error_mode=wf_data.get("error_mode", "fail_fast"),
            timeout=wf_data.get("timeout", 1800.0), created_at=now, updated_at=now,
        )

    errors = wf.validate()
    if errors:
        raise HTTPException(400, "; ".join(errors))

    _workflows[wf_id] = wf
    _executors[wf_id] = _make_executor()
    _runs[wf_id] = {}
    get_db().save_workflow(wf)
    logger.info(f"工作流已导入: {wf_id} ({wf.name})")
    return {"id": wf_id, "name": wf.name, "message": "工作流导入成功"}


# ============================================================
# 调度管理
# ============================================================

from croniter import croniter


class ScheduleCreateRequest(BaseModel):
    name: str = Field(..., description="调度名称")
    cron_expr: str = Field(..., description="Cron 表达式,如 '0 9 * * *'")
    inputs: dict = Field(default_factory=dict, description="运行输入变量")
    session_id: str = Field("default", description="关联会话")


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = None
    cron_expr: Optional[str] = None
    inputs: Optional[dict] = None
    enabled: Optional[bool] = None


# 内存中的调度器: {schedule_id: {next_run_at, task}}
_schedules: dict[str, dict] = {}


@router.post("/{workflow_id}/schedules")
async def create_schedule(workflow_id: str, req: ScheduleCreateRequest):
    """创建定时调度。"""
    wf = _load_workflow(workflow_id)
    if not wf:
        raise HTTPException(404, "工作流不存在")

    # 验证 cron 表达式
    try:
        cron = croniter(req.cron_expr, __import__("datetime").datetime.now())
        next_run = cron.get_next(__import__("datetime").datetime)
        next_run_ts = next_run.timestamp()
    except Exception as e:
        raise HTTPException(400, f"Cron 表达式无效: {e}")

    schedule_id = f"sch_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    get_db().save_schedule(
        schedule_id=schedule_id, workflow_id=workflow_id, cron_expr=req.cron_expr,
        name=req.name, enabled=True, next_run_at=next_run_ts,
    )
    _schedules[schedule_id] = {
        "workflow_id": workflow_id, "cron_expr": req.cron_expr, "inputs": req.inputs,
        "session_id": req.session_id, "next_run_at": next_run_ts, "enabled": True,
    }
    logger.info(f"定时调度已创建: {schedule_id} -> {workflow_id}")
    return {"schedule_id": schedule_id, "next_run_at": next_run_ts, "message": "调度已创建"}


@router.get("/{workflow_id}/schedules")
async def list_schedules(workflow_id: str):
    """列出工作流的定时调度。"""
    schedules = get_db().load_schedules(workflow_id=workflow_id)
    return {"schedules": schedules, "total": len(schedules)}


@router.patch("/{workflow_id}/schedules/{schedule_id}")
async def update_schedule(workflow_id: str, schedule_id: str, req: ScheduleUpdateRequest):
    """更新定时调度配置。"""
    schedule = get_db().load_schedules(workflow_id=workflow_id)
    schedule = next((s for s in schedule if s["schedule_id"] == schedule_id), None)
    if not schedule:
        raise HTTPException(404, "调度不存在")

    kwargs = {}
    if req.name is not None:
        kwargs["name"] = req.name
    if req.cron_expr is not None:
        try:
            cron = croniter(req.cron_expr, __import__("datetime").datetime.now())
            kwargs["cron_expr"] = req.cron_expr
            kwargs["next_run_at"] = cron.get_next(__import__("datetime").datetime).timestamp()
        except Exception as e:
            raise HTTPException(400, f"Cron 表达式无效: {e}")
    if req.inputs is not None:
        kwargs["inputs"] = req.inputs
    if req.enabled is not None:
        kwargs["enabled"] = req.enabled
        if schedule_id in _schedules:
            _schedules[schedule_id]["enabled"] = req.enabled

    get_db().update_schedule(schedule_id, **kwargs)
    if schedule_id in _schedules:
        _schedules[schedule_id].update(kwargs)
    return {"message": "调度已更新"}


@router.delete("/{workflow_id}/schedules/{schedule_id}")
async def delete_schedule(workflow_id: str, schedule_id: str):
    """删除定时调度。"""
    if get_db().delete_schedule(schedule_id):
        _schedules.pop(schedule_id, None)
        return {"message": "调度已删除"}
    raise HTTPException(404, "调度不存在")


@router.post("/{workflow_id}/schedules/{schedule_id}/toggle")
async def toggle_schedule(workflow_id: str, schedule_id: str):
    """切换调度启用/禁用状态。"""
    schedule = get_db().load_schedules(workflow_id=workflow_id)
    schedule = next((s for s in schedule if s["schedule_id"] == schedule_id), None)
    if not schedule:
        raise HTTPException(404, "调度不存在")
    new_enabled = not schedule["enabled"]
    get_db().update_schedule(schedule_id, enabled=new_enabled)
    if schedule_id in _schedules:
        _schedules[schedule_id]["enabled"] = new_enabled
    return {"enabled": new_enabled, "message": "调度状态已切换"}


# ============================================================
# 统计分析
# ============================================================

@router.get("/{workflow_id}/stats")
async def get_workflow_stats(workflow_id: str, days: int = 7):
    """获取工作流运行统计。"""
    stats = get_db().get_workflow_stats(workflow_id, days=days)
    return stats


@router.get("/{workflow_id}/perf/nodes")
async def get_node_performance(workflow_id: str, run_id: str = None):
    """获取节点性能统计。"""
    perf = get_db().get_node_performance(workflow_id, run_id=run_id)
    return {"workflow_id": workflow_id, "nodes": perf}
