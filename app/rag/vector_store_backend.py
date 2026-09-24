"""向量存储后端抽象层:统一接口,支持 faiss/Milvus/pgvector 等多后端切换。
"""
from __future__ import annotations

import abc
import os
import threading
import time
from typing import Optional

import numpy as np

from app.config import settings


class VectorStoreBackend(abc.ABC):
    """向量存储后端抽象基类。

    所有后端实现必须提供:
    - add(id, vector, metadata, tenant_id)
    - delete(id)
    - search(query_vector, top_k, tenant_id)
    - count()
    - clear()
    - backend_name()
    """

    @abc.abstractmethod
    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None, tenant_id: str = "default") -> None:
        """添加向量"""

    @abc.abstractmethod
    def delete(self, id: str) -> bool:
        """删除向量，返回是否成功"""

    @abc.abstractmethod
    def search(self, query_vector: list[float], top_k: int = 5, tenant_id: str = "default") -> list[dict]:
        """搜索向量，返回 [{id, score, metadata}, ...]"""

    @abc.abstractmethod
    def count(self) -> int:
        """向量总数"""

    @abc.abstractmethod
    def clear(self) -> None:
        """清空存储"""

    @abc.abstractmethod
    def backend_name(self) -> str:
        """后端名称: 'faiss_flat' / 'faiss_hnsw' / 'milvus' / 'pgvector' / 'sqlite'"""

    @abc.abstractmethod
    def close(self) -> None:
        """关闭连接/释放资源"""

    def iter_items(self, tenant_id: Optional[str] = None) -> list[tuple[str, dict]]:
        """遍历全部 (id, metadata);传 tenant_id 时只返回该租户的条目。

        默认实现读取底层 store 的 _ids / _metas。不支持枚举的后端(纯 Milvus /
        pgvector 适配器)会取不到这两个属性,此时**返回空列表而不是抛异常**,
        避免 /v1/documents/stats 之类的只读接口直接 500。
        """
        try:
            ids, metas = self._ids, self._metas  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return []
        items = list(zip(ids, metas))
        if tenant_id is not None:
            items = [(i, m) for i, m in items if m.get("tenant_id", "default") == tenant_id]
        return items


# 全局后端实例缓存
_backend_instance: Optional[VectorStoreBackend] = None
_backend_lock = threading.Lock()


def get_vector_store_backend(backend: Optional[str] = None) -> VectorStoreBackend:
    """获取向量存储后端实例(单例模式)。

    backend: 'faiss' / 'milvus' / 'pgvector' / 'sqlite' / 'auto'
    None 时使用 settings.vector_store_backend
    """
    global _backend_instance
    if _backend_instance is not None:
        # 如果请求的后端与当前实例不同，需要重新创建
        if backend and backend != _backend_instance.backend_name().split("_")[0]:
            _backend_instance.close()
            _backend_instance = None
        else:
            return _backend_instance

    with _backend_lock:
        if _backend_instance is None:
            target_backend = (backend or settings.vector_store_backend or "auto").lower()
            _backend_instance = _create_backend(target_backend)
    return _backend_instance


def _create_backend(backend_type: str) -> VectorStoreBackend:
    """根据类型创建后端实例"""
    if backend_type == "faiss":
        from app.rag.store import VectorStore as FaissStore
        return _FaissAdapter()
    elif backend_type == "milvus":
        return _MilvusAdapter()
    elif backend_type == "pgvector":
        return _PgVectorAdapter()
    elif backend_type == "sqlite":
        return _SQLiteOnlyAdapter()
    elif backend_type == "auto":
        # 自动选择:优先 faiss，不可用回退 sqlite
        try:
            import faiss  # noqa: F401
            return _FaissAdapter()
        except ImportError:
            return _SQLiteOnlyAdapter()
    else:
        raise ValueError(f"未知向量存储后端: {backend_type}")


