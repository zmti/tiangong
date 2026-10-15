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

import asyncio
import json
import os
import threading
import time
import uuid
from typing import Optional

from app.config import settings
from app.rag.embeddings import _keyword_tokens
from app.rag.store import cosine_similarity
from app.core.storage_backend import StorageBackend, get_storage_backend

# 向量分与关键词分的权重(混合打分)
VEC_WEIGHT = 0.75
KW_WEIGHT = 0.25
# 无关键词命中时,纯向量匹配的最低余弦阈值(过滤哈希嵌入噪声;
# API 嵌入下真正相关文本的余弦通常 >0.4)
# 可通过 settings.rag_min_relevance 覆盖
_MIN_VEC_SCORE = float(getattr(settings, "rag_min_relevance", 0.3))
MIN_VEC_SCORE = max(0.0, min(1.0, _MIN_VEC_SCORE))
# 混合打分后的最低总分数阈值(低于此值不注入上下文)
MIN_TOTAL_SCORE = float(getattr(settings, "rag_multi_min_relevance", 0.0))

# 并发锁：异步路径用 asyncio.Lock（不阻塞事件循环），
# 同步路径（无运行中事件循环时）用 threading.Lock，两者互斥使用。
_DB_ASYNC_LOCK: asyncio.Lock = asyncio.Lock()
_DB_SYNC_LOCK: threading.RLock = threading.RLock()
_instance = None


