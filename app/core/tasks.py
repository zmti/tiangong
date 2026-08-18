"""后台任务管理器:长期自主运行的 Agent 任务(独立于 HTTP 请求)。

设计:
- /v1/tasks 创建任务后,TaskManager 在后台 asyncio 任务里执行
  Agent.run_stream,支持"计划→执行→验证"的自主循环,无需一直挂起请求。
- 每个任务维护一个 asyncio.Queue 事件流,SSE 订阅方持续消费;
  事件同时落盘到 tasks.db,进程重启后仍可查询历史。
- 完成后把答案写入会话历史(session_store),实现"主动汇报"。

安全:任务数通过 settings 限制;取消即发给任务一个取消标志,在图节点
边界检查并中断。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from app.config import settings


class TaskRecord:
    def __init__(
        self,
        task_id: str,
        session_id: str,
        message: str,
        history: Optional[list[dict]] = None,
        mode: str = "auto",
        use_history: bool = True,
        provider: str = "",
    ):
        self.task_id = task_id
        self.session_id = session_id
        self.message = message
        self.history = history or []
        self.mode = mode
        self.use_history = use_history
        self.provider = provider
        self.report_to_session: bool = True
        self.status: str = "queued"  # queued|running|done|failed|cancelled
        self.answer: str = ""
        self.error: str = ""
        self.steps: list[dict] = []
        self.model: str = ""
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "message": self.message[:200],
            "status": self.status,
            "answer": self.answer[:500] if self.answer else "",
            "error": self.error[:300] if self.error else "",
            "model": self.model,
            "steps": len(self.steps),
            "tools": sum(1 for s in self.steps if s.get("type") == "tool"),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }


class TaskManager:
    """进程内后台任务注册表 + SQLite 持久化 + 事件流。

    事件流:每个任务一个 asyncio.Queue;SSE 订阅者轮流排队消费,
    没有订阅者时事件照常落盘(不丢失)。队列有界,超限丢弃中间 thinking 事件。
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(settings.resolved_data_dir, "tasks.db")
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running: int = 0
        self._init_db()
        self._reload()

    # ---- 持久化 ----

    def _connect(self):
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        session_id TEXT,
                        task_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _reload(self) -> None:
        """进程重启后从落盘恢复任务元数据(状态标记为上次值)。"""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT task_id, task_json FROM tasks ORDER BY updated_at DESC LIMIT 200"
                ).fetchall()
            finally:
                conn.close()
        for task_id, task_json in rows:
            try:
                data = json.loads(task_json)
                rec = TaskRecord(
                    task_id=data["task_id"],
                    session_id=data.get("session_id", ""),
                    message=data.get("message", ""),
                )
                rec.status = data.get("status", "unknown")
                rec.answer = data.get("answer", "")
                rec.error = data.get("error", "")
                rec.model = data.get("model", "")
                rec.steps = data.get("steps", [])
                rec.created_at = data.get("created_at", 0)
                rec.updated_at = data.get("updated_at", 0)
                rec.started_at = data.get("started_at")
                rec.finished_at = data.get("finished_at")
                self._tasks[task_id] = rec
            except Exception:  # noqa: BLE001
                continue

    def _persist(self, rec: TaskRecord) -> None:
        rec.updated_at = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks (task_id, session_id, task_json, updated_at) VALUES (?,?,?,?)",
                    (rec.task_id, rec.session_id, json.dumps(rec.to_dict(), ensure_ascii=False), rec.updated_at),
                )
                conn.commit()
            finally:
                conn.close()

    # ---- 创建 / 查询 ----

    def create(
        self,
        session_id: str,
        message: str,
        history: Optional[list[dict]] = None,
        mode: str = "auto",
        use_history: bool = True,
        provider: str = "",
    ) -> TaskRecord:
        rec = TaskRecord(
            task_id=uuid.uuid4().hex,
            session_id=session_id,
            message=message,
            history=history or [],
            mode=mode,
            use_history=use_history,
            provider=provider,
        )
        with self._lock:
            self._tasks[rec.task_id] = rec
            self._queues[rec.task_id] = asyncio.Queue(maxsize=500)
            self._cancel_flags[rec.task_id] = asyncio.Event()
        self._persist(rec)
        return rec

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def list(self, limit: int = 100) -> list[dict]:
        recs = sorted(
            self._tasks.values(), key=lambda r: r.updated_at, reverse=True
        )
        return [r.to_dict() for r in recs[:limit]]

    # ---- 运行 ----

    def can_start(self) -> bool:
        """并发上限检查:达到 agent_task_max_concurrent 时拒绝新任务(0=不限制)。"""
        limit = max(0, int(settings.agent_task_max_concurrent or 0))
        return limit == 0 or self._running < limit

    async def start(self, task_id: str) -> None:
        """后台执行任务(顺便派发队列消费)。

        并发上限由 can_start + 计数器保证;任务失败按 agent_task_retries
        自动重试(指数退避),重试耗尽才标记失败。进程内保留事件流,重启后
        已恢复的任务标记为 failed(避免重复执行非幂等动作)。
        """
        rec = self.get(task_id)
        if rec is None:
            return
        if not self.can_start():
            rec.status = "failed"
            rec.error = "后台任务并发已满,请稍后再试"
            self._persist(rec)
            await self._emit(task_id, {"type": "error", "message": rec.error})
            await self._emit(task_id, {"type": "task_done", "task_id": task_id,
                                       "status": rec.status, "answer": rec.answer})
            return

        rec.status = "running"
        rec.started_at = time.time()
        rec.error = ""
        self._persist(rec)
        with self._lock:
            self._running += 1
        try:
            max_retries = max(0, int(settings.agent_task_retries or 0))
            delay = max(0.0, float(settings.agent_task_retry_delay or 0))
            last_err = ""
            for attempt in range(max_retries + 1):
                cancel_flag = self._cancel_flags.get(task_id)
                if cancel_flag and cancel_flag.is_set():
                    rec.status = "cancelled"
                    rec.error = "任务已被用户取消"
                    break
                try:
                    await asyncio.wait_for(
                        self._drive(task_id),
                        timeout=max(60, int(settings.agent_task_max_minutes) * 60),
                    )
                    if rec.status != "running":
                        break
                    rec.status = "done"
                    break
                except asyncio.TimeoutError:
                    rec.status = "failed"
                    rec.error = (
                        f"任务运行超过 {settings.agent_task_max_minutes} 分钟上限,已终止"
                    )
                    break
                except asyncio.CancelledError:
                    # 已被 _drive 标记为 cancelled,直接跳出重试循环
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = str(e)
                    rec.status = "failed"
                    rec.error = str(e)[:500]
                    if attempt < max_retries:
                        rec.status = "running"
                        rec.error = ""
                        rec.steps = []
                        self._persist(rec)
                        await self._emit(task_id, {
                            "type": "retry",
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "message": f"任务失败,{delay * (2 ** attempt):.0f}s 后自动重试: {last_err[:200]}",
                        })
                        await asyncio.sleep(delay * (2 ** attempt))
                        continue
                    break
            await self._emit(task_id, {"type": "error" if rec.error else "info",
                                       "message": rec.error or "任务完成"})
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)
            if rec.status == "running":
                rec.status = "done"
            rec.finished_at = time.time()
            self._persist(rec)
            await self._emit(task_id, {"type": "task_done", "task_id": task_id,
                                       "status": rec.status, "answer": rec.answer})
            await self._drain(task_id)
            # 清理任务资源,防止内存泄漏
            self.cleanup_task(task_id)

    async def _drive(self, task_id: str) -> None:
        rec = self.get(task_id)
        if rec is None:
            return
        from app.core.agent import Agent
        from app.core.llm import LLMClient

        try:
            llm = LLMClient(rec.provider) if rec.provider else LLMClient()
        except Exception as e:  # noqa: BLE001
            rec.error = f"模型配置错误: {e}"
            rec.status = "failed"
            return
        agent = Agent(
            llm=llm,
            registry=None,
            session_id=rec.session_id,
            mode=rec.mode,
        )
        # 注入取消标志(asyncio.Event),支持 LLM-call 级中断
        cancel_event = self._cancel_flags.get(task_id)
        if cancel_event is not None:
            agent._cancel_flag = cancel_event
        history = rec.history if rec.use_history else []
        total = 0
        # A8: token 预算硬控制
        max_tokens = int(getattr(settings, "agent_task_max_tokens", 0) or 0)
        consumed_tokens = 0
        prev_total_tokens = 0  # 记录上一轮的累计值,计算增量
        try:
            async for event in agent.run_stream(rec.message, history=history):
                cancel_flag = self._cancel_flags.get(task_id)
                if cancel_flag is not None and cancel_flag.is_set():
                    rec.status = "cancelled"
                    rec.error = "任务已被用户取消"
                    break
                total += 1
                # 累计本次 run_stream 过程中产生的 token 用量(增量模式,避免重复累加)
                if getattr(llm, "last_usage", None):
                    cur_total = int(llm.last_usage.get("total_tokens", 0) or 0)
                    delta = max(0, cur_total - prev_total_tokens)
                    prev_total_tokens = cur_total
                    consumed_tokens += delta
                    if max_tokens > 0 and consumed_tokens >= max_tokens:
                        rec.status = "failed"
                        rec.error = f"任务消耗 token({consumed_tokens}) 超过上限({max_tokens}),已终止"
                        await self._emit(task_id, {"type": "error", "message": rec.error})
                        break
                await self._emit(task_id, event)
                if event.get("type") == "tool":
                    rec.steps.append(event)
                if event.get("type") == "done":
                    rec.steps = event.get("steps", rec.steps)
                    rec.model = event.get("model", rec.model)
                if total > 5000:  # 兜底防刷屏
                    break
        except asyncio.CancelledError:
            rec.status = "cancelled"
            rec.error = "任务已被用户取消(LLM-call 级中断)"
        except Exception as e:  # noqa: BLE001
            rec.error = str(e)[:500]
            rec.status = "failed"
        if rec.status == "running":
            rec.status = "done"
        rec.answer = next((s.get("content", "") for s in reversed(rec.steps)
                           if s.get("type") in ("answer", "verify_rewrite", "ensemble_judge")), "")

        # 主动汇报:把任务结果写入会话历史
        try:
            if rec.report_to_session:
                from app.memory.session import get_session_store

                store = get_session_store()
                await store.add_message(rec.session_id, "user", f"[后台任务] {rec.message}")
                if rec.answer:
                    await store.add_message(rec.session_id, "assistant", rec.answer)
        except Exception:  # noqa: BLE001
            pass

        # 异步发送 Webhook(不阻塞任务完成)
        try:
            from app.core.webhook import notify_task_done
            import asyncio as _asyncio

            _asyncio.create_task(notify_task_done(task_id=task_id, status=rec.status, answer=rec.answer))
        except Exception:  # noqa: BLE001
            pass

    async def _emit(self, task_id: str, event: dict) -> None:
        q = self._queues.get(task_id)
        if q is not None:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 退避拥挤:丢弃中间事件,不影响落盘

    async def _drain(self, task_id: str) -> None:
        """任务结束:把剩余事件清空,供 SSE 订阅者收尾。"""
        q = self._queues.get(task_id)
        if q is not None:
            while True:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def subscribe(self, task_id: str):
        """SSE 订阅者:持续产出事件 dict,任务结束后返回。"""
        q = self._queues.get(task_id)
        rec = self.get(task_id)
        if q is None or rec is None:
            return
        while True:
            try:
                event = q.get_nowait()
            except asyncio.QueueEmpty:
                if rec.status in ("done", "failed", "cancelled") and q.empty():
                    return
                await asyncio.sleep(0.15)
                continue
            yield event
            if event.get("type") == "task_done":
                return

    def cancel(self, task_id: str) -> bool:
        if task_id not in self._cancel_flags:
            return False
        self._cancel_flags[task_id].set()
        return True

    def cleanup_task(self, task_id: str) -> None:
        """清理任务相关资源(取消标志、队列),防止内存泄漏。"""
        with self._lock:
            self._cancel_flags.pop(task_id, None)
            self._queues.pop(task_id, None)
            # 同时清理内存中的任务记录,防止长时间运行积累
            self._tasks.pop(task_id, None)


