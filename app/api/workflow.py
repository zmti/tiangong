"""工作流 API 端点 (持久化版本)。"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from app.api.security import require_api_key
from app.config import settings
from app.core.workflow.models import (
    Workflow, WorkflowNode, WorkflowEdge, NodeType, NodeStatus
)
from app.core.workflow.executor import WorkflowExecutor
from app.core.workflow.db import get_db, init_db, shutdown_db
from app.core.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workflows", tags=["工作流"])


class WorkflowCreateRequest(BaseModel):
    """创建工作流请求。"""
    name: str = Field(..., description="工作流名称")
    description: str = Field("", description="工作流描述")
    nodes: list[dict] = Field(..., description="节点列表")
    edges: list[dict] = Field(..., description="边列表")
    entry_node: str = Field(..., description="入口节点 ID")


class WorkflowRunRequest(BaseModel):
    """运行工作流请求。"""
    inputs: dict = Field(default_factory=dict, description="输入变量")


# ===== 内存存储 (向后兼容) =====
_workflows: dict[str, Workflow] = {}
_executors: dict[str, WorkflowExecutor] = {}
_runs: dict[str, dict[str, Any]] = {}


# ===== 生命周期管理 =====

@router.on_event("startup")
async def startup():
    """启动时加载工作流。"""
    init_db()
    db = get_db()
    workflows = db.load_all_workflows()
    
    # 加载到内存字典
    for wf in workflows.values():
        _workflows[wf.id] = wf
        _executors[wf.id] = WorkflowExecutor(Agent())
    
    logger.info(f"已加载 {len(workflows)} 个工作流")


@router.on_event("shutdown")
async def shutdown():
    """关闭时保存工作流。"""
    db = get_db()
    for wf in _workflows.values():
        db.save_workflow(wf)
    shutdown_db()
    logger.info("工作流已保存到数据库")


# ===== API 端点 =====

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
                "created_at": wf.created_at,
                "updated_at": wf.updated_at,
            }
            for wf in _workflows.values()
        ]
    }


@router.post("")
async def create_workflow(req: WorkflowCreateRequest):
    """创建工作流。"""
    # 验证入口节点
    node_ids = {n["id"] for n in req.nodes}
    if req.entry_node not in node_ids:
        raise HTTPException(400, f"入口节点 {req.entry_node} 不存在")
    
    # 创建节点
    nodes = {}
    for n in req.nodes:
        node = WorkflowNode(
            id=n["id"],
            type=NodeType(n["type"]),
            name=n.get("name", n["id"]),
            config=n.get("config", {}),
        )
        nodes[node.id] = node
    
    # 创建边
    edges = []
    for e in req.edges:
        edge = WorkflowEdge(
            id=f"edge_{len(edges)}",
            source=e["source"],
            target=e["target"],
            condition=e.get("condition"),
        )
        edges.append(edge)
    
    # 创建工作流
    now = time.time()
    workflow = Workflow(
        id=f"wf_{int(now * 1000)}",
        name=req.name,
        description=req.description,
        nodes=nodes,
        edges=edges,
        entry_node=req.entry_node,
        created_at=now,
        updated_at=now,
    )
    
    # 保存到内存和数据库
    _workflows[workflow.id] = workflow
    _executors[workflow.id] = WorkflowExecutor(Agent())
    _runs[workflow.id] = {}
    
    get_db().save_workflow(workflow)
    
    return {"id": workflow.id, "message": "工作流创建成功"}


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str):
    """获取工作流详情。"""
    workflow = _workflows.get(workflow_id)
    if not workflow:
        # 尝试从数据库加载
        workflow = get_db().load_workflow(workflow_id)
        if workflow:
            _workflows[workflow_id] = workflow
            _executors[workflow_id] = WorkflowExecutor(Agent())
    
    if not workflow:
        raise HTTPException(404, "工作流不存在")
    
    return {
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "version": workflow.version,
        "entry_node": workflow.entry_node,
        "nodes": [
            {
                "id": n.id,
                "type": n.type.value,
                "name": n.name,
                "config": n.config,
                "description": n.description,
            }
            for n in workflow.nodes.values()
        ],
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "condition": e.condition,
            }
            for e in workflow.edges
        ],
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, req: WorkflowRunRequest):
    """运行工作流。"""
    workflow = _workflows.get(workflow_id)
    if not workflow:
        workflow = get_db().load_workflow(workflow_id)
        if workflow:
            _workflows[workflow_id] = workflow
            _executors[workflow_id] = WorkflowExecutor(Agent())
    
    if not workflow:
        raise HTTPException(404, "工作流不存在")
    
    # 验证入口节点
    if not workflow.entry_node or workflow.entry_node not in workflow.nodes:
        raise HTTPException(400, "入口节点不存在")
    
    # 创建运行记录
    run_id = f"run_{int(time.time() * 1000)}"
    if workflow_id not in _runs:
        _runs[workflow_id] = {}
    
    # 执行工作流
    executor = _executors.get(workflow_id)
    if not executor:
        executor = WorkflowExecutor(Agent())
        _executors[workflow_id] = executor
    
    run = await executor.run(workflow, req.inputs or {}, run_id=run_id)
    
    # 保存运行记录
    _runs[workflow_id][run_id] = {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration": run.duration,
        "output": run.output,
        "error": run.error,
        "node_states": run.node_states,
        "history": run.history,
    }
    
    # 保存到数据库
    get_db().save_run(run)
    
    return {
        "run_id": run_id,
        "status": run.status,
        "duration": run.duration,
        "output": run.output,
        "error": run.error,
    }


@router.get("/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: str):
    """列出工作流运行记录。"""
    # 从内存和数据库获取
    memory_runs = _runs.get(workflow_id, {})
    db_runs = get_db().load_runs(workflow_id)
    
    # 合并
    all_runs = list(memory_runs.values()) + db_runs
    
    return {
        "runs": all_runs,
        "total": len(all_runs),
    }


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str):
    """删除工作流。"""
    if workflow_id not in _workflows:
        raise HTTPException(404, "工作流不存在")
    
    del _workflows[workflow_id]
    del _executors[workflow_id]
    
    if workflow_id in _runs:
        del _runs[workflow_id]
    
    get_db().delete_workflow(workflow_id)
    
    return {"message": "工作流已删除"}
