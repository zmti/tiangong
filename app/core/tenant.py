"""多租户核心模型:租户管理、API Key 映射、用量统计。

设计:
- 每个租户有唯一 tenant_id(slug),名称,计划等级,状态
- API Key → Tenant 绑定:一个 Key 只能属于一个租户,支持轮换
- 用量统计:每租户独立 token/cost/工具调用计数,持久化到 SQLite
- RBAC:admin(管理+用量查看)/user(普通使用)/readonly(仅查询)
- 数据隔离:所有存储操作自动附加 tenant_id 过滤(通过 TenantContext)
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

# 租户数据表 schema
_TENANT_SCHEMA = {
    "tenants": [
        {"name": "tenant_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "plan", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'free'"},
        {"name": "status", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'active'"},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "metadata", "type": "TEXT", "constraints": "DEFAULT '{}'"},
    ],
    "api_keys": [
        {"name": "key_hash", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "key_prefix", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "tenant_id", "type": "TEXT", "constraints": "NOT NULL REFERENCES tenants(tenant_id)"},
        {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
        {"name": "role", "type": "TEXT", "constraints": "NOT NULL DEFAULT 'user'"},
        {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "last_used_at", "type": "REAL", "constraints": ""},
        {"name": "expires_at", "type": "REAL", "constraints": ""},
        {"name": "enabled", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 1"},
    ],
    "usage_stats": [
        {"name": "tenant_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
        {"name": "llm_calls", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "tool_calls", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "prompt_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "completion_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "cost_usd", "type": "REAL", "constraints": "DEFAULT 0.0"},
        {"name": "sessions_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "memory_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "chunks_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
        {"name": "last_reset", "type": "REAL", "constraints": "NOT NULL"},
        {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
    ],
}

# RBAC 角色权限
ROLE_PERMISSIONS = {
    "admin": {"read": True, "write": True, "manage": True, "stats": True},
    "user": {"read": True, "write": True, "manage": False, "stats": False},
    "readonly": {"read": True, "write": False, "manage": False, "stats": False},
}


def _hash_key(key: str) -> str:
    """对 API Key 做 SHA-256 哈希存储(不存明文)。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _prefix_key(key: str) -> str:
    """生成 Key 前缀用于展示(如 sk-xxxx...abcd)。"""
    if len(key) <= 8:
        return key
    return key[:4] + "..." + key[-4:]


