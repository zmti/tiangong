"""会话记忆:基于存储后端的持久化会话历史,重启不丢失。

接口与内存版一致(get_or_create / add_message / get_history / clear)。
支持 SQLite / Redis / PostgreSQL 后端切换。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

# 注意: storage backend 内部已使用 threading.RLock 保证并发安全,
# 此处不再重复加锁;asyncio.Lock 用于协程间互斥而非阻塞事件循环
# _DB_LOCK 已移除,由 storage backend 内部保证线程安全


def sanitize_history_for_llm(history: list[dict]) -> list[dict]:
    """把会话历史清洗成 LLM 安全消息(供记忆摘要/检索等非主链调用方使用)。

    - 自定义角色(error/plan/tool 等)降级为 user, 孤立 tool 消息(无 tool_call_id)
      也降级为 user, 避免 OpenAI 兼容网关 400。
    - 剥掉 kind/id/timestamp/meta 等非 OpenAI 字段。
    - 保留 user/assistant 文本 + 合法的 tool_call 配对, 供 LLM 理解上下文。
    """
    _VALID_ROLES = ("system", "user", "assistant", "tool", "function", "developer")
    out: list[dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        content = m.get("content", "") or m.get("kind", "")
        if not str(content).strip():
            continue
        content = str(content)
        llm_role = role if role in _VALID_ROLES else "user"
        msg: dict = {"role": llm_role, "content": content}
        if llm_role == "tool" and m.get("tool_call_id"):
            msg["tool_call_id"] = m["tool_call_id"]
        elif llm_role == "tool":
            # 孤立 tool 消息 → user
            msg["role"] = "user"
            msg["content"] = "[工具执行结果(历史)]: " + content
        if m.get("tool_calls"):
            msg["tool_calls"] = m["tool_calls"]
        out.append(msg)
    return out


class SessionStore:
    """会话存储,按 session_id 隔离,含历史容量限制。"""

    def __init__(self, db_path: Optional[str] = None, max_messages: int = 500, backend: Optional[StorageBackend] = None):
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
        # 使用 asyncio.Lock 替代 threading.Lock
        self._lock = asyncio.Lock()
        # 初始化数据库需要在事件循环中运行
        try:
            asyncio.get_running_loop()
            asyncio.create_task(self._init_db_async())
        except RuntimeError:
            # 没有运行中的事件循环，同步初始化
            self._init_db_sync()

    def _init_db_sync(self) -> None:
        """同步初始化数据库(用于启动阶段)。"""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._init_db_async())
        finally:
            loop.close()

    async def _init_db_async(self) -> None:
        schema = {
            "sessions": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "tenant_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                {"name": "messages", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "title", "type": "TEXT", "constraints": ""},
                {"name": "category", "type": "TEXT", "constraints": "DEFAULT '未分类'"},
            ],
        }
        self._backend.init_db(schema)
        # 迁移:为旧数据库补填 tenant_id 列
        self._migrate_add_tenant_column()
        # 迁移:为旧数据库补填 category 列
        self._migrate_add_category_column()

    def _migrate_add_tenant_column(self) -> None:
        """迁移:为旧表补加 tenant_id 列(如果不存在)。"""
        try:
            self._backend.execute(
                "ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
            )
        except Exception:
            pass  # 列已存在

    def _migrate_add_category_column(self) -> None:
        """迁移:为旧表补加 category 列(如果不存在)。"""
        try:
            self._backend.execute(
                "ALTER TABLE sessions ADD COLUMN category TEXT NOT NULL DEFAULT '未分类'"
            )
        except Exception:
            pass  # 列已存在

    async def _resolve_tid(self, tenant_id: Optional[str] = None) -> str:
        """解析 tenant_id,无锁。"""
        from app.core.tenant_context import get_tenant
        return tenant_id or get_tenant() or "default"

    def _read_session(self, session_id: str, tid: str) -> dict:
        """从后端读取会话,无锁(调用方须自行加锁)。"""
        now = time.time()
        rows = self._backend.select(
            "sessions",
            "id = ? AND tenant_id = ?",
            (session_id, tid),
            limit=1
        )
        if not rows:
            return {"id": session_id, "tenant_id": tid, "messages": [], "created_at": now, "updated_at": now, "_exists": False}
        row = rows[0]
        return {
            "id": session_id,
            "tenant_id": tid,
            "messages": json.loads(row["messages"]),
            "created_at": row["created_at"],
            "updated_at": row.get("updated_at", now),
            "_exists": True,
        }

    def _ensure_session(self, session_id: str, tid: str) -> dict:
        """确保会话存在(不存在则创建),无锁(调用方须自行加锁)。"""
        now = time.time()
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
            return {"id": session_id, "tenant_id": tid, "messages": [], "created_at": now, "updated_at": now}
        row = rows[0]
        return {
            "id": session_id,
            "tenant_id": tid,
            "messages": json.loads(row["messages"]),
            "created_at": row["created_at"],
            "updated_at": row.get("updated_at", now),
        }

    async def get_or_create(self, session_id: str, tenant_id: Optional[str] = None):
        tid = await self._resolve_tid(tenant_id)
        async with self._lock:
            return self._ensure_session(session_id, tid)

    async def add_message(self, session_id: str, role: str, kind: str = "text", content: Optional[str] = None, meta: Optional[dict] = None, tenant_id: Optional[str] = None):
        tid = await self._resolve_tid(tenant_id)
        now = time.time()
        async with self._lock:
            session = self._ensure_session(session_id, tid)
            messages = list(session["messages"])

            msg = {
                # 修复: 消息 ID 用 uuid4 保证全局唯一(旧实现 time.time()+id(self)
                # 在同一 store 实例下 id(self) 恒定, 同一毫秒内多条消息 ID 相同,
                # 导致去重/截断无法区分消息)
                "id": uuid.uuid4().hex,
                "role": role,
                "kind": kind,
                "content": content or "",
                "timestamp": now,
            }
            if meta:
                msg["meta"] = meta
            messages.append(msg)
            if len(messages) > self._max_messages:
                messages = messages[-self._max_messages:]
            self._backend.update(
                "sessions",
                {
                    "messages": json.dumps(messages, ensure_ascii=False),
                    "updated_at": now,
                },
                "id = ? AND tenant_id = ?",
                (session_id, tid)
            )
        return {"id": session_id, "tenant_id": tid, "messages": messages, "created_at": now, "updated_at": now}

    async def update_message(self, session_id: str, msg_id: str, content: str, meta: Optional[dict] = None, tenant_id: Optional[str] = None) -> bool:
        """原地更新指定消息的内容(用于流式场景实时保存 assistant 回复)。"""
        tid = await self._resolve_tid(tenant_id)
        now = time.time()
        async with self._lock:
            session = self._ensure_session(session_id, tid)
            messages = list(session["messages"])
            updated = False
            for i, m in enumerate(messages):
                if m.get("id") == msg_id:
                    messages[i] = {**m, "content": content, "timestamp": now}
                    if meta is not None:
                        messages[i]["meta"] = meta
                    updated = True
                    break
            if updated:
                self._backend.update(
                    "sessions",
                    {"messages": json.dumps(messages, ensure_ascii=False), "updated_at": now},
                    "id = ? AND tenant_id = ?",
                    (session_id, tid),
                )
            return updated

    async def get_history(self, session_id: str, last_n: Optional[int] = None) -> list[dict]:
        tid = await self._resolve_tid()
        async with self._lock:
            session = self._read_session(session_id, tid)
            messages = list(session["messages"])
            if last_n is not None:
                messages = messages[-last_n:]
            return messages

    async def clear(self, session_id: str) -> None:
        """清空会话消息但保留会话记录。"""
        async with self._lock:
            self._backend.update(
                "sessions",
                {"messages": json.dumps([], ensure_ascii=False), "updated_at": time.time()},
                "id = ?",
                (session_id,),
            )

    async def delete(self, session_id: str) -> bool:
        """删除整个会话记录。"""
        async with self._lock:
            n = self._backend.delete("sessions", "id = ?", (session_id,))
            return n > 0

    async def _extract_content(self, m: dict) -> str:
        """从新旧兼容的消息结构中提取可检索的文本内容。"""
        # 旧结构: {"role": "...", "content": "..."}
        # 新结构: {"role": "...", "kind": "...", "content": "..."} 或 tool kind 的 content 可能是 dict
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, dict):
            # tool kind 可能把 arguments/result 合并为文本,或返回空
            return str(c.get("content", c.get("result", "")))
        return ""

    async def search(self, q: str, limit: int = 20) -> list[dict]:
        """在全部会话消息中做大小写不敏感全文检索,返回命中片段。"""
        q = q.strip()
        if not q:
            return []
        low_q = q.lower()

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
                content = await self._extract_content(m)
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
                    "kind": m.get("kind", ""),
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
        tid = await self._resolve_tid()
        async with self._lock:
            self._ensure_session(session_id, tid)
            self._backend.update(
                "sessions",
                {
                    "title": title.strip(),
                    "updated_at": time.time(),
                },
                "id = ?",
                (session_id,)
            )

    async def set_category(self, session_id: str, category: str) -> None:
        """设置会话分类。"""
        tid = await self._resolve_tid()
        async with self._lock:
            self._ensure_session(session_id, tid)
            self._backend.update(
                "sessions",
                {
                    "category": category.strip() or "未分类",
                    "updated_at": time.time(),
                },
                "id = ?",
                (session_id,)
            )

    async def list_sessions(self, limit: int = 200) -> list[dict]:
        """列出当前租户的所有会话(含标题、消息数、更新时间、分类),按最近更新倒序。"""
        tid = await self._resolve_tid()
        rows = self._backend.select(
            "sessions",
            "tenant_id = ?",
            (tid,),
            order_by="updated_at DESC",
            limit=limit
        )

        out: list[dict] = []
        for row in rows:
            msgs = json.loads(row["messages"])
            custom = (row.get("title") or "").strip()
            auto = ""
            for m in msgs:
                # 兼容旧结构(role/content)和新结构(kind/content)
                content = await self._extract_content(m)
                if m.get("role") == "user" and content:
                    auto = content.strip()[:30]
                    break
            # 如果无自定义标题且无自动标题,用创建时间生成默认标题
            if not custom and not auto:
                ts = row.get("created_at", 0) or row.get("updated_at", 0) or 0
                if ts:
                    import datetime as _dt
                    dt = _dt.datetime.fromtimestamp(ts)
                    auto = dt.strftime("%m-%d %H:%M")
            out.append({
                "id": row["id"],
                "title": custom or auto or "新会话",
                "custom_title": custom,
                "messages": len(msgs),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "category": row.get("category") or "未分类",
            })
        return out

    async def cleanup_expired(
        self,
        max_age_days: float = None,
        min_messages: int = 0,
    ) -> dict:
        """清理过期会话。
        
        Args:
            max_age_days: 会话最大存活天数，超过则删除。None 表示不清理。
            min_messages: 最小消息数，低于此值的会话也视为空会话清理。
            
        Returns:
            清理统计信息
        """
        import time as _time
        from app.config import settings
        
        max_age_days = max_age_days or float(getattr(settings, "session_retention_days", 90))
        cutoff = _time.time() - (max_age_days * 86400)
        
        # 获取所有会话(注意: SqliteBackend.select 不支持 columns 参数, 需自行取需要的列)
        rows = self._backend.select("sessions", "1 = 1")
        
        to_delete = []
        for row in rows:
            session_id = row["id"]
            updated_at = row.get("updated_at", 0) or 0
            
            # 检查是否过期
            if updated_at < cutoff:
                to_delete.append(session_id)
                continue
            
            # 检查是否空会话
            if min_messages > 0:
                try:
                    msgs = json.loads(row["messages"] or "[]")
                    if len(msgs) < min_messages:
                        to_delete.append(session_id)
                except (json.JSONDecodeError, TypeError):
                    to_delete.append(session_id)
        
        # 执行删除
        deleted = 0
        for session_id in to_delete:
            await self.delete(session_id)
            deleted += 1
        
        logger.info("会话清理完成: 删除 %d 个过期/空会话", deleted)
        return {
            "total_checked": len(rows),
            "deleted": deleted,
            "kept": len(rows) - deleted,
        }

    async def archive_session(self, session_id: str) -> bool:
        """归档会话（压缩为摘要并删除详细消息）。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            是否成功归档
        """
        tid = await self._resolve_tid()
        async with self._lock:
            session = self._read_session(session_id, tid)
            messages = session.get("messages", [])
            
            if not messages:
                return True
            
            # 提取摘要信息
            first_message = ""
            last_message = ""
            message_count = len(messages)
            
            for m in messages:
                content = await self._extract_content(m)
                if m.get("role") == "user" and content:
                    if not first_message:
                        first_message = content.strip()[:100]
                    last_message = content.strip()[:100]
            
            # 保留摘要，删除详细消息
            summary = {
                "role": "system",
                "kind": "summary",
                "content": f"[会话摘要] 共 {message_count} 条消息\n首次: {first_message}\n最后: {last_message}",
            }
            
            tid = session.get("tenant_id", "default")
            now = time.time()
            self._backend.update(
                "sessions",
                {
                    "messages": json.dumps([summary], ensure_ascii=False),
                    "updated_at": now,
                },
                "id = ? AND tenant_id = ?",
                (session_id, tid)
            )
            
            logger.info("会话已归档: %s (%d 条消息压缩为摘要)", session_id, message_count)
            return True


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