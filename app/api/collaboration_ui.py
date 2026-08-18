"""Agent 协作可视化 API:多代理通信图谱、任务状态、消息流。

对标 Claude Projects 的协作视图 / Cursor Agent 面板:
- GET /v1/collab/agents  — 所有代理的实时状态
- GET /v1/collab/messages — 代理间消息历史(按时间线)
- GET /v1/collab/graph     — 代理通信图(节点 + 边)
- POST /v1/collab/agents/{id}/cancel — 取消运行中代理
- GET /v1/collab/agents/{id}/resume   — 从断点恢复代理
- GET /v1/collab/stats     — 协作统计概览
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/collab", tags=["collaboration-ui"])


class AgentStatus(BaseModel):
    agent_id: str
    session_id: str
    role: str
    task: str
    status: str          # queued/running/paused/done/failed/cancelled
    current_step: int
    max_steps: int
    created_at: float
    started_at: float
    completed_at: float
    error: str
    result_preview: str = ""


# ── 代理状态列表 ──────────────────────────────────────────────────
@router.get("/agents")
async def list_agents(
    session_id: str = Query("", description="按会话过滤"),
    status: str = Query("", description="按状态过滤(queued/running/done/...)"),
) -> dict:
    """获取所有代理状态列表。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    agents = store.list_active(session_id) if status else store.list_active("")
    if status:
        agents = [a for a in agents if a.status == status]
    if session_id:
        agents = [a for a in agents if a.session_id == session_id]

    return {
        "agents": [
            AgentStatus(
                agent_id=a.agent_id,
                session_id=a.session_id,
                role=a.role,
                task=a.task[:100],
                status=a.status,
                current_step=a.current_step,
                max_steps=a.max_steps,
                created_at=a.created_at,
                started_at=a.started_at or 0,
                completed_at=a.completed_at or 0,
                error=a.error,
                result_preview=(a.result or "")[:200],
            ).model_dump()
            for a in agents
        ],
        "total": len(agents),
    }


# ── 单个代理详情 ──────────────────────────────────────────────────
@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """获取单个代理的完整状态(含消息历史)。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    state = store.get(agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"代理 {agent_id} 不存在")

    return {
        **state.to_dict(),
        "messages_count": len(state.messages),
        "tool_calls_count": len(state.tool_calls),
        "intermediate_results_count": len(state.intermediate_results),
    }


# ── 代理通信图 ────────────────────────────────────────────────────
@router.get("/graph")
async def collaboration_graph(
    session_id: str = Query("", description="按会话过滤"),
) -> dict:
    """返回代理通信图(节点 + 边),供前端可视化。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    all_agents = store.list_by_session(session_id) if session_id else list(store._agents.values())

    nodes = []
    edges = []
    for i, agent in enumerate(all_agents):
        nodes.append({
            "id": agent.agent_id,
            "label": agent.role or "sub-agent",
            "session": agent.session_id,
            "status": agent.status,
            "step": agent.current_step,
            "max_steps": agent.max_steps,
            "error": agent.error,
        })
        if agent.parent_run_id:
            edges.append({
                "from": agent.parent_run_id[:8],
                "to": agent.agent_id,
                "type": "delegation",
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# ── 消息历史 ──────────────────────────────────────────────────────
@router.get("/messages")
async def agent_messages(
    agent_id: str = Query("", description="指定代理(空=全部)"),
    limit: int = Query(50, ge=1, le=200, description="返回消息数上限"),
) -> dict:
    """代理间消息历史。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    if agent_id:
        state = store.get(agent_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"代理 {agent_id} 不存在")
        messages = state.messages[-limit:]
    else:
        messages = []
        for state in store._agents.values():
            messages.extend(state.messages)
        messages.sort(key=lambda m: m.get("timestamp", 0))
        messages = messages[-limit:]

    return {
        "messages": messages,
        "total": len(messages),
    }


# ── 取消代理 ──────────────────────────────────────────────────────
@router.post("/agents/{agent_id}/cancel")
async def cancel_agent(agent_id: str) -> dict:
    """取消运行中的代理。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    state = store.get(agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"代理 {agent_id} 不存在")
    if state.status not in ("queued", "running", "paused"):
        return {"ok": False, "reason": f"代理已处于 {state.status} 状态"}

    store.mark_cancelled(agent_id)
    return {"ok": True, "agent_id": agent_id, "new_status": "cancelled"}


# ── 恢复代理 ──────────────────────────────────────────────────────
@router.post("/agents/{agent_id}/resume")
async def resume_agent(
    agent_id: str,
    resume_from_step: int = Query(0, description="从第几步恢复"),
) -> dict:
    """从断点恢复代理执行。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    state = store.get(agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"代理 {agent_id} 不存在")
    if state.status != "paused":
        return {"ok": False, "reason": f"代理当前状态为 {state.status},无法恢复"}

    store.update(agent_id, status="running", started_at=state.started_at or state.created_at)
    return {
        "ok": True,
        "agent_id": agent_id,
        "resume_from_step": resume_from_step or state.current_step,
        "checkpoint_node": state.checkpoint_node,
        "new_status": "running",
    }


# ── 统计概览 ──────────────────────────────────────────────────────
@router.get("/stats")
async def collab_stats() -> dict:
    """协作统计概览。"""
    from app.core.persistent_agent import get_agent_store

    store = get_agent_store()
    stats = store.stats()

    # 额外统计
    total_tool_calls = sum(len(s.tool_calls) for s in store._agents.values())
    total_messages = sum(len(s.messages) for s in store._agents.values())
    avg_steps = (
        sum(s.current_step for s in store._agents.values()) /
        max(1, len([s for s in store._agents.values() if s.current_step > 0]))
    )

    return {
        **stats,
        "total_tool_calls": total_tool_calls,
        "total_messages": total_messages,
        "avg_steps_per_agent": round(avg_steps, 1),
    }


# ── 手动触发热加载 ────────────────────────────────────────────────
@router.post("/reload-tools")
async def reload_tools() -> dict:
    """触发工具热加载(手动刷新注册表)。"""
    from app.core.tool_watcher import get_tool_hot_loader

    loader = get_tool_hot_loader()
    return loader.reload_all()
