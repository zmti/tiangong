"""Checkpoint 管理器:把图运行状态快照持久化到 SQLite,支持暂停/恢复/后台运行。

表 checkpoints 以 run_id 为主键,每次节点边界保存最新快照(upsert)。
- 进程重启后按 run_id 恢复,从头跑还是断点续跑由调用方决定。
- 保留 version 字段,未来可做时间旅行/分支回滚。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional

from app.runtime.state import AgentState

_DB_LOCK = threading.Lock()


class CheckpointManager:
    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            from app.config import settings

            db_path = os.path.join(settings.resolved_data_dir, "checkpoints.db")
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        run_id TEXT PRIMARY KEY,
                        node TEXT NOT NULL,
                        status TEXT NOT NULL,
                        session_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ckpt_status ON checkpoints(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ckpt_session ON checkpoints(session_id)"
                )
                # 时间旅行:每次 save 追加一份版本快照(不覆盖历史)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS checkpoint_versions (
                        run_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        node TEXT NOT NULL,
                        status TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (run_id, version)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ckptv_run ON checkpoint_versions(run_id)"
                )
                conn.commit()
            finally:
                conn.close()

    # ---- 写 ----

    def save(self, state: AgentState) -> str:
        """保存/更新一次运行的状态快照。返回 run_id。

        每次调用都会追加一份版本快照(checkpoint_versions),用于时间旅行/分支回滚;
        同时 upsert 到 checkpoints 作为"最新"指针。
        """
        now = time.time()
        payload = state.to_json()
        with _DB_LOCK:
            conn = self._connect()
            try:
                # 版本号取自版本历史最大值,保证追加单调递增(回滚不破坏计数)
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM checkpoint_versions WHERE run_id=?",
                    (state.run_id,),
                ).fetchone()
                version = int(row["v"]) + 1
                conn.execute(
                    """
                    INSERT INTO checkpoints
                    (run_id, node, status, session_id, state, version, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        node=excluded.node, status=excluded.status,
                        session_id=excluded.session_id, state=excluded.state,
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (
                        state.run_id,
                        state.node,
                        state.status,
                        state.session_id,
                        payload,
                        version,
                        now,
                        now,
                    ),
                )
                # 版本历史:append-only,不覆盖
                conn.execute(
                    """
                    INSERT OR REPLACE INTO checkpoint_versions
                    (run_id, version, node, status, state, created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (state.run_id, version, state.node, state.status, payload, now),
                )
                conn.commit()
            finally:
                conn.close()
        return state.run_id

    def delete(self, run_id: str) -> bool:
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM checkpoints WHERE run_id=?", (run_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    # ---- 读 ----

    def load(self, run_id: str) -> Optional[AgentState]:
        with _DB_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT state FROM checkpoints WHERE run_id=?", (run_id,)
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        try:
            return AgentState.from_json(row["state"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def exists(self, run_id: str) -> bool:
        with _DB_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM checkpoints WHERE run_id=?", (run_id,)
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    # ---- 时间旅行(按版本加载 / 分支回滚) ----

    def versions(self, run_id: str) -> list[dict]:
        """列出某次运行的全部历史版本(时间旅行视图)。"""
        with _DB_LOCK:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT version, node, status, created_at FROM checkpoint_versions "
                    "WHERE run_id=? ORDER BY version",
                    (run_id,),
                ).fetchall()
            finally:
                conn.close()
        return [
            {"version": r["version"], "node": r["node"], "status": r["status"], "created_at": r["created_at"]}
            for r in rows
        ]

    def load_version(self, run_id: str, version: int) -> Optional[AgentState]:
        """加载指定历史版本的运行状态(用于时间旅行/分支回滚)。"""
        with _DB_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT state FROM checkpoint_versions WHERE run_id=? AND version=?",
                    (run_id, int(version)),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        try:
            return AgentState.from_json(row["state"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def rollback(self, run_id: str, version: int) -> Optional[AgentState]:
        """把最新指针回滚到历史版本(时间旅行分支起点)。

        返回回滚后的 AgentState;其 run_id 保持原值,node 指向该版本所处节点,
        调用方据此从该节点继续执行(相当于从历史分支重新分叉)。
        """
        st = self.load_version(run_id, version)
        if st is None:
            return None
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO checkpoints
                    (run_id, node, status, session_id, state, version, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        node=excluded.node, status=excluded.status,
                        session_id=excluded.session_id, state=excluded.state,
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (
                        st.run_id,
                        st.node,
                        st.status,
                        st.session_id,
                        st.to_json(),
                        int(version),
                        time.time(),
                        time.time(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return st

    def list(self, limit: int = 100, status: Optional[str] = None,
             session_id: Optional[str] = None) -> list[dict]:
        sql = "SELECT run_id, node, status, session_id, version, created_at, updated_at, state FROM checkpoints"
        conds: list[str] = []
        params: list = []
        if status:
            conds.append("status=?")
            params.append(status)
        if session_id:
            conds.append("session_id=?")
            params.append(session_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with _DB_LOCK:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        out: list[dict] = []
        for r in rows:
            try:
                st = AgentState.from_json(r["state"])
            except Exception:  # noqa: BLE001  # 快照损坏时给出基础信息
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
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
                return int(row[0])
            finally:
                conn.close()

    def clear(self, status: Optional[str] = None) -> int:
        with _DB_LOCK:
            conn = self._connect()
            try:
                if status:
                    cur = conn.execute(
                        "DELETE FROM checkpoints WHERE status=?", (status,)
                    )
                else:
                    cur = conn.execute("DELETE FROM checkpoints")
                    conn.execute("DELETE FROM checkpoint_versions")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
