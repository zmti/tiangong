"""工作流数据库持久化模块。

提供工作流和运行记录的 SQLite 存储。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from app.core.workflow.models import Workflow, WorkflowRun, WorkflowNode, WorkflowEdge, NodeType

logger = logging.getLogger(__name__)

# 数据库路径
DATA_DIR = Path(__file__).parent.parent.parent / "data"
WORKFLOW_DB = DATA_DIR / "workflows.db"


class WorkflowDB:
    """工作流数据库管理器。"""
    
    def __init__(self, db_path: Path = WORKFLOW_DB):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
    
    def connect(self) -> sqlite3.Connection:
        """连接数据库，创建表。"""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._create_tables()
            self._migrate_tables()
            logger.info(f"工作流数据库已连接: {self.db_path}")
        return self._conn

    def _migrate_tables(self):
        """迁移表结构，添加新字段。"""
        conn = self.connect()

        # 检查并添加 error_mode 列
        try:
            conn.execute("ALTER TABLE workflows ADD COLUMN error_mode TEXT DEFAULT 'fail_fast'")
            logger.info("已添加 error_mode 列到 workflows 表")
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 检查并添加 timeout 列
        try:
            conn.execute("ALTER TABLE workflows ADD COLUMN timeout REAL DEFAULT 1800")
            logger.info("已添加 timeout 列到 workflows 表")
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 检查并添加 session_id 列
        try:
            conn.execute("ALTER TABLE workflow_runs ADD COLUMN session_id TEXT DEFAULT ''")
            logger.info("已添加 session_id 列到 workflow_runs 表")
        except sqlite3.OperationalError:
            pass  # 列已存在

        # 添加索引
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_session_id ON workflow_runs(session_id)")
            logger.info("已创建 session_id 索引")
        except sqlite3.OperationalError:
            pass  # 索引已存在

        # 检查并添加 workflow_versions 表
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_versions (
                    version_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    nodes TEXT NOT NULL,
                    edges TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow_id ON workflow_versions(workflow_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_versions_created_at ON workflow_versions(created_at DESC)")
            logger.info("已创建 workflow_versions 表")
        except sqlite3.OperationalError:
            pass  # 表已存在

        # 检查并添加 workflow_schedules 表
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    cron_expr TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    last_run_at REAL,
                    next_run_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_schedules_workflow_id ON workflow_schedules(workflow_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_schedules_enabled ON workflow_schedules(enabled)")
            logger.info("已创建 workflow_schedules 表")
        except sqlite3.OperationalError:
            pass  # 表已存在

        conn.commit()
    
    def close(self):
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("工作流数据库已关闭")
    
    def _create_tables(self):
        """创建工作流相关表。"""
        conn = self.connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                version TEXT DEFAULT '1.0.0',
                nodes TEXT NOT NULL,
                edges TEXT NOT NULL,
                entry_node TEXT,
                output_schema TEXT,
                metadata TEXT DEFAULT '{}',
                error_mode TEXT DEFAULT 'fail_fast',
                timeout REAL DEFAULT 1800,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                session_id TEXT DEFAULT '',
                context TEXT DEFAULT '{}',
                variables TEXT DEFAULT '{}',
                node_states TEXT DEFAULT '{}',
                history TEXT DEFAULT '[]',
                error TEXT,
                started_at REAL NOT NULL,
                finished_at REAL,
                output TEXT,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)")
        # session_id 索引在 _migrate_tables 中创建(兼容旧数据库)
        conn.commit()
        logger.info("工作流表创建完成")
    
    # ===== 工作流操作 =====
    
    def save_workflow(self, workflow: Workflow) -> str:
        """保存工作流。"""
        conn = self.connect()
        now = time.time()

        # 序列化节点和边
        nodes_json = json.dumps([
            {
                "id": n.id,
                "type": n.type.value,
                "name": n.name,
                "config": n.config,
                "description": n.description,
                "tool_name": n.tool_name,
                "agent_config": n.agent_config,
                "condition_expr": n.condition_expr,
                "loop_config": n.loop_config,
                "http_config": n.http_config,
                "script_code": n.script_code,
                "timeout": n.timeout,
                "branches": n.branches,
                # 新字段
                "error_strategy": n.error_strategy.value if hasattr(n.error_strategy, 'value') else str(n.error_strategy),
                "retry_count": n.retry_count,
                "retry_delay": n.retry_delay,
                "fallback_node_id": n.fallback_node_id,
                "loop_mode": n.loop_mode.value if hasattr(n.loop_mode, 'value') else str(n.loop_mode),
                "subgraph_ids": n.subgraph_ids,
                "position": n.position,
            }
            for n in workflow.nodes.values()
        ])
        
        edges_json = json.dumps([
            {
                "id": e.id,
                "source": e.source,
                "target": e.target,
                "condition": e.condition,
                "condition_expr": e.condition_expr,
            }
            for e in workflow.edges
        ])
        
        # 插入或更新
        conn.execute("""
            INSERT OR REPLACE INTO workflows
            (id, name, description, version, nodes, edges, entry_node, output_schema, metadata, error_mode, timeout, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            workflow.id,
            workflow.name,
            workflow.description,
            workflow.version,
            nodes_json,
            edges_json,
            workflow.entry_node,
            json.dumps(workflow.output_schema) if workflow.output_schema else None,
            json.dumps(workflow.metadata),
            workflow.error_mode,
            workflow.timeout,
            workflow.created_at,
            workflow.updated_at,
        ))
        conn.commit()
        logger.info(f"工作流已保存: {workflow.id}")
        return workflow.id
    
    def load_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """加载工作流。"""
        conn = self.connect()
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        
        if not row:
            return None
        
        return self._row_to_workflow(row)
    
    def load_all_workflows(self) -> dict[str, Workflow]:
        """加载所有工作流。"""
        conn = self.connect()
        rows = conn.execute("SELECT * FROM workflows ORDER BY updated_at DESC").fetchall()
        
        workflows = {}
        for row in rows:
            wf = self._row_to_workflow(row)
            if wf:
                workflows[wf.id] = wf
        
        logger.info(f"已加载 {len(workflows)} 个工作流")
        return workflows
    
    def delete_workflow(self, workflow_id: str) -> bool:
        """删除工作流。"""
        conn = self.connect()
        cursor = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        conn.commit()
        
        # 同时删除运行记录
        conn.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (workflow_id,))
        conn.commit()
        
        return cursor.rowcount > 0
    
    def _row_to_workflow(self, row: sqlite3.Row) -> Optional[Workflow]:
        """将数据库行转换为 Workflow 对象。"""
        try:
            nodes_data = json.loads(row["nodes"]) if row["nodes"] else []
            edges_data = json.loads(row["edges"]) if row["edges"] else []
            
            nodes = {}
            for n in nodes_data:
                # 解析错误策略
                error_strategy_str = n.get("error_strategy", "fail_fast")
                try:
                    from app.core.workflow.models import ErrorStrategy
                    error_strategy = ErrorStrategy(error_strategy_str)
                except ValueError:
                    error_strategy = ErrorStrategy.FAIL_FAST

                # 解析循环模式
                loop_mode_str = n.get("loop_mode", "sequential")
                try:
                    from app.core.workflow.models import LoopMode
                    loop_mode = LoopMode(loop_mode_str)
                except ValueError:
                    loop_mode = LoopMode.SEQUENTIAL

                node = WorkflowNode(
                    id=n["id"],
                    type=NodeType(n["type"]),
                    name=n["name"],
                    config=n.get("config", {}),
                    description=n.get("description", ""),
                    tool_name=n.get("tool_name"),
                    agent_config=n.get("agent_config"),
                    condition_expr=n.get("condition_expr"),
                    loop_config=n.get("loop_config"),
                    http_config=n.get("http_config"),
                    script_code=n.get("script_code"),
                    timeout=n.get("timeout", 60),
                    branches=n.get("branches", {}),
                    # 新字段
                    error_strategy=error_strategy,
                    retry_count=n.get("retry_count", 0),
                    retry_delay=n.get("retry_delay", 0.5),
                    fallback_node_id=n.get("fallback_node_id"),
                    loop_mode=loop_mode,
                    subgraph_ids=n.get("subgraph_ids", []),
                    position=n.get("position"),
                )
                nodes[node.id] = node
            
            edges = [
                WorkflowEdge(
                    id=e.get("id") or f"edge_{i}",
                    source=e["source"],
                    target=e["target"],
                    condition=e.get("condition"),
                    condition_expr=e.get("condition_expr"),
                )
                for i, e in enumerate(edges_data)
            ]
            
            output_schema = json.loads(row["output_schema"]) if row["output_schema"] else None
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            
            workflow = Workflow(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                version=row["version"],
                nodes=nodes,
                edges=edges,
                entry_node=row["entry_node"],
                output_schema=output_schema,
                error_mode=row["error_mode"] or "fail_fast",
                timeout=row["timeout"] if "timeout" in row.keys() and row["timeout"] is not None else 1800.0,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=metadata,
            )
            
            return workflow
        except Exception as e:
            logger.error(f"加载工作流失败 {row['id']}: {e}")
            return None
    
    # ===== 运行记录操作 =====
    
    def save_run(self, run: WorkflowRun) -> str:
        """保存运行记录。"""
        conn = self.connect()

        conn.execute("""
            INSERT OR REPLACE INTO workflow_runs
            (id, workflow_id, status, session_id, context, variables, node_states, history, error, started_at, finished_at, output)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run.run_id,
            run.workflow_id,
            run.status,
            run.session_id,
            json.dumps(run.context),
            json.dumps(run.variables, default=str),
            json.dumps(run.node_states),
            json.dumps(run.history),
            run.error,
            run.started_at,
            run.finished_at,
            json.dumps(run.output, default=str) if run.output else None,
        ))
        conn.commit()
        return run.run_id
    
    def load_runs(self, workflow_id: str, limit: int = 50) -> list[WorkflowRun]:
        """加载工作流的运行记录。"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT * FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT ?",
            (workflow_id, limit)
        ).fetchall()
        
        runs = []
        for row in rows:
            try:
                run = WorkflowRun(
                    run_id=row["id"],
                    workflow_id=row["workflow_id"],
                    status=row["status"],
                    context=json.loads(row["context"]) if row["context"] else {},
                    variables=json.loads(row["variables"]) if row["variables"] else {},
                    node_states=json.loads(row["node_states"]) if row["node_states"] else {},
                    history=json.loads(row["history"]) if row["history"] else [],
                    error=row["error"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    output=json.loads(row["output"]) if row["output"] else None,
                )
                runs.append(run)
            except Exception as e:
                logger.error(f"加载运行记录失败 {row['id']}: {e}")
        
        return runs
    
    def delete_runs(self, workflow_id: str):
        """删除工作流的所有运行记录。"""
        conn = self.connect()
        conn.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (workflow_id,))
        conn.commit()

    # ===== 版本管理操作 =====

    def save_version(
        self,
        workflow_id: str,
        version: str,
        nodes_json: str,
        edges_json: str,
        metadata: dict = None,
    ) -> str:
        """保存工作流版本快照。"""
        conn = self.connect()
        version_id = f"v{int(time.time() * 1000)}"
        now = time.time()
        conn.execute("""
            INSERT INTO workflow_versions
            (version_id, workflow_id, version, nodes, edges, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            version_id, workflow_id, version,
            nodes_json, edges_json,
            json.dumps(metadata or {}),
            now,
        ))
        conn.commit()
        return version_id

    def load_workflow_versions(self, workflow_id: str, limit: int = 50) -> list[dict]:
        """加载工作流版本历史。"""
        conn = self.connect()
        rows = conn.execute("""
            SELECT version_id, version, metadata, created_at
            FROM workflow_versions
            WHERE workflow_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (workflow_id, limit)).fetchall()
        return [
            {
                "version_id": r["version_id"],
                "version": r["version"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def load_version_content(self, version_id: str) -> Optional[dict]:
        """加载指定版本的节点和边数据。"""
        conn = self.connect()
        row = conn.execute(
            "SELECT nodes, edges FROM workflow_versions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "nodes": json.loads(row["nodes"]),
            "edges": json.loads(row["edges"]),
        }

    def delete_old_versions(self, workflow_id: str, keep: int = 20) -> int:
        """清理旧版本,只保留最近 keep 个。"""
        conn = self.connect()
        conn.execute("""
            DELETE FROM workflow_versions
            WHERE version_id IN (
                SELECT version_id FROM workflow_versions
                WHERE workflow_id = ?
                ORDER BY created_at DESC
                OFFSET ?
            )
        """, (workflow_id, keep))
        deleted = conn.total_changes
        conn.commit()
        return deleted

    # ===== 调度管理操作 =====

    def save_schedule(self, schedule_id: str, workflow_id: str, cron_expr: str, name: str, enabled: bool = True, last_run_at: float = None, next_run_at: float = None) -> None:
        """保存定时调度配置。"""
        conn = self.connect()
        conn.execute("""
            INSERT OR REPLACE INTO workflow_schedules
            (schedule_id, workflow_id, cron_expr, name, enabled, last_run_at, next_run_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            schedule_id, workflow_id, cron_expr, name,
            1 if enabled else 0,
            last_run_at,
            next_run_at,
            time.time(),
            time.time(),
        ))
        conn.commit()

    def load_schedules(self, workflow_id: str = None) -> list[dict]:
        """加载定时调度列表。"""
        conn = self.connect()
        if workflow_id:
            rows = conn.execute(
                "SELECT * FROM workflow_schedules WHERE workflow_id = ? ORDER BY created_at DESC",
                (workflow_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM workflow_schedules ORDER BY created_at DESC").fetchall()
        return [
            {
                "schedule_id": r["schedule_id"],
                "workflow_id": r["workflow_id"],
                "cron_expr": r["cron_expr"],
                "name": r["name"],
                "enabled": bool(r["enabled"]),
                "last_run_at": r["last_run_at"],
                "next_run_at": r["next_run_at"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def delete_schedule(self, schedule_id: str) -> bool:
        """删除定时调度。"""
        conn = self.connect()
        cursor = conn.execute("DELETE FROM workflow_schedules WHERE schedule_id = ?", (schedule_id,))
        conn.commit()
        return cursor.rowcount > 0

    def update_schedule(self, schedule_id: str, **kwargs) -> bool:
        """更新定时调度配置。"""
        if not kwargs:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [schedule_id]
        conn = self.connect()
        conn.execute(f"UPDATE workflow_schedules SET {set_clause}, updated_at = ? WHERE schedule_id = ?", values + [time.time()])
        conn.commit()
        return True

    # ===== 分析统计操作 =====

    def get_workflow_stats(self, workflow_id: str, days: int = 7) -> dict:
        """获取工作流运行统计。"""
        conn = self.connect()
        cutoff = time.time() - days * 86400
        # 总运行次数
        total = conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE workflow_id = ? AND started_at >= ?",
            (workflow_id, cutoff),
        ).fetchone()[0]
        # 成功次数
        success = conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE workflow_id = ? AND status = 'completed' AND started_at >= ?",
            (workflow_id, cutoff),
        ).fetchone()[0]
        # 失败次数
        failed = conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE workflow_id = ? AND status = 'failed' AND started_at >= ?",
            (workflow_id, cutoff),
        ).fetchone()[0]
        # 平均耗时
        avg_dur = conn.execute(
            "SELECT AVG(duration) FROM workflow_runs WHERE workflow_id = ? AND status = 'completed' AND started_at >= ? AND finished_at IS NOT NULL",
            (workflow_id, cutoff),
        ).fetchone()[0] or 0
        # 最近运行
        latest = conn.execute(
            "SELECT run_id, status, started_at, duration FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        return {
            "total_runs": total,
            "success_count": success,
            "failed_count": failed,
            "success_rate": round(success / max(1, total) * 100, 1) if total > 0 else 0,
            "avg_duration_sec": round(avg_dur, 2),
            "latest_run": {
                "run_id": latest["run_id"],
                "status": latest["status"],
                "started_at": latest["started_at"],
                "duration": latest["duration"],
            } if latest else None,
        }

    def get_node_performance(self, workflow_id: str, run_id: str = None) -> list[dict]:
        """获取节点性能统计(平均耗时、失败率)。"""
        conn = self.connect()
        if run_id:
            rows = conn.execute("""
                SELECT node_id, AVG(duration) as avg_dur, COUNT(*) as runs,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failures
                FROM checkpoints
                WHERE run_id = ?
                GROUP BY node_id
            """, (run_id,)).fetchall()
        else:
            # 最近10次运行的统计
            rows = conn.execute("""
                SELECT c.node_id, AVG(c.duration) as avg_dur, COUNT(*) as runs,
                       SUM(CASE WHEN c.status = 'failed' THEN 1 ELSE 0 END) as failures
                FROM checkpoints c
                JOIN workflow_runs r ON c.run_id = r.id
                WHERE r.workflow_id = ? AND r.finished_at IS NOT NULL
                GROUP BY c.node_id
                ORDER BY avg_dur DESC
                LIMIT 20
            """, (workflow_id,)).fetchall()
        return [
            {
                "node_id": r["node_id"],
                "avg_duration_ms": round(r["avg_dur"] * 1000, 1) if r["avg_dur"] else 0,
                "total_runs": r["runs"],
                "failure_rate": round(r["failures"] / max(1, r["runs"]) * 100, 1),
            }
            for r in rows
        ]

    # ===== 表创建与迁移 =====

    def _create_tables(self):
        """创建工作流相关表。"""
        conn = self.connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                version TEXT DEFAULT '1.0.0',
                nodes TEXT NOT NULL,
                edges TEXT NOT NULL,
                entry_node TEXT,
                output_schema TEXT,
                metadata TEXT DEFAULT '{}',
                error_mode TEXT DEFAULT 'fail_fast',
                timeout REAL DEFAULT 1800,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                session_id TEXT DEFAULT '',
                context TEXT DEFAULT '{}',
                variables TEXT DEFAULT '{}',
                node_states TEXT DEFAULT '{}',
                history TEXT DEFAULT '[]',
                error TEXT,
                started_at REAL NOT NULL,
                finished_at REAL,
                output TEXT,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_versions (
                version_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                version TEXT NOT NULL,
                nodes TEXT NOT NULL,
                edges TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_schedules (
                schedule_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                cron_expr TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                enabled INTEGER DEFAULT 1,
                last_run_at REAL,
                next_run_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (workflow_id) REFERENCES workflows(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow_id ON workflow_versions(workflow_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_versions_created_at ON workflow_versions(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_schedules_workflow_id ON workflow_schedules(workflow_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_schedules_enabled ON workflow_schedules(enabled)")
        conn.commit()
        logger.info("工作流表创建完成")


# 全局数据库实例
_db: Optional[WorkflowDB] = None


def get_db() -> WorkflowDB:
    """获取数据库实例。"""
    global _db
    if _db is None:
        _db = WorkflowDB()
    return _db


def init_db():
    """初始化数据库。"""
    get_db().connect()
    logger.info("工作流数据库初始化完成")


def shutdown_db():
    """关闭数据库。"""
    global _db
    if _db:
        _db.close()
        _db = None