class TenantManager:
    """租户管理器:多租户 CRUD、API Key 验证、用量统计。"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from app.config import settings
            db_path = os.path.join(settings.resolved_data_dir, "tenants.db")
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                for table_name, columns in _TENANT_SCHEMA.items():
                    cols_sql = ", ".join(
                        f"{c['name']} {c['type']}" + (f" {c['constraints']}" if c.get("constraints") else "")
                        for c in columns
                    )
                    conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({cols_sql})")
                    if table_name == "api_keys":
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_tenant ON api_keys(tenant_id)")
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_prefix ON api_keys(key_prefix)")
                    elif table_name == "tenants":
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status)")
                conn.commit()
            finally:
                conn.close()

    # ---- 租户 CRUD ----

    def create_tenant(self, tenant_id: str, name: str, plan: str = "free",
                      metadata: Optional[dict] = None) -> dict:
        """创建租户。"""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO tenants (tenant_id, name, plan, status, created_at, updated_at, metadata) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                    (tenant_id, name, plan, now, now, json.dumps(metadata or {}, ensure_ascii=False)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO usage_stats (tenant_id, last_reset, updated_at) VALUES (?, ?, ?)",
                    (tenant_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_tenants(self, limit: int = 100) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT t.*, COALESCE(u.llm_calls, 0) as llm_calls, "
                    "COALESCE(u.prompt_tokens, 0) as prompt_tokens, "
                    "COALESCE(u.cost_usd, 0) as cost_usd "
                    "FROM tenants t LEFT JOIN usage_stats u ON t.tenant_id = u.tenant_id "
                    "ORDER BY t.created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def update_tenant(self, tenant_id: str, **kwargs) -> Optional[dict]:
        allowed = {"name", "plan", "status", "metadata"}
        set_parts = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                set_parts.append(f"{k} = ?")
                params.append(v)
        if not set_parts:
            return self.get_tenant(tenant_id)
        params.append(time.time())
        params.append(tenant_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE tenants SET {', '.join(set_parts)}, updated_at = ? WHERE tenant_id = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_tenant(tenant_id)

    def delete_tenant(self, tenant_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
                conn.execute("DELETE FROM api_keys WHERE tenant_id = ?", (tenant_id,))
                conn.execute("DELETE FROM usage_stats WHERE tenant_id = ?", (tenant_id,))
                conn.commit()
                return True
            finally:
                conn.close()

    # ---- API Key 管理 ----

    def create_api_key(self, tenant_id: str, name: str, role: str = "user",
                       expires_at: Optional[float] = None) -> dict:
        """生成并注册新 API Key。返回明文 Key(仅一次返回)。"""
        # 生成随机 Key
        import secrets
        raw_key = "dsh_" + secrets.token_hex(24)
        key_hash = _hash_key(raw_key)
        key_prefix = _prefix_key(raw_key)
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                # 验证租户存在
                tenant = conn.execute("SELECT tenant_id FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
                if not tenant:
                    raise ValueError(f"租户不存在: {tenant_id}")
                conn.execute(
                    "INSERT INTO api_keys (key_hash, key_prefix, tenant_id, name, role, created_at, expires_at, enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (key_hash, key_prefix, tenant_id, name, role, now, expires_at or 0),
                )
                conn.commit()
            finally:
                conn.close()
        return {"key": raw_key, "key_prefix": key_prefix, "tenant_id": tenant_id, "role": role}

    def list_api_keys(self, tenant_id: str) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT key_prefix, name, role, enabled, created_at, last_used_at, expires_at "
                    "FROM api_keys WHERE tenant_id = ? ORDER BY created_at DESC",
                    (tenant_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def revoke_key(self, tenant_id: str, key_prefix: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE api_keys SET enabled = 0 WHERE tenant_id = ? AND key_prefix = ?",
                    (tenant_id, key_prefix),
                )
                conn.commit()
                return conn.total_changes > 0
            finally:
                conn.close()

    # ---- Key 验证 ----

    def verify_key(self, raw_key: str) -> Optional[dict]:
        """验证 API Key,返回 {tenant_id, role, key_prefix} 或 None。"""
        if not raw_key:
            return None
        key_hash = _hash_key(raw_key)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT ak.tenant_id, ak.role, ak.key_prefix, ak.enabled, ak.expires_at, t.status as tenant_status "
                    "FROM api_keys ak JOIN tenants t ON ak.tenant_id = t.tenant_id "
                    "WHERE ak.key_hash = ? AND ak.enabled = 1",
                    (key_hash,),
                ).fetchone()
                if not row:
                    return None
                if row["tenant_status"] != "active":
                    return None
                # 检查过期
                if row["expires_at"] and row["expires_at"] > 0 and time.time() > row["expires_at"]:
                    return None
                # 更新 last_used_at
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
                    (time.time(), key_hash),
                )
                conn.commit()
                return {
                    "tenant_id": row["tenant_id"],
                    "role": row["role"],
                    "key_prefix": row["key_prefix"],
                }
            finally:
                conn.close()

    # ---- 用量统计 ----

    def record_usage(self, tenant_id: str, llm_calls: int = 0, tool_calls: int = 0,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     cost_usd: float = 0.0) -> dict:
        """记录单次用量。"""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO usage_stats
                       (tenant_id, llm_calls, tool_calls, prompt_tokens, completion_tokens, cost_usd, updated_at, last_reset)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tenant_id) DO UPDATE SET
                       llm_calls = usage_stats.llm_calls + excluded.llm_calls,
                       tool_calls = usage_stats.tool_calls + excluded.tool_calls,
                       prompt_tokens = usage_stats.prompt_tokens + excluded.prompt_tokens,
                       completion_tokens = usage_stats.completion_tokens + excluded.completion_tokens,
                       cost_usd = usage_stats.cost_usd + excluded.cost_usd,
                       updated_at = excluded.updated_at""",
                    (tenant_id, llm_calls, tool_calls, prompt_tokens, completion_tokens,
                     cost_usd, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_usage(tenant_id)

    def get_usage(self, tenant_id: str) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM usage_stats WHERE tenant_id = ?", (tenant_id,)
                ).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

    def reset_usage(self, tenant_id: str) -> bool:
        """重置用量统计(月初清零等)。"""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE usage_stats SET llm_calls=0, tool_calls=0, prompt_tokens=0, "
                    "completion_tokens=0, cost_usd=0, last_reset=?, updated_at=? WHERE tenant_id=?",
                    (now, now, tenant_id),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    # ---- 初始化:写入默认管理员 Key ----

    def init_default_admin(self) -> None:
        """首次启动时创建默认 admin 租户和 Key。"""
        default_key = os.environ.get("DEFAULT_ADMIN_KEY", "")
        if not default_key:
            return
        tenant_id = "admin"
        tenant = self.get_tenant(tenant_id)
        if tenant:
            return
        self.create_tenant(tenant_id, "Admin", plan="enterprise")
        self.create_api_key(tenant_id, "Default Admin Key", role="admin")
        # 实际 Key 从环境变量读取
        import secrets
        raw_key = default_key
        key_hash = _hash_key(raw_key)
        key_prefix = _prefix_key(raw_key)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO api_keys (key_hash, key_prefix, tenant_id, name, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key_hash, key_prefix, tenant_id, "Default Admin", "admin", time.time()),
                )
                conn.commit()
            finally:
                conn.close()


# 模块级单例
_manager: Optional[TenantManager] = None


def get_tenant_manager() -> TenantManager:
    global _manager
    if _manager is None:
        _manager = TenantManager()
        _manager.init_default_admin()
    return _manager