_task_manager: Optional[TaskManager] = None
_task_manager_lock = threading.Lock()


def get_task_manager() -> TaskManager:
    """工厂:单例 TaskManager。"""
    global _task_manager
    if _task_manager is not None:
        return _task_manager
    with _task_manager_lock:
        if _task_manager is None:
            _task_manager = TaskManager()
    return _task_manager


# ---- 定期触发(可选,复用 scheduler 的 cron 能力) ----

def schedule_task_recurring(task_id: str, cron_expr: str, max_runs: int = 0) -> bool:
    """把一个后台任务注册为 cron 定期触发(通过全局调度器的注册函数)。

    返回 False 表示 cron 表达式非法或注册失败;成功返回 True。
    每次触发调用 TaskManager.start(task_id),并在超限后自动停用。
    """
    try:
        from app.core.scheduler import get_scheduler
        import croniter  # noqa: F401  # 仅用于表达式校验

        croniter.croniter(cron_expr)
    except Exception:  # noqa: BLE001
        return False
    try:
        scheduler = get_scheduler()

        run_times = {"n": 0, "max": max_runs}

        async def _trigger() -> dict:
            _mgr = get_task_manager()
            rec = _mgr.get(task_id)
            if rec is None:
                return {"ran": False, "reason": "task_gone"}
            if run_times["max"] and run_times["n"] >= run_times["max"]:
                _mgr.cancel(task_id)
                return {"ran": False, "reason": "max_runs_reached"}
            run_times["n"] += 1
            await _mgr.start(task_id)
            return {"ran": True, "round": run_times["n"]}

        scheduler.register_function(f"bg_task_{task_id}", _trigger)
        scheduler.add_task(
            name=f"后台任务 {task_id[:6]}",
            func_name=f"bg_task_{task_id}",
            cron_expr=cron_expr,
            enabled=True,
            description="定期触发后台 Agent 任务(由 /v1/tasks 创建)",
        )
        return True
    except Exception:  # noqa: BLE001
        return False