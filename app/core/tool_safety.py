"""工具安全/确认流的单一实现:供 Agent 与 ToolExecutor 共用,避免逻辑漂移。

此前 agent.py 与 tool_executor.py 各自实现了一套"危险工具确认扫描 / 确认请求持久化 /
恢复批准判定 / 危险工具限流",行为不一致导致潜在 bug(例如 ToolExecutor 的
is_tool_approved_by_resume 不回退查询持久化确认记录)。本模块把这些收敛为单一函数,
两处统一调用。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _parse_params(arguments: str) -> dict:
    """把工具参数字符串解析为 dict(失败返回 {"raw": ...})。"""
    try:
        params = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except (json.JSONDecodeError, TypeError):
        params = {}
    if not isinstance(params, dict):
        params = {"raw": str(params)}
    return params


def scan_pending_confirmations(
    tool_calls: list,
    approve_tools: Optional[list[str]],
    runtime_approved: Optional[set[str]],
    dangerous_tools: Optional[set[str]],
) -> list:
    """预扫描一批工具调用,返回需要人工确认且未批准的 (tool_call, decision) 列表。

    判定规则统一检查两套机制:
    - agent_tool_confirm 机制的危险工具(未在 approve_tools / runtime_approved)
    - agent_confirm_enabled 机制的 decide() 命中 confirm
    """
    from app.core.security import Decision, decide

    approve = set(approve_tools or [])
    approved = set(runtime_approved or [])
    danger = set(dangerous_tools or [])
    pending = []
    for tc in tool_calls:
        if tc.name in approve or tc.name in approved:
            continue
        # 机制1: 预定义危险工具清单
        if settings.agent_tool_confirm and tc.name in danger:
            pending.append((tc, Decision(
                confirm=True, reason=f"工具 {tc.name} 属于危险操作,需要用户确认",
            )))
            continue
        # 机制2: LLM 决策确认(agent_confirm_enabled)
        if settings.agent_confirm_enabled:
            decision = decide(tc.name, tc.arguments)
            if decision.confirm:
                pending.append((tc, decision))
    return pending


def create_confirm_requests(pending: list) -> list[dict]:
    """为待确认的工具调用创建持久化确认请求,返回请求列表(供事件与 resume 使用)。"""
    from app.core.security import describe
    from app.tools.collaboration_tool import _get_store

    store = _get_store()
    requests = []
    for tc, decision in pending:
        try:
            params = _parse_params(tc.arguments)
        except (json.JSONDecodeError, TypeError):
            params = {"raw": str(tc.arguments)}
        rec = store.create(
            action=tc.name,
            description=f"{getattr(decision, 'reason', '') or ''}。\n详情: {describe(tc.name, tc.arguments)}",
            params=params,
        )
        requests.append({
            "confirmation_id": rec["id"],
            "tool": tc.name,
            "arguments": tc.arguments,
            "reason": getattr(decision, "reason", "") or "",
        })
    return requests


def is_tool_approved_by_resume(tc, state, runtime_approved: Optional[set[str]] = None) -> bool:
    """恢复运行时,判断该工具是否已被用户批准。

    优先采用 runtime_approved(本次运行已获批集合);其次解析 resume 值:
    - list: ["tool_name", ...](已批准的工单名白名单)
    - dict: {"confirmation_id": {"approved": bool}}
    最后回退查询 persistent confirmations.db(用户在中断期间可能已通过前端批准)。
    """
    if runtime_approved and tc.name in runtime_approved:
        return True

    resume = (state.interrupt or {}).get("resume")
    if isinstance(resume, list):
        return tc.name in resume
    resume = resume or {}
    requests = (state.interrupt or {}).get("value", {}).get("requests") or []
    for req in requests:
        if req.get("tool") != tc.name:
            continue
        conf_id = req.get("confirmation_id", "")
        if isinstance(resume, dict) and conf_id in resume:
            return bool(resume.get(conf_id, {}).get("approved"))
        # 从持久化确认请求读状态(用户可能已通过前端批准)
        try:
            from app.tools.collaboration_tool import _get_store
            conf = _get_store().get(conf_id)
            if conf and conf.get("status") == "approved":
                return True
        except Exception as e:  # noqa: BLE001
            logger.warning("读取持久化确认状态失败: %s", e)
    return bool(requests and False)


class DangerousToolRateLimiter:
    """按危险工具名做滑动窗口限流(默认 60s 窗口)。

    ToolExecutor 原本内联实现限流,Agent 缺失;提取为共享类后两处统一。
    """

    def __init__(self, rate_limit_per_minute: Optional[int] = None):
        self._limit = int(rate_limit_per_minute) if rate_limit_per_minute is not None \
            else int(settings.agent_dangerous_tool_rate_limit or 10)
        self._calls: dict[str, list[float]] = {}
        self._window = 60.0

    def allow(self, name: str) -> tuple[bool, str]:
        """检查并登记一次调用。返回 (是否放行, 被限流时的提示文案)。"""
        if self._limit <= 0:
            return True, ""
        now = time.time()
        recent = [t for t in self._calls.get(name, []) if now - t < self._window]
        if len(recent) >= self._limit:
            return False, (
                f"工具 {name} 触发频率限制:最近 {int(self._window)} 秒内已调用 "
                f"{self._limit} 次,请稍后重试"
            )
        recent.append(now)
        self._calls[name] = recent
        return True, ""
