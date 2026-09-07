"""Agent 工作流工具：让 Agent 可以在对话中调用工作流。

工具列表：
- run_workflow: 执行工作流
- list_workflows: 列出所有可用工作流
- get_workflow: 获取工作流详情
- create_workflow_from_desc: 根据描述 AI 生成工作流
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from pydantic import Field

from app.tools.registry import tool
from app.core.workflow.executor import WorkflowExecutor
from app.core.workflow.db import get_db
from app.tools.memory_tool import _current_session

logger = logging.getLogger(__name__)


@tool("run_workflow", "执行一个已定义的工作流，返回执行结果", timeout=300)
async def run_workflow(
    workflow_id: str = Field(description="工作流 ID"),
    inputs: dict = Field(default_factory=dict, description="输入变量（可选）"),
) -> str:
    """运行工作流并返回人类可读的结果摘要。

    Agent 可以基于对话上下文智能选择并运行工作流。
    工作流执行结果会自动关联到当前会话。
    """
    db = get_db()
    workflow = db.load_workflow(workflow_id)
    if not workflow:
        return json.dumps({"error": f"工作流不存在: {workflow_id}"}, ensure_ascii=False)

    # 每次创建独立 Agent 实例,避免并发污染
    from app.core.agent import Agent
    agent = Agent()
    session_id = _current_session.get() or ""

    executor = WorkflowExecutor(agent)
    run = await asyncio.wait_for(
        executor.run(workflow, inputs or {}, session_id=session_id),
        timeout=300,
    )

    # 人类可读摘要
    status_icon = "✅" if run.status == "completed" else "❌"
    summary = (
        f"{status_icon} 工作流「{workflow.name}」执行{run.status}\n"
        f"耗时: {run.duration:.1f}秒"
    )
    if run.output and isinstance(run.output, dict):
        nodes = run.output.get("nodes", {})
        node_summaries = []
        for nid, result in nodes.items():
            if result and isinstance(result, dict) and result.get("status") == "success":
                output = result.get("output")
                if output:
                    preview = str(output)[:100].replace("\n", " ")
                    node_summaries.append(f"  {nid}: {preview}")
            elif result and isinstance(result, dict) and result.get("error"):
                node_summaries.append(f"  {nid}: 失败 - {result['error'][:80]}")
        if node_summaries:
            summary += "\n节点输出:\n" + "\n".join(node_summaries[:5])
    if run.error:
        summary += f"\n错误: {run.error}"

    logger.info("工作流 %s 执行完成: %s", workflow_id, run.status)
    return summary


@tool("list_workflows", "列出所有可用工作流", timeout=10)
async def list_workflows() -> str:
    """列出工作流，供 Agent 选择。"""
    db = get_db()
    workflows = db.load_all_workflows()

    result = []
    for w in workflows.values():
        result.append(f"- {w.id}: {w.name} ({w.description}) | {len(w.nodes)} 节点 | 入口: {w.entry_node}")
    return json.dumps({
        "workflows": result,
        "total": len(workflows),
    }, ensure_ascii=False)


@tool("get_workflow", "获取工作流详细信息", timeout=10)
async def get_workflow(
    workflow_id: str = Field(description="工作流 ID"),
) -> str:
    """获取工作流详情，供 Agent 了解结构。"""
    db = get_db()
    workflow = db.load_workflow(workflow_id)
    if not workflow:
        return json.dumps({"error": f"工作流不存在: {workflow_id}"}, ensure_ascii=False)

    nodes_info = []
    for n in workflow.nodes.values():
        nodes_info.append(f"  {n.id} [{n.type.value}]: {n.name}")
    edges_info = []
    for e in workflow.edges:
        edges_info.append(f"  {e.source} → {e.target}")

    return json.dumps({
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "entry_node": workflow.entry_node,
        "nodes": nodes_info,
        "edges": edges_info,
    }, ensure_ascii=False)


@tool("create_workflow_from_desc", "根据自然语言描述 AI 自动生成工作流", timeout=120)
async def create_workflow_from_desc(
    name: str = Field(description="工作流名称"),
    description: str = Field(description="工作流描述(自然语言)"),
) -> str:
    """调用 AI 根据描述生成工作流并返回结果。

    例如: name="天气助手", description="搜索北京天气,根据天气推荐穿衣"
    """
    try:
        import httpx
        from app.config import settings
        # 从配置读取服务地址,支持容器化部署
        base_url = getattr(settings, "workflow_service_base_url", "http://127.0.0.1:8517")
        headers = {"Content-Type": "application/json"}
        if settings.agent_api_key:
            headers["Authorization"] = f"Bearer {settings.agent_api_key}"

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base_url}/v1/workflows/generate",
                headers=headers,
                json={"name": name, "description": description, "input_variables": {}},
            )
        if resp.status_code == 200:
            data = resp.json()
            return json.dumps({
                "ok": True,
                "workflow_id": data.get("id"),
                "name": data.get("name"),
                "message": data.get("message"),
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "ok": False,
                "error": f"生成失败 (HTTP {resp.status_code})",
                "detail": resp.text[:500],
            }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "ok": False,
            "error": f"生成异常: {e}",
        }, ensure_ascii=False)
