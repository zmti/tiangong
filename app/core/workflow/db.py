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
            logger.info(f"工作流数据库已连接: {self.db_path}")
        return self._conn
    
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
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                status TEXT DEFAULT 'running',
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
            }
            for n in workflow.nodes.values()
        ])
        
        edges_json = json.dumps([
            {
                "id": e.id,
                "source": e.source,
                "target": e.target,
                "condition": e.condition,
            }
            for e in workflow.edges
        ])
        
        # 插入或更新
        conn.execute("""
            INSERT OR REPLACE INTO workflows 
            (id, name, description, version, nodes, edges, entry_node, output_schema, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                )
                nodes[node.id] = node
            
            edges = [
                WorkflowEdge(
                    id=e["id"],
                    source=e["source"],
                    target=e["target"],
                    condition=e.get("condition"),
                )
                for e in edges_data
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
            (id, workflow_id, status, context, variables, node_states, history, error, started_at, finished_at, output)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run.run_id,
            run.workflow_id,
            run.status,
            json.dumps(run.context),
            json.dumps(run.variables),
            json.dumps(run.node_states),
            json.dumps(run.history),
            run.error,
            run.started_at,
            run.finished_at,
            json.dumps(run.output) if run.output else None,
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
