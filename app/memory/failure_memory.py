"""跨会话失败记忆:记录工具/操作的失败模式,帮助智能体在未来避免同类错误。

对标主流 agent 的"经验学习"能力:
- 同一工具反复失败时,沉淀"失败原因→正确用法"的经验
- 跨会话共享(全局 scope),新会话自动检索相关经验注入上下文
- 支持 TTL 衰减,过期经验自动遗忘

数据存储:支持 SQLite / Redis / PostgreSQL 后端切换
检索时注入到 system prompt,格式:
  [失败经验] 工具 xxx 之前曾失败,原因是:..., 建议: ...
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend


_DB_LOCK = threading.RLock()
_instance = None


class FailureRecord:
    """一条失败经验记录。"""

    def __init__(
        self,
        tool_name: str,
        failure_pattern: str,
        fix_hint: str,
        run_id: str = "",
        session_id: str = "",
        first_seen: Optional[float] = None,
        last_seen: Optional[float] = None,
        count: int = 1,
        score: float = 1.0,
    ):
        self.id = uuid.uuid4().hex
        self.tool_name = tool_name
        self.failure_pattern = failure_pattern[:500]
        self.fix_hint = fix_hint[:500]
        self.run_id = run_id
        self.session_id = session_id
        self.first_seen = first_seen or time.time()
        self.last_seen = last_seen or time.time()
        self.count = count
        self.score = score  # 使用频率衰减后的得分

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "failure_pattern": self.failure_pattern,
            "fix_hint": self.fix_hint,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "score": round(self.score, 4),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FailureRecord":
        rec = cls(
            tool_name=data.get("tool_name", ""),
            failure_pattern=data.get("failure_pattern", ""),
            fix_hint=data.get("fix_hint", ""),
            run_id=data.get("run_id", ""),
            session_id=data.get("session_id", ""),
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            count=data.get("count", 1),
        )
        rec.id = data.get("id", rec.id)
        rec.score = data.get("score", 1.0)
        return rec


class FailureMemoryStore:
    """失败经验存储。"""

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        """初始化 FailureMemoryStore
        
        Args:
            db_path: 数据库路径（SQLite 模式）
            backend: 存储后端实例，优先使用
        """
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(
                settings.resolved_data_dir, "failure_memory.db"
            )
            self._backend = get_storage_backend(db_path=self._db_path)
        
        self._lock = threading.Lock()
        self._records: dict[str, FailureRecord] = {}
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        with _DB_LOCK:
            schema = {
                "failure_memory": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "tool_name", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "failure_pattern", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "fix_hint", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "run_id", "type": "TEXT", "constraints": ""},
                    {"name": "session_id", "type": "TEXT", "constraints": ""},
                    {"name": "first_seen", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "last_seen", "type": "REAL", "constraints": "NOT NULL"},
                    {"name": "count", "type": "INTEGER", "constraints": "NOT NULL DEFAULT 1"},
                    {"name": "score", "type": "REAL", "constraints": "NOT NULL DEFAULT 1.0"},
                ],
            }
            self._backend.init_db(schema)
            self._backend.execute("CREATE INDEX IF NOT EXISTS idx_tool ON failure_memory(tool_name)")
            self._backend.execute("CREATE INDEX IF NOT EXISTS idx_score ON failure_memory(score DESC)")

    def _load(self) -> None:
        with self._lock:
            rows = self._backend.select("failure_memory", "1 = 1", order_by="score DESC")
            for row in rows:
                self._records[row["id"]] = FailureRecord.from_dict(row)

    def _save(self, rec: FailureRecord) -> None:
        with self._lock:
            self._backend.upsert(
                "failure_memory",
                {
                    "id": rec.id,
                    "tool_name": rec.tool_name,
                    "failure_pattern": rec.failure_pattern,
                    "fix_hint": rec.fix_hint,
                    "run_id": rec.run_id,
                    "session_id": rec.session_id,
                    "first_seen": rec.first_seen,
                    "last_seen": rec.last_seen,
                    "count": rec.count,
                    "score": rec.score,
                },
                conflict_columns=["id"]
            )
            self._records[rec.id] = rec

    def record_failure(
        self,
        tool_name: str,
        failure_pattern: str,
        run_id: str = "",
        session_id: str = "",
    ) -> FailureRecord:
        """记录一次工具失败,若同类模式已存在则累加计数并更新得分。"""
        pattern = failure_pattern.strip()[:500]
        if not pattern:
            pattern = f"工具 {tool_name} 执行失败"

        # 查找相近的已有记录(同工具名 + 模式重叠)
        with self._lock:
            candidates = [
                r for r in self._records.values()
                if r.tool_name == tool_name
                and (pattern[:50] in r.failure_pattern or r.failure_pattern[:50] in pattern)
            ]

        if candidates:
            # 复用已有记录
            rec = candidates[0]
            rec.count += 1
            rec.last_seen = time.time()
            # 得分随失败次数递增(对数增长),并结合时间衰减
            import math
            count_factor = min(1.0, math.log1p(rec.count) / math.log1p(10))  # 10次封顶
            time_factor = max(0.3, 1.0 - (time.time() - rec.last_seen) / 86400)
            rec.score = min(1.0, count_factor * 0.6 + time_factor * 0.4)
            # 补充新的失败描述(去重合并)
            if pattern not in rec.failure_pattern:
                rec.failure_pattern = (rec.failure_pattern + "；" + pattern)[:500]
        else:
            rec = FailureRecord(
                tool_name=tool_name,
                failure_pattern=pattern,
                fix_hint="",  # 后续可由 LLM 补充
                run_id=run_id,
                session_id=session_id,
                score=0.5,  # 首次失败得分从 0.5 开始,避免立即注入提示
            )
        self._save(rec)
        return rec

    def add_fix_hint(self, record_id: str, hint: str) -> bool:
        """为已有失败记录补充修复建议。"""
        with self._lock:
            rec = self._records.get(record_id)
        if rec is None:
            return False
        rec.fix_hint = (rec.fix_hint + "\n" + hint).strip()[:1000]
        self._save(rec)
        return True

    def search(self, tool_name: str, top_k: int = 5) -> list[FailureRecord]:
        """检索与指定工具相关的失败经验。"""
        with self._lock:
            results = [
                r for r in self._records.values()
                if r.tool_name == tool_name and r.score > 0.05
            ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def search_all(self, top_k: int = 20) -> list[FailureRecord]:
        """检索所有高得分失败经验。"""
        with self._lock:
            results = sorted(
                self._records.values(),
                key=lambda r: r.score,
                reverse=True,
            )
        return results[:top_k]

    def build_experience_note(self, tool_names: list[str], max_tokens: int = 400) -> str:
        """为给定的工具名列表构建注入提示块。"""
        from app.memory.context import estimate_tokens

        lines = []
        total_tokens = 0
        for name in tool_names:
            recs = self.search(name, top_k=2)
            for rec in recs:
                if rec.count < 1 or rec.score < 0.05:
                    continue
                part = f"[失败经验] 工具 `{rec.tool_name}` 曾失败 {rec.count} 次, 原因: {rec.failure_pattern[:100]}"
                if rec.fix_hint:
                    part += f", 建议: {rec.fix_hint[:150]}"
                part_tokens = estimate_tokens(part)
                if total_tokens + part_tokens > max_tokens:
                    break
                lines.append(part)
                total_tokens += part_tokens
            if total_tokens >= max_tokens:
                break
        if not lines:
            return ""
        header = "根据历史经验,以下工具之前曾遇到问题,请注意:"
        header_tokens = estimate_tokens(header)
        if header_tokens + total_tokens > max_tokens:
            return ""
        return header + "\n" + "\n".join(lines)

    def stats(self) -> dict:
        with self._lock:
            total = len(self._records)
            with_hints = sum(1 for r in self._records.values() if r.fix_hint)
            return {
                "total_records": total,
                "with_fix_hints": with_hints,
                "avg_count": round(
                    sum(r.count for r in self._records.values()) / max(1, total), 1
                ),
            }

    def clear(self) -> int:
        with self._lock:
            self._backend.delete("failure_memory", "1 = 1")
            self._records.clear()
            return 1

    def cleanup_expired(self, ttl_hours: float = 720.0) -> int:
        """清理超过 TTL 且低得分的记录。"""
        cutoff = time.time() - ttl_hours * 3600
        with self._lock:
            to_delete = [
                rid for rid, r in self._records.items()
                if r.last_seen < cutoff and r.score < 0.1
            ]
            for rid in to_delete:
                self._backend.delete("failure_memory", "id = ?", (rid,))
                if rid in self._records:
                    del self._records[rid]
            return len(to_delete)


def get_failure_memory() -> FailureMemoryStore:
    global _instance
    if _instance is None:
        with _DB_LOCK:
            if _instance is None:
                _instance = FailureMemoryStore()
    return _instance
