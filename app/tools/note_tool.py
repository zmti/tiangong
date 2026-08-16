"""笔记系统:创建、搜索、管理笔记,支持标签和分类。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


class NoteDatabase:
    """笔记数据库。"""
    
    def __init__(self, db_path: str = "data/notes.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """初始化数据库。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(pinned)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_deleted ON notes(deleted)
        """)
        
        conn.commit()
        conn.close()
    
    def create(self, title: str, content: str, category: str = "", tags: list[str] = None) -> dict:
        """创建笔记。"""
        note_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO notes (id, title, content, category, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (note_id, title, content, category, json.dumps(tags or []), now, now))
        
        conn.commit()
        conn.close()
        
        return {
            "id": note_id,
            "title": title,
            "content": content,
            "category": category,
            "tags": tags or [],
            "created_at": now,
            "updated_at": now,
        }
    
    def update(self, note_id: str, **kwargs) -> Optional[dict]:
        """更新笔记。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # 检查笔记是否存在
        cursor.execute("SELECT * FROM notes WHERE id = ? AND deleted = 0", (note_id,))
        note = cursor.fetchone()
        if not note:
            conn.close()
            return None
        
        # 构建更新语句
        updates = []
        params = []
        
        for key in ["title", "content", "category", "pinned"]:
            if key in kwargs:
                updates.append(f"{key} = ?")
                params.append(kwargs[key])
        
        if "tags" in kwargs:
            updates.append("tags = ?")
            params.append(json.dumps(kwargs["tags"]))
        
        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(note_id)
            
            cursor.execute(f"UPDATE notes SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
        
        # 返回更新后的笔记
        cursor.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        updated = cursor.fetchone()
        conn.close()
        
        return self._row_to_dict(updated)
    
    def delete(self, note_id: str, permanent: bool = False) -> bool:
        """删除笔记。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        if permanent:
            cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        else:
            cursor.execute("UPDATE notes SET deleted = 1 WHERE id = ?", (note_id,))
        
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected > 0
    
    def get(self, note_id: str) -> Optional[dict]:
        """获取单个笔记。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE id = ? AND deleted = 0", (note_id,))
        note = cursor.fetchone()
        conn.close()
        
        return self._row_to_dict(note) if note else None
    
    def list_notes(
        self,
        category: str = "",
        tags: list[str] = None,
        pinned_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """列出笔记。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        conditions = ["deleted = 0"]
        params = []
        
        if category:
            conditions.append("category = ?")
            params.append(category)
        
        if pinned_only:
            conditions.append("pinned = 1")
        
        where = " AND ".join(conditions)
        
        cursor.execute(f"""
            SELECT * FROM notes 
            WHERE {where}
            ORDER BY pinned DESC, updated_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset])
        
        notes = [self._row_to_dict(row) for row in cursor.fetchall()]
        
        # 过滤标签
        if tags:
            notes = [n for n in notes if any(t in n.get("tags", []) for t in tags)]
        
        conn.close()
        return notes
    
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """搜索笔记。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # 搜索标题和内容
        search_term = f"%{query}%"
        cursor.execute("""
            SELECT * FROM notes 
            WHERE deleted = 0 AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
            ORDER BY updated_at DESC
            LIMIT ?
        """, (search_term, search_term, search_term, limit))
        
        notes = [self._row_to_dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return notes
    
    def get_categories(self) -> list[dict]:
        """获取所有分类。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT category, COUNT(*) as count 
            FROM notes 
            WHERE deleted = 0 AND category != ''
            GROUP BY category
            ORDER BY count DESC
        """)
        
        categories = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]
        conn.close()
        
        return categories
    
    def get_tags(self) -> list[dict]:
        """获取所有标签。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT tags FROM notes WHERE deleted = 0")
        
        tag_counts = {}
        for row in cursor.fetchall():
            tags = json.loads(row[0]) if row[0] else []
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        
        conn.close()
        
        return [{"name": tag, "count": count} for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1])]
    
    def get_stats(self) -> dict:
        """获取统计信息。"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM notes WHERE deleted = 0")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM notes WHERE deleted = 0 AND pinned = 1")
        pinned = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM notes WHERE deleted = 1")
        deleted = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "total": total,
            "pinned": pinned,
            "deleted": deleted,
        }
    
    def _row_to_dict(self, row) -> dict:
        """将数据库行转换为字典。"""
        if row is None:
            return {}
        
        return {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "category": row["category"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "pinned": bool(row["pinned"]),
        }


# 全局数据库实例
_note_db: Optional[NoteDatabase] = None


def get_note_db() -> NoteDatabase:
    """获取笔记数据库实例。"""
    global _note_db
    if _note_db is None:
        _note_db = NoteDatabase()
    return _note_db


@tool("create_note", "创建笔记", timeout=10)
def create_note(
    title: str,
    content: str,
    category: str = "",
    tags: str = "",
) -> dict:
    """
    创建新笔记。
    
    Args:
        title: 笔记标题
        content: 笔记内容
        category: 分类(可选)
        tags: 标签,用逗号分隔(可选)
    
    Returns:
        创建结果
    """
    db = get_note_db()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    
    note = db.create(title=title, content=content, category=category, tags=tag_list)
    return {"status": "ok", "note": note}


@tool("update_note", "更新笔记", timeout=10)
def update_note(
    note_id: str,
    title: str = "",
    content: str = "",
    category: str = "",
    tags: str = "",
) -> dict:
    """
    更新笔记。
    
    Args:
        note_id: 笔记 ID
        title: 新标题(留空不更新)
        content: 新内容(留空不更新)
        category: 新分类(留空不更新)
        tags: 新标签,用逗号分隔(留空不更新)
    
    Returns:
        更新结果
    """
    db = get_note_db()
    
    updates = {}
    if title:
        updates["title"] = title
    if content:
        updates["content"] = content
    if category:
        updates["category"] = category
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    
    if not updates:
        return {"status": "error", "error": "未提供更新内容"}
    
    note = db.update(note_id, **updates)
    
    if note:
        return {"status": "ok", "note": note}
    else:
        return {"status": "error", "error": f"笔记不存在: {note_id}"}


@tool("delete_note", "删除笔记", timeout=10)
def delete_note(
    note_id: str,
    permanent: bool = False,
) -> dict:
    """
    删除笔记。
    
    Args:
        note_id: 笔记 ID
        permanent: 是否永久删除(默认移到回收站)
    
    Returns:
        删除结果
    """
    db = get_note_db()
    
    if db.delete(note_id, permanent=permanent):
        msg = "已永久删除" if permanent else "已移到回收站"
        return {"status": "ok", "message": f"{msg}: {note_id}"}
    else:
        return {"status": "error", "error": f"笔记不存在: {note_id}"}


@tool("get_note", "获取笔记详情", timeout=10)
def get_note(
    note_id: str,
) -> dict:
    """
    获取笔记详情。
    
    Args:
        note_id: 笔记 ID
    
    Returns:
        笔记详情
    """
    db = get_note_db()
    note = db.get(note_id)
    
    if note:
        return {"status": "ok", "note": note}
    else:
        return {"status": "error", "error": f"笔记不存在: {note_id}"}


@tool("list_notes", "列出笔记", timeout=10)
def list_notes(
    category: str = "",
    tags: str = "",
    pinned_only: bool = False,
    limit: int = 20,
) -> dict:
    """
    列出笔记。
    
    Args:
        category: 按分类过滤
        tags: 按标签过滤,用逗号分隔
        pinned_only: 只显示置顶
        limit: 返回数量
    
    Returns:
        笔记列表
    """
    db = get_note_db()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    
    notes = db.list_notes(
        category=category,
        tags=tag_list,
        pinned_only=pinned_only,
        limit=limit,
    )
    
    return {
        "status": "ok",
        "total": len(notes),
        "notes": notes,
    }


@tool("search_notes", "搜索笔记", timeout=10)
def search_notes(
    query: str,
    limit: int = 20,
) -> dict:
    """
    搜索笔记(标题、内容、标签)。
    
    Args:
        query: 搜索关键词
        limit: 返回数量
    
    Returns:
        搜索结果
    """
    db = get_note_db()
    notes = db.search(query, limit=limit)
    
    return {
        "status": "ok",
        "query": query,
        "total": len(notes),
        "notes": notes,
    }


@tool("pin_note", "置顶/取消置顶笔记", timeout=10)
def pin_note(
    note_id: str,
    pinned: bool = True,
) -> dict:
    """
    设置笔记置顶状态。
    
    Args:
        note_id: 笔记 ID
        pinned: True 置顶, False 取消置顶
    
    Returns:
        操作结果
    """
    db = get_note_db()
    note = db.update(note_id, pinned=1 if pinned else 0)
    
    if note:
        msg = "已置顶" if pinned else "已取消置顶"
        return {"status": "ok", "message": f"{msg}: {note_id}", "note": note}
    else:
        return {"status": "error", "error": f"笔记不存在: {note_id}"}


@tool("list_note_categories", "列出所有笔记分类", timeout=10)
def list_note_categories() -> dict:
    """
    列出所有笔记分类及数量。
    
    Returns:
        分类列表
    """
    db = get_note_db()
    categories = db.get_categories()
    
    return {
        "status": "ok",
        "categories": categories,
    }


@tool("list_note_tags", "列出所有笔记标签", timeout=10)
def list_note_tags() -> dict:
    """
    列出所有笔记标签及使用次数。
    
    Returns:
        标签列表
    """
    db = get_note_db()
    tags = db.get_tags()
    
    return {
        "status": "ok",
        "tags": tags,
    }


@tool("get_note_stats", "获取笔记统计", timeout=10)
def get_note_stats() -> dict:
    """
    获取笔记统计信息。
    
    Returns:
        统计信息
    """
    db = get_note_db()
    stats = db.get_stats()
    
    return {
        "status": "ok",
        "stats": stats,
    }


@tool("export_notes", "导出笔记", timeout=30)
def export_notes(
    format: str = "markdown",
    category: str = "",
) -> dict:
    """
    导出笔记为 Markdown 或 JSON 格式。
    
    Args:
        format: 导出格式(markdown 或 json)
        category: 按分类导出(留空导出全部)
    
    Returns:
        导出内容
    """
    db = get_note_db()
    notes = db.list_notes(category=category, limit=1000)
    
    if format == "json":
        return {
            "status": "ok",
            "format": "json",
            "content": json.dumps(notes, ensure_ascii=False, indent=2),
            "count": len(notes),
        }
    
    # Markdown 格式
    lines = []
    lines.append(f"# 笔记导出\n")
    lines.append(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"共 {len(notes)} 条笔记\n")
    lines.append("---\n")
    
    for note in notes:
        lines.append(f"## {note['title']}\n")
        if note.get("category"):
            lines.append(f"**分类**: {note['category']}\n")
        if note.get("tags"):
            lines.append(f"**标签**: {', '.join(note['tags'])}\n")
        lines.append(f"**创建时间**: {note.get('created_at', '')}\n")
        lines.append(f"**更新时间**: {note.get('updated_at', '')}\n")
        if note.get("pinned"):
            lines.append(f"**置顶**: 是\n")
        lines.append(f"\n{note['content']}\n")
        lines.append("---\n")
    
    return {
        "status": "ok",
        "format": "markdown",
        "content": "\n".join(lines),
        "count": len(notes),
    }
