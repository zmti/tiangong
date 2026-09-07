"""性能剖析与失败经验接口: profiling / failure_memory / file_lock / webhook 配置 / 工具路由缓存 / Checkpoint 统计"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.tool_route_cache import get_tool_route_cache, clear_tool_route_cache
from app.runtime.batch_checkpoint import get_batch_checkpoint_manager

router = APIRouter(prefix="/v1", tags=["analytics"])


@router.get("/profiling/tools")
async def profiling_tools(
    limit: int = Query(20, ge=1, le=100),
    sort_by: str = Query("total_duration", description="排序字段: total_duration / avg_duration / count / error_rate"),
) -> dict:
    """工具性能排行。"""
    from app.core.profiling import get_profile_manager

    pm = get_profile_manager()
    return {"tools": pm.get_tool_stats(limit=limit, sort_by=sort_by)}


@router.get("/profiling/runs/{run_id}")
async def profiling_run(run_id: str) -> dict:
    """单次运行的完整性能轨迹。"""
    from app.core.profiling import get_profile_manager

    pm = get_profile_manager()
    return pm.get_run_profile(run_id)


@router.get("/profiling/daily")
async def profiling_daily(days: int = Query(7, ge=1, le=90)) -> dict:
    """最近 N 天的性能汇总。"""
    from app.core.profiling import get_profile_manager

    pm = get_profile_manager()
    return {"summary": pm.get_daily_summary(days=days)}


@router.post("/profiling/clear")
async def profiling_clear() -> dict:
    """清空所有性能分析数据。"""
    from app.core.profiling import get_profile_manager, reset_profile_manager

    pm = get_profile_manager()
    count = pm.clear_history()
    reset_profile_manager()
    return {"cleared": count}


@router.get("/failure-memory")
async def failure_memory_list(
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """列出失败经验记录。"""
    from app.memory.failure_memory import get_failure_memory

    store = get_failure_memory()
    all_recs = store.search_all(top_k=limit)
    return {
        "records": [r.to_dict() for r in all_recs],
        "stats": store.stats(),
    }


@router.get("/failure-memory/tools/{tool_name}")
async def failure_memory_for_tool(tool_name: str, top_k: int = Query(5, ge=1, le=20)) -> dict:
    """查询指定工具的失败经验。"""
    from app.memory.failure_memory import get_failure_memory

    store = get_failure_memory()
    recs = store.search(tool_name, top_k=top_k)
    return {"tool": tool_name, "records": [r.to_dict() for r in recs]}


@router.post("/failure-memory/hints/{record_id}")
async def failure_memory_add_hint(record_id: str, hint: str = Query(..., description="修复建议")) -> dict:
    """为失败记录补充修复建议。"""
    from app.memory.failure_memory import get_failure_memory

    store = get_failure_memory()
    ok = store.add_fix_hint(record_id, hint)
    return {"updated": ok}


@router.post("/failure-memory/clear")
async def failure_memory_clear() -> dict:
    """清空所有失败经验记录。"""
    from app.memory.failure_memory import get_failure_memory

    store = get_failure_memory()
    count = store.clear()
    return {"cleared": count}


@router.get("/file-locks")
async def file_locks_list() -> dict:
    """列出当前持有的文件锁。"""
    from app.core.file_lock import get_lock_manager

    mgr = get_lock_manager()
    return {"locks": mgr.list_locks()}


@router.post("/file-locks/cleanup")
async def file_locks_cleanup(max_age_s: float = Query(300.0, ge=60, le=3600)) -> dict:
    """清理超期文件锁。"""
    from app.core.file_lock import get_lock_manager

    mgr = get_lock_manager()
    cleaned = mgr.cleanup_expired(max_age_s=max_age_s)
    return {"cleaned": cleaned}


@router.get("/webhook/status")
async def webhook_status() -> dict:
    """查看 Webhook 配置状态(不暴露 secret)。"""
    import os

    urls = [u.strip() for u in os.environ.get("WEBHOOK_URLS", "").split(",") if u.strip()]
    events = os.environ.get("WEBHOOK_EVENTS", "")
    sender = None
    try:
        from app.core.webhook import get_webhook_sender
        sender = get_webhook_sender()
    except Exception as e:
        logger.warning("Webhook 配置读取失败(不影响接口响应): %s", e)
    return {
        "configured": bool(urls),
        "urls_count": len(urls),
        "urls_preview": [u.split("@")[-1] if "@" in u else u for u in urls],
        "events_filter": events,
        "secret_configured": bool(os.environ.get("WEBHOOK_SECRET")),
        "timeout_s": float(os.environ.get("WEBHOOK_TIMEOUT", "10")),
        "retries": int(os.environ.get("WEBHOOK_RETRIES", "2")),
        "queue_stats": sender.get_queue_stats() if sender else None,
    }


# ---- 成功经验记忆接口 ----

@router.get("/success-memory")
async def success_memory_list(
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """列出成功经验记录。"""
    from app.memory.success_memory import get_success_memory

    store = get_success_memory()
    all_recs = store.search_all(top_k=limit)
    return {
        "records": [r.to_dict() for r in all_recs],
        "stats": store.stats(),
    }


@router.get("/success-memory/tools/{tool_name}")
async def success_memory_for_tool(tool_name: str, top_k: int = Query(5, ge=1, le=20)) -> dict:
    """查询指定工具的成功经验。"""
    from app.memory.success_memory import get_success_memory

    store = get_success_memory()
    recs = store.search(tool_name, top_k=top_k)
    return {"tool": tool_name, "records": [r.to_dict() for r in recs]}


@router.post("/success-memory/clear")
async def success_memory_clear() -> dict:
    """清空所有成功经验记录。"""
    from app.memory.success_memory import get_success_memory

    store = get_success_memory()
    count = store.clear()
    return {"cleared": count}


# ---- 工具路由缓存接口 ----

@router.get("/tool_route_cache/stats")
async def tool_route_cache_stats() -> dict:
    """查看工具路由缓存统计信息。"""
    cache = get_tool_route_cache()
    return cache.get_stats()


@router.post("/tool_route_cache/clear")
async def tool_route_cache_clear() -> dict:
    """清空工具路由缓存。"""
    await clear_tool_route_cache()
    return {"cleared": True}


# ---- Checkpoint 批量写入统计接口 ----

@router.get("/checkpoint/stats")
async def checkpoint_stats() -> dict:
    """查看 Checkpoint 批量写入统计信息。"""
    # 获取当前活跃的 batch manager
    from app.runtime.batch_checkpoint import _batch_manager
    if _batch_manager is not None:
        return _batch_manager.get_stats()
    return {
        "buffer_size": 0,
        "max_buffer_size": 10,
        "flush_interval": 2.0,
        "buffered_count": 0,
        "flushed_count": 0,
        "skipped_count": 0,
        "avg_write_time": 0.0,
        "running": False,
        "note": "批量写入未启用或管理器未初始化",
    }


# ---- Telemetry 清理接口 ----

@router.post("/traces/cleanup")
async def cleanup_traces(
    retention_days: float = Query(7.0, ge=1, le=365, description="保留天数"),
) -> dict:
    """清理过期的 trace 记录。"""
    from app.core.telemetry import cleanup_traces as _cleanup
    cleaned = await _cleanup()
    return {"cleaned": cleaned, "retention_days": retention_days}


@router.get("/traces/stats")
async def traces_stats() -> dict:
    """查看 Telemetry 统计信息。"""
    from app.core.telemetry import tracer
    stats = tracer.stats()
    stats["buffer_size"] = len(tracer._buffer)  # noqa: SLF001
    return stats


# ---- 会话管理接口 ----

@router.post("/sessions/cleanup")
async def cleanup_sessions(
    max_age_days: float = Query(90.0, ge=1, le=365, description="最大存活天数"),
    min_messages: int = Query(0, ge=0, le=100, description="最小消息数"),
    dry_run: bool = Query(True, description="仅统计，不实际删除"),
) -> dict:
    """清理过期或空会话。"""
    from app.memory.session import get_session_store
    store = get_session_store()
    result = await store.cleanup_expired(
        max_age_days=max_age_days,
        min_messages=min_messages if dry_run else 0,
    )
    if dry_run:
        result["dry_run"] = True
        result["message"] = f"统计结果: 将删除 {result['deleted']} 个会话"
    else:
        result["dry_run"] = False
        result["message"] = f"已删除 {result['deleted']} 个会话"
    return result
