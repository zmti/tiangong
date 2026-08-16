"""文件版本历史:write_file 覆盖/创建前自动备份,支持历史查看、回滚、撤销。

存储于数据目录 file_versions.db,每条记录 = 一次写入前的旧内容快照。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from app.config import settings

_DB_LOCK = threading.Lock()
# 超过该大小的文件不自动备份(避免把大文件内容塞进 SQLite)
MAX_VERSION_SIZE = 2_000_000
# 每个文件最多保留的版本数
MAX_VERSIONS_PER_FILE = 50


class FileVersionStore:
    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or os.path.join(settings.resolved_data_dir, "file_versions.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS file_versions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        action TEXT NOT NULL,
                        size INTEGER NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_fv_path ON file_versions(path)")
                conn.commit()
            finally:
                conn.close()

    def save_version(self, path: str, content: str, action: str) -> int | None:
        """保存一个版本快照。content 为空且 action=create 表示"文件在此前不存在"。"""
        if len(content) > MAX_VERSION_SIZE:
            return None
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO file_versions (path, content, created_at, action, size) VALUES (?,?,?,?,?)",
                    (path, content, time.time(), action, len(content.encode("utf-8"))),
                )
                # 裁剪超量历史
                conn.execute(
                    "DELETE FROM file_versions WHERE path=? AND id NOT IN "
                    "(SELECT id FROM file_versions WHERE path=? ORDER BY id DESC LIMIT ?)",
                    (path, path, MAX_VERSIONS_PER_FILE),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def list_versions(self, path: str, limit: int = 20) -> list[dict]:
        with _DB_LOCK:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, path, created_at, action, size FROM file_versions "
                    "WHERE path=? ORDER BY id DESC LIMIT ?",
                    (path, limit),
                ).fetchall()
            finally:
                conn.close()
        return [dict(r) for r in rows]

    def get_version(self, version_id: int) -> dict | None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM file_versions WHERE id=?", (version_id,)
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row else None

    def latest_version(self, path: str) -> dict | None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM file_versions WHERE path=? ORDER BY id DESC LIMIT 1",
                    (path,),
                ).fetchone()
            finally:
                conn.close()
        return dict(row) if row else None

    def delete_version(self, version_id: int) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM file_versions WHERE id=?", (version_id,))
                conn.commit()
            finally:
                conn.close()

    def delete_versions_for(self, path: str) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM file_versions WHERE path=?", (path,))
                conn.commit()
            finally:
                conn.close()

    def stats(self) -> dict:
        with _DB_LOCK:
            conn = self._connect()
            try:
                total = conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]
                files = conn.execute("SELECT COUNT(DISTINCT path) FROM file_versions").fetchone()[0]
            finally:
                conn.close()
        return {"versions": total, "files": files}


file_version_store = FileVersionStore()
