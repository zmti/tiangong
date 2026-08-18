"""会话成本预算 API:/v1/budget/*"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.core.budget import get_budget_tracker
from app.core.tenant_context import get_tenant

router = APIRouter(prefix="/v1/budget", tags=["budget"])


class SetBudgetRequest(BaseModel):
    session_id: str = Field("default", description="会话 ID")
    budget_usd: float = Field(0.0, ge=0, description="美元预算(0=不设)")
    budget_tokens: int = Field(0, ge=0, description="token 预算(0=不设)")


class RecordCostRequest(BaseModel):
    session_id: str = Field("default", description="会话 ID")
    prompt_tokens: int = Field(0, ge=0, description="prompt token 数")
    completion_tokens: int = Field(0, ge=0, description="completion token 数")
    model: str = Field("", description="模型名(用于成本估算)")
    budget_usd: float = Field(0.0, ge=0, description="美元预算(0=不设)")
    budget_tokens: int = Field(0, ge=0, description="token 预算(0=不设)")


@router.get("/status")
async def budget_status(session_id: str = Query("default"), tenant_id: str = Query("")) -> dict:
    tid = tenant_id or get_tenant()
    tracker = get_budget_tracker(tenant_id=tid)
    return tracker.get_status(session_id)


@router.post("/set")
async def set_budget(req: SetBudgetRequest) -> dict:
    """设置某会话的预算上限。下次记录时生效。"""
    from app.core.tenant_context import get_tenant
    tid = get_tenant()
    tracker = get_budget_tracker(tenant_id=tid)
    status = tracker.get_status(req.session_id)
    tracker.set(req.session_id, req.budget_usd, req.budget_tokens)
    return {
        "session_id": req.session_id,
        "budget_usd": req.budget_usd,
        "budget_tokens": req.budget_tokens,
        "current": status,
    }


@router.post("/record")
async def record_cost(req: RecordCostRequest) -> dict:
    """手动记录一次 LLM 调用的成本(由 Agent 在调用 LLM 后回调)。"""
    from app.core.tenant_context import get_tenant
    tid = get_tenant()
    tracker = get_budget_tracker(tenant_id=tid)
    return tracker.record(
        session_id=req.session_id,
        prompt_tokens=req.prompt_tokens,
        completion_tokens=req.completion_tokens,
        model=req.model,
        budget_usd=req.budget_usd,
        budget_tokens=req.budget_tokens,
    )


@router.post("/reset")
async def reset_budget(session_id: str = Query("default")) -> dict:
    from app.core.tenant_context import get_tenant
    tid = get_tenant()
    tracker = get_budget_tracker(tenant_id=tid)
    ok = tracker.reset(session_id)
    return {"session_id": session_id, "reset": ok}


@router.get("/sessions")
async def list_sessions(limit: int = Query(50, ge=1, le=200)) -> dict:
    from app.core.tenant_context import get_tenant
    tid = get_tenant()
    tracker = get_budget_tracker(tenant_id=tid)
    return {"sessions": tracker.list_sessions(limit=limit), "total": limit}


@router.post("/clear_all")
async def clear_all() -> dict:
    tracker = get_budget_tracker()
    n = tracker.clear_all()
    return {"cleared": n}
