"""工作流 API 端点。"""
from __future__ import annotations

import asyncio
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
from app.core.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workflows", tags=["工作流"])

# 全局工作流存储（生产环境应使用数据库）
_workflows: dict[str, Workflow] = {}
_executors: dict[str, WorkflowExecutor] = {}
_runs: dict[str, dict[str, Any]] = {}  # {workflow_id: {run_id: WorkflowRun}}


class WorkflowCreateRequest(BaseModel):
    """创建工作流请求。"""
    name: str = Field(..., description="工作流名称")
    description: str = Field("", description="工作流描述")
    nodes: list[dict] = Field(..., description="节点列表")
    edges: list[dict] = Field(..., description="边列表")
    entry_node: str = Field(..., description="入口节点 ID")


class WorkflowNodeCreate(BaseModel):
    """创建节点请求。"""
    id: str
    type: NodeType
    name: str
    config: dict = Field(default_factory=dict)
    branches: dict[str, str] = Field(default_factory=dict)


class WorkflowEdgeCreate(BaseModel):
    """创建边请求。"""
    source: str
    target: str
    condition: Optional[str] = None


class WorkflowRunRequest(BaseModel):
    """运行工作流请求。"""
    inputs: dict[str, str] = Field(default_factory=dict, description="输入变量")


class WorkflowRunResponse(BaseModel):
    """运行响应。"""
    run_id: str
    status: str
    duration: Optional[float] = None
    output: Optional[dict] = None
    error: Optional[str] = None


# ===== 路由 =====

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
                "entry_node": wf.entry_node
            }
            for wf in _workflows.values()
        ]
    }


@router.post("")
async def create_workflow(req: WorkflowCreateRequest):
    """创建工作流。"""
    workflow = Workflow(
        id=f"wf_{len(_workflows) + 1}",
        name=req.name,
        description=req.description
    )
    
    # 添加节点
    for node_data in req.nodes:
        node = WorkflowNode(
            id=node_data["id"],
            type=NodeType(node_data["type"]),
            name=node_data.get("name", node_data["id"]),
            config=node_data.get("config", {}),
            branches=node_data.get("branches", {})
        )
        workflow.add_node(node)
    
    # 添加边
    for edge_data in req.edges:
        edge_id = f"edge_{edge_data['source']}_{edge_data['target']}"
        edge = WorkflowEdge(
            id=edge_id,
            source=edge_data["source"],
            target=edge_data["target"],
            condition=edge_data.get("condition")
        )
        workflow.add_edge(edge)
    
    # 设置入口节点
    workflow.entry_node = req.entry_node
    
    # 验证
    errors = workflow.validate()
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    
    _workflows[workflow.id] = workflow
    _executors[workflow.id] = WorkflowExecutor(Agent())
    _runs[workflow.id] = {}  # 初始化运行记录存储
    
    return {"id": workflow.id, "message": "工作流创建成功"}


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str):
    """获取工作流详情。"""
    workflow = _workflows.get(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    
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
                "branches": n.branches
            }
            for n in workflow.nodes.values()
        ],
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "condition": e.condition
            }
            for e in workflow.edges
        ],
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at
    }


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, req: WorkflowRunRequest):
    """运行工作流。"""
    workflow = _workflows.get(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="工作流不存在")
    
    executor = _executors.get(workflow_id)
    if not executor:
        executor = WorkflowExecutor(Agent())
        _executors[workflow_id] = executor
    
    # 创建运行记录
    run_id = f"run_{int(time.time()*1000)}"
    if workflow_id not in _runs:
        _runs[workflow_id] = {}
    
    # 异步执行并等待结果
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
        "history": run.history
    }
    
    return {
        "run_id": run_id,
        "status": run.status,
        "duration": run.duration,
        "output": run.output,
        "error": run.error,
        "node_results": {
            node_id: state.get("result")
            for node_id, state in run.node_states.items()
        }
    }


@router.get("/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: str):
    """列出工作流运行记录。"""
    workflow_runs = _runs.get(workflow_id, {})
    runs = list(workflow_runs.values())
    
    return {
        "runs": runs,
        "total": len(runs)
    }


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str):
    """删除工作流。"""
    if workflow_id not in _workflows:
        raise HTTPException(status_code=404, detail="工作流不存在")
    
    del _workflows[workflow_id]
    del _executors[workflow_id]
    if workflow_id in _runs:
        del _runs[workflow_id]
    
    return {"message": "工作流已删除"}
