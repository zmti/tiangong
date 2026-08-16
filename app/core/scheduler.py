"""定时任务调度器:支持 Cron 表达式和简单间隔调度。"""
from __future__ import annotations

import asyncio
import json
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
        
        self.status = TaskStatus.PENDING
        self.last_run: Optional[datetime] = None
        self.next_run: Optional[datetime] = None
        self.run_count: int = 0
        self.last_error: Optional[str] = None
        self.created_at = datetime.now()
        
        # 计算下次执行时间
        self._calculate_next_run()
    
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
        if self.next_run is None:
            return False
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
        """执行任务。"""
        func = self._functions.get(task.func_name)
        if not func:
            raise ValueError(f"函数不存在: {task.func_name}")
        
        task.status = TaskStatus.RUNNING
        task.last_run = datetime.now()
        
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*task.args, **task.kwargs)
            else:
                result = func(*task.args, **task.kwargs)
            
            task.status = TaskStatus.COMPLETED
            task.run_count += 1
            task.last_error = None
            return result
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.last_error = str(e)
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
        """调度循环。"""
        while self._running:
            async with self._lock:
                for task in list(self._tasks.values()):
                    if task.should_run():
                        try:
                            await self.execute_task(task)
                        except Exception as e:
                            print(f"任务执行失败: {task.name} - {e}")
            
            await asyncio.sleep(1)
    
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
                )
                task.status = TaskStatus(task_data.get("status", "pending"))
                task.run_count = task_data.get("run_count", 0)
                if task_data.get("last_run"):
                    task.last_run = datetime.fromisoformat(task_data["last_run"])
                self._tasks[task_id] = task
        except Exception as e:
            print(f"加载任务失败: {e}")


# 全局调度器实例
_scheduler: Optional[TaskScheduler] = None


def get_scheduler() -> TaskScheduler:
    """获取全局调度器。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler
