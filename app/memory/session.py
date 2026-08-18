"""会话记忆:基于存储后端的持久化会话历史,重启不丢失。

接口与内存版一致(get_or_create / add_message / get_history / clear)。
支持 SQLite / Redis / PostgreSQL 后端切换。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

# 单条消息 {"role": "user"/"assistant", "content": str, ...}
_DB_LOCK = threading.Lock()


class SessionStore:
    """会话存储,按 session_id 隔离,含历史容量限制。"""

    def __init__(self, db_path: Optional[str] = None, max_messages: int = 60, backend: Optional[StorageBackend] = None):
        """初始化 SessionStore
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            max_messages: 最大消息数
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "sessions.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        
        self._max_messages = max_messages
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with _DB_LOCK:
            schema = {
                "sessions": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "tenant_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                    {"name": "messages", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "title", "type": "TEXT", "constraints": ""},
                ],
            }
            self._backend.init_db(schema)
            # 迁移:为旧数据库补填 tenant_id 列
            self._migrate_add_tenant_column()

    def _migrate_add_tenant_column(self) -> None:
        """迁移:为旧表补加 tenant_id 列(如果不存在)。"""
        try:
            self._backend.execute(
                "ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
            )
        except Exception:
            pass  # 列已存在

    async def get_or_create(self, session_id: str, tenant_id: Optional[str] = None):
        from app.core.tenant_context import get_tenant
        tid = tenant_id or get_tenant() or "default"
        now = time.time()
        with _DB_LOCK:
            rows = self._backend.select(
                "sessions",
                "id = ? AND tenant_id = ?",
                (session_id, tid),
                limit=1
            )
            if not rows:
                self._backend.insert(
                    "sessions",
                    {
                        "id": session_id,
                        "tenant_id": tid,
                        "messages": json.dumps([]),
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                messages = []
                created = now
            else:
                row = rows[0]
                messages = json.loads(row["messages"])
                created = row["created_at"]
        return {
            "id": session_id,
            "tenant_id": tid,
            "messages": messages,
            "created_at": created,
            "updated_at": now,
        }

    async def add_message(self, session_id: str, role: str, content: str, tenant_id: Optional[str] = None):
        session = await self.get_or_create(session_id, tenant_id)
        messages = list(session["messages"])
        messages.append({"role": role, "content": content})
        if len(messages) > self._max_messages:
            messages = messages[-self._max_messages:]
        now = time.time()
        tid = tenant_id or session.get("tenant_id", "default")
        with _DB_LOCK:
            self._backend.update(
                "sessions",
                {
                    "messages": json.dumps(messages, ensure_ascii=False),
                    "updated_at": now,
                },
                "id = ? AND tenant_id = ?",
                (session_id, tid)
            )
        return session

    async def get_history(self, session_id: str, last_n: Optional[int] = None) -> list[dict]:
        session = await self.get_or_create(session_id)
        messages = list(session["messages"])
        if last_n is not None:
            messages = messages[-last_n:]
        return messages

    async def clear(self, session_id: str) -> None:
        with _DB_LOCK:
            self._backend.delete("sessions", "id = ?", (session_id,))

    async def search(self, q: str, limit: int = 20) -> list[dict]:
        """在全部会话消息中做大小写不敏感全文检索,返回命中片段。"""
        q = q.strip()
        if not q:
            return []
        low_q = q.lower()
        
        with _DB_LOCK:
            rows = self._backend.select(
                "sessions",
                "1 = 1",
                order_by="updated_at DESC",
                limit=500
            )
        
        out: list[dict] = []
        for row in rows:
            try:
                msgs = json.loads(row["messages"])
            except Exception:  # noqa: BLE001
                continue
            custom = (row.get("title") or "").strip()
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
            self._backend.update(
                "sessions",
                {
                    "title": title.strip(),
                    "updated_at": time.time(),
                },
                "id = ?",
                (session_id,)
            )

    async def list_sessions(self, limit: int = 200) -> list[dict]:
        """列出所有会话(含标题、消息数、更新时间),按最近更新倒序。"""
        with _DB_LOCK:
            rows = self._backend.select(
                "sessions",
                "1 = 1",
                order_by="updated_at DESC",
                limit=limit
            )
        
        out: list[dict] = []
        for row in rows:
            msgs = json.loads(row["messages"])
            custom = (row.get("title") or "").strip()
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
    """按配置选择会话存储实现,导入时求值一次。"""
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
    """运行时选择可用后端(Redis 可用则缓存,否则回退 SQLite singleton)。"""
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