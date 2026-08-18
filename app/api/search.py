"""实时搜索 API:/v1/search/*"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.get("")
async def search(
    q: str = Query(..., description="搜索关键词"),
    max_results: int = Query(10, ge=1, le=20),
    use_paid: bool = Query(False, description="优先付费搜索"),
) -> dict:
    """实时搜索入口。"""
    from app.tools.search_tool import search_realtime
    import asyncio

    results = asyncio.run(search_realtime(q, max_results, use_paid))
    return {
        "query": q,
        "results_count": len(results),
        "results": results,
    }


@router.get("/news")
async def news(
    topic: str = Query(..., description="新闻主题"),
    max_results: int = Query(5, ge=1, le=15),
) -> dict:
    """新闻搜索。"""
    from app.tools.search_tool import news_search
    import asyncio

    results = asyncio.run(news_search(topic, max_results))
    return results


@router.get("/suggest")
async def suggestions(
    q: str = Query(..., description="搜索词根"),
    limit: int = Query(5, ge=1, le=10),
) -> dict:
    """搜索建议。"""
    from app.tools.search_tool import search_duckduckgo
    import asyncio

    # DuckDuckGo 不提供自动补全 API,简单回退
    try:
        results = asyncio.run(search_duckduckgo(q, max_results=limit))
    except Exception:  # noqa: BLE001
        results = []
    return {"query": q, "suggestions": [r.get("title", "")[:80] for r in results[:limit]]}


@router.get("/status")
async def search_status() -> dict:
    """搜索服务状态。"""
    from app.config import settings
    config = {
        "provider": settings.search_provider,
        "api_key_configured": bool(settings.search_api_key),
        "fallback_available": True,  # DuckDuckGo 始终可用
    }
    return {"status": "ok", "config": config}
