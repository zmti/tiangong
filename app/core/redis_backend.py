"""Redis 存储后端实现"""
from __future__ import annotations

import json
import threading
import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


class RedisBackend:
    """Redis 存储后端（用于会话和缓存数据）"""

    def __init__(self, redis_url: str):
        import redis as redis_lib
        self._client = redis_lib.from_url(redis_url, decode_responses=True)
        self._lock = threading.RLock()
        self._prefix = "agent:"

    def _key(self, table: str, key: str) -> str:
        return f"{self._prefix}{table}:{key}"

    def connect(self) -> Any:
        return self._client

    def close(self, conn: Any) -> None:
        pass  # Redis 连接由连接池管理

    def init_db(self, schema: dict[str, list[dict]]) -> None:
        pass  # Redis 无 schema 概念

    def execute(self, sql: str, params: tuple = (), *, fetch: bool = False) -> list[dict]:
        raise NotImplementedError("Redis 不支持 SQL 执行")

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        raise NotImplementedError("Redis 不支持 SQL 执行")

    def upsert(self, table: str, data: dict, *, conflict_columns: list[str] | None = None) -> None:
        key_field = conflict_columns[0] if conflict_columns else "id"
        key = data.get(key_field)
        if key is None:
            raise ValueError(f"缺少主键字段: {key_field}")
        with self._lock:
            self._client.set(self._key(table, key), json.dumps(data, ensure_ascii=False))

    def insert(self, table: str, data: dict) -> None:
        key = data.get("id")
        if key is None:
            import uuid
            key = str(uuid.uuid4())
            data["id"] = key
        with self._lock:
            self._client.set(self._key(table, key), json.dumps(data, ensure_ascii=False))

    def update(self, table: str, data: dict, where: str, where_params: tuple = ()) -> None:
        # 简化实现：假设 where 是 "id = ?"
        if "id" in data:
            with self._lock:
                existing = self._client.get(self._key(table, data["id"]))
                if existing:
                    existing_data = json.loads(existing)
                    existing_data.update(data)
                    self._client.set(self._key(table, data["id"]), json.dumps(existing_data, ensure_ascii=False))

    def delete(self, table: str, where: str, where_params: tuple = ()) -> None:
        if where_params and len(where_params) == 1:
            with self._lock:
                self._client.delete(self._key(table, where_params[0]))

    def select(self, table: str, where: str = "", where_params: tuple =(),
               *, order_by: str = "", limit: int = 0, offset: int = 0) -> list[dict]:
        pattern = f"{self._prefix}{table}:*"
        with self._lock:
            keys = list(self._client.scan_iter(match=pattern, count=1000))
            results = []
            for key in keys:
                data = self._client.get(key)
                if data:
                    results.append(json.loads(data))
            return results[:limit] if limit else results

    def count(self, table: str, where: str = "", where_params: tuple = ()) -> int:
        pattern = f"{self._prefix}{table}:*"
        with self._lock:
            return sum(1 for _ in self._client.scan_iter(match=pattern, count=1000))

    def exists(self, table: str, where: str, where_params: tuple = ()) -> bool:
        if where_params and len(where_params) == 1:
            with self._lock:
                return self._client.exists(self._key(table, where_params[0])) > 0
        return False

    @contextmanager
    def transaction(self):
        # Redis 无事务（简化实现）
        yield

    def begin(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass
