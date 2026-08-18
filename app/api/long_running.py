"""长时自主任务 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.long_running import get_long_running_manager

router = APIRouter(prefix="/v1/long-running", tags=["long-running"])


def _mgr():
    try:
        return get_long_running_manager()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"长时任务管理器不可用: {e}") from e


@router.get("")
async def list_tasks(
    limit: int = Query(50, ge=1, le=200),
    status: str = Query("", description="按状态过滤: queued/running/done/failed/cancelled"),
) -> dict:
    """列出所有长时任务。"""
    mgr = _mgr()
    tasks = mgr.list(limit=limit, status=status or None)
    stats = mgr.get_stats()
    return {"tasks": tasks, "stats": stats, "total": len(tasks)}


@router.post("")
async def create_task(
    name: str = Query(..., description="任务名称"),
    prompt: str = Query(..., description="任务提示/指令"),
    mode: str = Query("auto", description="执行模式:auto/research/code"),
    max_duration_minutes: int = Query(120, ge=1, le=1440, description="最大执行时长(分钟)"),
    checkpoint_interval_minutes: int = Query(10, ge=1, le=60, description="周期 checkpoint 间隔(分钟)"),
    retry_max: int = Query(3, ge=0, le=10, description="最大重试次数"),
) -> dict:
    """创建长时自主任务。"""
    mgr = _mgr()
    task = mgr.create(
        name=name,
        prompt=prompt,
        mode=mode,
        max_duration_minutes=max_duration_minutes,
        checkpoint_interval_minutes=checkpoint_interval_minutes,
        retry_max=retry_max,
    )
    return task.to_dict()


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict:
    """单个长时任务详情。"""
    mgr = _mgr()
    task = mgr.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return task.to_dict()


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    """取消任务(仅 queued/running 状态可取消)。"""
    mgr = _mgr()
    ok = mgr.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务无法取消(可能已完成/不存在)")
    return {"task_id": task_id, "cancelled": True}


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> dict:
    """删除任务记录。"""
    mgr = _mgr()
    ok = mgr.delete(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {"task_id": task_id, "deleted": True}


@router.get("/stats")
async def get_stats() -> dict:
    """任务统计摘要。"""
    mgr = _mgr()
    return mgr.get_stats()
