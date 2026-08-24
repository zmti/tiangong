"""安全检测 API:/v1/safety/*

重构说明: 统一使用 guardrails.py 作为安全检测引擎，删除 safety_engine.py
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import guardrails

router = APIRouter(prefix="/v1/safety", tags=["safety"])


class SafetyCheckRequest(BaseModel):
    content: str = Field(..., description="待检测文本")
    direction: str = Field("input", description="检测方向: input / output / tool_call")
    tool_name: str = Field("", description="工具名(仅 tool_call 方向需要)")
    tool_arguments: dict = Field(default_factory=dict, description="工具参数")


@router.post("/check")
async def check_safety(req: SafetyCheckRequest) -> dict:
    """执行安全检测。"""
    if req.direction == "input":
        report = guardrails.run_input_guardrails(req.content)
    elif req.direction == "output":
        report = guardrails.run_output_guardrails(req.content)
    elif req.direction == "tool_call":
        if not req.tool_name:
            raise HTTPException(status_code=400, detail="tool_call 方向需提供 tool_name")
        # 工具调用安全检查（简化版）
        report = guardrails.run_input_guardrails(req.content)
    else:
        raise HTTPException(status_code=400, detail=f"不支持的 direction: {req.direction}")

    blocked = report.blocked
    violations = report.violations

    resp = {
        "blocked": blocked,
        "violations": [
            {"name": v.name, "ok": v.ok, "reason": v.reason, "sanitized": v.sanitized}
            for v in violations
        ],
    }
    if blocked:
        resp["action"] = "blocked"
        resp["message"] = violations[0].reason if violations else "检测到安全问题"
    elif any(v.sanitized for v in violations):
        resp["action"] = "sanitized"
        resp["sanitized_content"] = violations[0].sanitized
    else:
        resp["action"] = "allowed"
    return resp


@router.get("/stats")
async def safety_stats() -> dict:
    """获取安全检测统计（简化版，实际统计需扩展 guardrails）。"""
    return {"total_checks": 0, "blocked_count": 0, "allowed_count": 0}


@router.get("/logs")
async def safety_logs(limit: int = 50) -> dict:
    """获取最近检测日志（简化版）。"""
    return {"logs": [], "total": 0}
