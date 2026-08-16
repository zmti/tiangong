"""定时任务工具:创建、管理、执行定时任务。"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


def _get_scheduler():
    """获取调度器并注册内置函数。"""
    from app.core.scheduler import get_scheduler
    
    scheduler = get_scheduler()
    
    # 注册内置函数
    if not scheduler._functions:
        from app.tools.builtin import get_current_time, calculate
        from app.tools.web_tool import fetch_webpage
        
        scheduler.register_function("get_current_time", get_current_time.func)
        scheduler.register_function("calculate", calculate.func)
        scheduler.register_function("fetch_webpage", fetch_webpage.func)
        
        # 注册自定义任务执行函数
        async def run_scheduled_task(task_id: str):
            return await scheduler.run_task_now(task_id)
        
        scheduler.register_function("run_scheduled_task", run_scheduled_task)
    
    return scheduler


@tool("create_schedule", "创建定时任务", timeout=10)
def create_schedule(
    name: str,
    func_name: str,
    cron_expr: str = "",
    interval_seconds: float = 0,
    description: str = "",
    enabled: bool = True,
) -> dict:
    """
    创建定时任务。
    
    Args:
        name: 任务名称
        func_name: 要执行的函数名(get_current_time/calculate/fetch_webpage)
        cron_expr: Cron 表达式(如 "0 9 * * *" 表示每天9点)
        interval_seconds: 执行间隔(秒),与 cron_expr 二选一
        description: 任务描述
        enabled: 是否立即启用
    
    Returns:
        创建结果
    """
    scheduler = _get_scheduler()
    
    try:
        task = scheduler.add_task(
            name=name,
            func_name=func_name,
            cron_expr=cron_expr if cron_expr else None,
            interval_seconds=interval_seconds if interval_seconds > 0 else None,
            enabled=enabled,
            description=description,
        )
        return {"status": "ok", "task": task.to_dict()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool("list_schedules", "列出所有定时任务", timeout=10)
def list_schedules(
    enabled_only: bool = False,
) -> dict:
    """
    列出所有定时任务。
    
    Args:
        enabled_only: 是否只列出启用的任务
    
    Returns:
        任务列表
    """
    scheduler = _get_scheduler()
    tasks = scheduler.list_tasks(enabled_only=enabled_only)
    
    return {
        "total": len(tasks),
        "tasks": [t.to_dict() for t in tasks],
    }


@tool("get_schedule", "获取定时任务详情", timeout=10)
def get_schedule(
    task_id: str,
) -> dict:
    """
    获取指定任务的详细信息。
    
    Args:
        task_id: 任务 ID
    
    Returns:
        任务详情
    """
    scheduler = _get_scheduler()
    task = scheduler.get_task(task_id)
    
    if task is None:
        return {"error": f"任务不存在: {task_id}"}
    
    return {"task": task.to_dict()}


@tool("delete_schedule", "删除定时任务", timeout=10)
def delete_schedule(
    task_id: str,
) -> dict:
    """
    删除定时任务。
    
    Args:
        task_id: 任务 ID
    
    Returns:
        删除结果
    """
    scheduler = _get_scheduler()
    
    if scheduler.remove_task(task_id):
        return {"status": "ok", "message": f"任务 {task_id} 已删除"}
    else:
        return {"status": "error", "error": f"任务不存在: {task_id}"}


@tool("pause_schedule", "暂停定时任务", timeout=10)
def pause_schedule(
    task_id: str,
) -> dict:
    """
    暂停定时任务。
    
    Args:
        task_id: 任务 ID
    
    Returns:
        操作结果
    """
    scheduler = _get_scheduler()
    
    if scheduler.pause_task(task_id):
        return {"status": "ok", "message": f"任务 {task_id} 已暂停"}
    else:
        return {"status": "error", "error": f"任务不存在: {task_id}"}


@tool("resume_schedule", "恢复定时任务", timeout=10)
def resume_schedule(
    task_id: str,
) -> dict:
    """
    恢复定时任务。
    
    Args:
        task_id: 任务 ID
    
    Returns:
        操作结果
    """
    scheduler = _get_scheduler()
    
    if scheduler.resume_task(task_id):
        return {"status": "ok", "message": f"任务 {task_id} 已恢复"}
    else:
        return {"status": "error", "error": f"任务不存在: {task_id}"}


@tool("run_schedule_now", "立即执行定时任务", timeout=30)
def run_schedule_now(
    task_id: str,
) -> dict:
    """
    立即执行指定任务(不等待调度)。
    
    Args:
        task_id: 任务 ID
    
    Returns:
        执行结果
    """
    import asyncio
    scheduler = _get_scheduler()
    result = asyncio.run(scheduler.run_task_now(task_id))
    return result


@tool("run_custom_task", "执行自定义定时任务", timeout=30)
def run_custom_task(
    func_name: str,
    cron_expr: str = "",
    interval_seconds: float = 0,
    task_name: str = "临时任务",
) -> dict:
    """
    创建并立即执行一个临时任务。
    
    Args:
        func_name: 要执行的函数名
        cron_expr: Cron 表达式(可选,用于创建定时任务)
        interval_seconds: 执行间隔(可选,用于创建定时任务)
        task_name: 任务名称
    
    Returns:
        执行结果
    """
    import asyncio
    from app.core.scheduler import get_scheduler
    
    scheduler = get_scheduler()
    
    # 检查函数是否存在
    if func_name not in scheduler._functions:
        return {"error": f"函数不存在: {func_name}"}
    
    # 如果指定了调度参数,创建定时任务
    if cron_expr or interval_seconds > 0:
        task = scheduler.add_task(
            name=task_name,
            func_name=func_name,
            cron_expr=cron_expr if cron_expr else None,
            interval_seconds=interval_seconds if interval_seconds > 0 else None,
            enabled=True,
        )
        return {"status": "ok", "task": task.to_dict(), "message": "任务已创建"}
    
    # 否则直接执行
    func = scheduler._functions[func_name]
    try:
        if asyncio.iscoroutinefunction(func):
            result = asyncio.run(func())
        else:
            result = func()
        return {"status": "ok", "result": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}
