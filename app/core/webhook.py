"""任务完成 Webhook 回调:后台任务/聊天响应完成后,向配置的 HTTP 端点发送通知。

支持多种触发场景:
- 后台任务完成 (task_done)
- 对话完成 (chat_done)
- 中断确认完成 (confirmation_resolved)

持久化重试:发送失败的事件落盘到 data/webhook_queue.db(SQLite),
进程重启后自动重放未送达事件(指数退避,最多 WEBHOOK_RETRIES 次)。

配置方式(.env):
  WEBHOOK_URLS=https://hooks.example.com/agent,https://other.webhook/notify
  WEBHOOK_TIMEOUT=10          # 请求超时秒数
  WEBHOOK_RETRIES=2           # 失败重试次数(含持久化队列)
  WEBHOOK_SECRET=xxx          # HmacSHA256 签名密钥(可选)
  WEBHOOK_EVENTS=task_done,chat_done,confirmation_resolved  # 过滤事件类型
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import httpx
import json
import os
import sqlite3
import threading
import time
from typing import Optional


class WebhookEvent:
    """Webhook 事件载荷。"""

    def __init__(
        self,
        event_type: str,
        payload: dict,
    ):
        self.event_type = event_type
        self.payload = payload
        self.timestamp = time.time()
        self.id = f"wh_{event_type}_{int(time.time() * 1000)}_{os.urandom(4).hex()}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.event_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


class WebhookSender:
    """异步 Webhook 发送器(带重试 + HMAC 签名 + SQLite 持久化队列)。"""

    def __init__(
        self,
        urls: list[str],
        secret: str = "",
        timeout: float = 10.0,
        retries: int = 2,
        events_filter: Optional[set[str]] = None,
        db_path: Optional[str] = None,
    ):
        self._urls = [u.strip() for u in urls if u.strip()]
        self._secret = secret
        self._timeout = timeout
        self._retries = retries
        self._events_filter = events_filter or set()
        self._stats = {"sent": 0, "failed": 0, "skipped": 0, "queued": 0}
        self._lock = threading.Lock()
        self._db_conn: Optional[sqlite3.Connection] = None
        # 用于保护 _drain_queue 和 _enqueue 的数据库操作
        self._queue_lock = threading.Lock()

        # 持久化重试队列(使用共享缓存模式以支持 :memory: 多连接场景)
        self._db_path = db_path or os.path.join(
            __import__("app.config", fromlist=["settings"]).settings.resolved_data_dir,
            "webhook_queue.db",
        )
        self._pending_events: list[dict] = []  # 内存缓冲,定期落盘
        self._init_queue_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取持久化数据库连接(:memory: 用 shared_cache 模式确保多连接共享)。"""
        if self._db_conn is not None:
            return self._db_conn
        # :memory: 必须用 shared_cache URI 才能跨连接共享数据
        if self._db_path == ":memory:":
            conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
            conn.execute("PRAGMA journal_mode=WAL")
        else:
            conn = sqlite3.connect(self._db_path)
        self._db_conn = conn
        return conn

    def _init_queue_db(self) -> None:
        """初始化重试队列表。"""
        try:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS webhook_queue (
                    id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    url TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    next_retry_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (id, url)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wq_status ON webhook_queue(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wq_next ON webhook_queue(next_retry_at)")
            conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def _enqueue(self, event: WebhookEvent) -> None:
        """将失败事件加入持久化重试队列(每个 URL 独立一行)。"""
        with self._queue_lock:
            for url in self._urls:
                try:
                    conn = self._get_conn()
                    conn.execute("""
                        INSERT INTO webhook_queue
                        (id, event_type, payload, url, attempts, max_retries,
                         last_error, created_at, next_retry_at, status)
                        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 'pending')
                        ON CONFLICT(id, url) DO UPDATE SET
                            attempts = excluded.attempts,
                            last_error = excluded.last_error,
                            next_retry_at = excluded.next_retry_at,
                            status = excluded.status
                    """, (
                        event.id, event.event_type,
                        json.dumps(event.to_dict(), ensure_ascii=False),
                        url, self._retries, "",
                        time.time(), time.time(),
                    ))
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass

    def _drain_queue(self) -> int:
        """从持久化队列中取出到期事件并重发,返回成功数。"""
        now = time.time()
        with self._queue_lock:
            try:
                conn = self._get_conn()
                rows = conn.execute(
                    "SELECT id, event_type, payload, url, attempts, max_retries FROM webhook_queue "
                    "WHERE status='pending' AND next_retry_at <= ?", (now,)
                ).fetchall()
                success = 0
                for row in rows:
                    event_data = json.loads(row[2])
                    event = WebhookEvent(row[1], event_data.get("payload", {}))
                    event.id = row[0]  # 保留原始 ID
                    result = self._send_one(event, row[3])
                    if result["ok"]:
                        success += 1
                        conn.execute("DELETE FROM webhook_queue WHERE id=? AND url=?", (row[0], row[3]))
                    else:
                        # 安排下次重试(指数退避)
                        attempts = int(row[4]) + 1
                        max_retries = int(row[5])
                        if attempts > max_retries:
                            # 超过最大重试次数,标记为失败
                            conn.execute(
                                "UPDATE webhook_queue SET status='failed' WHERE id=? AND url=?",
                                (row[0], row[3]),
                            )
                            continue
                        delay = min(60 * (2 ** attempts), 3600)
                        conn.execute("""
                            UPDATE webhook_queue SET
                                attempts=?, last_error=?, next_retry_at=?, status='pending'
                            WHERE id=? AND url=?
                        """, (attempts, result["error"][:300], time.time() + delay, row[0], row[3]))
                conn.commit()
                return success
            except Exception:  # noqa: BLE001
                return 0

    def _send_one(self, event: WebhookEvent, url: str) -> dict:
        """向单个 URL 发送事件。"""
        body = json.dumps(event.to_dict(), ensure_ascii=False)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": event.event_type,
            "X-Webhook-ID": event.id,
            "X-Webhook-Signature": self._sign(body),
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    return {"ok": True}
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def should_send(self, event_type: str) -> bool:
        """检查是否应该发送该事件(基于过滤配置)。"""
        if not self._events_filter:
            return True
        return event_type in self._events_filter

    def _sign(self, body: str) -> str:
        """生成 HmacSHA256 签名。"""
        if not self._secret:
            return ""
        return hmac.new(
            self._secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def send(self, event: WebhookEvent) -> dict:
        """向所有配置的 URL 发送事件(同步发送 + 持久化重试队列兜底)。"""
        if not self._urls:
            return {"skipped": True, "reason": "no_urls_configured"}
        if not self.should_send(event.event_type):
            self._stats["skipped"] += 1
            return {"skipped": True, "reason": f"event {event.event_type} not in filter"}

        all_ok = True
        results = []
        for url in self._urls:
            result = self._send_one(event, url)
            if not result["ok"]:
                all_ok = False
                # 失败时入持久化队列
                self._enqueue(event)
            results.append({
                "url": url,
                "ok": result["ok"],
                "error": result.get("error", "") if not result["ok"] else "",
            })

        self._stats["sent" if all_ok else "failed"] += 1
        return {
            "event_id": event.id,
            "event_type": event.event_type,
            "results": results,
            "stats": dict(self._stats),
        }

    def get_queue_stats(self) -> dict:
        """查询重试队列状态。"""
        try:
            conn = self._get_conn()
            pending = conn.execute("SELECT COUNT(*) FROM webhook_queue WHERE status='pending'").fetchone()[0]
            return {"queued": pending, "stats": dict(self._stats)}
        except Exception:  # noqa: BLE001
            return {"queued": 0, "stats": dict(self._stats)}

    def drain_queue_sync(self) -> int:
        """同步版排空队列(供调度器定期调用)。"""
        return self._drain_queue()


# 从配置读取(settings 优先,兼容 .env 文件;os.environ 兜底)
def _load_config() -> dict:
    from app.config import settings as _cfg

    urls_str = os.environ.get("WEBHOOK_URLS", "") or _cfg.webhook_urls
    urls = [u.strip() for u in urls_str.split(",") if u.strip()] if urls_str else []
    events_str = os.environ.get("WEBHOOK_EVENTS", "") or _cfg.webhook_events
    return {
        "urls": urls,
        "secret": os.environ.get("WEBHOOK_SECRET", "") or _cfg.webhook_secret,
        "timeout": float(os.environ.get("WEBHOOK_TIMEOUT", str(_cfg.webhook_timeout))),
        "retries": int(os.environ.get("WEBHOOK_RETRIES", str(_cfg.webhook_retries))),
        "events": set(events_str.split(",")) if events_str else set(),
    }


_sender: Optional[WebhookSender] = None
_drain_task: Optional[object] = None


def get_webhook_sender() -> Optional[WebhookSender]:
    """懒加载单例:有配置时返回 sender,否则返回 None。"""
    global _sender
    if _sender is None:
        cfg = _load_config()
        if not cfg["urls"]:
            return None
        _sender = WebhookSender(
            urls=cfg["urls"],
            secret=cfg["secret"],
            timeout=cfg["timeout"],
            retries=cfg["retries"],
            events_filter=cfg["events"],
        )
    return _sender


def start_webhook_queue_drainer() -> None:
    """启动定期排空重试队列的后台任务(进程启动时调用)。

    返回后可通过 stop_webhook_queue_drainer() 优雅停止。
    """
    global _drain_task
    if _drain_task is not None:
        return
    sender = get_webhook_sender()
    if sender is None:
        return

    async def _drain_loop():
        while True:
            try:
                sender.drain_queue_sync()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.sleep(30)  # 每 30 秒检查一次
            except asyncio.CancelledError:
                break

    try:
        loop = asyncio.get_running_loop()
        _drain_task = loop.create_task(_drain_loop())
    except RuntimeError:
        # 无运行中的事件循环,延迟启动
        import asyncio as _asyncio
        _drain_task = _asyncio.create_task(_drain_loop())


async def stop_webhook_queue_drainer() -> None:
    """优雅停止 Webhook 队列排空任务。"""
    global _drain_task
    if _drain_task is not None:
        _drain_task.cancel()
        try:
            await _drain_task
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass
        finally:
            _drain_task = None


async def notify_task_done(task_id: str, status: str, answer: str = "") -> None:
    """后台任务完成时发送 Webhook。"""
    sender = get_webhook_sender()
    if sender is None:
        return
    event = WebhookEvent("task_done", {
        "task_id": task_id,
        "status": status,
        "answer_preview": answer[:500],
    })
    await asyncio.to_thread(sender.send, event)


async def notify_chat_done(
    session_id: str,
    run_id: str,
    answer: str,
    model: str,
    steps: list[dict],
) -> None:
    """对话完成时发送 Webhook。"""
    sender = get_webhook_sender()
    if sender is None:
        return
    tool_calls = sum(1 for s in steps if s.get("type") == "tool")
    event = WebhookEvent("chat_done", {
        "session_id": session_id,
        "run_id": run_id,
        "answer_preview": answer[:500],
        "model": model,
        "steps_count": len(steps),
        "tool_calls": tool_calls,
    })
    await asyncio.to_thread(sender.send, event)


async def notify_confirmation_resolved(
    run_id: str,
    session_id: str,
    confirmation_id: str,
    approved: bool,
) -> None:
    """危险工具确认流完成时发送 Webhook。"""
    sender = get_webhook_sender()
    if sender is None:
        return
    event = WebhookEvent("confirmation_resolved", {
        "run_id": run_id,
        "session_id": session_id,
        "confirmation_id": confirmation_id,
        "approved": approved,
    })
    await asyncio.to_thread(sender.send, event)
