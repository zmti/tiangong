"""长期记忆:跨会话记住事实、偏好、结论,对话前检索注入上下文。

表结构:
  memories(id TEXT PRIMARY KEY, scope TEXT, session_id TEXT, content TEXT,
           keywords TEXT, source TEXT, hit_count INTEGER, created_at REAL,
           updated_at REAL, vector TEXT)

scope:
  global   -> 所有会话可见(默认)
  session  -> 仅指定 session 可见
检索:向量余弦相似度(复用 rag 嵌入器)为主、关键词覆盖率为辅的混合打分;
历史遗留无向量的记忆自动回退关键词打分,并在检索时懒补向量。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

from app.config import settings
from app.rag.embeddings import _keyword_tokens
from app.rag.store import cosine_similarity

# 向量分与关键词分的权重(混合打分)
VEC_WEIGHT = 0.75
KW_WEIGHT = 0.25
# 无关键词命中时,纯向量匹配的最低余弦阈值(过滤哈希嵌入噪声;
# API 嵌入下真正相关文本的余弦通常 >0.4)
MIN_VEC_SCORE = 0.2

_DB_LOCK = threading.RLock()  # RLock: 构造器内部会再次获取同一把锁(非重入锁会死锁)
_instance = None


class MemoryStore:
    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(settings.resolved_data_dir, "memory.db")
        self._lock = threading.Lock()
        self._mem_conn = None  # 仅用于 :memory:
        self._init_db()

    def _connect(self):
        import sqlite3

        if self._db_path == ":memory:":
            if self._mem_conn is None:
                # 工具经 asyncio.to_thread 在子线程执行,共享连接需允许跨线程使用;
                # 并发安全由 self._lock 保证
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _close(self, conn) -> None:
        if self._db_path != ":memory:":
            conn.close()

    def _init_db(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL DEFAULT 'global',
                        session_id TEXT,
                        content TEXT NOT NULL,
                        keywords TEXT,
                        source TEXT DEFAULT 'manual',
                        hit_count INTEGER DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)"
                )
                cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
                if "vector" not in cols:
                    conn.execute("ALTER TABLE memories ADD COLUMN vector TEXT")
                conn.commit()
            finally:
                self._close(conn)

    def save(
        self,
        content: str,
        keywords: Optional[list[str]] = None,
        scope: str = "global",
        session_id: Optional[str] = None,
        source: str = "manual",
        memory_id: Optional[str] = None,
    ) -> dict:
        """保存一条记忆。返回记录 dict。"""
        content = (content or "").strip()
        if not content:
            return {"error": "记忆内容不能为空"}
        mid = memory_id or uuid.uuid4().hex
        now = time.time()
        kw = [k for k in (keywords or []) if str(k).strip()]
        vec = None
        try:
            from app.rag.embeddings import get_embedder, run_embed

            vec = run_embed(get_embedder(), [content])[0]
        except Exception:  # noqa: BLE001 嵌入失败不影响记忆保存
            vec = None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memories
                    (id, scope, session_id, content, keywords, source, created_at, updated_at, vector)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (mid, scope, session_id, content, json.dumps(kw, ensure_ascii=False), source, now, now,
                     json.dumps(vec) if vec is not None else None),
                )
                conn.commit()
            finally:
                self._close(conn)
        return {"id": mid, "scope": scope, "content": content, "keywords": kw}

    def delete(self, memory_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                self._close(conn)

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        keywords: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """更新一条记忆的内容/关键词,并重新计算向量。返回更新后的记录;不存在返回 None。"""
        row = self._get_row(memory_id)
        if row is None:
            return None
        new_content = str(content) if content is not None else row["content"]
        new_kw = list(keywords) if keywords is not None else json.loads(row["keywords"] or "[]")
        vec = None
        try:
            from app.rag.embeddings import get_embedder, run_embed

            vec = run_embed(get_embedder(), [new_content])[0]
        except Exception:  # noqa: BLE001 嵌入失败不影响更新
            vec = None
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE memories SET content=?, keywords=?, vector=?, updated_at=? WHERE id=?",
                    (
                        new_content.strip(),
                        json.dumps(new_kw, ensure_ascii=False),
                        json.dumps(vec) if vec is not None else None,
                        time.time(),
                        memory_id,
                    ),
                )
                conn.commit()
            finally:
                self._close(conn)
        return self.get(memory_id)

    def get(self, memory_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
            finally:
                self._close(conn)
        return self._row_to_dict(row) if row else None

    def _get_row(self, memory_id: str):
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM memories WHERE id=?", (memory_id,)
                ).fetchone()
            finally:
                self._close(conn)
        return row

    def clear(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM memories")
                conn.commit()
                return cur.rowcount
            finally:
                self._close(conn)

    def list(self, limit: int = 50, scope: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM memories"
        params: list = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                self._close(conn)
        return [self._row_to_dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                manual = conn.execute("SELECT COUNT(*) FROM memories WHERE source='manual'").fetchone()[0]
                auto = conn.execute("SELECT COUNT(*) FROM memories WHERE source='auto'").fetchone()[0]
                hits = conn.execute("SELECT SUM(hit_count) FROM memories").fetchone()[0] or 0
            finally:
                self._close(conn)
        return {"total": total, "manual": manual, "auto": auto, "hits": hits}

    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
        count_hit: bool = False,
    ) -> list[dict]:
        """混合检索:向量余弦相似度 + 关键词覆盖率。

        默认只检索 global 作用域;显式传 scope="session" 才检索会话级记忆。
        count_hit=True 时对被命中的记忆增加 hit_count(用于统计常用记忆)。
        历史遗留无向量的记忆回退关键词打分,并在此处懒补嵌入。
        """
        if scope is None:
            scope = "global"
        sql = "SELECT * FROM memories WHERE scope=?"
        params: list = [scope]
        if scope == "session" and session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        sql += " ORDER BY updated_at DESC LIMIT 200"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                self._close(conn)
        mems = [self._row_to_dict(r, with_vector=True) for r in rows]
        if not mems:
            return []

        q_tokens = set(_keyword_tokens(query or ""))
        q_vec = None
        try:
            from app.rag.embeddings import get_embedder, run_embed

            q_vec = run_embed(get_embedder(), [query])[0]
        except Exception:  # noqa: BLE001 嵌入失败回退纯关键词
            q_vec = None

        backfill: list[tuple[dict, str]] = []
        scored = []
        for mem in mems:
            content_tokens = set(_keyword_tokens(mem["content"]))
            kw_tokens = set(mem["keywords"])
            tokens = content_tokens | kw_tokens
            kw_score = 0.0
            if q_tokens and tokens:
                overlap = len(q_tokens & tokens)
                kw_score = overlap / len(q_tokens) + 0.01 * overlap
            vec = mem.get("vector")
            if q_vec is not None and vec is not None:
                cos = cosine_similarity(q_vec, vec)
                if cos <= 0 and kw_score <= 0:
                    continue
                if kw_score <= 0 and cos < MIN_VEC_SCORE:
                    continue
                score = VEC_WEIGHT * max(cos, 0.0) + KW_WEIGHT * kw_score
            elif kw_score > 0:
                score = kw_score
            else:
                continue
            scored.append((score, mem))
            if q_vec is not None and vec is None and len(backfill) < 10:
                backfill.append((mem, mem["id"]))
        if backfill:
            try:
                from app.rag.embeddings import get_embedder, run_embed

                vectors = run_embed(get_embedder(), [m["content"] for m, _ in backfill])
                for (mem, mid), v in zip(backfill, vectors):
                    self._set_vector(mid, v)
                    mem["vector"] = v
            except Exception:  # noqa: BLE001 补嵌入失败不影响本次检索
                pass
        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for score, mem in scored[:top_k]:
            result.append({**mem, "vector": None, "relevance": round(score, 4)})
        if count_hit and result:
            self._hit([m["id"] for m in result])
        return result

    def _set_vector(self, memory_id: str, vector: list[float]) -> None:
        """为记忆写入向量(懒补嵌入用)。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE memories SET vector=? WHERE id=?",
                    (json.dumps(vector), memory_id),
                )
                conn.commit()
            finally:
                self._close(conn)

    def _hit(self, memory_ids: list[str]) -> None:
        """命中计数(用于统计哪个记忆常被使用)。"""
        if not memory_ids:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "UPDATE memories SET hit_count = hit_count + 1 WHERE id=?",
                    [(i,) for i in memory_ids],
                )
                conn.commit()
            finally:
                self._close(conn)

    @staticmethod
    def _row_to_dict(row, with_vector: bool = False) -> dict:
        d = {
            "id": row["id"],
            "scope": row["scope"],
            "session_id": row["session_id"],
            "content": row["content"],
            "keywords": json.loads(row["keywords"] or "[]"),
            "source": row["source"],
            "hit_count": row["hit_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_vector:
            d["vector"] = json.loads(row["vector"]) if row["vector"] else None
        return d


def get_memory_store() -> MemoryStore:
    """单例。"""
    global _instance
    if _instance is None:
        with _DB_LOCK:
            if _instance is None:
                _instance = MemoryStore()
    return _instance
