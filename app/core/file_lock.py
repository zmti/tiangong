"""文件锁:防止多个 Agent/进程同时修改同一文件导致冲突。

设计思路(对标 Claude Code 的 file locking):
- 加锁粒度:单个文件路径
- 锁持有者:run_id + session_id(可追溯)
- 超时自动释放:防止死锁
- 阻塞等待 vs 非阻塞:两种模式

与 edit_file / write_file 配合使用,在编辑前获取锁,编辑后释放。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import threading
from typing import Optional


class FileLock:
    """单个文件的锁。"""

    def __init__(self, path: str):
        self._path = os.path.realpath(path)
        self._holder: Optional[str] = None  # "run_id:session_id"
        self._acquired_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    def is_locked_by(self, run_id: str, session_id: str) -> bool:
        with self._lock:
            return self._holder == f"{run_id}:{session_id}"

    def acquire(self, run_id: str, session_id: str, timeout: float = 30.0) -> bool:
        """非阻塞尝试加锁;返回是否成功。"""
        key = f"{run_id}:{session_id}"
        with self._lock:
            if self._holder is None:
                self._holder = key
                self._acquired_at = time.time()
                return True
            # 超时自动释放
            if self._acquired_at > 0 and (time.time() - self._acquired_at) > timeout:
                self._holder = key
                self._acquired_at = time.time()
                return True
            return False

    def release(self, run_id: str, session_id: str) -> bool:
        key = f"{run_id}:{session_id}"
        with self._lock:
            if self._holder == key:
                self._holder = None
                self._acquired_at = 0.0
                return True
            return False

    def info(self) -> dict:
        with self._lock:
            return {
                "path": self._path,
                "locked": self._holder is not None,
                "holder": self._holder,
                "acquired_at": self._acquired_at,
                "hold_duration_s": round(time.time() - self._acquired_at, 1) if self._acquired_at else 0,
            }


class FileLockManager:
    """全局文件锁管理器。"""

    def __init__(self, db_path: Optional[str] = None):
        self._locks: dict[str, FileLock] = {}
        self._lock = threading.RLock()
        self._db_path = db_path or os.path.join(
            __import__("app.config", fromlist=["settings"]).settings.resolved_data_dir,
            "file_locks.db",
        )
        self._load_persistent()

    def _load_persistent(self) -> None:
        """从持久化存储恢复锁状态(进程重启后保留)。"""
        if not os.path.exists(self._db_path):
            return
        try:
            import sqlite3
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT path, holder, acquired_at FROM file_locks WHERE holder IS NOT NULL"
                ).fetchall()
            for path, holder, acquired_at in rows:
                if holder and (time.time() - acquired_at) < 300:  # 只恢复 5 分钟内的锁
                    lock = FileLock(path)
                    lock._holder = holder
                    lock._acquired_at = acquired_at
                    self._locks[path] = lock
        except Exception:
            pass

    def _persist(self, path: str, holder: Optional[str], acquired_at: float) -> None:
        try:
            import sqlite3
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS file_locks (path TEXT PRIMARY KEY, holder TEXT, acquired_at REAL)")
                if holder:
                    conn.execute(
                        "INSERT OR REPLACE INTO file_locks (path, holder, acquired_at) VALUES (?, ?, ?)",
                        (path, holder, acquired_at),
                    )
                else:
                    conn.execute("DELETE FROM file_locks WHERE path=?", (path,))
                conn.commit()
        except Exception:
            pass

    def _get_lock(self, path: str) -> FileLock:
        real = os.path.realpath(path)
        with self._lock:
            if real not in self._locks:
                self._locks[real] = FileLock(real)
            return self._locks[real]

    def try_acquire(self, path: str, run_id: str, session_id: str, timeout: float = 30.0) -> dict:
        """尝试获取文件锁。返回 {acquired, path, holder, info}。"""
        lock = self._get_lock(path)
        acquired = lock.acquire(run_id, session_id, timeout)
        if acquired:
            self._persist(path, f"{run_id}:{session_id}", time.time())
        return {
            "acquired": acquired,
            "path": path,
            "holder": lock.info()["holder"],
            "info": lock.info(),
        }

    def release(self, path: str, run_id: str, session_id: str) -> dict:
        """释放文件锁。"""
        lock = self._get_lock(path)
        released = lock.release(run_id, session_id)
        if released:
            self._persist(path, None, 0)
        return {"released": released, "path": path, "info": lock.info()}

    def list_locks(self) -> list[dict]:
        """列出所有当前持有的锁。"""
        result = []
        with self._lock:
            for lock in self._locks.values():
                info = lock.info()
                if info["locked"]:
                    result.append(info)
        return result

    def cleanup_expired(self, max_age_s: float = 300.0) -> int:
        """清理超期锁(超过 max_age_s 秒的锁自动释放)。"""
        now = time.time()
        cleaned = 0
        with self._lock:
            to_remove = []
            for path, lock in self._locks.items():
                if lock._holder and (now - lock._acquired_at) > max_age_s:
                    lock._holder = None
                    lock._acquired_at = 0.0
                    to_remove.append(path)
                    cleaned += 1
            for path in to_remove:
                del self._locks[path]
                self._persist(path, None, 0)
        return cleaned


# 全局单例
_lock_manager: Optional[FileLockManager] = None


def get_lock_manager() -> FileLockManager:
    global _lock_manager
    if _lock_manager is None:
        _lock_manager = FileLockManager()
    return _lock_manager