# ----- 透传底层 VectorStore 的适配器基类 -----
class _StoreBackedBackend(VectorStoreBackend):
    """包装 app.rag.store.VectorStore 的适配器基类。

    引入本抽象层前,get_store() 直接返回 VectorStore 实例,因此一批调用方会直接
    访问它的成员,例如:
      - app/api/documents.py: store._metas / store._ids / store._int_ids / store._index
      - app/rag/documents.py: store._ids / store._metas
      - app/rag/multi_retriever.py: store._connect() / store._close(conn)  (BM25 召回)
    抽象层若只暴露 add/delete/search/count/clear,这些调用会抛 AttributeError:
    /v1/documents/stats 直接 500,BM25 路径则被 try/except 静默吞掉导致召回缺失。

    这里用 __getattr__ 把未实现的属性透明透传给底层 store,保证"引入抽象层"这一
    改动对既有调用方是行为兼容的;抽象接口仍然由显式方法(add/delete/search...)提供。
    """

    def __getattr__(self, name: str):
        # 只在常规查找失败时触发;_store 已写入 __dict__ 时不会递归。
        store = self.__dict__.get("_store")
        if store is not None:
            return getattr(store, name)
        raise AttributeError(
            f"{type(self).__name__} 尚未初始化底层 store,无法访问属性 {name!r}"
        )


# ----- Faiss 适配器（复用现有 VectorStore） -----
class _FaissAdapter(_StoreBackedBackend):
    def __init__(self, db_path: Optional[str] = None, dim: int = 0):
        from app.rag.store import VectorStore
        self._store = VectorStore(db_path=db_path, dim=dim)

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None, tenant_id: str = "default") -> None:
        self._store.add(id, vector, metadata, tenant_id)

    def delete(self, id: str) -> bool:
        return self._store.delete(id)

    def search(self, query_vector: list[float], top_k: int = 5, tenant_id: str = "default") -> list[dict]:
        return self._store.search(query_vector, top_k, tenant_id)

    def count(self) -> int:
        return self._store.count()

    def clear(self) -> None:
        self._store.clear()

    def backend_name(self) -> str:
        return self._store.backend()

    def close(self) -> None:
        # faiss 索引在内存中，无需显式关闭
        pass


# ----- SQLite-only 适配器 -----
class _SQLiteOnlyAdapter(_StoreBackedBackend):
    def __init__(self, db_path: Optional[str] = None, dim: int = 0):
        from app.rag.store import VectorStore
        # 强制关闭 faiss
        import app.config as cfg
        orig = cfg.settings.vector_index_backend
        cfg.settings.vector_index_backend = "off"
        self._store = VectorStore(db_path=db_path, dim=dim)
        cfg.settings.vector_index_backend = orig

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None, tenant_id: str = "default") -> None:
        self._store.add(id, vector, metadata, tenant_id)

    def delete(self, id: str) -> bool:
        return self._store.delete(id)

    def search(self, query_vector: list[float], top_k: int = 5, tenant_id: str = "default") -> list[dict]:
        return self._store.search(query_vector, top_k, tenant_id)

    def count(self) -> int:
        return self._store.count()

    def clear(self) -> None:
        self._store.clear()

    def backend_name(self) -> str:
        return "sqlite"

    def close(self) -> None:
        pass


