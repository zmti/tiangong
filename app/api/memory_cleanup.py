"""记忆清理与一致性 API。"""
from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter(prefix="/v1/memory-cleanup", tags=["memory-cleanup"])


@router.post("/decay")
async def run_decay(dry_run: bool = Query(True, description="仅模拟不实际删除")) -> dict:
    """执行记忆衰减计算。"""
    from app.core.memory_cleanup import MemoryCleanupEngine
    engine = MemoryCleanupEngine()
    return engine.run_decay(dry_run=dry_run)


@router.post("/cleanup")
async def cleanup(max_age_days: int = Query(365, ge=30, le=3650)) -> dict:
    """清理过期记忆。"""
    from app.core.memory_cleanup import MemoryCleanupEngine
    engine = MemoryCleanupEngine()
    return engine.cleanup_expired(max_age_days=max_age_days)


@router.post("/merge")
async def merge(threshold: float = Query(0.9, ge=0.5, le=0.99)) -> dict:
    """合并高相似度记忆。"""
    from app.core.memory_cleanup import MemoryCleanupEngine
    engine = MemoryCleanupEngine()
    return engine.merge_duplicates(threshold=threshold)


@router.get("/stats")
async def stats() -> dict:
    """记忆清理统计。"""
    from app.core.memory_cleanup import MemoryCleanupEngine
    from app.core.smart_router import get_routing_stats
    from app.core.smart_cache import get_cache

    engine = MemoryCleanupEngine()
    decay = engine.run_decay(dry_run=True)
    return {
        "memory_decay": decay,
        "routing": get_routing_stats(),
        "cache": get_cache().stats(),
    }
