"""安全检测 API:/v1/safety/*

功能:
- POST /v1/safety/check — 输入/输出安全检测
- GET /v1/safety/stats — 安全检测统计
- GET /v1/safety/logs — 最近检测日志
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/safety", tags=["safety"])


class SafetyCheckRequest(BaseModel):
    content: str = Field(..., description="待检测文本")
    direction: str = Field("input", description="检测方向: input / output / tool_call")
    tool_name: str = Field("", description="工具名(仅 tool_call 方向需要)")
    tool_arguments: dict = Field(default_factory=dict, description="工具参数")


@router.post("/check")
async def check_safety(req: SafetyCheckRequest) -> dict:
    """执行安全检测。"""
    from app.core.safety_engine import SafetyLevel, get_safety_engine

    engine = get_safety_engine()

    if req.direction == "input":
        result = engine.check_input(req.content)
    elif req.direction == "output":
        result = engine.check_output(req.content)
    elif req.direction == "tool_call":
        if not req.tool_name:
            raise HTTPException(status_code=400, detail="tool_call 方向需提供 tool_name")
        result = engine.check_tool_call(req.tool_name, req.tool_arguments)
    else:
        raise HTTPException(status_code=400, detail=f"不支持的 direction: {req.direction}")

    engine.record_check(result, req.content)

    resp = {"result": result.to_dict()}
    if result.is_blocked:
        resp["action"] = "blocked"
        resp["message"] = result.message
    elif result.needs_sanitization:
        resp["action"] = "sanitized"
        resp["sanitized_content"] = result.sanitized_content
    else:
        resp["action"] = "allowed"
    return resp


@router.get("/stats")
async def safety_stats() -> dict:
    """获取安全检测统计。"""
    from app.core.safety_engine import get_safety_engine

    engine = get_safety_engine()
    return engine.get_stats()


@router.get("/logs")
async def safety_logs(limit: int = 50) -> dict:
    """获取最近检测日志。"""
    from app.core.safety_engine import get_safety_engine

    engine = get_safety_engine()
    logs = engine._check_log[-limit:]  # noqa: SLF001
    return {"logs": logs, "total": len(logs)}
