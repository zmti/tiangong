"""PostgreSQL 存储后端实现"""
from __future__ import annotations

import json
import threading
import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


class PostgresBackend:
    """PostgreSQL 存储后端"""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._lock = threading.RLock()

    def connect(self) -> Any:
        import psycopg
        return psycopg.connect(self._dsn)

    def close(self, conn: Any) -> None:
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
                        pg_type = col["type"].replace("TEXT", "TEXT").replace("INTEGER", "INTEGER").replace("REAL", "DOUBLE PRECISION")
                        cols.append(f'"{col["name"]}" {pg_type} {col.get("constraints", "")}')
                    sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(cols)})'
                    conn.execute(sql)

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
        placeholders = ", ".join(["%s" for _ in cols])
        col_names = ", ".join([f'"{c}"' for c in cols])
        
        if conflict_columns:
            conflict_cols = ", ".join([f'"{c}"' for c in conflict_columns])
            update_clause = ", ".join([f'"{k}" = EXCLUDED."{k}"' for k in cols if k not in conflict_columns])
            sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders}) ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_clause}'
        else:
            sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'
        
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, tuple(data.values()))
                conn.commit()
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
                conn.commit()
            finally:
                self.close(conn)

    def update(self, table: str, data: dict, where: str, where_params: tuple = ()) -> None:
        set_clause = ", ".join([f'"{k}" = %s' for k in data.keys()])
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
                conn.autocommit = False
                yield
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
                self.close(conn)

    def begin(self) -> None:
        self._lock.acquire()
        conn = self.connect()
        try:
            conn.autocommit = False
        except Exception:
            self._lock.release()
            raise

    def commit(self) -> None:
        try:
            conn = self.connect()
            conn.commit()
        finally:
            self._lock.release()

    def rollback(self) -> None:
        try:
            conn = self.connect()
            conn.rollback()
        finally:
            self._lock.release()
