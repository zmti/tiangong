"""长上下文与自主规划 API:/v1/long-context/* 和 /v1/planning/*"""
from __future__ import annotations

import json
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.config import settings

# ── 长上下文 API ──────────────────────────────────────────────────
long_context_router = APIRouter(prefix="/v1/long-context", tags=["long-context"])


@long_context_router.get("/stats")
async def context_stats(session_id: str = Query("default")) -> dict:
    """当前会话的上下文 token 统计。"""
    from app.memory.context import estimate_tokens
    from app.memory.session import get_session_store

    store = get_session_store()
    history = await store.get_history(session_id)
    total_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in history)
    total_chars = sum(len(str(m.get("content", ""))) for m in history)

    token_budget = settings.agent_token_budget or 200000
    char_budget = settings.context_char_budget

    return {
        "session_id": session_id,
        "messages": len(history),
        "chars": total_chars,
        "tokens": total_tokens,
        "token_budget": token_budget,
        "char_budget": char_budget,
        "token_usage_pct": round(min(100, total_tokens / token_budget * 100), 1) if token_budget else 0,
        "char_usage_pct": round(min(100, total_chars / char_budget * 100), 1) if char_budget else 0,
        "over_token_budget": total_tokens > token_budget if token_budget else False,
    }


@long_context_router.post("/compress")
async def compress_context(req: BaseModel) -> dict:
    """手动触发上下文压缩,返回压缩统计。"""
    from app.memory.context import LongContextEngine, get_long_context_engine
    from app.memory.session import get_session_store
    from app.core.llm import LLMClient

    session_id = getattr(req, "session_id", "default")
    max_tokens = getattr(req, "max_tokens", 0) or 200000

    store = get_session_store()
    history = await store.get_history(session_id)

    llm = LLMClient()
    engine = LongContextEngine(max_tokens=max_tokens, llm=llm)

    compressed, stats = await engine.compress(history)
    return {
        "session_id": session_id,
        "strategy": stats.strategy,
        "original_messages": len(history),
        "compressed_messages": len(compressed),
        "original_tokens": stats.original_tokens,
        "final_tokens": stats.final_tokens,
        "dropped_messages": stats.dropped_messages,
        "summary": engine.format_context_summary(stats),
    }


# ── 自主规划 API ──────────────────────────────────────────────────
planning_router = APIRouter(prefix="/v1/planning", tags=["planning"])


class PlanRequest(BaseModel):
    task: str = Field(..., description="任务描述")
    max_steps: int = Field(10, ge=1, le=20, description="最大步骤数")
    session_id: str = Field("default", description="关联会话 ID")


@planning_router.post("/generate")
async def generate_plan(req: PlanRequest) -> dict:
    """生成执行计划。"""
    from app.core.autonomous_planner import generate_execution_plan
    from app.core.llm import LLMClient

    llm = LLMClient()
    plan = await generate_execution_plan(llm, req.task, req.max_steps)

    if plan is None:
        return {
            "task_id": "",
            "task_type": "simple",
            "message": "任务简单,无需多步规划",
            "steps": [],
        }

    return {
        "task_id": plan.task_id,
        "task_type": plan.task_type,
        "steps": [s.to_dict() for s in plan.steps],
        "step_count": len(plan.steps),
    }


class ExecutePlanRequest(BaseModel):
    plan_json: dict = Field(..., description="执行计划(JSON)")
    session_id: str = Field("default", description="关联会话 ID")


@planning_router.post("/execute")
async def execute_plan(req: ExecutePlanRequest) -> dict:
    """执行已生成的计划。"""
    from app.core.autonomous_planner import ExecutionPlan
    from app.core.llm import LLMClient
    from app.tools.registry import get_registry
    from app.core.tool_executor import ToolExecutor

    plan = ExecutionPlan.from_dict(req.plan_json)
    llm = LLMClient()
    registry = get_registry()
    from app.tools.project_context import get_project_path
    executor = ToolExecutor(registry, project_path=get_project_path())

    async def _tool_exec(tool_name: str, args: dict) -> str:
        return await executor.execute_tool(tool_name, json.dumps(args))

    from app.core.autonomous_planner import ExecutionEngine
    engine = ExecutionEngine(llm, _tool_exec)
    final_plan = await engine.execute(plan, req.plan_json.get("task_description", ""))

    return {
        "status": final_plan.status,
        "completed_steps": sum(1 for s in final_plan.steps if s.status == "done"),
        "failed_steps": sum(1 for s in final_plan.steps if s.status == "failed"),
        "total_steps": len(final_plan.steps),
        "plan": final_plan.to_dict(),
    }


@planning_router.get("/status/{task_id}")
async def get_plan_status(task_id: str) -> dict:
    """查询计划执行状态。"""
    from app.runtime.checkpoint import CheckpointManager
    import os
    from app.config import settings

    checkpoint_dir = os.path.join(settings.resolved_data_dir, "checkpoints.db")
    mgr = CheckpointManager(checkpoint_dir)
    runs = await mgr.list(limit=100)
    for r in runs:
        if task_id in r.get("run_id", ""):
            return {"task_id": task_id, "status": r.get("status", "unknown"), "plan": r}
    return {"task_id": task_id, "status": "not_found"}


# 注册路由
router = APIRouter()
router.include_router(long_context_router)
router.include_router(planning_router)
