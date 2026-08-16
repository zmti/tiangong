"""数据层后端选择器:Redis(会话/记忆)与 pgvector(向量)可插拔存储。

原则:能力可用时才启用新后端,否则自动回退现有实现,绝不因缺依赖/缺服务
而让主流程崩溃:
- 会话/记忆:storage_backend=redis 且已安装 redis-py 且 REDIS_URL 可连接 → Redis;
  否则回退 SQLite(默认)。
- 向量:vector_store_backend 为 auto/pgvector 且已安装 psycopg 且
  PGVECTOR_CLIENT_DSN 可连接 → pgvector;否则回退 SQLite+faiss(默认)。

新增实现需满足与原有存储一致的接口(见各自文件注释)。
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def _redis_available() -> tuple[bool, object]:
    """返回 (是否可用, 客户端或 None)。redis-py 未装/配置缺失/连接失败均记不可用。"""
    if (settings.storage_backend or "sqlite").lower() != "redis":
        return False, None
    if not settings.redis_url:
        return False, None
    try:
        import redis  # type: ignore
    except ImportError:  # noqa: BLE001
        logger.warning("storage_backend=redis 但未安装 redis-py,回退 SQLite")
        return False, None
    try:
        client = redis.from_url(settings.redis_url)
        client.ping()
        return True, client
    except Exception:  # noqa: BLE001
        logger.warning("Redis 连接失败(%s),回退 SQLite", settings.redis_url.split("@")[-1])
        return False, None


def get_session_backend() -> tuple[str, object]:
    """返回 (backend, session_store 实例)。backend ∈ {sqlite, redis}。"""
    ok, client = _redis_available()
    if ok:
        from app.memory.session_redis import RedisSessionStore

        return "redis", RedisSessionStore(client=client, max_messages=60)
    from app.memory.session import SessionStore

    return "sqlite", SessionStore()


def _pgvector_available() -> tuple[bool, object]:
    """返回 (是否可用, dsn)。缺 psycopg / 未配置 DSN 记不可用。"""
    backend = (settings.vector_store_backend or "auto").lower()
    if backend == "sqlite":
        return False, None
    if not settings.pgvector_client_dsn:
        return False, None
    try:
        import psycopg  # type: ignore  # noqa: F401, PLC0415
    except ImportError:  # noqa: BLE001
        if backend == "pgvector":
            logger.warning("vector_store_backend=pgvector 但未安装 psycopg,回退 SQLite+faiss")
        return False, None
    try:
        import psycopg  # noqa: F401, PLC0415

        with psycopg.connect(settings.pgvector_client_dsn, connect_timeout=3):
            pass
        return True, settings.pgvector_client_dsn
    except Exception:  # noqa: BLE001
        if backend == "pgvector":
            logger.warning("pgvector 连接失败(%s),回退 SQLite+faiss", settings.pgvector_client_dsn.split("@")[-1])
        return False, None


def get_vector_backend() -> tuple[str, str]:
    """返回 (backend, dsn)。backend ∈ {sqlite, pgvector}。"""
    ok, dsn = _pgvector_available()
    if ok:
        return "pgvector", dsn
    return "sqlite", ""