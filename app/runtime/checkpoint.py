"""Checkpoint 管理器:把图运行状态快照持久化到存储后端,支持暂停/恢复/后台运行。

表 checkpoints 以 run_id 为主键,每次节点边界保存最新快照(upsert)。
- 进程重启后按 run_id 恢复,从头跑还是断点续跑由调用方决定。
- 保留 version 字段,未来可做时间旅行/分支回滚。
- 支持 SQLite / Redis / PostgreSQL 后端切换。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from app.runtime.state import AgentState
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()


class CheckpointManager:
    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 CheckpointManager
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self.db_path = None
        else:
            if not db_path:
                from app.config import settings
                db_path = os.path.join(settings.resolved_data_dir, "checkpoints.db")
            self.db_path = db_path
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            self._backend = get_storage_backend(db_path=db_path)
        
        self._init_db()

    def _init_db(self) -> None:
        with _DB_LOCK:
            schema = {
                "checkpoints": [
                    {"name": "run_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "node", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "status", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
                    {"name": "state", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "version", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 1"},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
                "checkpoint_versions": [
                    {"name": "run_id", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "version", "type": "INTEGER", "constraints": "NOT NULL"},
                    {"name": "node", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "status", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "state", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
            }
            self._backend.init_db(schema)
            # 创建索引
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_ckpt_status ON checkpoints(status)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_ckpt_session ON checkpoints(session_id)"
            )
            self._backend.execute(
                "CREATE INDEX IF NOT EXISTS idx_ckptv_run ON checkpoint_versions(run_id)"
            )

    # ---- 写 ----

    def save(self, state: AgentState) -> str:
        """保存/更新一次运行的状态快照。返回 run_id。"""
        now = time.time()
        payload = state.to_json()

        # 大消息截断保护:防止超长 messages 导致 SQLite 写入失败
        from app.config import settings as _cfg
        cap = int(getattr(_cfg, "checkpoint_message_char_cap", 0) or 0)
        if cap > 0 and payload and len(payload) > cap * 4:
            # 逐条截断 messages 中的大字段
            try:
                data = json.loads(payload)
                if isinstance(data, dict) and "messages" in data:
                    truncated = []
                    for m in data["messages"]:
                        mm = dict(m)
                        content = mm.get("content", "")
                        if isinstance(content, str) and len(content) > cap:
                            mm["content"] = content[:cap] + f"...[已截断,{len(content)}字符]"
                        truncated.append(mm)
                    data["messages"] = truncated
                    payload = json.dumps(data, ensure_ascii=False)
                    logger.debug("Checkpoint: messages truncated to cap=%d, total_len=%d", cap, len(payload))
            except Exception:  # noqa: BLE001
                pass  # 截断失败不影响落盘
        
        # 使用事务包裹双写,防止 checkpoint_versions 写入成功但 checkpoints 失败导致版本不一致
        with self._backend.transaction():
            # 获取当前版本号
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ?",
                (state.run_id,),
                order_by="version DESC",
                limit=1
            )
            version = (rows[0]["version"] + 1) if rows else 1
            
            # 插入版本历史
            self._backend.upsert(
                "checkpoint_versions",
                {
                    "run_id": state.run_id,
                    "version": version,
                    "node": state.node,
                    "status": state.status,
                    "state": payload,
                    "created_at": now,
                },
                conflict_columns=["run_id", "version"]
            )
            
            # 插入或更新 checkpoints
            self._backend.upsert(
                "checkpoints",
                {
                    "run_id": state.run_id,
                    "node": state.node,
                    "status": state.status,
                    "session_id": state.session_id,
                    "state": payload,
                    "version": version,
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["run_id"]
            )
        
        return state.run_id

    def delete(self, run_id: str) -> bool:
        with _DB_LOCK:
            self._backend.delete("checkpoints", "run_id = ?", (run_id,))
            self._backend.delete("checkpoint_versions", "run_id = ?", (run_id,))
            return True

    # ---- 读 ----

    def load(self, run_id: str) -> Optional[AgentState]:
        with _DB_LOCK:
            rows = self._backend.select(
                "checkpoints",
                "run_id = ?",
                (run_id,),
                limit=1
            )
        
        if not rows:
            return None
        try:
            return AgentState.from_json(rows[0]["state"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def exists(self, run_id: str) -> bool:
        with _DB_LOCK:
            return self._backend.exists("checkpoints", "run_id = ?", (run_id,))

    # ---- 时间旅行(按版本加载 / 分支回滚) ----

    def versions(self, run_id: str) -> list[dict]:
        """列出某次运行的全部历史版本(时间旅行视图)。"""
        with _DB_LOCK:
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ?",
                (run_id,),
                order_by="version"
            )
        return [
            {"version": r["version"], "node": r["node"], "status": r["status"], "created_at": r["created_at"]}
            for r in rows
        ]

    def load_version(self, run_id: str, version: int) -> Optional[AgentState]:
        """加载指定历史版本的运行状态(用于时间旅行/分支回滚)。"""
        with _DB_LOCK:
            rows = self._backend.select(
                "checkpoint_versions",
                "run_id = ? AND version = ?",
                (run_id, int(version)),
                limit=1
            )
        
        if not rows:
            return None
        try:
            return AgentState.from_json(rows[0]["state"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def rollback(self, run_id: str, version: int) -> Optional[AgentState]:
        """把最新指针回滚到历史版本(时间旅行分支起点)。"""
        st = self.load_version(run_id, version)
        if st is None:
            return None
        
        with _DB_LOCK:
            now = time.time()
            self._backend.upsert(
                "checkpoints",
                {
                    "run_id": st.run_id,
                    "node": st.node,
                    "status": st.status,
                    "session_id": st.session_id,
                    "state": st.to_json(),
                    "version": int(version),
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["run_id"]
            )
        
        return st

    def list(self, limit: int = 100, offset: int = 0, status: Optional[str] = None,
             session_id: Optional[str] = None, tenant_id: Optional[str] = None) -> list[dict]:
        conditions = []
        params = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = " AND ".join(conditions) if conditions else ""
        
        with _DB_LOCK:
            rows = self._backend.select(
                "checkpoints",
                where,
                tuple(params),
                order_by="updated_at DESC",
                limit=limit,
                offset=offset,
            )
        
        out: list[dict] = []
        for r in rows:
            try:
                st = AgentState.from_json(r["state"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkpoint %s state decode failed: %s", r["run_id"], exc)
                st = None
            base = {
                "run_id": r["run_id"],
                "node": r["node"],
                "status": r["status"],
                "session_id": r["session_id"],
                "version": r["version"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            if st is not None:
                base["summary"] = st.summary()
            out.append(base)
        return out

    def count(self) -> int:
        with _DB_LOCK:
            return self._backend.count("checkpoints")

    def clear(self, status: Optional[str] = None) -> int:
        with _DB_LOCK:
            if status:
                rows = self._backend.select(
                    "checkpoints", "status = ?", (status,), columns=["run_id"]
                )
                run_ids = [r["run_id"] for r in rows]
                self._backend.delete("checkpoints", "status = ?", (status,))
                if run_ids:
                    placeholders = ",".join("?" * len(run_ids))
                    self._backend.delete(
                        "checkpoint_versions",
                        f"run_id IN ({placeholders})",
                        tuple(run_ids),
                    )
            else:
                self._backend.delete("checkpoints", "1 = 1")
                self._backend.delete("checkpoint_versions", "1 = 1")
            return 1

    def cleanup_expired(self, ttl_days: float = 7.0) -> int:
        """清理超过 TTL 的已完成/已取消 checkpoint。

        只清理 status 为 done/failed/cancelled 且 updated_at 超过 ttl_days 天的记录。
        正在运行的运行不会被清理。
        """
        import time as _time
        cutoff = _time.time() - ttl_days * 86400
        with _DB_LOCK:
            # 先查出过期记录
            rows = self._backend.select(
                "checkpoints",
                "status IN ('done', 'failed', 'cancelled') AND updated_at < ?",
                (cutoff,),
                columns=["run_id"],
            )
            if not rows:
                return 0
            run_ids = [r["run_id"] for r in rows]
            self._backend.delete("checkpoints", "status IN ('done', 'failed', 'cancelled') AND updated_at < ?", (cutoff,))
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                self._backend.delete(
                    "checkpoint_versions",
                    f"run_id IN ({placeholders})",
                    tuple(run_ids),
                )
            return len(run_ids)
