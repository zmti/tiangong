"""待办事项工具:增删改查 + 标记完成,持久化到 data/todos.db。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.registry import tool

_DB_LOCK = None  # 延迟初始化

def _db_path() -> str:
    return os.path.join(settings.resolved_data_dir, "todos.db")


def _connect():
    global _DB_LOCK
    if _DB_LOCK is None:
        _DB_LOCK = sqlite3.connect(_db_path(), check_same_thread=False)
        _DB_LOCK.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                note TEXT DEFAULT '',
                category TEXT DEFAULT '默认',
                done INTEGER DEFAULT 0,
                due_at REAL,
                created_at REAL NOT NULL
            )
            """
        )
        _DB_LOCK.commit()
    return _DB_LOCK


def _row_to_dict(r) -> dict:
    return {
        "id": r[0],
        "title": r[1],
        "note": r[2],
        "category": r[3],
        "done": bool(r[4]),
        "due_at": r[5],
        "due": datetime.fromtimestamp(r[5]).strftime("%Y-%m-%d %H:%M") if r[5] else None,
        "created_at": r[6],
    }


@tool("create_todo", "创建一个待办事项,可指定分类与截止时间(如 明天 18:00)", timeout=10)
def create_todo(
    title: str = Field(description="待办内容"),
    category: str = Field("默认", description="分类,如 工作/生活/学习"),
    note: str = Field("", description="备注"),
    due_at: str = Field("", description="截止时间描述,如 '2026-08-15 18:00' 或留空"),
) -> str:
    conn = _connect()
    due_ts = None
    if due_at.strip():
        try:
            due_ts = datetime.strptime(due_at.strip(), "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            return {"error": f"截止时间格式错误,请用 YYYY-MM-DD HH:MM,收到: {due_at}"}
    cur = conn.execute(
        "INSERT INTO todos (title, note, category, done, due_at, created_at) VALUES (?,?,?,0,?,?)",
        (title.strip(), note.strip(), category.strip(), due_ts, time.time()),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "title": title.strip(), "category": category.strip()}


@tool("list_todos", "列出待办事项,可按分类过滤,支持只看未完成", timeout=10)
def list_todos(
    category: str = Field("", description="按分类过滤,留空全部"),
    only_pending: bool = Field(True, description="True 只显示未完成"),
) -> str:
    conn = _connect()
    sql = "SELECT id,title,note,category,done,due_at,created_at FROM todos"
    where, args = [], []
    if category.strip():
        where.append("category=?")
        args.append(category.strip())
    if only_pending:
        where.append("done=0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY done ASC, COALESCE(due_at, 1e18) ASC, created_at DESC"
    rows = conn.execute(sql, args).fetchall()
    items = [_row_to_dict(r) for r in rows]
    return {"todos": items, "count": len(items)}


@tool("toggle_todo", "切换待办完成/未完成状态", timeout=10)
def toggle_todo(todo_id: int = Field(description="待办 ID")) -> str:
    conn = _connect()
    row = conn.execute("SELECT done FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        return {"error": f"待办不存在: {todo_id}"}
    conn.execute("UPDATE todos SET done=? WHERE id=?", (0 if row[0] else 1, todo_id))
    conn.commit()
    return {"ok": True, "id": todo_id, "done": not bool(row[0])}


@tool("delete_todo", "删除一个待办事项", timeout=10)
def delete_todo(todo_id: int = Field(description="待办 ID")) -> str:
    conn = _connect()
    cur = conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    conn.commit()
    return {"ok": cur.rowcount > 0, "id": todo_id, "deleted": cur.rowcount > 0}


@tool("get_todo_stats", "获取待办统计(总数/已完成/未完成/各分类数量)", timeout=10)
def get_todo_stats() -> str:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM todos WHERE done=1").fetchone()[0]
    by_cat = {}
    for r in conn.execute("SELECT category, COUNT(*) FROM todos GROUP BY category"):
        by_cat[r[0]] = r[1]
    return {"total": total, "done": done, "pending": total - done, "by_category": by_cat}
