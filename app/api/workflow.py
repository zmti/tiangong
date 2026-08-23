"""工作流 API 端点。"""
from __future__ import annotations

import logging
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
                "edges_count": len(wf.edges)
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
        ]
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
    
    # 异步执行
    import asyncio
    task = asyncio.create_task(executor.run(workflow, req.inputs))
    
    return {
        "message": "工作流已开始执行",
        "workflow_id": workflow_id,
        "inputs": req.inputs
    }


@router.get("/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: str):
    """列出工作流运行记录。"""
    # 这里应该查询数据库
    return {"runs": [], "total": 0}


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: str):
    """删除工作流。"""
    if workflow_id not in _workflows:
        raise HTTPException(status_code=404, detail="工作流不存在")
    
    del _workflows[workflow_id]
    del _executors[workflow_id]
    
    return {"message": "工作流已删除"}