class MemoryStore:
    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 MemoryStore
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "memory.db")
            # 使用 :memory: 时必须开启 memory_mode，否则每次 connect() 都创建新库
            mem = self._db_path == ":memory:"
            self._backend = get_storage_backend(db_path=self._db_path, memory_mode=mem)
        
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """同步初始化数据库。"""
        with _DB_SYNC_LOCK:
            schema = {
                "memories": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "tenant_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                    {"name": "scope", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'global'"},
                    {"name": "session_id", "type": "TEXT", "constraints": ""},
                    {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "keywords", "type": "TEXT", "constraints": ""},
                    {"name": "source", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'auto'"},
                    {"name": "hit_count", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 0"},
                    {"name": "vector", "type": "TEXT", "constraints": ""},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
            }
            self._backend.init_db(schema)
            # 迁移:为旧数据库补填 tenant_id 列和索引
            self._migrate_add_tenant_column()
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tenant_scope ON memories(tenant_id, scope)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)"
            )

    def _migrate_add_tenant_column(self) -> None:
        """迁移:为旧表补加 tenant_id 列(如果不存在)。"""
        try:
            self._backend.execute(
                "ALTER TABLE memories ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
            )
        except Exception:
            pass  # 列已存在

    async def _init_db_async(self) -> None:
        async with _DB_ASYNC_LOCK:
            schema = {
                "memories": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "tenant_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'default'"},
                    {"name": "scope", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'global'"},
                    {"name": "session_id", "type": "TEXT", "constraints": ""},
                    {"name": "content", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "keywords", "type": "TEXT", "constraints": ""},
                    {"name": "source", "type": "TEXT", "constraints": "DEFAULT 'manual'"},
                    {"name": "hit_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "vector", "type": "TEXT", "constraints": ""},
                ],
            }
            self._backend.init_db(schema)
            # 迁移:为旧数据库补填 tenant_id 列和索引
            await self._migrate_add_tenant_column_async()
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_tenant_scope ON memories(tenant_id, scope)"
            )

    async def _migrate_add_tenant_column_async(self) -> None:
        """迁移:为旧表补加 tenant_id 列(如果不存在)。"""
        try:
            self._backend.execute(
                "ALTER TABLE memories ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
            )
        except Exception:
            pass  # 列已存在

    def save(
        self,
        content: str,
        keywords: Optional[list[str]] = None,
        scope: str = "global",
        session_id: Optional[str] = None,
        source: str = "manual",
        memory_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> dict:
        """保存一条记忆。返回记录 dict。"""
        from app.core.tenant_context import get_tenant
        content = (content or "").strip()
        if not content:
            return {"error": "记忆内容不能为空"}
        mid = memory_id or uuid.uuid4().hex
        now = time.time()
        tid = tenant_id or get_tenant() or "default"
        kw = [k for k in (keywords or []) if str(k).strip()]
        vec = None
        try:
            from app.rag.embeddings import get_embedder, run_embed

            vec = run_embed(get_embedder(), [content])[0]
        except Exception:  # noqa: BLE001 嵌入失败不影响记忆保存
            vec = None

        with _DB_SYNC_LOCK:
            self._backend.upsert(
                "memories",
                {
                    "id": mid,
                    "tenant_id": tid,
                    "scope": scope,
                    "session_id": session_id,
                    "content": content,
                    "keywords": json.dumps(kw, ensure_ascii=False),
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                    "vector": json.dumps(vec) if vec is not None else None,
                },
                conflict_columns=["id"]
            )
        return {"id": mid, "scope": scope, "content": content, "keywords": kw}

    async def delete(self, memory_id: str, tenant_id: Optional[str] = None) -> bool:
        from app.core.tenant_context import get_tenant
        tid = tenant_id or get_tenant() or "default"
        async with self._lock:
            self._backend.delete("memories", "id = ? AND tenant_id = ?", (memory_id, tid))
            return True

    async def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        keywords: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """更新一条记忆的内容/关键词,并重新计算向量。返回更新后的记录;不存在返回 None。"""
        row = await self._get_row(memory_id)
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
        
        async with self._lock:
            self._backend.update(
                "memories",
                {
                    "content": new_content.strip(),
                    "keywords": json.dumps(new_kw, ensure_ascii=False),
                    "vector": json.dumps(vec) if vec is not None else None,
                    "updated_at": time.time(),
                },
                "id = ?",
                (memory_id,)
            )
        return await self.get(memory_id)

    async def get(self, memory_id: str) -> Optional[dict]:
        async with self._lock:
            rows = self._backend.select("memories", "id = ?", (memory_id,), limit=1)
        return self._row_to_dict(rows[0]) if rows else None

    async def _get_row(self, memory_id: str):
        async with self._lock:
            rows = self._backend.select("memories", "id = ?", (memory_id,), limit=1)
        return rows[0] if rows else None

    async def clear(self) -> int:
        async with self._lock:
            self._backend.delete("memories", "1 = 1")
            return 1

    async def list(self, limit: int = 50, scope: Optional[str] = None, tenant_id: Optional[str] = None) -> list[dict]:
        from app.core.tenant_context import get_tenant
        tid = tenant_id or get_tenant() or "default"
        conditions = ["tenant_id = ?"]
        params: list = [tid]
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        where = " AND ".join(conditions)
        async with self._lock:
            rows = self._backend.select(
                "memories",
                where,
                tuple(params),
                order_by="updated_at DESC",
                limit=limit
            )
        return [self._row_to_dict(r) for r in rows]

    async def stats(self) -> dict:
        from app.core.tenant_context import get_tenant
        tid = get_tenant() or "default"
        async with self._lock:
            total = self._backend.count("memories", "tenant_id = ?", (tid,))
            manual = self._backend.count("memories", "tenant_id = ? AND source = 'manual'", (tid,))
            auto = self._backend.count("memories", "tenant_id = ? AND source = 'auto'", (tid,))
            rows = self._backend.select("memories", "tenant_id = ?", (tid,))
            hits = sum(r.get("hit_count", 0) for r in rows)
        return {"total": total, "manual": manual, "auto": auto, "hits": hits, "tenant_id": tid}

    def count(self, scope: Optional[str] = None, tenant_id: Optional[str] = None) -> int:
        """同步统计记忆条数(供 memory_manager 等同步调用方使用)。"""
        from app.core.tenant_context import get_tenant
        tid = tenant_id or get_tenant() or "default"
        conditions = ["tenant_id = ?"]
        params: list = [tid]
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        where = " AND ".join(conditions)
        with _DB_SYNC_LOCK:
            return self._backend.count("memories", where, tuple(params))

    def list_sync(self, limit: int = 50, scope: Optional[str] = None, tenant_id: Optional[str] = None) -> list[dict]:
        """同步版 list(供 memory_manager 等同步调用方使用),语义与 async list() 一致。"""
        from app.core.tenant_context import get_tenant
        tid = tenant_id or get_tenant() or "default"
        conditions = ["tenant_id = ?"]
        params: list = [tid]
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        where = " AND ".join(conditions)
        with _DB_SYNC_LOCK:
            rows = self._backend.select(
                "memories", where, tuple(params),
                order_by="updated_at DESC", limit=limit,
            )
        return [self._row_to_dict(r) for r in rows]

    def search(
        self,
        query: str,
        top_k: int = 5,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
        count_hit: bool = False,
        tenant_id: Optional[str] = None,
    ) -> list[dict]:
        """混合检索:向量余弦相似度 + 关键词覆盖率。"""
        # 同步版本:直接调用内部实现
        return self._search_sync(query, top_k, scope, session_id, count_hit, tenant_id)

    async def search_async(
        self,
        query: str,
        top_k: int = 5,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
        count_hit: bool = False,
        tenant_id: Optional[str] = None,
    ) -> list[dict]:
        """异步混合检索:向量余弦相似度 + 关键词覆盖率。

        与 search() 逻辑一致,但嵌入计算走 asyncio 事件循环,
        适合在 async 上下文中调用以避免线程池开销。
        """
        return await self._search_async_impl(query, top_k, scope, session_id, count_hit, tenant_id)

    def _search_sync(
        self,
        query: str,
        top_k: int,
        scope: Optional[str],
        session_id: Optional[str],
        count_hit: bool,
        tenant_id: Optional[str],
    ) -> list[dict]:
        """搜索内部实现(同步)。"""
        from app.core.tenant_context import get_tenant
        if scope is None:
            scope = "global"
        tid = tenant_id or get_tenant() or "default"

        conditions = ["tenant_id = ?", "scope = ?"]
        params: list = [tid, scope]
        if scope == "session" and session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = " AND ".join(conditions)

        with _DB_SYNC_LOCK:
            rows = self._backend.select(
                "memories",
                where,
                tuple(params),
                order_by="updated_at DESC",
                limit=200
            )
        
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
            # 过滤低于总分数阈值的候选
            if score < MIN_TOTAL_SCORE:
                continue
            scored.append((score, mem))
            if q_vec is not None and vec is None and len(backfill) < 20:
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
        # 可选:对结果做二次重排(RRF融合)
        if getattr(settings, "rerank_mode", "rrf") == "rrf" and len(result) > 1:
            result = _rerank_rrf(result, query)
        if count_hit and result:
            self._hit([m["id"] for m in result])
        return result

    async def _search_async_impl(
        self,
        query: str,
        top_k: int,
        scope: Optional[str],
        session_id: Optional[str],
        count_hit: bool,
        tenant_id: Optional[str],
    ) -> list[dict]:
        """异步搜索内部实现。"""
        from app.core.tenant_context import get_tenant
        if scope is None:
            scope = "global"
        tid = tenant_id or get_tenant() or "default"

        conditions = ["tenant_id = ?", "scope = ?"]
        params: list = [tid, scope]
        if scope == "session" and session_id:
            conditions.append("session_id = ?")
            params.append(session_id)

        where = " AND ".join(conditions)
        
        async with self._lock:
            rows = self._backend.select(
                "memories",
                where,
                tuple(params),
                order_by="updated_at DESC",
                limit=200
            )
        
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
            # 过滤低于总分数阈值的候选
            if score < MIN_TOTAL_SCORE:
                continue
            scored.append((score, mem))
            if q_vec is not None and vec is None and len(backfill) < 20:
                backfill.append((mem, mem["id"]))
        
        if backfill:
            try:
                from app.rag.embeddings import get_embedder, run_embed

                vectors = run_embed(get_embedder(), [m["content"] for m, _ in backfill])
                for (mem, mid), v in zip(backfill, vectors):
                    await self._set_vector(mid, v)
                    mem["vector"] = v
            except Exception:  # noqa: BLE001 补嵌入失败不影响本次检索
                pass
        
        scored.sort(key=lambda x: x[0], reverse=True)
        result = []
        for score, mem in scored[:top_k]:
            result.append({**mem, "vector": None, "relevance": round(score, 4)})
        # 可选:对结果做二次重排(RRF融合)
        if getattr(settings, "rerank_mode", "rrf") == "rrf" and len(result) > 1:
            result = _rerank_rrf(result, query)
        if count_hit and result:
            await self._hit([m["id"] for m in result])
        return result

    async def _set_vector(self, memory_id: str, vector: list[float]) -> None:
        """为记忆写入向量(懒补嵌入用)。"""
        async with self._lock:
            self._backend.update(
                "memories",
                {"vector": json.dumps(vector)},
                "id = ?",
                (memory_id,)
            )

    def _hit(self, memory_ids: list[str]) -> None:
        """命中计数(用于统计哪个记忆常被使用)。"""
        if not memory_ids:
            return
        with _DB_SYNC_LOCK:
            # SQLite 特殊语法，需要手动处理
            if hasattr(self._backend, '_db_path'):
                conn = self._backend.connect()
                try:
                    conn.executemany(
                        "UPDATE memories SET hit_count = hit_count + 1 WHERE id=?",
                        [(i,) for i in memory_ids],
                    )
                    conn.commit()
                finally:
                    self._backend.close(conn)
            else:
                # 其他后端逐个更新
                for mid in memory_ids:
                    row = self._get_row(mid)
                    if row:
                        self._backend.update(
                            "memories",
                            {"hit_count": row.get("hit_count", 0) + 1},
                            "id = ?",
                            (mid,)
                        )

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


def _rerank_rrf(candidates: list[dict], query: str, k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion 二次重排:融合向量排名与关键词排名。

    对同一组候选分别按向量分数和关键词重叠度计算 RRF 分,
    取加权融合后重新排序,提升 top-k 精度。
    """
    if len(candidates) <= 1:
        return candidates
    # 向量分已存在于 candidate["relevance"]
    vec_scores = {m["id"]: m["relevance"] for m in candidates}
    # 关键词分(基于查询 token 重叠)
    q_tokens = set(_keyword_tokens(query or ""))
    kw_scores: dict[str, float] = {}
    for m in candidates:
        mem_tokens = set(m.get("keywords", [])) | set(_keyword_tokens(m.get("content", "")))
        if q_tokens and mem_tokens:
            overlap = len(q_tokens & mem_tokens)
            kw_scores[m["id"]] = overlap / max(1, len(q_tokens))
        else:
            kw_scores[m["id"]] = 0.0
    # RRF 融合
    rrf_scores: dict[str, float] = {}
    for m in candidates:
        mid = m["id"]
        vec_rank = sum(1 for v in vec_scores.values() if v > vec_scores[mid]) + 1
        kw_rank = sum(1 for v in kw_scores.values() if v > kw_scores[mid]) + 1
        rrf_scores[mid] = (1.0 / (k + vec_rank) + 1.0 / (k + kw_rank))
    # 按 RRF 分重排
    candidates.sort(key=lambda m: rrf_scores.get(m["id"], 0), reverse=True)
    return candidates


def get_memory_store() -> MemoryStore:
    """单例。"""
    global _instance
    with _DB_SYNC_LOCK:
        if _instance is None:
            _instance = MemoryStore()
    return _instance


def run_decay_cleanup(dry_run: bool = False, scope: Optional[str] = None) -> dict:
    """执行一次记忆衰退清理(供 API 调用)。"""
    from app.memory.decay import run_decay_cleanup as _inner
    store = get_memory_store()
    return _inner(store, scope=scope, dry_run=dry_run)
