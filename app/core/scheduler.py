"""定时任务调度器:支持 Cron 表达式和简单间隔调度。

增强(A4):
- 任务依赖链:支持 depends_on 字段,前置任务完成后才执行后续任务
- 指数退避重试:任务失败后按 2^attempt 延迟重试,最多 RETRY_MAX_ATTEMPTS 次
- 高精度轮询:支持 <1s 间隔(最低 0.1s)
- 任务执行日志:每次执行记录开始/结束时间、耗时、输出摘要
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional

import croniter


class TaskStatus(str, Enum):
    """任务状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class ScheduledTask:
    """定时任务。"""

    # 最大重试次数(失败后指数退避重试)
    MAX_RETRIES = 5
    # 退避基础延迟(秒),第 N 次重试延迟 = base * 2^(N-1)
    RETRY_BASE_DELAY = 2.0

    def __init__(
        self,
        task_id: str,
        name: str,
        func_name: str,
        args: tuple = (),
        kwargs: dict = None,
        cron_expr: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        enabled: bool = True,
        description: str = "",
        depends_on: Optional[list[str]] = None,  # A4: 前置任务依赖
        retry_max: int = 3,  # A4: 最大重试次数
    ):
        self.task_id = task_id
        self.name = name
        self.func_name = func_name
        self.args = args
        self.kwargs = kwargs or {}
        self.cron_expr = cron_expr
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.description = description
        self.depends_on = depends_on or []  # A4: 前置任务 ID 列表
        self.retry_max = min(retry_max, self.MAX_RETRIES)  # A4: 上限

        self.status = TaskStatus.PENDING
        self.last_run: Optional[datetime] = None
        self.next_run: Optional[datetime] = None
        self.run_count: int = 0
        self.last_error: Optional[str] = None
        self.created_at = datetime.now()
        # A4: 执行日志
        self._exec_log: list[dict] = []  # 最近 10 条执行记录
        # A4: 当前重试状态
        self._retry_count: int = 0
        self._retry_at: float = 0.0

        # 计算下次执行时间
        self._calculate_next_run()

    def _add_exec_log(self, status: str, duration: float, error: str = "") -> None:
        """记录一条执行日志。"""
        entry = {
            "status": status,
            "duration_s": round(duration, 3),
            "error": error[:200] if error else "",
            "timestamp": time.time(),
        }
        self._exec_log.append(entry)
        # 只保留最近 10 条
        if len(self._exec_log) > 10:
            self._exec_log = self._exec_log[-10:]
        # 重置重试计数
        self._retry_count = 0
        self._retry_at = 0.0

    def should_retry(self) -> bool:
        """检查是否需要重试(指数退避)。"""
        if self._retry_count >= self.retry_max:
            return False
        now = time.time()
        if now < self._retry_at:
            return True
        # 计算下次重试时间
        delay = self.RETRY_BASE_DELAY * (2 ** self._retry_count)
        self._retry_at = now + delay
        return True

    def record_retry(self) -> None:
        """记录一次重试。"""
        self._retry_count += 1

    def _calculate_next_run(self):
        """计算下次执行时间。"""
        now = datetime.now()

        if self.cron_expr:
            try:
                cron = croniter.croniter(self.cron_expr, now)
                self.next_run = cron.get_next(datetime)
            except Exception:
                self.next_run = now + timedelta(seconds=60)
        elif self.interval_seconds:
            if self.last_run:
                self.next_run = self.last_run + timedelta(seconds=self.interval_seconds)
            else:
                self.next_run = now + timedelta(seconds=self.interval_seconds)
        else:
            self.next_run = None

    def should_run(self) -> bool:
        """检查是否应该执行。"""
        if not self.enabled:
            return False
        # A4: 检查前置依赖是否已完成
        if self.depends_on:
            from app.core.scheduler import get_scheduler
            sched = get_scheduler()
            for dep_id in self.depends_on:
                dep_task = sched.get_task(dep_id)
                if dep_task is None or dep_task.status != TaskStatus.COMPLETED:
                    return False
        if self.next_run is None:
            return False
        # A4: 如果在重试窗口内,检查是否到了重试时间
        if self._retry_count > 0 and self._retry_at > 0:
            return datetime.fromtimestamp(self._retry_at) <= datetime.now()
        return datetime.now() >= self.next_run

    def to_dict(self) -> dict:
        """转换为字典。"""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "func_name": self.func_name,
            "args": self.args,
            "kwargs": self.kwargs,
            "cron_expr": self.cron_expr,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "description": self.description,
            "status": self.status.value,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            # A4: 新增字段
            "depends_on": self.depends_on,
            "retry_max": self.retry_max,
            "retry_count": self._retry_count,
            "exec_log": self._exec_log[-5:],  # 最近 5 条
        }


