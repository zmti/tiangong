"""向量存储:SQLite 持久化 + 可选 faiss 内存 ANN 索引。

SQLite 表 vectors 是权威存储(落盘不丢),faiss 索引仅作查询加速,
启动时从 SQLite 重建。维度/索引后端变化时自动重建索引。

后端(settings.vector_index_backend):
  auto: 装了 faiss 用 faiss,否则回退 SQLite 线性扫描
  flat: 强制 faiss IndexFlatIP(精确余弦,本地规模足够快)
  hnsw: 强制 faiss HNSW(近似,万级向量更快)
  off:  只用 SQLite 线性扫描

id_map 表维护业务字符串 id <-> faiss 整数 id 的映射,保证删除/替换正确。
"""
from __future__ import annotations

import json
import os
import struct
import threading
import time
from typing import Optional

import numpy as np

from app.config import settings


class VectorStore:
    def __init__(self, db_path: Optional[str] = None, dim: int = 0):
        """SQLite 向量存储(可带 faiss 加速)。

        db_path: 指定路径;None 用默认 data/knowledge.db
        dim: 期望向量维度(>0 时 add 校验维度;0 不校验)。兼容旧用法 VectorStore(dim=N)
        """
        if db_path is None and dim:
            db_path = ":memory:"
        self.db_path = db_path or os.path.join(settings.resolved_data_dir, "knowledge.db")
        self.dim = dim
        self._lock = threading.RLock()
        self._mem_conn = None  # 仅用于 :memory:
        self._faiss = None  # 惰性加载:None=未初始化,False=不可用
        self._index = None
        self._int_ids: dict[str, int] = {}  # 业务 id -> faiss 整数 id
        self._init_db()
        self._rebuild_index()

    # ---- faiss 可用性 ----

    @staticmethod
    def _faiss_available() -> bool:
        try:
            import faiss  # noqa: F401, PLC0415

            return True
        except ImportError:  # noqa: BLE001
            return False

    def _faiss_enabled(self) -> bool:
        backend = (settings.vector_index_backend or "auto").lower()
        if backend == "off":
            return False
        if not self._faiss_available():
            return False
        return True

    # ---- SQLite 连接 ----

    def _connect(self):
        import sqlite3

        if self.db_path == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            return self._mem_conn
        return sqlite3.connect(self.db_path)

    def _close(self, conn) -> None:
        if self.db_path != ":memory:":
            conn.close()

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vectors (
                        id TEXT PRIMARY KEY,
                        dim INTEGER NOT NULL,
                        vec BLOB NOT NULL,
                        meta TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS id_map (
                        id TEXT PRIMARY KEY,
                        int_id INTEGER NOT NULL
                    )
                    """
                )
                # 迁移:补填 tenant_id 列
                try:
                    conn.execute("ALTER TABLE vectors ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_tenant ON vectors(tenant_id)")
                except Exception:
                    pass  # 列已存在
                conn.commit()
            finally:
                self._close(conn)

    def _serialize(self, vector: list[float]) -> bytes:
        arr = np.asarray(vector, dtype=np.float32)
        return struct.pack(f"<{len(arr)}f", *arr)

    def _deserialize(self, blob: bytes, dim: int) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32).reshape(dim)

    @staticmethod
    def _norm(v: np.ndarray) -> np.ndarray:
        n = float(np.linalg.norm(v))
        return v if n == 0 else (v / n)

    # ---- faiss 索引重建 ----

    def _build_index(self, dim: int) -> None:
        import faiss

        backend = (settings.vector_index_backend or "auto").lower()
        if backend == "hnsw":
            index = faiss.IndexHNSWFlat(dim, int(settings.hnsw_m))
            index.hnsw.efConstruction = int(settings.hnsw_ef_construction)
            index.hnsw.efSearch = int(settings.hnsw_ef_search)
        else:  # flat / auto
            index = faiss.IndexFlatIP(dim)
        self._index = faiss.IndexIDMap2(index)

    def _rebuild_index(self) -> None:
        """从 SQLite 重建 faiss 索引与 id 映射。"""
        self._int_ids = {}
        if not self._faiss_enabled():
            self._index = None
            return
        try:
            import faiss  # noqa: PLC0415

            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT id, dim, vec FROM vectors ORDER BY created_at"
                    ).fetchall()
                finally:
                    self._close(conn)
            if not rows:
                return
            dims = {r[1] for r in rows}
            dim = next(iter(dims))
            self._build_index(dim)
            ids = []
            vecs = []
            for r in rows:
                v = self._deserialize(r[2], r[1])
                ids.append(self._int_id_for(r[0]))
                vecs.append(self._norm(v))
            self._index.add_with_ids(np.vstack(vecs).astype(np.float32), np.asarray(ids, dtype=np.int64))
        except Exception:  # noqa: BLE001  # 索引重建失败退化为线性扫描
            self._index = None

    def _int_id_for(self, biz_id: str) -> int:
        """获取业务 id 对应的 faiss 整数 id(稳定映射,持久化在 id_map)。"""
        if biz_id in self._int_ids:
            return self._int_ids[biz_id]
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT int_id FROM id_map WHERE id=?", (biz_id,)).fetchone()
            finally:
                self._close(conn)
            if row is not None:
                self._int_ids[biz_id] = int(row[0])
                return int(row[0])
            new_id = max(self._int_ids.values(), default=-1) + 1
            self._int_ids[biz_id] = new_id
            try:
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO id_map (id, int_id) VALUES (?,?)",
                        (biz_id, new_id),
                    )
                    conn.commit()
                finally:
                    self._close(conn)
            except Exception:  # noqa: BLE001
                pass
            return new_id

    # ---- 写操作 ----

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None, tenant_id: str = "default") -> None:
        if self.dim and len(vector) != self.dim:
            raise ValueError(f"向量维度 {len(vector)} 与存储维度 {self.dim} 不匹配")
        blob = self._serialize(vector)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO vectors (id, dim, vec, meta, tenant_id, created_at) VALUES (?,?,?,?,?,?)",
                    (id, len(vector), blob, meta_json, tenant_id, time.time()),
                )
                conn.commit()
            finally:
                self._close(conn)
        self._sync_index_add(id, vector)

    def _sync_index_add(self, id: str, vector: list[float]) -> None:
        if not self._faiss_enabled():
            return
        try:
            # 首次 add 且索引尚未建立时,按当前向量维度建索引
            if self._index is None:
                self._build_index(len(vector))
            vec = self._norm(np.asarray(vector, dtype=np.float32))
            int_id = self._int_id_for(id)
            self._index.add_with_ids(vec.reshape(1, -1), np.asarray([int_id], dtype=np.int64))
        except Exception:  # noqa: BLE001  # 索引同步失败不影响主存储
            pass

    def delete(self, id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM vectors WHERE id=?", (id,))
                conn.execute("DELETE FROM id_map WHERE id=?", (id,))
                conn.commit()
                deleted = cur.rowcount > 0
            finally:
                self._close(conn)
        if deleted and self._index is not None:
            try:
                int_id = self._int_ids.get(id)
                if int_id is not None:
                    self._index.remove_ids(np.asarray([int_id], dtype=np.int64))
                self._int_ids.pop(id, None)
            except Exception:  # noqa: BLE001
                pass
        return deleted

    def clear(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM vectors")
                conn.execute("DELETE FROM id_map")
                conn.commit()
            finally:
                self._close(conn)
        self._int_ids = {}
        self._index = None

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
            return row[0]
        finally:
            self._close(conn)

    @property
    def _ids(self) -> list[str]:
        conn = self._connect()
        try:
            return [r[0] for r in conn.execute("SELECT id FROM vectors").fetchall()]
        finally:
            self._close(conn)

    @property
    def _metas(self) -> list[dict]:
        conn = self._connect()
        try:
            return [json.loads(r[0]) for r in conn.execute("SELECT meta FROM vectors").fetchall()]
        finally:
            self._close(conn)

    def backend(self) -> str:
        """当前生效的检索后端:'faiss_flat' / 'faiss_hnsw' / 'sqlite'。"""
        if self._index is None:
            return "sqlite"
        import faiss

        idx = self._index if isinstance(self._index, faiss.IndexIDMap2) else self._index
        target = getattr(idx, "index", idx)
        if isinstance(target, faiss.IndexHNSWFlat):
            return "faiss_hnsw"
        return "faiss_flat"

    # ---- 查询 ----

    def search(self, query_vector: list[float], top_k: int = 5, tenant_id: str = "default") -> list[dict]:
        """返回相关向量:优先 faiss,回退 SQLite 线性扫描余弦。支持 tenant_id 过滤。"""
        q = np.asarray(query_vector, dtype=np.float32)
        if self._index is not None:
            try:
                return self._faiss_search(q, top_k, tenant_id)
            except Exception:  # noqa: BLE001  # faiss 维度不匹配等异常回退线性
                pass
        return self._linear_search(q, top_k, tenant_id)

    def _faiss_search(self, q: np.ndarray, top_k: int, tenant_id: str = "default") -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, dim, vec, meta FROM vectors WHERE dim=? AND tenant_id=?",
                (int(q.shape[0]), tenant_id),
            ).fetchall()
        finally:
            self._close(conn)
        if not rows:
            return []
        index = self._index
        # faiss 索引维度与查询不一致时回退线性(切换 embedding 后可能发生)
        if index.d != q.shape[0]:
            return self._linear_search(q, top_k, tenant_id)
        qn = self._norm(q).reshape(1, -1).astype(np.float32)
        n = len(rows)
        k = min(top_k, n)
        if k <= 0:
            return []
        D, I = index.search(qn, k)
        # 用索引映射反查业务 id
        int_to_meta: dict[int, tuple[str, dict]] = {}
        for r in rows:
            int_to_meta[self._int_id_for(r[0])] = (r[0], json.loads(r[3]))
        results = []
        for d, i in zip(D[0], I[0]):
            if i == -1:
                continue
            entry = int_to_meta.get(int(i))
            if entry is None:
                continue
            biz_id, meta = entry
            results.append({"id": biz_id, "score": float(d), "metadata": meta})
        return results

    def _linear_search(self, q: np.ndarray, top_k: int, tenant_id: str = "default") -> list[dict]:
        qnorm = float(np.linalg.norm(q))
        if qnorm == 0:
            return []
        all_rows: list = []
        conn = self._connect()
        try:
            all_rows = conn.execute(
                "SELECT id, dim, vec, meta FROM vectors WHERE dim=? AND tenant_id=?",
                (int(q.shape[0]), tenant_id),
            ).fetchall()
        finally:
            self._close(conn)
        if not all_rows:
            return []
        results = []
        for row in all_rows:
            vec = self._deserialize(row[2], row[1])
            vnorm = float(np.linalg.norm(vec))
            if vnorm == 0:
                continue
            score = float(np.dot(vec, q) / (vnorm * qnorm))
            if score > 0:
                results.append(
                    {"id": row[0], "score": score, "metadata": json.loads(row[3])}
                )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]

    # 兼容旧接口
    def save(self, path: str) -> None:
        pass  # SQLite 已落盘

    def load(self, path: str) -> None:
        pass  # SQLite 已落盘


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.size == 0 or vb.size == 0:
        return 0.0
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)
