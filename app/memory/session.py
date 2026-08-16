"""会话记忆:基于 SQLite 的持久化会话历史,重启不丢失。

接口与内存版一致(get_or_create / add_message / get_history / clear)。
生产环境可替换为 Redis,接口保持一致。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Optional

from app.config import settings

# 单条消息 {"role": "user"/"assistant", "content": str, ...}
_DB_LOCK = threading.Lock()


class SessionStore:
    """SQLite 会话存储,按 session_id 隔离,含历史容量限制。"""

    def __init__(self, db_path: Optional[str] = None, max_messages: int = 60):
        self._db_path = db_path or os.path.join(settings.resolved_data_dir, "sessions.db")
        self._max_messages = max_messages
        self._lock = asyncio.Lock()
        self._init_db()

    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        messages TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                # 迁移: 旧库补充 title 列(自定义会话名,空 = 未命名)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
                if "title" not in cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
                conn.commit()
            finally:
                conn.close()

    async def get_or_create(self, session_id: str):
        now = time.time()
        with _DB_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT messages, created_at, updated_at FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO sessions (id, messages, created_at, updated_at) VALUES (?,?,?,?)",
                        (session_id, json.dumps([]), now, now),
                    )
                    conn.commit()
                    messages = []
                    created = now
                else:
                    messages = json.loads(row["messages"])
                    created = row["created_at"]
            finally:
                conn.close()
        return {
            "id": session_id,
            "messages": messages,
            "created_at": created,
            "updated_at": now,
        }

    async def add_message(self, session_id: str, role: str, content: str):
        session = await self.get_or_create(session_id)
        messages = list(session["messages"])
        messages.append({"role": role, "content": content})
        if len(messages) > self._max_messages:
            messages = messages[-self._max_messages:]
        now = time.time()
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE sessions SET messages=?, updated_at=? WHERE id=?",
                    (json.dumps(messages, ensure_ascii=False), now, session_id),
                )
                conn.commit()
            finally:
                conn.close()
        return session

    async def get_history(self, session_id: str, last_n: Optional[int] = None) -> list[dict]:
        session = await self.get_or_create(session_id)
        messages = list(session["messages"])
        if last_n is not None:
            messages = messages[-last_n:]
        return messages

    async def clear(self, session_id: str) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                conn.commit()
            finally:
                conn.close()

    async def search(self, q: str, limit: int = 20) -> list[dict]:
        """在全部会话消息中做大小写不敏感全文检索,返回命中片段。"""
        q = q.strip()
        if not q:
            return []
        low_q = q.lower()
        sessions = []
        with _DB_LOCK:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, messages, created_at, updated_at, title FROM sessions "
                    "ORDER BY updated_at DESC LIMIT 500"
                ).fetchall()
            finally:
                conn.close()
        out: list[dict] = []
        for row in rows:
            try:
                msgs = json.loads(row["messages"])
            except Exception:  # noqa: BLE001
                continue
            custom = (row["title"] or "").strip()
            title = custom
            snippets: list[dict] = []
            for i, m in enumerate(msgs):
                content = str(m.get("content", ""))
                if low_q not in content.lower():
                    continue
                if not title:
                    title = content.strip()[:30]
                pos = content.lower().find(low_q)
                start = max(0, pos - 60)
                end = min(len(content), pos + len(q) + 60)
                snippets.append({
                    "index": i,
                    "role": m.get("role", ""),
                    "snippet": f"...{content[start:end]}...",
                })
            if snippets:
                out.append({
                    "session_id": row["id"],
                    "title": title or "新会话",
                    "updated_at": row["updated_at"],
                    "snippets": snippets[:5],
                })
                if len(out) >= limit:
                    break
        return out

    async def rename(self, session_id: str, title: str) -> None:
        """设置会话自定义名称(空串表示清除自定义名,回到自动标题)。"""
        await self.get_or_create(session_id)
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                    (title.strip(), time.time(), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    async def list_sessions(self, limit: int = 200) -> list[dict]:
        """列出所有会话(含标题、消息数、更新时间),按最近更新倒序。"""
        with _DB_LOCK:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, messages, created_at, updated_at, title FROM sessions "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        out: list[dict] = []
        for row in rows:
            msgs = json.loads(row["messages"])
            custom = (row["title"] or "").strip()
            auto = ""
            for m in msgs:
                if m.get("role") == "user" and m.get("content"):
                    auto = str(m["content"]).strip()[:30]
                    break
            out.append({
                "id": row["id"],
                "title": custom or auto or "新会话",
                "custom_title": custom,
                "messages": len(msgs),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return out


def _select_session_store():
    """按配置选择会话存储实现(SQLite 或 Redis),导入时求值一次。"""
    try:  # noqa: PLW0129
        from app.core.storage import get_session_backend

        backend, store = get_session_backend()
        if backend == "redis":
            return store
    except Exception:  # noqa: BLE001
        pass
    return SessionStore()


session_store = _select_session_store()

# 运行时选中的后端缓存:首次成功解析后复用,避免每次请求新建 Redis 客户端。
# Redis 中途不可用时由调用方捕获并回退 session_store(SQLite)。
_runtime_store = None


def get_session_store():
    """运行时选择可用后端(Redis 可用则缓存,否则回退 SQLite singleton)。

    与模块导入时求值的 session_store 相比:
    - Redis 在进程启动后才就绪时,此函数仍能在首次调用时选中 Redis;
    - 选中后缓存客户端(带连接池),避免每个请求重建。
    """
    global _runtime_store
    if _runtime_store is not None:
        return _runtime_store
    try:  # noqa: PLW0129
        from app.core.storage import get_session_backend

        backend, store = get_session_backend()
        if backend == "redis":
            _runtime_store = store
            return store
    except Exception:  # noqa: BLE001
        pass
    return session_store