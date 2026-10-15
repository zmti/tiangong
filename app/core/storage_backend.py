"""统一存储后端抽象层

设计原则:
1. 所有后端实现统一接口，支持 SQLite / Redis / PostgreSQL 切换
2. 自动降级: 高级后端不可用时回退 SQLite
3. 并发安全: 统一使用 threading.RLock
4. 事务支持: 所有写操作支持事务上下文

Usage:
    from app.core.storage_backend import get_storage_backend
    backend = get_storage_backend()
    with backend.transaction():
        backend.upsert("checkpoints", {"run_id": "123", "node": "start"})
"""
from __future__ import annotations

import sqlite3
import threading
import time
import json
import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 默认数据目录
DEFAULT_DATA_DIR = Path("data")


class StorageBackend(ABC):
    """存储后端抽象基类"""

    @abstractmethod
    def connect(self) -> Any:
        """获取连接"""

    @abstractmethod
    def close(self, conn: Any) -> None:
        """关闭连接（:memory: 模式可能不关闭）"""

    @abstractmethod
    def init_db(self, schema: dict[str, list[dict]]) -> None:
        """初始化数据库表结构
        
        Args:
            schema: {
                "table_name": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
                    ...
                ]
            }
        """

    @abstractmethod
    def execute(self, sql: str, params: tuple = (), *, fetch: bool = False) -> list[dict]:
        """执行 SQL 语句"""

    @abstractmethod
    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """批量执行 SQL"""

    @abstractmethod
    def upsert(self, table: str, data: dict, *, conflict_columns: list[str] | None = None) -> None:
        """插入或更新记录"""

    @abstractmethod
    def insert(self, table: str, data: dict) -> None:
        """插入记录"""

    @abstractmethod
    def update(self, table: str, data: dict, where: str, where_params: tuple = ()) -> None:
        """更新记录"""

    @abstractmethod
    def delete(self, table: str, where: str, where_params: tuple = ()) -> None:
        """删除记录"""

    @abstractmethod
    def select(self, table: str, where: str = "", where_params: tuple =(),
               *, order_by: str = "", limit: int = 0, offset: int = 0) -> list[dict]:
        """查询记录"""

    @abstractmethod
    def count(self, table: str, where: str = "", where_params: tuple = ()) -> int:
        """统计记录数"""

    @abstractmethod
    def exists(self, table: str, where: str, where_params: tuple = ()) -> bool:
        """检查记录是否存在"""

    @abstractmethod
    @contextmanager
    def transaction(self):
        """事务上下文管理器"""

    @abstractmethod
    def begin(self) -> None:
        """开始事务"""

    @abstractmethod
    def commit(self) -> None:
        """提交事务"""

    @abstractmethod
    def rollback(self) -> None:
        """回滚事务"""