class TaskScheduler:
    """任务调度器。"""
    
    def __init__(self, task_file: Optional[str] = None):
        self._tasks: dict[str, ScheduledTask] = {}
        self._functions: dict[str, Callable] = {}
        self._running = False
        from app.config import settings

        self._task_file = task_file or os.path.join(settings.resolved_data_dir, "scheduled_tasks.json")
        self._lock = asyncio.Lock()
    
    def register_function(self, name: str, func: Callable):
        """注册可调度的函数。"""
        self._functions[name] = func
    
    def add_task(
        self,
        name: str,
        func_name: str,
        args: tuple = (),
        kwargs: dict = None,
        cron_expr: Optional[str] = None,
        interval_seconds: Optional[float] = None,
        enabled: bool = True,
        description: str = "",
        depends_on: Optional[list[str]] = None,  # A4: 前置任务依赖
        retry_max: int = 3,  # A4: 最大重试次数
    ) -> ScheduledTask:
        """添加定时任务。"""
        if func_name not in self._functions:
            raise ValueError(f"未注册的函数: {func_name}")

        # 验证 cron 表达式
        if cron_expr:
            try:
                croniter.croniter(cron_expr)
            except Exception as e:
                raise ValueError(f"无效的 cron 表达式: {e}")

        # A4: 验证前置依赖
        if depends_on:
            for dep_id in depends_on:
                if dep_id not in self._tasks:
                    raise ValueError(f"前置任务不存在: {dep_id}")

        if not cron_expr and not interval_seconds:
            raise ValueError("必须指定 cron_expr 或 interval_seconds")

        task_id = str(uuid.uuid4())[:8]
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            func_name=func_name,
            args=args,
            kwargs=kwargs,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            enabled=enabled,
            description=description,
            depends_on=depends_on or [],
            retry_max=retry_max,
        )

        self._tasks[task_id] = task
        self._save_tasks()
        return task
    
    def remove_task(self, task_id: str) -> bool:
        """移除任务。"""
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save_tasks()
            return True
        return False
    
    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        """获取任务。"""
        return self._tasks.get(task_id)
    
    def list_tasks(self, enabled_only: bool = False) -> list[ScheduledTask]:
        """列出所有任务。"""
        tasks = list(self._tasks.values())
        if enabled_only:
            tasks = [t for t in tasks if t.enabled]
        return tasks
    
    def pause_task(self, task_id: str) -> bool:
        """暂停任务。"""
        task = self._tasks.get(task_id)
        if task:
            task.enabled = False
            task.status = TaskStatus.PAUSED
            self._save_tasks()
            return True
        return False
    
    def resume_task(self, task_id: str) -> bool:
        """恢复任务。"""
        task = self._tasks.get(task_id)
        if task:
            task.enabled = True
            task.status = TaskStatus.PENDING
            task._calculate_next_run()
            self._save_tasks()
            return True
        return False
    
    async def execute_task(self, task: ScheduledTask) -> Any:
        """执行任务(含指数退避重试)。"""
        func = self._functions.get(task.func_name)
        if not func:
            raise ValueError(f"函数不存在: {task.func_name}")

        task.status = TaskStatus.RUNNING
        task.last_run = datetime.now()
        t0 = time.time()

        for attempt in range(task.retry_max + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*task.args, **task.kwargs)
                else:
                    result = func(*task.args, **task.kwargs)

                task.status = TaskStatus.COMPLETED
                task.run_count += 1
                task.last_error = None
                duration = time.time() - t0
                task._add_exec_log("completed", duration)
                return result
            except Exception as e:
                duration = time.time() - t0
                if attempt < task.retry_max and task.should_retry():
                    task.record_retry()
                    task.last_error = str(e)[:200]
                    task._add_exec_log(f"retry_{attempt+1}", duration, str(e))
                    task.status = TaskStatus.FAILED
                    # 等待退避延迟
                    delay = ScheduledTask.RETRY_BASE_DELAY * (2 ** (attempt))
                    await asyncio.sleep(min(delay, 60))  # 最长等 60s
                    task._calculate_next_run()
                    continue
                else:
                    task.status = TaskStatus.FAILED
                    task.last_error = str(e)[:200]
                    task._add_exec_log("failed", duration, str(e))
                    raise
            finally:
                task._calculate_next_run()
                self._save_tasks()
    
    async def run_task_now(self, task_id: str) -> dict:
        """立即执行任务。"""
        task = self._tasks.get(task_id)
        if not task:
            return {"error": f"任务不存在: {task_id}"}
        
        try:
            result = await self.execute_task(task)
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    
    async def _run_loop(self):
        """调度循环(A4: 支持高精度轮询最低 0.1s)。"""
        while self._running:
            async with self._lock:
                for task in list(self._tasks.values()):
                    if task.should_run():
                        try:
                            await self.execute_task(task)
                        except Exception as e:
                            print(f"任务执行失败: {task.name} - {e}")

            # A4: 高精度轮询,最低 0.1s
            await asyncio.sleep(0.1)
    
    def start(self):
        """启动调度器。"""
        if not self._running:
            self._running = True
            self._load_tasks()
            asyncio.create_task(self._run_loop())
    
    def stop(self):
        """停止调度器。"""
        self._running = False
    
    def _save_tasks(self):
        """保存任务到文件。"""
        import os
        os.makedirs(os.path.dirname(self._task_file), exist_ok=True)
        
        data = {}
        for task_id, task in self._tasks.items():
            data[task_id] = task.to_dict()
        
        with open(self._task_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _load_tasks(self):
        """从文件加载任务。"""
        import os
        if not os.path.exists(self._task_file):
            return
        
        try:
            with open(self._task_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            for task_id, task_data in data.items():
                task = ScheduledTask(
                    task_id=task_id,
                    name=task_data.get("name", ""),
                    func_name=task_data.get("func_name", ""),
                    args=tuple(task_data.get("args", [])),
                    kwargs=task_data.get("kwargs", {}),
                    cron_expr=task_data.get("cron_expr"),
                    interval_seconds=task_data.get("interval_seconds"),
                    enabled=task_data.get("enabled", True),
                    description=task_data.get("description", ""),
                    depends_on=task_data.get("depends_on") or [],
                    retry_max=task_data.get("retry_max", 3),
                )
                task.status = TaskStatus(task_data.get("status", "pending"))
                task.run_count = task_data.get("run_count", 0)
                if task_data.get("last_run"):
                    task.last_run = datetime.fromisoformat(task_data["last_run"])
                # A4: 恢复执行日志
                exec_log = task_data.get("exec_log")
                if exec_log:
                    task._exec_log = exec_log
                self._tasks[task_id] = task
        except Exception as e:
            print(f"加载任务失败: {e}")


# 全局调度器实例
_scheduler: Optional[TaskScheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> TaskScheduler:
    """获取全局调度器。"""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = TaskScheduler()
    return _scheduler
