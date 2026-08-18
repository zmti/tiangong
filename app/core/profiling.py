"""Agent 性能剖析工具:记录每次运行的工具耗时、LLM 调用耗时、token 用量等指标。

对标主流 agent 的 profiling 能力(Claude Code / Devin),帮助定位:
- 哪个工具最慢
- 哪次 LLM 调用延迟最高
- 单次运行的总耗时分布

数据持久化到 data/profiling.jsonl,可通过 API 查询聚合统计。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from typing import Optional

from app.config import settings


class ProfileRecord:
    """单次工具/LLM 调用的性能记录。"""

    __slots__ = (
        "run_id", "session_id", "kind", "name", "duration", "tokens",
        "cost", "success", "error", "timestamp",
    )

    def __init__(
        self,
        run_id: str,
        session_id: str,
        kind: str,          # "tool" | "llm" | "graph_node"
        name: str,
        duration: float,
        tokens: int = 0,
        cost: float = 0.0,
        success: bool = True,
        error: str = "",
    ):
        self.run_id = run_id
        self.session_id = session_id
        self.kind = kind
        self.name = name
        self.duration = round(duration, 3)
        self.tokens = tokens
        self.cost = round(cost, 6)
        self.success = success
        self.error = error[:500]
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "name": self.name,
            "duration": self.duration,
            "tokens": self.tokens,
            "cost": self.cost,
            "success": self.success,
            "error": self.error,
            "timestamp": self.timestamp,
        }


class ProfileManager:
    """进程内 + 持久化的性能剖析管理器。"""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(settings.resolved_data_dir, "profiling.jsonl")
        self._records: list[ProfileRecord] = []
        self._lock: Optional[asyncio.Lock] = None
        self._daily_stats: dict[str, dict] = {}
        self._tool_stats: dict[str, dict] = defaultdict(lambda: {
            "count": 0, "total_duration": 0.0, "errors": 0,
            "durations": [],
        })
        self._load_history()

    def _load_history(self) -> None:
        """启动时加载历史 profiling 数据。"""
        if not os.path.exists(self._db_path):
            return
        try:
            with open(self._db_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        rec = ProfileRecord(
                            run_id=data.get("run_id", ""),
                            session_id=data.get("session_id", ""),
                            kind=data.get("kind", "tool"),
                            name=data.get("name", ""),
                            duration=data.get("duration", 0.0),
                            tokens=data.get("tokens", 0),
                            cost=data.get("cost", 0.0),
                            success=data.get("success", True),
                            error=data.get("error", ""),
                        )
                        self._records.append(rec)
                        self._update_tool_stats(rec)
                    except Exception:
                        pass
        except Exception:
            pass

    def _update_tool_stats(self, rec: ProfileRecord) -> None:
        stats = self._tool_stats[rec.name]
        stats["count"] += 1
        stats["total_duration"] += rec.duration
        if not rec.success:
            stats["errors"] += 1
        if len(stats["durations"]) < 100:
            stats["durations"].append(rec.duration)

    def record(self, rec: ProfileRecord) -> None:
        """记录一条性能数据(进程内 + 落盘)。"""
        self._records.append(rec)
        self._update_tool_stats(rec)
        self._persist(rec)

    def _persist(self, rec: ProfileRecord) -> None:
        """追加到 JSONL 文件。"""
        try:
            with open(self._db_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    def get_tool_stats(
        self,
        limit: int = 20,
        sort_by: str = "total_duration",
    ) -> list[dict]:
        """返回工具性能排行(按总耗时/平均耗时/错误率排序)。"""
        items = []
        for name, stats in self._tool_stats.items():
            count = stats["count"]
            avg = stats["total_duration"] / count if count else 0
            p95 = sorted(stats["durations"])[int(len(stats["durations"]) * 0.95)] if stats["durations"] else 0
            items.append({
                "name": name,
                "count": count,
                "total_duration": round(stats["total_duration"], 3),
                "avg_duration": round(avg, 3),
                "p95_duration": round(p95, 3),
                "error_rate": round(stats["errors"] / count, 4) if count else 0,
                "errors": stats["errors"],
            })
        reverse = sort_by in ("total_duration", "count", "errors")
        items.sort(key=lambda x: x.get(sort_by, 0), reverse=reverse)
        return items[:limit]

    def get_run_profile(self, run_id: str) -> dict:
        """返回某次运行的完整性能轨迹。"""
        records = [r.to_dict() for r in self._records if r.run_id == run_id]
        if not records:
            return {"run_id": run_id, "records": [], "total_duration": 0}
        total = sum(r["duration"] for r in records)
        return {
            "run_id": run_id,
            "records": records,
            "total_duration": round(total, 3),
            "tool_calls": sum(1 for r in records if r["kind"] == "tool"),
            "llm_calls": sum(1 for r in records if r["kind"] == "llm"),
            "total_cost": round(sum(r["cost"] for r in records), 6),
            "total_tokens": sum(r["tokens"] for r in records),
        }

    def get_daily_summary(self, days: int = 7) -> list[dict]:
        """最近 N 天的性能汇总。"""
        now = time.time()
        result = []
        for day_offset in range(days):
            day_start = now - (day_offset + 1) * 86400
            day_end = now - day_offset * 86400
            day_records = [
                r for r in self._records
                if day_start <= r.timestamp < day_end
            ]
            if not day_records:
                continue
            tool_count = sum(1 for r in day_records if r.kind == "tool")
            llm_count = sum(1 for r in day_records if r.kind == "llm")
            total_duration = sum(r.duration for r in day_records)
            total_cost = sum(r.cost for r in day_records)
            errors = sum(1 for r in day_records if not r.success)
            result.append({
                "date": time.strftime("%Y-%m-%d", time.localtime(day_start)),
                "tool_calls": tool_count,
                "llm_calls": llm_count,
                "total_duration_s": round(total_duration, 1),
                "total_cost_usd": round(total_cost, 6),
                "errors": errors,
                "error_rate": round(errors / len(day_records), 4) if day_records else 0,
            })
        return result

    def clear_history(self) -> int:
        """清空所有 profiling 数据。"""
        count = len(self._records)
        self._records.clear()
        self._tool_stats.clear()
        try:
            if os.path.exists(self._db_path):
                os.remove(self._db_path)
        except Exception:
            pass
        return count


# 全局单例
_profile_manager: Optional[ProfileManager] = None


def get_profile_manager() -> ProfileManager:
    global _profile_manager
    if _profile_manager is None:
        _profile_manager = ProfileManager()
    return _profile_manager


def reset_profile_manager() -> None:
    global _profile_manager
    _profile_manager = None