class SqliteBackend(StorageBackend):
    """SQLite 存储后端实现"""

    def __init__(self, db_path: str | Path, *, memory_mode: bool = False):
        self._db_path = str(db_path)
        self._memory_mode = memory_mode
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._in_transaction = False

        if memory_mode:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

    def connect(self) -> sqlite3.Connection:
        if self._memory_mode and self._conn:
            return self._conn
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        # C 盘空间不足时 SQLite ORDER BY 需要临时文件会报 "database or disk is full"
        # 设为 MEMORY 后排序/聚合在内存完成，避免依赖系统 TEMP
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def close(self, conn: sqlite3.Connection) -> None:
        if self._memory_mode:
            return
        try:
            conn.close()
        except Exception:
            pass

    def init_db(self, schema: dict[str, list[dict]]) -> None:
        with self._lock:
            conn = self.connect()
            try:
                for table_name, columns in schema.items():
                    cols = []
                    for col in columns:
                        cols.append(f'"{col["name"]}" {col["type"]} {col.get("constraints", "")}')
                    sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(cols)})'
                    conn.execute(sql)

                    # 创建索引
                    for col in columns:
                        if col.get("index"):
                            idx_name = f"idx_{table_name}_{col['name']}"
                            conn.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}" ("{col["name"]}")')

                conn.commit()
            finally:
                self.close(conn)

    def execute(self, sql: str, params: tuple = (), *, fetch: bool = False) -> list[dict]:
        with self._lock:
            conn = self.connect()
            try:
                cursor = conn.execute(sql, params)
                if fetch:
                    rows = cursor.fetchall()
                    return [dict(row) for row in rows]
                conn.commit()
                return []
            finally:
                self.close(conn)

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executemany(sql, params_list)
                conn.commit()
            finally:
                self.close(conn)

    def upsert(self, table: str, data: dict, *, conflict_columns: list[str] | None = None) -> None:
        cols = list(data.keys())
        placeholders = ", ".join(["?" for _ in cols])
        col_names = ", ".join([f'"{c}"' for c in cols])
        sql = f'INSERT OR REPLACE INTO "{table}" ({col_names}) VALUES ({placeholders})'
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, tuple(data.values()))
                conn.commit()
            finally:
                self.close(conn)

    def insert(self, table: str, data: dict) -> None:
        cols = list(data.keys())
        placeholders = ", ".join(["?" for _ in cols])
        col_names = ", ".join([f'"{c}"' for c in cols])
        sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, tuple(data.values()))
                conn.commit()
            finally:
                self.close(conn)

    def update(self, table: str, data: dict, where: str, where_params: tuple = ()) -> None:
        set_clause = ", ".join([f'"{k}" = ?' for k in data.keys()])
        sql = f'UPDATE "{table}" SET {set_clause} WHERE {where}'
        params = tuple(data.values()) + where_params
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                self.close(conn)

    def delete(self, table: str, where: str, where_params: tuple = ()) -> None:
        sql = f'DELETE FROM "{table}" WHERE {where}'
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, where_params)
                conn.commit()
            finally:
                self.close(conn)

    def select(self, table: str, where: str = "", where_params: tuple =(),
               *, order_by: str = "", limit: int = 0, offset: int = 0) -> list[dict]:
        sql = f'SELECT * FROM "{table}"'
        params = list(where_params)
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {limit}"
        if offset:
            sql += f" OFFSET {offset}"
        with self._lock:
            conn = self.connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            finally:
                self.close(conn)

    def select_one(self, table: str, where: str = "", where_params: tuple = (),
                   *, order_by: str = "") -> Optional[dict]:
        """查询单条记录,无匹配返回 None。等价于 select(..., limit=1) 取首行。"""
        sql = f'SELECT * FROM "{table}"'
        params = list(where_params)
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        sql += " LIMIT 1"
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(sql, params).fetchone()
                return dict(row) if row is not None else None
            finally:
                self.close(conn)

    def count(self, table: str, where: str = "", where_params: tuple = ()) -> int:
        sql = f'SELECT COUNT(*) FROM "{table}"'
        params = list(where_params)
        if where:
            sql += f" WHERE {where}"
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(sql, params).fetchone()
                return row[0] if row else 0
            finally:
                self.close(conn)

    def exists(self, table: str, where: str, where_params: tuple = ()) -> bool:
        sql = f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1'
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(sql, where_params).fetchone()
                return row is not None
            finally:
                self.close(conn)

    @contextmanager
    def transaction(self):
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN")
                self._in_transaction = True
                yield
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                self._in_transaction = False
                self.close(conn)

    def begin(self) -> None:
        self._lock.acquire()
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            self._in_transaction = True
        except Exception:
            self._lock.release()
            raise

    def commit(self) -> None:
        try:
            conn = self.connect()
            conn.execute("COMMIT")
        finally:
            self._in_transaction = False
            self._lock.release()

    def rollback(self) -> None:
        try:
            conn = self.connect()
            conn.execute("ROLLBACK")
        finally:
            self._in_transaction = False
            self._lock.release()


class InMemoryBackend(SqliteBackend):
    """内存存储后端（测试用）"""

    def __init__(self):
        super().__init__(":memory:", memory_mode=True)


