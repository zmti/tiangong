"""Embedding 缓存:避免对相同文本重复调用嵌入 API,节省 token 和延迟。

策略:
- 内存 dict (str -> vector) 作为一级缓存
- SQLite 文件作为二级持久化缓存(跨进程重启有效)
- 缓存失效:当 embedding backend 切换时自动清除
- 默认启用,可通过 AGENT_EMBEDDING_CACHE_ENABLED 关闭
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Optional

from app.config import settings
from app.rag.embeddings import Embedder


class EmbeddingCache:
    """文本 -> 向量 缓存,带 TTL 和持久化。"""

    def __init__(self, max_size: int = 10000, ttl_seconds: float = 86400.0):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._mem: dict[str, tuple[list[float], float]] = {}  # hash -> (vector, ts)
        self._access_order: list[str] = []  # LRU 顺序
        self._lock = threading.RLock()
        self._backend_signature: str = ""  # 用于检测后端变更
        self._init_db()
        self._load_from_disk()

    def _init_db(self) -> None:
        try:
            path = os.path.join(settings.resolved_data_dir, "embedding_cache.db")
            self._db_path = path
            conn = sqlite3.connect(path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS embedding_cache (
                        hash TEXT PRIMARY KEY,
                        text TEXT NOT NULL,
                        vector TEXT NOT NULL,
                        backend_sig TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            self._db_path = ""

    def _load_from_disk(self) -> None:
        if not self._db_path or not os.path.exists(self._db_path):
            return
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                rows = conn.execute(
                    "SELECT hash, vector, backend_sig FROM embedding_cache"
                ).fetchall()
                now = time.time()
                for h, vec_str, sig in rows:
                    if sig == self._backend_signature or not settings.agent_embedding_cache_persistent:
                        continue
                    try:
                        vec = json.loads(vec_str)
                        self._mem[h] = (vec, now)
                        if h not in self._access_order:
                            self._access_order.append(h)
                    except Exception:  # noqa: BLE001
                        continue
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> Optional[list[float]]:
        """查询缓存;命中的更新 LRU 顺序并返回向量。"""
        if not settings.agent_embedding_cache_enabled:
            return None
        h = self._hash(text)
        with self._lock:
            entry = self._mem.get(h)
            if entry is None:
                return None
            vec, ts = entry
            if time.time() - ts > self._ttl:
                del self._mem[h]
                if h in self._access_order:
                    self._access_order.remove(h)
                return None
            # 更新 LRU
            if h in self._access_order:
                self._access_order.remove(h)
            self._access_order.append(h)
            return vec

    def set(self, text: str, vector: list[float]) -> None:
        """写入缓存,超限则驱逐最久未使用的。"""
        if not settings.agent_embedding_cache_enabled:
            return
        h = self._hash(text)
        with self._lock:
            if h in self._mem:
                self._mem[h] = (vector, time.time())
                if h in self._access_order:
                    self._access_order.remove(h)
                self._access_order.append(h)
                return
            while len(self._mem) >= self._max_size and self._access_order:
                oldest = self._access_order.pop(0)
                self._mem.pop(oldest, None)
            self._mem[h] = (vector, time.time())
            self._access_order.append(h)
            # 异步落盘
            self._persist(h, text, vector)

    def _persist(self, h: str, text: str, vector: list[float]) -> None:
        if not self._db_path or not settings.agent_embedding_cache_persistent:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO embedding_cache (hash, text, vector, backend_sig, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (h, text, json.dumps(vector), self._backend_signature, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def invalidate_all(self) -> int:
        """清除全部缓存(后端切换时调用)。"""
        with self._lock:
            n = len(self._mem)
            self._mem.clear()
            self._access_order.clear()
        return n

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._mem),
                "max_size": self._max_size,
                "ttl_hours": round(self._ttl / 3600, 1),
            }


_cache: Optional[EmbeddingCache] = None
_cache_lock = threading.Lock()


def get_embedding_cache() -> EmbeddingCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = EmbeddingCache()
    return _cache
