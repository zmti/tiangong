"""后台任务接口:创建 / 查询 / 事件流(SSE)/ 取消 / 删除(桌面端任务面板)。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.tasks import get_task_manager
from app.memory.session import get_session_store

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def _store():
    try:  # noqa: PLW0129
        return get_session_store()
    except Exception:  # noqa: BLE001
        from app.memory.session import session_store

        return session_store


class TaskCreateRequest(BaseModel):
    message: str = Field(..., min_length=1, description="任务指令")
    session_id: str = Field("default", description="写入结果的目标会话 ID")
    use_history: bool = Field(True, description="任务运行时是否携带会话历史作为背景")
    mode: str = Field("auto", description="协作模式: auto / single / review / ensemble")
    provider: str = Field("", description="指定模型 provider,留空用配置")
    report_to_session: bool = Field(True, description="完成后把答案写入会话历史(主动汇报)")
    schedule: str = Field("", description="定期触发 cron 表达式(如 0 9 * * 1-5);留空=单次")
    max_runs: int = Field(0, ge=0, description="定期任务最大触发次数,0=无限")


@router.post("")
async def create_task(req: TaskCreateRequest) -> dict:
    """创建后台任务并立即开始后台执行,返回 task_id 供订阅事件流。"""
    if not getattr(__import__("app.config", fromlist=["settings"]).settings,
                   "agent_background_tasks", True):
        raise HTTPException(status_code=400, detail="后台任务已关闭(AGENT_BACKGROUND_TASKS=false)")
    mgr = get_task_manager()
    history = []
    if req.use_history:
        try:
            history = await _store().get_history(req.session_id)
        except Exception:  # noqa: BLE001
            history = []
    rec = mgr.create(
        session_id=req.session_id,
        message=req.message,
        history=history if req.use_history else [],
        mode=req.mode,
        use_history=req.use_history,
        provider=req.provider,
    )
    rec.report_to_session = req.report_to_session

    import asyncio

    schedule = (req.schedule or "").strip()
    if schedule:
        # 定期任务:由 scheduler 调度执行(避免单独线程),scheduled=True 仅入队不立即跑
        from app.core.tasks import schedule_task_recurring

        ok = schedule_task_recurring(
            task_id=rec.task_id,
            cron_expr=schedule,
            max_runs=max(0, int(req.max_runs or 0)),
        )
        if not ok:
            mgr.cancel(rec.task_id)
            raise HTTPException(status_code=400, detail=f"无效的 cron 表达式: {schedule}")
        await _emit_started(mgr, rec, scheduled=True)
        return {
            "task_id": rec.task_id,
            "status": rec.status,
            "session_id": req.session_id,
            "scheduled": True,
            "schedule": schedule,
            "detail": "任务将按 cron 定期触发(可在 /v1/tasks/{id}/events 订阅每次运行)",
        }

    asyncio.create_task(mgr.start(rec.task_id))
    return {"task_id": rec.task_id, "status": rec.status, "session_id": req.session_id}


async def _emit_started(mgr, rec, scheduled: bool = False) -> None:
    await mgr._emit(rec.task_id, {  # noqa: SLF001
        "type": "info",
        "message": "定期任务已注册,等待 cron 触发" if scheduled else "任务已开始",
    })


@router.get("")
async def list_tasks(limit: int = Query(100, ge=1, le=500)) -> dict:
    """列出后台任务(摘要)。"""
    tasks = get_task_manager().list(limit=limit)
    return {"tasks": tasks, "total": len(tasks)}


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict:
    """查询单个任务(状态/答案/步骤数与摘要)。"""
    rec = get_task_manager().get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")
    return rec.to_dict()


@router.get("/{task_id}/events")
async def task_events(task_id: str) -> StreamingResponse:
    """SSE 事件流:订阅任务进度(thinking/tool/plan/answer/error/task_done)。"""
    mgr = get_task_manager()
    rec = mgr.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")

    async def gen():
        async for event in mgr.subscribe(task_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        rec = mgr.get(task_id)
        if rec:
            final = {"type": "task_done", "task_id": task_id,
                     "status": rec.status, "answer": rec.answer}
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    """取消正在运行的后台任务(图节点边界检查取消标志)。"""
    ok = get_task_manager().cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")
    return {"task_id": task_id, "cancelling": True}


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> dict:
    """删除任务记录(不影响运行)。"""
    mgr = get_task_manager()
    rec = mgr.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")
    import os
    import sqlite3

    db = mgr._db_path  # noqa: SLF001
    try:
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return {"task_id": task_id, "deleted": True}