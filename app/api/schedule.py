"""定时任务接口:列出 / 创建 / 暂停 / 恢复 / 立即执行 / 删除(桌面端定时任务面板)。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.tools.schedule_tool import _get_scheduler

router = APIRouter(prefix="/v1/schedules", tags=["schedule"])


class ScheduleCreate(BaseModel):
    name: str = Field(..., description="任务名称")
    func_name: str = Field(..., description="要执行的函数名(get_current_time/calculate/fetch_webpage)")
    cron_expr: str = Field("", description="Cron 表达式,如 '0 9 * * *'")
    interval_seconds: float = Field(0, description="执行间隔(秒),与 cron_expr 二选一")
    description: str = Field("", description="任务描述")
    enabled: bool = Field(True, description="是否立即启用")


@router.get("")
async def list_schedules(enabled_only: bool = False) -> dict:
    scheduler = _get_scheduler()
    tasks = scheduler.list_tasks(enabled_only=enabled_only)
    return {"tasks": [t.to_dict() for t in tasks], "total": len(tasks)}


@router.post("")
async def create_schedule(req: ScheduleCreate) -> dict:
    scheduler = _get_scheduler()
    try:
        task = scheduler.add_task(
            name=req.name,
            func_name=req.func_name,
            cron_expr=req.cron_expr or None,
            interval_seconds=req.interval_seconds if req.interval_seconds > 0 else None,
            enabled=req.enabled,
            description=req.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"task": task.to_dict()}


@router.get("/{task_id}")
async def get_schedule(task_id: str) -> dict:
    task = _get_scheduler().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {"task": task.to_dict()}


@router.post("/{task_id}/pause")
async def pause_schedule(task_id: str) -> dict:
    ok = _get_scheduler().pause_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {"task_id": task_id, "status": "paused"}


@router.post("/{task_id}/resume")
async def resume_schedule(task_id: str) -> dict:
    ok = _get_scheduler().resume_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {"task_id": task_id, "status": "resumed"}


@router.post("/{task_id}/run")
async def run_schedule(task_id: str) -> dict:
    scheduler = _get_scheduler()
    task = scheduler.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    result = await scheduler.run_task_now(task_id)
    if result.get("status") == "error":
        return {"task_id": task_id, "status": "error", "error": result.get("error", "")}
    return {"task_id": task_id, "status": "ok", "result": result.get("result")}


@router.delete("/{task_id}")
async def delete_schedule(task_id: str) -> dict:
    ok = _get_scheduler().remove_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {"task_id": task_id, "deleted": True}
