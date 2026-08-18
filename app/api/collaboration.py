"""多代理协作 & 工具编排 API。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/collaboration", tags=["collaboration"])


class CreateAgentRequest(BaseModel):
    role: str = "researcher"
    task: str = Field(..., description="任务描述")
    session_id: str = "default"
    custom_tools: list[str] = Field(default_factory=list)
    timeout: int = 0


@router.post("/agents/create")
async def create_agent(req: CreateAgentRequest) -> dict:
    """创建并运行一个动态子代理。"""
    from app.core.dynamic_agents import get_agent_pool

    pool = get_agent_pool()
    result = await pool.create_and_run(
        role_name=req.role,
        task=req.task,
        parent_session_id=req.session_id,
        custom_tools=req.custom_tools,
        timeout=req.timeout,
    )
    return result


@router.get("/agents/active")
async def list_active_agents() -> dict:
    """列出当前活跃的代理。"""
    from app.core.dynamic_agents import get_agent_pool

    pool = get_agent_pool()
    return {"agents": pool.list_active(), "total": len(pool.list_active())}


@router.post("/orchestrate")
async def run_orchestration(req: BaseModel) -> dict:
    """执行工具编排。"""
    from app.tools.registry import get_registry
    from app.core.tool_executor import ToolExecutor
    from app.core.tool_orchestrator import ToolOrchestrator, OrchestrationNode, NodeType, OrchestrationPlan
    import asyncio as _asyncio

    tasks = getattr(req, "tasks", [])
    max_parallel = getattr(req, "max_parallel", 5)

    registry = get_registry()
    executor = ToolExecutor(registry)

    async def _tool_exec(name: str, args: str) -> str:
        return await executor.execute_tool(name, args)

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
        plan_id=f"plan_{int(__import__('time').time())}",
        description=getattr(req, "description", f"编排 {len(tasks)} 个任务"),
        nodes=nodes,
    )

    result = _asyncio.run(engine.execute(plan))
    return {
        "plan_id": result.plan_id,
        "status": result.status,
        "visualization": engine.visualize(result),
        "results": {n.node_id: n.result for n in result.nodes if n.status.value == "done"},
        "errors": {n.node_id: n.error for n in result.nodes if n.status.value == "failed"},
    }


@router.get("/pool/stats")
async def pool_stats() -> dict:
    """代理池统计。"""
    from app.core.dynamic_agents import get_agent_pool
    from app.core.smart_router import get_routing_stats
    from app.core.smart_cache import get_cache
    from app.core.memory_cleanup import MemoryCleanupEngine

    pool = get_agent_pool()
    active = pool.list_active()
    cleanup = MemoryCleanupEngine()
    decay = cleanup.run_decay(dry_run=True)
    cache = get_cache()
    return {
        "active_agents": len(active),
        "routing": get_routing_stats(),
        "cache": cache.stats(),
        "memory_decay": decay,
    }
