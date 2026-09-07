"""Redis 会话存储:与 SQLite SessionStore 接口一致的可选后端。

接口(get_or_create / add_message / get_history / clear / rename / list_sessions)
与 app/memory/session.py 完全一致,由 app/core/storage.get_session_backend 选择。
历史以 JSON 数组存 Redis Hash:key session:{id} field=json。

Redis 不可用时由 storage.get_session_backend 负责回退,本类不处理。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional


class RedisSessionStore:
    def __init__(self, client, max_messages: int = 500, prefix: str = "agent:session"):
        self._client = client
        self._max_messages = max_messages
        self._prefix = prefix
        self._lock = asyncio.Lock()

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}:{session_id}"

    async def get_or_create(self, session_id: str):
        key = self._key(session_id)
        raw = await asyncio.to_thread(self._client.hget, key, "json")
        now = time.time()
        if raw is None:
            # 用带 NX 的 setnx 初始化,并发安全
            done = await asyncio.to_thread(
                self._client.hsetnx, key, "json", json.dumps([])
            )
            if done:
                await asyncio.to_thread(self._client.hset, key, "created_at", now)
                messages = []
                created = now
            else:
                raw = await asyncio.to_thread(self._client.hget, key, "json")
                messages = json.loads(raw or "[]")
                created = float((await asyncio.to_thread(self._client.hget, key, "created_at")) or now)
        else:
            messages = json.loads(raw)
            created = float((await asyncio.to_thread(self._client.hget, key, "created_at")) or now)
        return {"id": session_id, "messages": messages, "created_at": created, "updated_at": now}

    async def add_message(self, session_id: str, role: str, kind: str = "text", content: Optional[str] = None, meta: Optional[dict] = None):
        session = await self.get_or_create(session_id)
        messages = list(session["messages"])
        # 结构化消息: {id, role, kind, content, meta}
        msg = {
            "id": str(time.time()) + str(id(self)),
            "role": role,
            "kind": kind,
            "content": content or "",
        }
        if meta:
            msg["meta"] = meta
        messages.append(msg)
        if len(messages) > self._max_messages:
            messages = messages[-self._max_messages:]
        key = self._key(session_id)
        async with self._lock:
            await asyncio.to_thread(
                self._client.hset, key, "json", json.dumps(messages, ensure_ascii=False)
            )
            await asyncio.to_thread(self._client.hset, key, "updated_at", time.time())
        return session

    async def get_history(self, session_id: str, last_n: Optional[int] = None) -> list[dict]:
        session = await self.get_or_create(session_id)
        messages = list(session["messages"])
        if last_n is not None:
            messages = messages[-last_n:]
        return messages

    async def clear(self, session_id: str) -> None:
        await asyncio.to_thread(self._client.delete, self._key(session_id))

    async def delete(self, session_id: str) -> bool:
        """删除整个会话记录。"""
        key = self._key(session_id)
        n = await asyncio.to_thread(self._client.delete, key)
        return n > 0

    async def _extract_content_redis(self, m: dict) -> str:
        """从新旧兼容的消息结构中提取可检索的文本内容(Redis 专用)。"""
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, dict):
            # tool kind content 可能是 {name, arguments, result} 结构，转文字
            args = c.get("arguments", "")
            result = c.get("result", "")
            if args and result:
                return f"{args} -> {result}"
            return str(c)
        return ""

    async def search(self, q: str, limit: int = 20) -> list[dict]:
        """在全部会话消息中做大小写不敏感全文检索,返回命中片段。"""
        q = q.strip()
        if not q:
            return []
        low_q = q.lower()
        sessions = await self.list_sessions(limit=500)
        out: list[dict] = []
        for s in sessions:
            msgs = await self.get_history(s["id"])
            title = s.get("title") or ""
            snippets: list[dict] = []
            for i, m in enumerate(msgs):
                content = await self._extract_content_redis(m)
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
                    "session_id": s["id"],
                    "title": title or "新会话",
                    "updated_at": s["updated_at"],
                    "snippets": snippets[:5],
                })
                if len(out) >= limit:
                    break
        return out

    async def rename(self, session_id: str, title: str) -> None:
        key = self._key(session_id)
        await self.get_or_create(session_id)
        await asyncio.to_thread(self._client.hset, key, "title", title.strip())
        await asyncio.to_thread(self._client.hset, key, "updated_at", time.time())

    async def list_sessions(self, limit: int = 200) -> list[dict]:
        pattern = f"{self._prefix}:*"
        keys = await asyncio.to_thread(self._client.keys, pattern)
        out: list[dict] = []
        for key in sorted(keys, reverse=True):
            raw = await asyncio.to_thread(self._client.hget, key, "json")
            if raw is None:
                continue
            msgs = json.loads(raw)
            session_id = key.split(":", 2)[2]
            custom = (await asyncio.to_thread(self._client.hget, key, "title")) or ""
            auto = ""
            for m in msgs:
                if m.get("role") == "user" and m.get("content"):
                    auto = str(m["content"]).strip()[:30]
                    break
            updated = float((await asyncio.to_thread(self._client.hget, key, "updated_at")) or 0)
            created = float((await asyncio.to_thread(self._client.hget, key, "created_at")) or updated)
            out.append({
                "id": session_id,
                "title": (custom or auto or "新会话"),
                "custom_title": custom,
                "messages": len(msgs),
                "created_at": created,
                "updated_at": updated,
            })
            if len(out) >= limit:
                break
        out.sort(key=lambda s: s["updated_at"], reverse=True)
        return out[:limit]