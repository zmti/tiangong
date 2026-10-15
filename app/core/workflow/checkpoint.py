"""工作流节点级 Checkpoint 管理器。

支持:
- 每个节点执行完成后自动保存 checkpoint
- 断点续跑:跳过已成功的节点,从失败节点恢复
- Checkpoint 版本历史(时间旅行)
- 自动清理过期 checkpoint
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional
import sqlite3

logger = logging.getLogger(__name__)


@dataclass
class NodeCheckpoint:
    """节点检查点。"""
    run_id: str
    node_id: str
    status: str  # running / success / failed / skipped
    variables_snapshot: dict = field(default_factory=dict)
    result: Optional[dict] = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    checksum: str = ""
    duration: float = 0.0
    attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "status": self.status,
            "variables_snapshot": self.variables_snapshot,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp,
            "checksum": self.checksum,
            "duration": self.duration,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NodeCheckpoint":
        return cls(
            run_id=data["run_id"],
            node_id=data["node_id"],
            status=data["status"],
            variables_snapshot=data.get("variables_snapshot", {}),
            result=data.get("result"),
            error=data.get("error"),
            timestamp=data.get("timestamp", time.time()),
            checksum=data.get("checksum", ""),
            duration=data.get("duration", 0.0),
            attempt=data.get("attempt", 1),
        )


class WorkflowCheckpointManager:
    """工作流节点级 Checkpoint 管理器。

    存储结构:
    - checkpoints 表: 每个节点的检查点记录
    - checkpoints_history 表: 历史版本(支持时间旅行)
    - runs_metadata 表: 运行元数据

    使用 SQLite 持久化,支持多运行并发。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        if db_path:
            self._get_conn()

    def _no_db(self) -> bool:
        return not self.db_path

    def _get_conn(self) -> Optional[sqlite3.Connection]:
        """获取数据库连接(懒初始化)。"""
        if not self.db_path:
            return None
        if self._conn is None:
            import os
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL 写前日志:降低单连接写锁冲突概率(多运行并发持久化 checkpoint 时)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                # C 盘空间不足时 ORDER BY 需临时文件 → "database or disk is full"
                # MEMORY 模式让排序/聚合在内存完成,避免依赖系统 TEMP
                self._conn.execute("PRAGMA temp_store=MEMORY")
            except sqlite3.DatabaseError:
                pass
            self._create_tables()
        return self._conn

    def _create_tables(self) -> None:
        """创建数据表。"""
        conn = self._get_conn()
        conn.executescript("""
            -- 节点检查点表
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                variables_snapshot TEXT DEFAULT '{}',
                result TEXT,
                error TEXT,
                timestamp REAL NOT NULL,
                checksum TEXT DEFAULT '',
                duration REAL DEFAULT 0,
                attempt INTEGER DEFAULT 1,
                PRIMARY KEY (run_id, node_id)
            );

            -- 检查点历史表(支持时间旅行)
            CREATE TABLE IF NOT EXISTS checkpoints_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                status TEXT NOT NULL,
                variables_snapshot TEXT DEFAULT '{}',
                result TEXT,
                error TEXT,
                timestamp REAL NOT NULL,
                checksum TEXT DEFAULT '',
                duration REAL DEFAULT 0,
                attempt INTEGER DEFAULT 1
            );

            -- 运行元数据表
            CREATE TABLE IF NOT EXISTS runs_metadata (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                context TEXT DEFAULT '{}',
                variables TEXT DEFAULT '{}',
                started_at REAL NOT NULL,
                finished_at REAL,
                error TEXT,
                total_nodes INTEGER DEFAULT 0,
                completed_nodes INTEGER DEFAULT 0,
                skipped_nodes INTEGER DEFAULT 0
            );

            -- 索引
            CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id ON checkpoints(run_id);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_history_run_id ON checkpoints_history(run_id);
            CREATE INDEX IF NOT EXISTS idx_runs_metadata_status ON runs_metadata(status);
        """)
        conn.commit()

    def save_checkpoint(
        self,
        run_id: str,
        node_id: str,
        status: str,
        variables: dict[str, Any],
        result: Optional[dict] = None,
        error: Optional[str] = None,
        duration: float = 0.0,
        attempt: int = 1,
    ) -> NodeCheckpoint:
        """保存节点检查点。

        Args:
            run_id: 运行 ID
            node_id: 节点 ID
            status: 节点状态
            variables: 当前变量快照
            result: 节点执行结果
            error: 错误信息(如果失败)
            duration: 执行耗时(秒)
            attempt: 重试次数

        Returns:
            保存的检查点对象
        """
        if self._no_db():
            return NodeCheckpoint(run_id=run_id, node_id=node_id, status=status, variables_snapshot=variables, result=result, error=error, duration=duration, attempt=attempt)
        conn = self._get_conn()
        now = time.time()

        # 计算结果 checksum
        checksum = ""
        if result:
            checksum = hashlib.sha256(
                json.dumps(result, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]

        # 插入或更新
        conn.execute("""
            INSERT OR REPLACE INTO checkpoints
            (run_id, node_id, status, variables_snapshot, result, error, timestamp, checksum, duration, attempt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, node_id, status,
            json.dumps(variables, default=str),
            json.dumps(result, default=str) if result else None,
            error,
            now,
            checksum,
            duration,
            attempt,
        ))

        # 同时写入历史表
        conn.execute("""
            INSERT INTO checkpoints_history
            (run_id, node_id, status, variables_snapshot, result, error, timestamp, checksum, duration, attempt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, node_id, status,
            json.dumps(variables, default=str),
            json.dumps(result, default=str) if result else None,
            error,
            now,
            checksum,
            duration,
            attempt,
        ))

        conn.commit()

        return NodeCheckpoint(
            run_id=run_id,
            node_id=node_id,
            status=status,
            variables_snapshot=variables,
            result=result,
            error=error,
            timestamp=now,
            checksum=checksum,
            duration=duration,
            attempt=attempt,
        )

    def load_checkpoint(self, run_id: str, node_id: str) -> Optional[NodeCheckpoint]:
        """加载节点检查点。

        Returns:
            检查点对象,如果不存在则返回 None
        """
        if self._no_db():
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? AND node_id = ?",
            (run_id, node_id),
        ).fetchone()

        if not row:
            return None

        return NodeCheckpoint(
            run_id=row["run_id"],
            node_id=row["node_id"],
            status=row["status"],
            variables_snapshot=json.loads(row["variables_snapshot"] or "{}"),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            timestamp=row["timestamp"],
            checksum=row["checksum"] or "",
            duration=row["duration"] or 0.0,
            attempt=row["attempt"] or 1,
        )

    def get_skippable_nodes(self, run_id: str) -> set[str]:
        """获取可以跳过的已完成节点(ID 集合)。

        Returns:
            已成功执行的节点 ID 集合
        """
        if self._no_db():
            return set()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT node_id FROM checkpoints WHERE run_id = ? AND status = 'success'",
            (run_id,),
        ).fetchall()
        return {row["node_id"] for row in rows}

    def get_failed_nodes(self, run_id: str) -> set[str]:
        """获取失败的节点 ID 集合。"""
        if self._no_db():
            return set()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT node_id FROM checkpoints WHERE run_id = ? AND status = 'failed'",
            (run_id,),
        ).fetchall()
        return {row["node_id"] for row in rows}

    def get_running_nodes(self, run_id: str) -> set[str]:
        """获取正在执行的节点 ID 集合。"""
        if self._no_db():
            return set()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT node_id FROM checkpoints WHERE run_id = ? AND status = 'running'",
            (run_id,),
        ).fetchall()
        return {row["node_id"] for row in rows}

    def get_all_checkpoints(self, run_id: str) -> list[NodeCheckpoint]:
        """获取运行所有的检查点。"""
        if self._no_db():
            return []
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY timestamp",
            (run_id,),
        ).fetchall()
        return [
            NodeCheckpoint(
                run_id=row["run_id"],
                node_id=row["node_id"],
                status=row["status"],
                variables_snapshot=json.loads(row["variables_snapshot"] or "{}"),
                result=json.loads(row["result"]) if row["result"] else None,
                error=row["error"],
                timestamp=row["timestamp"],
                checksum=row["checksum"] or "",
                duration=row["duration"] or 0.0,
                attempt=row["attempt"] or 1,
            )
            for row in rows
        ]

    def save_run_metadata(
        self,
        run_id: str,
        workflow_id: str,
        context: dict,
        variables: dict,
    ) -> None:
        """保存运行元数据。"""
        if self._no_db():
            return
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO runs_metadata
            (run_id, workflow_id, status, context, variables, started_at, completed_nodes)
            VALUES (?, ?, 'running', ?, ?, ?, 0)
        """, (
            run_id, workflow_id,
            json.dumps(context, default=str),
            json.dumps(variables, default=str),
            time.time(),
        ))
        conn.commit()

    def update_run_metadata(
        self,
        run_id: str,
        status: str,
        error: Optional[str] = None,
        completed_nodes: int = 0,
        skipped_nodes: int = 0,
    ) -> None:
        """更新运行元数据。"""
        if self._no_db():
            return
        conn = self._get_conn()
        conn.execute("""
            UPDATE runs_metadata
            SET status = ?, error = ?, finished_at = ?, completed_nodes = ?, skipped_nodes = ?
            WHERE run_id = ?
        """, (
            status, error, time.time(), completed_nodes, skipped_nodes, run_id
        ))
        conn.commit()

    def load_run_metadata(self, run_id: str) -> Optional[dict]:
        """加载运行元数据。"""
        if self._no_db():
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM runs_metadata WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "workflow_id": row["workflow_id"],
            "status": row["status"],
            "context": json.loads(row["context"] or "{}"),
            "variables": json.loads(row["variables"] or "{}"),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "completed_nodes": row["completed_nodes"],
            "skipped_nodes": row["skipped_nodes"],
        }

    def get_checkpoint_versions(self, run_id: str, node_id: str) -> list[dict]:
        """获取节点的检查点版本历史。"""
        if self._no_db():
            return []
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM checkpoints_history
               WHERE run_id = ? AND node_id = ?
               ORDER BY timestamp""",
            (run_id, node_id),
        ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "status": row["status"],
                "duration": row["duration"],
                "attempt": row["attempt"],
                "checksum": row["checksum"],
            }
            for row in rows
        ]

    def cleanup_old_checkpoints(self, max_age_hours: float = 24.0) -> int:
        """清理过期的检查点。

        Args:
            max_age_hours: 保留时长(小时)

        Returns:
            清理的记录数
        """
        if self._no_db():
            return 0
        conn = self._get_conn()
        cutoff = time.time() - max_age_hours * 3600

        # 清理检查点表
        before_total = conn.total_changes
        deleted_checkpoints = conn.execute(
            "DELETE FROM checkpoints WHERE timestamp < ?",
            (cutoff,),
        ).rowcount

        # 清理历史表(保留最近 100 条每节点)
        conn.execute("""
            DELETE FROM checkpoints_history
            WHERE id NOT IN (
                SELECT id FROM checkpoints_history
                WHERE run_id = checkpoints_history.run_id
                ORDER BY timestamp DESC
                LIMIT 100
            )
            AND timestamp < ?
        """, (cutoff,))
        # 正确计算本次语句删除的历史行数(conn.total_changes 是连接生命周期累计值,
        # 必须取差值)
        deleted_history = (conn.total_changes - before_total) - deleted_checkpoints

        conn.commit()
        logger.info(f"清理过期检查点: checkpoints={deleted_checkpoints}, history={deleted_history}")
        return deleted_checkpoints + deleted_history

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
