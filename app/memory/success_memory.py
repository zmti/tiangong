"""成功经验记忆:记录工具成功执行的场景,帮助智能体避免重复低效操作。

与 failure_memory.py 对称设计:
- failure_memory:记录"什么会导致失败" → 下次避免
- success_memory: 记录"什么方式成功了" → 下次复用

数据流:
1. Agent 执行工具成功后,将 (工具名, 参数摘要, 结果摘要) 写入 store
2. 新会话启动时检索相关成功模式注入 system prompt
3. 格式: "[成功经验] 工具 xxx 曾成功完成同类任务,参数:..., 建议参考此方式"

应用场景:
- 某次 API 调用用特定参数成功,下次同类任务直接复用
- 某次代码编辑策略有效,避免重复尝试低效方案
- 与失败记忆互补,形成完整的经验学习闭环
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

from app.config import settings


_DB_LOCK = threading.RLock()
_instance = None


class SuccessRecord:
    """一条成功操作记录。"""

    def __init__(
        self,
        tool_name: str,
        param_hash: str,
        param_summary: str,
        result_summary: str,
        run_id: str = "",
        session_id: str = "",
        first_seen: Optional[float] = None,
        last_seen: Optional[float] = None,
        count: int = 1,
        score: float = 1.0,
    ):
        self.id = uuid.uuid4().hex
        self.tool_name = tool_name
        self.param_hash = param_hash  # 用于匹配同类参数
        self.param_summary = param_summary[:300]
        self.result_summary = result_summary[:300]
        self.run_id = run_id
        self.session_id = session_id
        self.first_seen = first_seen or time.time()
        self.last_seen = last_seen or time.time()
        self.count = count
        self.score = score

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "param_hash": self.param_hash,
            "param_summary": self.param_summary,
            "result_summary": self.result_summary,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "score": round(self.score, 4),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SuccessRecord":
        rec = cls(
            tool_name=data.get("tool_name", ""),
            param_hash=data.get("param_hash", ""),
            param_summary=data.get("param_summary", ""),
            result_summary=data.get("result_summary", ""),
            run_id=data.get("run_id", ""),
            session_id=data.get("session_id", ""),
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            count=data.get("count", 1),
        )
        rec.id = data.get("id", rec.id)
        rec.score = data.get("score", 1.0)
        return rec


def _simple_hash(text: str) -> str:
    """生成参数的简单哈希(用于归类相似参数)。"""
    import hashlib
    return hashlib.md5(text.encode()).hexdigest()[:12]


class SuccessMemoryStore:
    """成功经验存储。"""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(
            settings.resolved_data_dir, "success_memory.db"
        )
        self._lock = threading.Lock()
        self._records: dict[str, SuccessRecord] = {}
        self._init_db()
        self._load()

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS success_memory (
                        id TEXT PRIMARY KEY,
                        tool_name TEXT NOT NULL,
                        param_hash TEXT NOT NULL,
                        param_summary TEXT NOT NULL,
                        result_summary TEXT NOT NULL,
                        run_id TEXT,
                        session_id TEXT,
                        first_seen REAL NOT NULL,
                        last_seen REAL NOT NULL,
                        count INTEGER NOT NULL DEFAULT 1,
                        score REAL NOT NULL DEFAULT 1.0
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_suc_tool ON success_memory(tool_name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_suc_score ON success_memory(score DESC)")
                conn.commit()
            finally:
                conn.close()

    def _load(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM success_memory ORDER BY score DESC"
                ).fetchall()
                for row in rows:
                    self._records[row["id"]] = SuccessRecord.from_dict(dict(row))
            finally:
                conn.close()

    def _save(self, rec: SuccessRecord) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO success_memory
                    (id, tool_name, param_hash, param_summary, result_summary,
                     run_id, session_id, first_seen, last_seen, count, score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec.id, rec.tool_name, rec.param_hash, rec.param_summary,
                    rec.result_summary, rec.run_id, rec.session_id,
                    rec.first_seen, rec.last_seen, rec.count, rec.score,
                ))
                conn.commit()
            finally:
                conn.close()
            self._records[rec.id] = rec

    def record_success(
        self,
        tool_name: str,
        parameters: str,
        result_preview: str,
        run_id: str = "",
        session_id: str = "",
   ) -> SuccessRecord:
        """记录一次工具成功执行。若同类参数已存在则累加计数。"""
        param_hash = _simple_hash(parameters[:200])
        param_summary = parameters[:200]

        with self._lock:
            candidates = [
                r for r in self._records.values()
                if r.tool_name == tool_name and r.param_hash == param_hash
            ]

        if candidates:
            rec = candidates[0]
            rec.count += 1
            rec.last_seen = time.time()
            import math
            count_factor = min(1.0, math.log1p(rec.count) / math.log1p(10))
            time_factor = max(0.3, 1.0 - (time.time() - rec.last_seen) / 86400)
            rec.score = min(1.0, count_factor * 0.6 + time_factor * 0.4)
        else:
            rec = SuccessRecord(
                tool_name=tool_name,
                param_hash=param_hash,
                param_summary=param_summary,
                result_summary=result_preview[:300],
                run_id=run_id,
                session_id=session_id,
                score=0.5,  # 首次成功得分从 0.5 开始
            )
        self._save(rec)
        return rec

    def search(self, tool_name: str, top_k: int = 5) -> list[SuccessRecord]:
        """检索与指定工具相关的成功经验。"""
        with self._lock:
            results = [
                r for r in self._records.values()
                if r.tool_name == tool_name and r.score > 0.05
            ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def search_all(self, top_k: int = 20) -> list[SuccessRecord]:
        """检索所有高得分成功经验。"""
        with self._lock:
            results = sorted(
                self._records.values(),
                key=lambda r: r.score,
                reverse=True,
            )
        return results[:top_k]

    def build_experience_note(self, tool_names: list[str], max_tokens: int = 300) -> str:
        """为工具名列表构建成功经验分享提示块。"""
        from app.memory.context import estimate_tokens

        lines = []
        total_tokens = 0
        for name in tool_names:
            recs = self.search(name, top_k=1)
            for rec in recs:
                if rec.count < 1 or rec.score < 0.1:
                    continue
                part = (
                    f"[成功经验] 工具 `{rec.tool_name}` 曾以参数「{rec.param_summary[:60]}」"
                    f"成功完成 {rec.count} 次,结果: {rec.result_summary[:80]}"
                )
                part_tokens = estimate_tokens(part)
                if total_tokens + part_tokens > max_tokens:
                    break
                lines.append(part)
                total_tokens += part_tokens
            if total_tokens >= max_tokens:
                break
        if not lines:
            return ""
        header = "历史成功经验(同类任务可参考以下成功模式):\n"
        header_tokens = estimate_tokens(header)
        if header_tokens + total_tokens > max_tokens:
            return ""
        return header + "\n".join(lines)

    def stats(self) -> dict:
        with self._lock:
            total = len(self._records)
            return {
                "total_records": total,
                "avg_count": round(
                    sum(r.count for r in self._records.values()) / max(1, total), 1
                ),
            }

    def clear(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM success_memory")
                conn.commit()
                count = cur.rowcount
            finally:
                conn.close()
            self._records.clear()
            return count


def get_success_memory() -> SuccessMemoryStore:
    global _instance
    if _instance is None:
        with _DB_LOCK:
            if _instance is None:
                _instance = SuccessMemoryStore()
    return _instance
