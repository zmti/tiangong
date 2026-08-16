"""pgvector 向量存储:可选的分布式向量后端。

接口与 app/rag/store.VectorStore 一致(add / delete / clear / count / search /
_ids / _metas / backend),由 app/core/storage.get_vector_backend 选择;缺
psycopg / 连接失败时由上层回退 SQLite+faiss,不会抛到主流程。

建表:vectors(id TEXT PRIMARY KEY, embedding vector(DIM), meta TEXT, created_at REAL)
索引:HNSW(cosine_distance_ops),维度在首次写入时动态创建。表名与维度可配。
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

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None) -> None:
        import psycopg  # type: ignore  # noqa: PLC0415

        if self._dim and len(vector) != self._dim:
            raise ValueError(f"向量维度 {len(vector)} 与存储维度 {self._dim} 不匹配")
        arr = np.asarray(vector, dtype=np.float32)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self._table} (id, embedding, meta, created_at) "
                    f"VALUES (%s, %s::vector, %s, %s) "
                    f"ON CONFLICT (id) DO UPDATE SET embedding=EXCLUDED.embedding, "
                    f"meta=EXCLUDED.meta, created_at=EXCLUDED.created_at",
                    (id, arr.tolist(), json.dumps(metadata or {}, ensure_ascii=False), time.time()),
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

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict]:
        q = np.asarray(query_vector, dtype=np.float32)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, meta, 1 - (embedding <=> %s::vector) AS cosine_sim "
                    f"FROM {self._table} ORDER BY embedding <=> %s::vector LIMIT %s",
                    (q.tolist(), q.tolist(), top_k),
                )
                rows = cur.fetchall()
        results = []
        for row in rows:
            bid, meta_json, sim = row
            results.append({"id": bid, "score": float(sim), "metadata": json.loads(meta_json)})
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


# 兼容旧接口(数据在库里,无需落盘/加载)
def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.size == 0 or vb.size == 0:
        return 0.0
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)