# =============== 后端工厂 ===============
# 按 (backend, db_path, memory_mode) 键缓存: 不同数据库文件的后端各自独立,
# 避免旧实现"第一个后端覆盖全局"导致 sessions/checkpoints/memory 数据散落同一文件
_backend_cache: dict[tuple, "StorageBackend"] = {}
_backend_lock = threading.Lock()


def _backend_key(backend: str, db_path: str | Path | None, memory_mode: bool) -> tuple:
    """生成后端缓存键。db_path 归一化为绝对路径字符串, 避免相对/绝对路径不一致导致重复创建。"""
    if db_path is None:
        resolved = str(DEFAULT_DATA_DIR / "storage.db")
    else:
        resolved = str(Path(db_path).absolute())
    return (backend, resolved, memory_mode)


def get_storage_backend(
    backend: str = "auto",
    db_path: str | Path | None = None,
    *,
    memory_mode: bool = False,
) -> StorageBackend:
    """获取存储后端实例(按 (backend, db_path, memory_mode) 键缓存复用)。

    修复: 旧实现用单一全局单例, 第一次调用后缓存, 后续所有不同 db_path
    的调用全部返回第一个后端, 导致 sessions.db / checkpoints.db / memory.db
    数据散落到同一物理文件。现改为按 key 缓存, 不同数据库文件各自独立。

    Args:
        backend: "auto" | "sqlite" | "redis" | "postgresql"
        db_path: 数据库路径，None 时使用默认路径
        memory_mode: 是否使用 SQLite 内存模式（用于测试）

    Returns:
        StorageBackend 实例
    """
    global _backend_cache

    if backend == "auto":
        from app.config import settings
        backend = (settings.storage_backend or "sqlite").lower()

    key = _backend_key(backend, db_path, memory_mode)
    cached = _backend_cache.get(key)
    if cached is not None:
        return cached

    with _backend_lock:
        cached = _backend_cache.get(key)
        if cached is not None:
            return cached

        if backend == "redis":
            try:
                from app.core.redis_backend import RedisBackend
                from app.config import settings
                inst = RedisBackend(settings.redis_url)
                _backend_cache[key] = inst
                logger.info("使用 Redis 存储后端")
                return inst
            except Exception as e:
                logger.warning(f"Redis 不可用，回退 SQLite: {e}")
                backend = "sqlite"
                key = _backend_key(backend, db_path, memory_mode)
                cached = _backend_cache.get(key)
                if cached is not None:
                    return cached

        if backend == "postgresql":
            try:
                import psycopg  # noqa: F401
                from app.core.postgres_backend import PostgresBackend
                from app.config import settings
                # 结构化存储优先用 storage_postgres_dsn; 未配则回落向量侧 pgvector DSN
                dsn = settings.storage_postgres_dsn or settings.pgvector_client_dsn
                if not dsn:
                    raise RuntimeError(
                        "storage_backend=postgresql 但未配置 STORAGE_POSTGRES_DSN / PGVECTOR_CLIENT_DSN, 回退 SQLite"
                    )
                inst = PostgresBackend(dsn)
                _backend_cache[key] = inst
                logger.info("使用 PostgreSQL 存储后端")
                return inst
            except ImportError as e:
                logger.warning(f"PostgreSQL 不可用(未安装 psycopg)，回退 SQLite: {e}")
                backend = "sqlite"
                key = _backend_key(backend, db_path, memory_mode)
                cached = _backend_cache.get(key)
                if cached is not None:
                    return cached
            except Exception as e:
                logger.warning(f"PostgreSQL 不可用，回退 SQLite: {e}")
                backend = "sqlite"
                key = _backend_key(backend, db_path, memory_mode)
                cached = _backend_cache.get(key)
                if cached is not None:
                    return cached

        # 默认 SQLite
        if db_path is None:
            db_path = DEFAULT_DATA_DIR / "storage.db"
        inst = SqliteBackend(db_path, memory_mode=memory_mode)
        _backend_cache[key] = inst
        logger.info(f"使用 SQLite 存储后端: {db_path}")
        return inst


def reset_storage_backend() -> None:
    """重置后端缓存（测试用）"""
    global _backend_cache
    with _backend_lock:
        _backend_cache = {}
