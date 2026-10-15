"""PostgreSQL 存储后端实现（psycopg 3）。

与 SQLite 后端的关键差异:
1. **占位符翻译**: 调用方统一按 SQLite 风格传 `?` 占位符
   (见 `runtime/checkpoint.py`、`memory/*.py` 等), 但 Postgres 要求 `%s`。
   本后端在**执行前**统一把 SQL 文本里的 `?` 翻成 `%s`(`_pg_sql`),
   各方法的 SQL 构造因此可与 SQLite 版逐行对齐、便于阅读与 diff。
2. **类型映射**: SQLite schema 里的 TEXT/INTEGER/REAL/BLOB 映射到
   Postgres 的 TEXT/BIGINT/DOUBLE PRECISION/BYTEA。
3. **连接策略**: 基线 `autocommit=True`, 单条语句自动提交;
   `transaction()` 内临时 `autocommit=False` 并显式提交/回滚。

依赖: 需 `pip install "psycopg[binary]"`。未安装时由
`get_storage_backend` 捕获 ImportError 自动回退 SQLite(绝不崩主流程)。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Optional

from app.core.storage_backend import StorageBackend

logger = logging.getLogger(__name__)

# SQLite schema 类型 → Postgres 类型(按小写基类型匹配, 未命中回退原样大写)
_PG_TYPES = {
    "text": "TEXT",
    "integer": "BIGINT",
    "int": "BIGINT",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "real": "DOUBLE PRECISION",
    "float": "DOUBLE PRECISION",
    "double": "DOUBLE PRECISION",
    "numeric": "NUMERIC",
    "blob": "BYTEA",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "datetime": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
}


def _pg_type(sqlite_type: str) -> str:
    """把 schema 里的 SQLite 类型映射为 Postgres 类型。"""
    base = sqlite_type.strip().lower().split(" ")[0]
    return _PG_TYPES.get(base, sqlite_type.strip().upper())


class PostgresBackend(StorageBackend):
    """PostgreSQL 存储后端（psycopg 3, 支持会话/记忆等表结构与 SQLite 同构）。"""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgresBackend 需要非空 DSN")
        self._dsn = dsn
        self._lock = threading.RLock()
        self._tx_conn: Any = None  # begin/commit/rollback 复用的开事务连接

    # ---- 连接管理 ----
    def connect(self) -> Any:
        import psycopg

        conn = psycopg.connect(self._dsn)
        conn.autocommit = True  # 单条语句自动提交, 只有 transaction() 内关闭
        return conn

    def close(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _pg_sql(sql: str) -> str:
        """把调用方 SQL 里的 SQLite `?` 占位符翻成 Postgres `%s`。

        仅替换独立的 `?`(本项目 SQL 文本中 `?` 只作占位符, 不出现在字面量里)。
        """
        return sql.replace("?", "%s")

    def _exec(self, conn: Any, sql: str, params: tuple = (), *, fetch: bool = False) -> list[dict]:
        """执行并统一做占位符翻译 + dict 化返回(与 SQLite execute 语义一致)。"""
        cur = conn.execute(self._pg_sql(sql), params)
        if fetch:
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        return []

    # ---- 结构初始化 ----
    def init_db(self, schema: dict[str, list[dict]]) -> None:
        with self._lock:
            conn = self.connect()
            try:
                for table_name, columns in schema.items():
                    cols = []
                    for col in columns:
                        pg_type = _pg_type(col["type"])
                        cols.append(f'"{col["name"]}" {pg_type} {col.get("constraints", "")}')
                    sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(cols)})'
                    conn.execute(sql)

                    for col in columns:
                        if col.get("index"):
                            idx_name = f"idx_{table_name}_{col['name']}"
                            conn.execute(
                                f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                                f'ON "{table_name}" ("{col["name"]}")'
                            )
            finally:
                self.close(conn)

    # ---- 基础读写 ----
    def execute(self, sql: str, params: tuple = (), *, fetch: bool = False) -> list[dict]:
        with self._lock:
            conn = self.connect()
            try:
                return self._exec(conn, sql, params, fetch=fetch)
            finally:
                self.close(conn)

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executemany(self._pg_sql(sql), params_list)
            finally:
                self.close(conn)

    def upsert(self, table: str, data: dict, *, conflict_columns: list[str] | None = None) -> None:
        cols = list(data.keys())
        placeholders = ", ".join(["%s" for _ in cols])
        col_names = ", ".join([f'"{c}"' for c in cols])

        if conflict_columns:
            conflict_cols = ", ".join([f'"{c}"' for c in conflict_columns])
            update_cols = [k for k in cols if k not in conflict_columns]
            if update_cols:
                update_clause = ", ".join([f'"{k}" = EXCLUDED."{k}"' for k in update_cols])
                sql = (
                    f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders}) '
                    f'ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_clause}'
                )
            else:
                sql = (
                    f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders}) '
                    f'ON CONFLICT ({conflict_cols}) DO NOTHING'
                )
        else:
            sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'

        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, tuple(data.values()))
            finally:
                self.close(conn)

    def insert(self, table: str, data: dict) -> None:
        cols = list(data.keys())
        placeholders = ", ".join(["%s" for _ in cols])
        col_names = ", ".join([f'"{c}"' for c in cols])
        sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, tuple(data.values()))
            finally:
                self.close(conn)

    def update(self, table: str, data: dict, where: str, where_params: tuple = ()) -> None:
        set_clause = ", ".join([f'"{k}" = %s' for k in data.keys()])
        sql = f'UPDATE "{table}" SET {set_clause} WHERE {where}'
        params = tuple(data.values()) + where_params
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(self._pg_sql(sql), params)
            finally:
                self.close(conn)

    def delete(self, table: str, where: str, where_params: tuple = ()) -> None:
        sql = f'DELETE FROM "{table}" WHERE {where}'
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(self._pg_sql(sql), where_params)
            finally:
                self.close(conn)

    def select(self, table: str, where: str = "", where_params: tuple = (),
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
                return self._exec(conn, sql, params, fetch=True)
            finally:
                self.close(conn)

    def select_one(self, table: str, where: str = "", where_params: tuple = (),
                   *, order_by: str = "") -> Optional[dict]:
        """查询单条记录, 无匹配返回 None(对齐 SqliteBackend.select_one)。"""
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
                row = conn.execute(self._pg_sql(sql), params).fetchone()
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
                row = conn.execute(self._pg_sql(sql), params).fetchone()
                return row[0] if row else 0
            finally:
                self.close(conn)

    def exists(self, table: str, where: str, where_params: tuple = ()) -> bool:
        sql = f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1'
        with self._lock:
            conn = self.connect()
            try:
                row = conn.execute(self._pg_sql(sql), where_params).fetchone()
                return row is not None
            finally:
                self.close(conn)

    # ---- 事务 ----
    @contextmanager
    def transaction(self):
        with self._lock:
            conn = self.connect()
            try:
                conn.autocommit = False
                yield
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.autocommit = True
                self.close(conn)

    def begin(self) -> None:
        """开事务: 存一个 autocommit=False 的连接, 供 commit/rollback 复用。"""
        self._lock.acquire()
        conn = self.connect()
        try:
            conn.autocommit = False
            self._tx_conn = conn
        except Exception:
            self._lock.release()
            self._tx_conn = None
            raise

    def commit(self) -> None:
        try:
            if self._tx_conn is not None:
                self._tx_conn.commit()
                self.close(self._tx_conn)
        finally:
            self._tx_conn = None
            self._lock.release()

    def rollback(self) -> None:
        try:
            if self._tx_conn is not None:
                self._tx_conn.rollback()
                self.close(self._tx_conn)
        finally:
            self._tx_conn = None
            self._lock.release()
