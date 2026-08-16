"""可观测性接口:查询最近 trace 与聚合统计(对标 Langfuse/OTel 的基础查询面)。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.telemetry import tracer

router = APIRouter(prefix="/v1/traces", tags=["observability"])


@router.get("")
async def list_traces(
    limit: int = Query(100, ge=1, le=1000),
    run_id: str = Query("", description="按运行 id 过滤"),
    type: str = Query("", description="按事件类型过滤: llm_call / tool / run_start / run_end"),
) -> dict:
    events = tracer.recent(limit=limit, run_id=run_id, type_=type)
    return {"total": len(events), "events": events}


@router.get("/runs")
async def list_runs(limit: int = Query(50, ge=1, le=500)) -> dict:
    """最近运行摘要(每次运行的 LLM 调用数、token、耗时、是否成功)。"""
    return {"runs": tracer.runs(limit=limit)}


@router.get("/stats")
async def trace_stats(limit: int = Query(1000, ge=1, le=10000)) -> dict:
    """聚合统计:LLM 调用数、工具调用数、token 总量、估算成本、错误数。"""
    return tracer.stats(limit=limit)