# ----- Milvus 适配器 -----
class _MilvusAdapter(VectorStoreBackend):
    """Milvus 向量数据库适配器。

    需安装: pip install pymilvus
    配置: MILVUS_URI, MILVUS_TOKEN, MILVUS_COLLECTION
    """

    def __init__(self, dim: int = 1536):
        self._dim = dim
        self._collection_name = os.environ.get("MILVUS_COLLECTION", "agent_vectors")
        self._uri = os.environ.get("MILVUS_URI", "http://localhost:19530")
        self._token = os.environ.get("MILVUS_TOKEN", "")
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        try:
            from pymilvus import MilvusClient
            self._client = MilvusClient(uri=self._uri, token=self._token)
            # 创建 collection
            if not self._client.has_collection(self._collection_name):
                self._client.create_collection(
                    collection_name=self._collection_name,
                    dimension=self._dim,
                    metric_type="IP",  # 内积 = 余弦相似度(向量已归一化)
                    consistency_level="Strong",
                )
        except ImportError:
            raise RuntimeError("Milvus 后端需要安装 pymilvus: pip install pymilvus")
        except Exception as e:
            raise RuntimeError(f"Milvus 连接失败: {e}")

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None, tenant_id: str = "default") -> None:
        if self._client is None:
            raise RuntimeError("Milvus 客户端未初始化")
        # 归一化向量
        vec = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        data = [{
            "id": id,
            "vector": vec.tolist(),
            "metadata": metadata or {},
            "tenant_id": tenant_id,
            "created_at": time.time(),
        }]
        self._client.insert(self._collection_name, data)

    def delete(self, id: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.delete(self._collection_name, ids=[id])
            return True
        except Exception:
            return False

    def search(self, query_vector: list[float], top_k: int = 5, tenant_id: str = "default") -> list[dict]:
        if self._client is None:
            return []
        q = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm > 0:
            q = q / norm
        results = self._client.search(
            collection_name=self._collection_name,
            data=[q.tolist()],
            limit=top_k,
            filter=f"tenant_id == '{tenant_id}'",
            output_fields=["metadata"],
        )
        out = []
        for hit in results[0]:
            out.append({
                "id": hit["id"],
                "score": hit["distance"],  # IP 距离，越大越相似
                "metadata": hit.get("entity", {}).get("metadata", {}),
            })
        return out

    def count(self) -> int:
        if self._client is None:
            return 0
        try:
            stats = self._client.get_collection_stats(self._collection_name)
            return stats.get("row_count", 0)
        except Exception:
            return 0

    def clear(self) -> None:
        if self._client is not None:
            try:
                self._client.drop_collection(self._collection_name)
                self._init_client()
            except Exception:
                pass

    def backend_name(self) -> str:
        return "milvus"

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


# ----- pgvector 适配器 -----
class _PgVectorAdapter(VectorStoreBackend):
    """PostgreSQL + pgvector 适配器。

    需安装: pip install psycopg2-binary
    配置: PGVECTOR_DSN (postgresql://user:pass@host:port/db)
    数据库需启用 pgvector 扩展: CREATE EXTENSION vector;
    """

    def __init__(self, dim: int = 1536):
        self._dim = dim
        self._dsn = os.environ.get("PGVECTOR_DSN", "")
        if not self._dsn:
            raise RuntimeError("pgvector 后端需要配置 PGVECTOR_DSN 环境变量")
        self._conn = None
        self._init_db()

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            import psycopg2
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS vectors (
                        id TEXT PRIMARY KEY,
                        dim INTEGER NOT NULL,
                        vec vector({self._dim}) NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}',
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        created_at DOUBLE PRECISION NOT NULL
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vectors_tenant ON vectors(tenant_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_vectors_vec ON vectors USING ivfflat (vec vector_ip_ops) WITH (lists = 100);")
        except Exception as e:
            raise RuntimeError(f"pgvector 初始化失败: {e}")

    def add(self, id: str, vector: list[float], metadata: Optional[dict] = None, tenant_id: str = "default") -> None:
        conn = self._get_conn()
        vec_str = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        import json
        meta_json = json.dumps(metadata or {})
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO vectors (id, dim, vec, metadata, tenant_id, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                       dim=EXCLUDED.dim, vec=EXCLUDED.vec, metadata=EXCLUDED.metadata,
                       tenant_id=EXCLUDED.tenant_id, created_at=EXCLUDED.created_at""",
                (id, len(vector), vec_str, meta_json, tenant_id, time.time()),
            )

    def delete(self, id: str) -> bool:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vectors WHERE id=%s", (id,))
            return cur.rowcount > 0

    def search(self, query_vector: list[float], top_k: int = 5, tenant_id: str = "default") -> list[dict]:
        conn = self._get_conn()
        q = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm > 0:
            q = q / norm
        vec_str = "[" + ",".join(f"{v:.6f}" for v in q) + "]"
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, metadata, 1 - (vec <=> %s) AS score
                   FROM vectors
                   WHERE tenant_id = %s
                   ORDER BY vec <=> %s
                   LIMIT %s""",
                (vec_str, tenant_id, vec_str, top_k),
            )
            rows = cur.fetchall()
        out = []
        for row in rows:
            out.append({
                "id": row[0],
                "score": float(row[2]),
                "metadata": row[1] if isinstance(row[1], dict) else json.loads(row[1]),
            })
        return out

    def count(self) -> int:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM vectors")
            row = cur.fetchone()
            return row[0] if row else 0

    def clear(self) -> None:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vectors")

    def backend_name(self) -> str:
        return "pgvector"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ----- 便捷函数 -----
def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.size == 0 or vb.size == 0:
        return 0.0
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)