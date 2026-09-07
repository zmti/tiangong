"""长时自主循环:支持小时/天级后台 Agent 任务。

设计:
- 与现有 /v1/tasks 短任务区分:长时任务可持续数小时,通过周期 checkpoint
  保持状态不丢失,支持断点续跑和进度查询。
- 任务状态: queued / running / paused / done / failed / cancelled
- 每个长时任务有独立 session_id,消息落盘到 session store
- 支持最大执行时长、周期检查点、失败重试
- 后台 asyncio 任务循环执行,主进程不阻塞 HTTP 请求

使用场景:
- 每日报告生成(每天 9 点运行)
- 代码库全量扫描(持续 30 分钟)
- 监控告警自动响应(随时触发)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

# 长时任务数据表
_SCHEMA = {
    "long_running_tasks": [
        {"name": "task_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "prompt", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "status", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'queued'"},
        {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
        {"name": "mode", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'auto'"},
        {"name": "max_duration_minutes", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 120"},
        {"name": "checkpoint_interval_minutes", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 10"},
        {"name": "retry_max", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 3"},
        {"name": "retry_count", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 0"},
        {"name": "progress", "type": "TEXT", "constraints": "NOT NULL DEFAULT '{}'"},
        {"name": "answer", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
        {"name": "error", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
        {"name": "started_at", "type": "REAL", "constraints": ""},
        {"name": "finished_at", "type": "REAL", "constraints": ""},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
    ],
}


class LongRunningTask:
    """长时自主任务记录。"""

    def __init__(
        self,
        task_id: str,
        name: str,
        prompt: str,
        mode: str = "auto",
        max_duration_minutes: int = 120,
        checkpoint_interval_minutes: int = 10,
        retry_max: int = 3,
        session_id: str = "",
    ):
        self.task_id = task_id
        self.name = name
        self.prompt = prompt
        self.mode = mode
        self.max_duration_minutes = max_duration_minutes
        self.checkpoint_interval_minutes = checkpoint_interval_minutes
        self.retry_max = retry_max
        self.session_id = session_id or f"lr_{task_id[:8]}"
        self.status = "queued"
        self.progress: dict = {}
        self.answer = ""
        self.error = ""
        self.retry_count = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self._last_checkpoint = self.created_at

    def to_dict(self) -> dict:
        elapsed = (time.time() - self.started_at) / 60 if self.started_at else 0
        return {
            "task_id": self.task_id,
            "name": self.name,
            "prompt_preview": self.prompt[:200],
            "status": self.status,
            "session_id": self.session_id,
            "mode": self.mode,
            "max_duration_minutes": self.max_duration_minutes,
            "checkpoint_interval_minutes": self.checkpoint_interval_minutes,
            "retry_count": self.retry_count,
            "retry_max": self.retry_max,
            "progress": self.progress,
            "answer_preview": self.answer[:300] if self.answer else "",
            "error": self.error[:300] if self.error else "",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_minutes": round(elapsed, 1),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LongRunningTaskManager:
    """长时任务管理器:持久化 + 后台循环执行 + 周期 checkpoint。"""

    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            db_path = os.path.join(settings.resolved_data_dir, "long_running.db")
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._backend = get_storage_backend(db_path=db_path)
        self._tasks: dict[str, LongRunningTask] = {}
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        self._backend.init_db(_SCHEMA)
        self._backend.execute(
            "CREATE INDEX IF NOT EXISTS idx_lrt_status ON long_running_tasks(status)"
        )
        self._backend.execute(
            "CREATE INDEX IF NOT EXISTS idx_lrt_session ON long_running_tasks(session_id)"
        )

    def _load(self) -> None:
        rows = self._backend.select("long_running_tasks", "1=1", order_by="created_at DESC")
        for row in rows:
            task = self._row_to_task(row)
            self._tasks[task.task_id] = task

    def _row_to_task(self, row: dict) -> LongRunningTask:
        return LongRunningTask(
            task_id=row["task_id"],
            name=row["name"],
            prompt=row["prompt"],
            mode=row.get("mode", "auto"),
            max_duration_minutes=row.get("max_duration_minutes", 120),
            checkpoint_interval_minutes=row.get("checkpoint_interval_minutes", 10),
            retry_max=row.get("retry_max", 3),
            session_id=row.get("session_id", ""),
        )

    def create(
        self,
        name: str,
        prompt: str,
        mode: str = "auto",
        max_duration_minutes: int = 120,
        checkpoint_interval_minutes: int = 10,
        retry_max: int = 3,
    ) -> LongRunningTask:
        task_id = f"lr_{uuid.uuid4().hex[:12]}"
        task = LongRunningTask(
            task_id=task_id,
            name=name,
            prompt=prompt,
            mode=mode,
            max_duration_minutes=max_duration_minutes,
            checkpoint_interval_minutes=checkpoint_interval_minutes,
            retry_max=retry_max,
        )
        self._tasks[task_id] = task
        self._save_task(task)
        return task

    def _save_task(self, task: LongRunningTask) -> None:
        now = time.time()
        progress_json = json.dumps(task.progress, ensure_ascii=False)
        self._backend.upsert(
            "long_running_tasks",
            {
                "task_id": task.task_id,
                "name": task.name,
                "prompt": task.prompt,
                "status": task.status,
                "session_id": task.session_id,
                "mode": task.mode,
                "max_duration_minutes": task.max_duration_minutes,
                "checkpoint_interval_minutes": task.checkpoint_interval_minutes,
                "retry_max": task.retry_max,
                "retry_count": task.retry_count,
                "progress": progress_json,
                "answer": task.answer,
                "error": task.error,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "created_at": task.created_at,
                "updated_at": now,
            },
            conflict_columns=["task_id"],
        )

    def list(self, limit: int = 50, status: Optional[str] = None) -> list[dict]:
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.to_dict() for t in tasks[:limit]]

    def get(self, task_id: str) -> Optional[LongRunningTask]:
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("queued", "running"):
            return False
        task.status = "cancelled"
        task.finished_at = time.time()
        self._save_task(task)
        return True

    def delete(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._backend.delete("long_running_tasks", "task_id = ?", (task_id,))
        return True

    async def start_background_loop(self) -> None:
        """启动后台执行循环。"""
        if self._running:
            return
        self._running = True
        loop = asyncio.get_running_loop()
        self._loop_task = loop.create_task(self._run_loop())
        logger.info("长时任务后台循环已启动")

    async def stop_background_loop(self) -> None:
        """停止后台循环。"""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        logger.info("长时任务后台循环已停止")

    async def _run_loop(self) -> None:
        """后台执行循环:轮询 queued 任务并执行。"""
        while self._running:
            try:
                await self._process_next()
            except Exception as e:
                logger.warning("长时任务循环出错: %s", e)
            await asyncio.sleep(2)

    async def _process_next(self) -> None:
        """处理下一个待执行任务。"""
        queued = [t for t in self._tasks.values() if t.status == "queued"]
        running = [t for t in self._tasks.values() if t.status == "running"]

        # 并发执行上限
        max_concurrent = int(getattr(settings, "agent_max_concurrent_long_running", 3) or 3)
        if len(running) >= max_concurrent:
            return

        if not queued:
            return

        # 取最早的 queued 任务
        task = queued[0]
        task.status = "running"
        task.started_at = time.time()
        task.progress = {"step": "starting", "message": "任务已开始执行"}
        self._save_task(task)

        # 在后台 asyncio 任务中执行
        asyncio.create_task(self._execute_task(task))

    async def _execute_task(self, task: LongRunningTask) -> None:
        """执行单个长时任务(异步,不阻塞循环)。"""
        import asyncio
        from app.core.agent import Agent
        from app.core.telemetry import _current_run_id, tracer

        run_id = f"lr_{task.task_id}"
        _current_run_id.set(run_id)
        tracer.start_run(run_id=run_id, message=task.prompt, session_id=task.session_id)

        try:
            agent = Agent(session_id=task.session_id)
            # 设置超时
            timeout = task.max_duration_minutes * 60
            try:
                result = await asyncio.wait_for(
                    agent.run_stream(message=task.prompt, use_history=True),
                    timeout=timeout,
                )
                task.answer = result.answer if hasattr(result, "answer") else str(result)
                task.progress = {
                    "step": "completed",
                    "steps": len(result.steps) if hasattr(result, "steps") else 0,
                    "message": "执行完成",
                }
                task.status = "done"
                tracer.end_run(run_id=run_id, ok=True)
            except asyncio.TimeoutError:
                task.error = f"执行超时({timeout}s)"
                task.progress = {"step": "timeout", "message": task.error}
                task.status = "failed"
                tracer.end_run(run_id=run_id, ok=False, error=task.error)
            except Exception as e:
                task.error = str(e)[:500]
                task.retry_count += 1
                if task.retry_count < task.retry_max:
                    task.progress = {
                        "step": "retry",
                        "retry_count": task.retry_count,
                        "message": f"执行失败,第{task.retry_count}次重试: {task.error}",
                    }
                    task.status = "queued"
                    task.started_at = None
                    logger.warning("长时任务 %s 失败,准备重试 (%d/%d): %s",
                                   task.task_id, task.retry_count, task.retry_max, task.error)
                else:
                    task.progress = {"step": "failed", "message": task.error}
                    task.status = "failed"
                    tracer.end_run(run_id=run_id, ok=False, error=task.error)

        except Exception as e:
            task.error = str(e)[:500]
            task.status = "failed"
            task.progress = {"step": "error", "message": task.error}
            logger.error("长时任务 %s 执行异常: %s", task.task_id, e)
        finally:
            task.finished_at = time.time()
            task.updated_at = time.time()
            self._save_task(task)

    def get_stats(self) -> dict:
        """返回任务统计。"""
        all_tasks = list(self._tasks.values())
        by_status: dict[str, int] = {}
        for t in all_tasks:
            by_status[t.status] = by_status.get(t.status, 0) + 1
        return {
            "total": len(all_tasks),
            "by_status": by_status,
            "running": sum(1 for t in all_tasks if t.status == "running"),
            "queued": sum(1 for t in all_tasks if t.status == "queued"),
            "done": sum(1 for t in all_tasks if t.status == "done"),
            "failed": sum(1 for t in all_tasks if t.status == "failed"),
            "cancelled": sum(1 for t in all_tasks if t.status == "cancelled"),
        }


# 模块级单例
_manager: Optional[LongRunningTaskManager] = None
_manager_lock = threading.Lock()


def get_long_running_manager() -> LongRunningTaskManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = LongRunningTaskManager()
    return _manager
