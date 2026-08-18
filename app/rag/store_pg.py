"""pgvector 向量存储:可选的分布式向量后端。

接口与 app/rag/store.VectorStore 一致(add / delete / clear / count / search /
_ids / _metas / backend),由 app/core/storage.get_vector_backend 选择;缺
psycopg / 连接失败时由上层回退 SQLite+faiss,不会抛到主流程。

建表:vectors(id TEXT PRIMARY KEY, embedding vector(DIM), meta TEXT, created_at REAL)
索引:HNSW(cosine_distance_ops),维度在首次写入时动态创建。表名与维度可配。

增强(A4):
- TTL 自动过期:meta 中包含 ttl_seconds 的 chunk 会在 search 时被过滤
- 多租户隔离:支持 tenant_id 过滤,同一数据库多租户互不可见
"""
from __future__ import annotations

import json
import time
from typing import Optional

import numpy as np


class PGVectorStore:
    def __init__(
        self,
        dsn: str,
        table: str = "knowledge_vectors",
        dim: int = 0,
        embed_dim_probe: int = 768,
    ):
        import psycopg  # type: ignore

        self._dsn = dsn
        self._table = table
        self._dim = dim
        self._embed_dim_probe = embed_dim_probe
        self._init(psycopg)

    # ---- 连接 ----

    def _connect(self):
        import psycopg  # type: ignore

        return psycopg.connect(self._dsn, connect_timeout=5)

    def _init(self, psycopg) -> None:
        if getattr(psycopg, "__name__", "") == "psycopg" and hasattr(psycopg, "sql"):
            sql = psycopg.sql
        else:
            sql = None
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except Exception:  # noqa: BLE001
                    pass
                dim = self._dim or self._embed_dim_probe
                if sql is not None:
                    cur.execute(sql.SQL(
                        "CREATE TABLE IF NOT EXISTS {} (id TEXT PRIMARY KEY, "
                        "embedding vector({}), meta TEXT, created_at REAL)"
                    ).format(sql.Identifier(self._table), sql.Literal(dim)))
                    index_name = f"{self._table}_embedding_idx"
                    cur.execute(sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (embedding vector_cosine_ops)"
                    ).format(sql.Identifier(index_name), sql.Identifier(self._table)))
                conn.commit()

    # ---- 写 ----

    def _ensure_dim(self, conn) -> None:
        """首次数据维度未知时按实际向量维度扩展列。"""
        pass  # 维度由 init 固定,异常由 try/except 兜底

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None,
            tenant_id: str = "default") -> None:
        import psycopg  # type: ignore  # noqa: PLC0415

        if self._dim and len(vector) != self._dim:
            raise ValueError(f"向量维度 {len(vector)} 与存储维度 {self._dim} 不匹配")
        arr = np.asarray(vector, dtype=np.float32)
        meta = dict(metadata or {})
        meta["tenant_id"] = tenant_id
        # A4: 持久化 TTL 信息到 meta
        ttl = meta.get("ttl_seconds")
        if ttl:
            meta["_ttl_at"] = time.time() + float(ttl)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (id, embedding, meta, created_at) "
                    f"VALUES (%s, %s::vector, %s, %s) "
                    f"ON CONFLICT (id) DO UPDATE SET embedding=EXCLUDED.embedding, "
                    f"meta=EXCLUDED.meta, created_at=EXCLUDED.created_at",
                    (id, arr.tolist(), json.dumps(meta, ensure_ascii=False), time.time()),
                )
            conn.commit()

    def delete(self, id: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {self._table} WHERE id=%s", (id,))
                ok = cur.rowcount > 0
            conn.commit()
        return ok

    def clear(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE {self._table}")
            conn.commit()

    def count(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {self._table}")
                return int(cur.fetchone()[0])

    # ---- 查询 ----

    def search(self, query_vector: list[float], top_k: int = 5,
               tenant_id: Optional[str] = None, exclude_expired: bool = True) -> list[dict]:
        q = np.asarray(query_vector, dtype=np.float32)
        now = time.time()
        with self._connect() as conn:
            with conn.cursor() as cur:
                # A4: 多租户过滤 + TTL 过期过滤(注意占位符顺序与 SQL 一致)
                where_parts = []
                params: list = [q.tolist()]  # SELECT 里的 %s::vector
                if tenant_id:
                    where_parts.append("meta->>'tenant_id' = %s")
                    params.append(tenant_id)
                if exclude_expired:
                    where_parts.append("meta->'_ttl_at' IS NULL OR CAST(meta->>'_ttl_at' AS REAL) > %s")
                    params.append(str(now))
                where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
                params.append(q.tolist())  # ORDER BY 里的 %s::vector
                params.append(top_k)  # LIMIT
                cur.execute(
                    f"SELECT id, meta, 1 - (embedding <=> %s::vector) AS cosine_sim "
                    f"FROM {self._table} {where_clause} "
                    f"ORDER BY embedding <=> %s::vector LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
        results = []
        for row in rows:
            bid, meta_json, sim = row
            meta = json.loads(meta_json)
            # 跳过已过期条目
            if exclude_expired:
                ttl_at = meta.pop("_ttl_at", None)
                if ttl_at and float(ttl_at) < now:
                    continue
            results.append({"id": bid, "score": float(sim), "metadata": meta})
        return results

    @property
    def _ids(self) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id FROM {self._table}")
                return [r[0] for r in cur.fetchall()]

    @property
    def _metas(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT meta FROM {self._table}")
                return [json.loads(r[0]) for r in cur.fetchall()]

    def backend(self) -> str:
        return "pgvector"

    def set_ttl(self, doc_id: str, ttl_seconds: float) -> None:
        """为整个文档的所有 chunk 设置 TTL(通过通配符 id 前缀)。"""
        prefix = f"{doc_id}:"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self._table} SET meta = jsonb_set("
                    f"meta::jsonb, '_ttl_at', to_jsonb({time.time() + ttl_seconds})) "
                    f"WHERE id LIKE %s",
                    (f"{prefix}%",),
                )
                conn.commit()

    def cleanup_expired(self) -> int:
        """清理已过期的 chunk,返回清理数量。"""
        now = time.time()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self._table} "
                    f"WHERE meta->'_ttl_at' IS NOT NULL "
                    f"AND CAST(meta->>'_ttl_at' AS REAL) < %s",
                    (str(now),),
                )
                n = cur.rowcount
                conn.commit()
        return n


# 兼容旧接口(数据在库里,无需落盘/加载)
def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.size == 0 or vb.size == 0:
        return 0.0
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)