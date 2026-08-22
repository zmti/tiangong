"""会话成本预算控制:按会话追踪 token 用量与美元成本,超出预算时自动提示或中止。

设计(对齐 Claude Code / Cursor 的 budget 模式):
- 每个 session_id 维护累计 token 和成本(SQLite 持久化)
- 请求级可设置 budget_usd / budget_tokens 硬上限
- 软上限触发"即将超预算"警告事件
- 硬上限触发中止并返回明确提示
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from app.config import settings
from app.core.telemetry import estimate_cost
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


class SessionBudgetTracker:
    """会话级成本追踪与预算控制。"""

    def __init__(self, backend: Optional[StorageBackend] = None):
        self._backend = backend or get_storage_backend()
        self._init_db()

    def _init_db(self) -> None:
        schema = {
            "session_budgets": [
                {"name": "session_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "total_prompt_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "total_completion_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "total_cost_usd", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "call_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "budget_usd", "type": "REAL", "constraints": "DEFAULT 0.0"},
                {"name": "budget_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "updated_at", "type": "REAL", "constraints": "NOT NULL"},
            ],
        }
        self._backend.init_db(schema)

    def record(
        self,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        model: str = "",
        budget_usd: float = 0.0,
        budget_tokens: int = 0,
    ) -> dict:
        """记录一次 LLM 调用的 token 和成本,返回当前状态。"""
        cost = estimate_cost(model, prompt_tokens, completion_tokens)
        cur = self._backend.select(
            "session_budgets", "session_id = ?", (session_id,), limit=1,
        )
        if not cur:
            self._backend.insert("session_budgets", {
                "session_id": session_id,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cost_usd": 0.0,
                "call_count": 0,
                "budget_usd": budget_usd,
                "budget_tokens": budget_tokens,
                "updated_at": time.time(),
            })
            cur = self._backend.select(
                "session_budgets", "session_id = ?", (session_id,), limit=1,
            )

        row = cur[0]
        new_prompt = (row["total_prompt_tokens"] or 0) + prompt_tokens
        new_comp = (row["total_completion_tokens"] or 0) + completion_tokens
        new_cost = (row["total_cost_usd"] or 0.0) + cost
        new_calls = (row["call_count"] or 0) + 1

        self._backend.update("session_budgets", {
            "total_prompt_tokens": new_prompt,
            "total_completion_tokens": new_comp,
            "total_cost_usd": new_cost,
            "call_count": new_calls,
            "budget_usd": budget_usd or (row["budget_usd"] or 0),
            "budget_tokens": budget_tokens or (row["budget_tokens"] or 0),
            "updated_at": time.time(),
        }, "session_id = ?", (session_id,))

        eff_budget_usd = budget_usd or (row["budget_usd"] or 0)
        eff_budget_tokens = budget_tokens or (row["budget_tokens"] or 0)
        return {
            "session_id": session_id,
            "prompt_tokens": new_prompt,
            "completion_tokens": new_comp,
            "cost_usd": round(new_cost, 6),
            "call_count": new_calls,
            "budget_usd": eff_budget_usd,
            "budget_tokens": eff_budget_tokens,
            "soft_warning": self._is_soft_warning(new_cost, eff_budget_usd),
            "hard_limit_hit": self._is_hard_limit_hit(
                new_cost, eff_budget_usd, new_prompt, eff_budget_tokens,
            ),
        }

    def get_status(self, session_id: str) -> dict:
        """查询某会话当前预算状态。"""
        rows = self._backend.select(
            "session_budgets", "session_id = ?", (session_id,), limit=1,
        )
        if not rows:
            return {"session_id": session_id, "not_found": True}
        r = rows[0]
        return {
            "session_id": session_id,
            "prompt_tokens": r["total_prompt_tokens"] or 0,
            "completion_tokens": r["total_completion_tokens"] or 0,
            "cost_usd": round(r["total_cost_usd"] or 0.0, 6),
            "call_count": r["call_count"] or 0,
            "budget_usd": r["budget_usd"] or 0,
            "budget_tokens": r["budget_tokens"] or 0,
            "soft_warning": self._is_soft_warning(
                r["total_cost_usd"] or 0, r["budget_usd"] or 0),
            "hard_limit_hit": self._is_hard_limit_hit(
                r["total_cost_usd"] or 0, r["budget_usd"] or 0,
                r["total_prompt_tokens"] or 0, r["budget_tokens"] or 0),
        }

    def reset(self, session_id: str) -> bool:
        """重置某会话预算统计。"""
        exists = self._backend.exists(
            "session_budgets", "session_id = ?", (session_id,),
        )
        if exists:
            self._backend.delete("session_budgets", "session_id = ?", (session_id,))
        return exists

    def clear_all(self) -> int:
        """清空所有预算记录。"""
        count = self._backend.count("session_budgets")
        self._backend.delete("session_budgets", "1 = 1")
        return count

    @staticmethod
    def _is_soft_warning(cost_usd: float, budget_usd: float) -> bool:
        """软警告:已用成本达到预算的 80%。"""
        if budget_usd <= 0:
            return False
        return cost_usd >= budget_usd * 0.8

    @staticmethod
    def _is_hard_limit_hit(cost_usd: float, budget_usd: float,
                           prompt_tokens: int, budget_tokens: int) -> bool:
        """硬限制:超出美元预算或 token 预算。"""
        if budget_usd > 0 and cost_usd >= budget_usd:
            return True
        if budget_tokens > 0 and prompt_tokens >= budget_tokens:
            return True
        return False

    def check_before_call(self, session_id: str) -> dict:
        """在 LLM 调用前检查预算,返回 {"allowed": bool, "reason": str}。"""
        status = self.get_status(session_id)
        if status.get("not_found"):
            return {"allowed": True, "reason": ""}
        hard = self._is_hard_limit_hit(
            status.get("cost_usd", 0), status.get("budget_usd", 0),
            status.get("prompt_tokens", 0), status.get("budget_tokens", 0),
        )
        if hard:
            return {
                "allowed": False,
                "reason": (
                    f"会话预算已耗尽: 已用 ${status['cost_usd']:.4f}/"
                    f"${status.get('budget_usd', 0):.4f}, "
                    f"已用 {status.get('prompt_tokens', 0)}/{status.get('budget_tokens', 0)} tokens"
                ),
            }
        soft = self._is_soft_warning(
            status.get("cost_usd", 0), status.get("budget_usd", 0),
        )
        if soft:
            remaining = max(0, status.get("budget_usd", 0) - status.get("cost_usd", 0))
            return {
                "allowed": True,
                "reason": f"警告:会话预算即将耗尽,剩余约 ${remaining:.4f}",
            }
        return {"allowed": True, "reason": ""}

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """列出所有会话的预算状态(按成本降序)。"""
        rows = self._backend.select(
            "session_budgets", order_by="total_cost_usd DESC", limit=limit,
        )
        return [dict(r) for r in rows]


_budget_tracker: Optional[SessionBudgetTracker] = None
_budget_lock = threading.Lock()


def get_budget_tracker(tenant_id: Optional[str] = None) -> SessionBudgetTracker:
    """获取预算追踪器。支持按租户隔离(多租户模式下创建独立实例)。"""
    global _budget_tracker
    if tenant_id and tenant_id != "default":
        # 多租户:按租户创建独立实例
        key = f"_{tenant_id}"
        tracker = globals().get(key)
        if tracker is None:
            with _budget_lock:
                tracker = globals().get(key)
                if tracker is None:
                    tracker = SessionBudgetTracker()
                    globals()[key] = tracker
        return tracker
    # 单租户或未指定:使用默认实例
    if _budget_tracker is None:
        with _budget_lock:
            if _budget_tracker is None:
                _budget_tracker = SessionBudgetTracker()
    return _budget_tracker
