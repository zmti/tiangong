"""会话长期存储:自动归档冷数据 + 导出/导入(JSONL 格式)。

A4 改进:
- 会话 TTL 自动归档:长时间不活跃的会话自动压缩为摘要并归档
- 会话导出:JSONL 格式导出完整对话历史
- 会话导入:从 JSONL 恢复会话上下文
- 按大小自动清理:超过阈值的旧会话定期清理
- 后台自动清理:启动时启动后台任务，定时扫描并清理过期会话
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class SessionArchiver:
    """会话归档管理器。"""

    def __init__(self, data_dir: Optional[str] = None):
        self._data_dir = data_dir or settings.resolved_data_dir
        self._archive_dir = os.path.join(self._data_dir, "session_archives")
        os.makedirs(self._archive_dir, exist_ok=True)
        # 归档阈值:会话超过此字符数后触发归档
        self._archive_threshold = int(getattr(settings, "session_archive_chars", 50000) or 50000)
        # 保留天数:超过此天数的会话自动清理
        self._retention_days = int(getattr(settings, "session_retention_days", 90) or 90)
        # 自动清理间隔(秒)
        self._cleanup_interval = int(getattr(settings, "session_cleanup_interval_seconds", 3600) or 3600)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ---- 会话列表（从 SQLite 读取）----

    def list_sessions(self) -> list[dict]:
        """列出所有活跃会话及其基本信息。"""
        sessions = []
        try:
            from app.memory.session import SessionStore
            store = SessionStore()
            # 直接查 SQLite 获取所有 session 信息
            db_path = store._db_path
            if not db_path or not os.path.exists(db_path):
                return sessions
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT id, messages, created_at, updated_at, title FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
            conn.close()
            for sid, msgs_json, created, updated, title in rows:
                try:
                    msgs = json.loads(msgs_json) if msgs_json else []
                except Exception:
                    msgs = []
                sessions.append({
                    "session_id": sid,
                    "title": title or f"会话 {sid[:8]}",
                    "message_count": len(msgs),
                    "total_chars": sum(len(str(m.get("content", ""))) for m in msgs),
                    "created_at": created,
                    "updated_at": updated,
                    "age_days": round((time.time() - updated) / 86400, 1),
                })
        except Exception as e:
            logger.warning("列出会话失败: %s", e)
        return sessions

    def should_archive(self, session_id: str, current_size: int) -> bool:
        """判断会话是否需要归档。"""
        return current_size >= self._archive_threshold

    def archive_session(self, session_id: str, messages: list[dict]) -> str:
        """将会话压缩归档为 JSON 文件,返回归档路径。"""
        # 保留最后 50 条消息用于快速恢复
        recent = messages[-50:]
        last_user = None
        last_assistant = None
        for m in reversed(recent):
            if m.get("role") == "user" and last_user is None:
                last_user = m.get("content", "")[:200]
            elif m.get("role") == "assistant" and last_assistant is None:
                last_assistant = m.get("content", "")[:200]

        archive_data = {
            "session_id": session_id,
            "archived_at": time.time(),
            "message_count": len(messages),
            "kept_messages": len(recent),
            "total_chars": sum(len(str(m.get("content", ""))) for m in messages),
            "last_user_message": last_user or "",
            "last_assistant_message": last_assistant or "",
            "messages": recent,
        }
        path = os.path.join(self._archive_dir, f"{session_id}.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(archive_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # atomic on POSIX, best-effort on Windows
        return path

    def export_session(self, session_id: str, messages: list[dict]) -> str:
        """导出会话为 JSONL 格式(可迁移/备份)。"""
        path = os.path.join(self._archive_dir, f"{session_id}_export.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        return path

    def import_session(self, file_path: str) -> list[dict]:
        """从 JSONL 文件导入会话消息。"""
        messages = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return messages

    def delete_session(self, session_id: str) -> bool:
        """从 SessionStore 中删除会话记录。"""
        try:
            from app.memory.session import SessionStore
            store = SessionStore()
            store.clear(session_id)
            return True
        except Exception as e:
            logger.warning("删除会话 %s 失败: %s", session_id, e)
            return False

    def cleanup_expired(self) -> int:
        """清理超过保留天数的归档文件和活跃会话,返回清理数量。"""
        cutoff = time.time() - (self._retention_days * 86400)
        cleaned = 0

        # 1. 清理过期归档文件
        try:
            for fname in os.listdir(self._archive_dir):
                fpath = os.path.join(self._archive_dir, fname)
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    cleaned += 1
        except Exception:
            pass

        # 2. 清理过期活跃会话
        try:
            from app.memory.session import SessionStore
            store = SessionStore()
            db_path = store._db_path
            if db_path and os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT id, updated_at FROM sessions WHERE updated_at < ?",
                    (cutoff,)
                ).fetchall()
                conn.close()
                for sid, _ in rows:
                    store.clear(sid)
                    cleaned += 1
        except Exception:
            pass

        return cleaned

    async def auto_cleanup(self) -> int:
        """执行完整自动清理:归档大会话 + 清理过期会话。返回清理数量。"""
        total_cleaned = 0

        # 1. 归档超过大小阈值的活跃会话
        sessions = self.list_sessions()
        archived = 0
        for s in sessions:
            if s["total_chars"] >= self._archive_threshold:
                try:
                    from app.memory.session import SessionStore
                    store = SessionStore()
                    messages = await store.get_history(s["session_id"])
                    if messages:
                        self.archive_session(s["session_id"], messages)
                        await store.clear(s["session_id"])
                        archived += 1
                        total_cleaned += 1
                        logger.info("已归档大会话 %s (%d 字符, %d 消息)",
                                    s["session_id"], s["total_chars"], len(messages))
                except Exception as e:
                    logger.warning("归档会话 %s 失败: %s", s["session_id"], e)

        # 2. 清理过期活跃会话
        cleaned_expired = self.cleanup_expired()
        total_cleaned += cleaned_expired

        if total_cleaned > 0:
            logger.info("自动清理完成: 归档 %d 个大会话, 清理 %d 个过期会话/文件", archived, cleaned_expired)
        return total_cleaned

    def list_archives(self) -> list[dict]:
        """列出所有归档文件信息。"""
        archives = []
        try:
            for fname in os.listdir(self._archive_dir):
                if fname.endswith(".json"):
                    fpath = os.path.join(self._archive_dir, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    archives.append({
                        "filename": fname,
                        "session_id": data.get("session_id", ""),
                        "message_count": data.get("message_count", 0),
                        "total_chars": data.get("total_chars", 0),
                        "archived_at": data.get("archived_at", 0),
                    })
        except Exception:
            pass
        return sorted(archives, key=lambda x: x["archived_at"], reverse=True)

    def start_background_cleanup(self) -> None:
        """启动后台自动清理任务。"""
        if self._running:
            return
        self._running = True

        async def _loop():
            while self._running:
                try:
                    await self.auto_cleanup()
                except Exception as e:
                    logger.warning("自动清理循环出错: %s", e)
                await asyncio.sleep(self._cleanup_interval)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            self._task = loop.create_task(_loop())
        else:
            loop.run_until_complete(_loop())

    def stop_background_cleanup(self) -> None:
        """停止后台自动清理任务。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None


# 模块级单例
_archiver: Optional[SessionArchiver] = None


def get_session_archiver() -> SessionArchiver:
    global _archiver
    if _archiver is None:
        _archiver = SessionArchiver()
    return _archiver
