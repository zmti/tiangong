"""cost_tracker 工具:跨运行会话的成本追踪与汇总报告。

主要能力:
- cost_summary: 按 session_id/run_id 聚合 token 用量与估算成本
- cost_daily: 最近 N 天的成本趋势
- cost_model_breakdown: 各模型的用量/成本分布
- cost_report: 生成自然语言总结报告(适合在对话中调用)
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from pydantic import Field

from app.config import settings
from app.core.telemetry import TOKEN_PRICES, tracer
from app.tools.registry import tool


def _load_traces() -> list[dict]:
    """从 JSONL 文件加载所有 trace 事件。内存缓冲仅保留最近 1000 条,此处补全历史。"""
    base = os.path.join(settings.resolved_data_dir, "traces")
    if not os.path.isdir(base):
        return []
    events: list[dict] = list(tracer.recent(limit=10000))
    # 补充读文件
    for fname in sorted(os.listdir(base)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(base, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:  # noqa: BLE001
            pass
    return events


def _aggregate(events: list[dict]) -> dict:
    """从 trace 事件聚合 cost 相关指标。"""
    by_session: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    total_prompt = total_completion = total_cache_hit = 0
    total_cost = 0.0
    total_llm_calls = 0
    total_tool_calls = 0
    total_errors = 0

    for e in events:
        etype = e.get("type", "")
        if etype == "llm_call":
            total_llm_calls += 1
            pt = int(e.get("prompt_tokens", 0) or 0)
            ct = int(e.get("completion_tokens", 0) or 0)
            ch = int(e.get("cache_hit_tokens", 0) or 0)
            cost = float(e.get("cost", 0) or 0)
            total_prompt += pt
            total_completion += ct
            total_cache_hit += ch
            total_cost += cost
            if not e.get("ok", True):
                total_errors += 1
            model = e.get("model", "unknown")
            mb = by_model.setdefault(model, {"calls": 0, "prompt": 0, "completion": 0, "cost": 0.0, "cache_hit": 0})
            mb["calls"] += 1
            mb["prompt"] += pt
            mb["completion"] += ct
            mb["cost"] += cost
            mb["cache_hit"] += ch
            sid = e.get("session_id", "") or ""
            rid = e.get("run_id", "") or ""
            key = sid or rid or "unknown"
            sb = by_session.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0, "cost": 0.0, "runs": set()})
            sb["calls"] += 1
            sb["prompt"] += pt
            sb["completion"] += ct
            sb["cost"] += cost
            if rid:
                sb["runs"].add(rid)
            # 按天聚合
            ts = e.get("ts", 0) or 0
            day = time.strftime("%Y-%m-%d", time.localtime(float(ts))) if ts else "unknown"
            db = daily.setdefault(day, {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0})
            db["prompt"] += pt
            db["completion"] += ct
            db["cost"] += cost
            db["calls"] += 1
        elif etype == "tool":
            total_tool_calls += 1
            if not e.get("ok", True):
                total_errors += 1

    sessions = []
    for sid, sb in sorted(by_session.items(), key=lambda x: x[1]["cost"], reverse=True):
        sessions.append({
            "session_id": sid,
            "runs": len(sb["runs"]),
            "calls": sb["calls"],
            "prompt_tokens": sb["prompt"],
            "completion_tokens": sb["completion"],
            "cost_usd": round(sb["cost"], 6),
        })
    models = []
    for mn, mb in sorted(by_model.items(), key=lambda x: x[1]["cost"], reverse=True):
        models.append({
            "model": mn,
            "calls": mb["calls"],
            "prompt_tokens": mb["prompt"],
            "completion_tokens": mb["completion"],
            "cache_hit_tokens": mb["cache_hit"],
            "cost_usd": round(mb["cost"], 6),
        })
    days = []
    for day, db in sorted(daily.items(), reverse=True)[:30]:
        days.append({"date": day, **{k: round(v, 6) if k == "cost" else v for k, v in db.items()}})
    return {
        "total_llm_calls": total_llm_calls,
        "total_tool_calls": total_tool_calls,
        "total_errors": total_errors,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_cache_hit_tokens": total_cache_hit,
        "total_cost_usd": round(total_cost, 6),
        "sessions": sessions,
        "models": models,
        "daily": days,
    }


@tool("cost_summary", "查看成本汇总:按会话/模型/天聚合 token 用量与美元估算", timeout=5)
def cost_summary(
    session_id: str = Field("", description="按会话过滤;留空=全部"),
    days: int = Field(7, description="只看最近 N 天的数据(默认 7)"),
) -> dict:
    """返回成本汇总数据。"""
    events = _load_traces()
    # 按时间过滤
    if days > 0:
        cutoff = time.time() - days * 86400
        events = [e for e in events if (e.get("ts", 0) or 0) >= cutoff]
    if session_id:
        events = [e for e in events if e.get("session_id") == session_id or e.get("run_id", "").startswith(session_id[:8])]
    agg = _aggregate(events)
    if session_id:
        agg["filter"] = {"session_id": session_id, "days": days}
    return agg


@tool("cost_report", "生成自然语言成本报告,适合在对话中直接调用", timeout=5)
def cost_report(
    session_id: str = Field("", description="按会话生成;留空=全局"),
    top_models: int = Field(5, description="列出成本最高的前 N 个模型"),
) -> str:
    """返回人类可读的成本报告。"""
    events = _load_traces()
    agg = _aggregate(events)
    if session_id:
        events = [e for e in events if e.get("session_id") == session_id or e.get("run_id", "").startswith(session_id[:8])]
        agg = _aggregate(events)
    lines = [
        f"## 成本报告",
        f"- 总 LLM 调用次数: {agg['total_llm_calls']}",
        f"- 总工具调用次数: {agg['total_tool_calls']}",
        f"- 总输入 token: {agg['total_prompt_tokens']:,}",
        f"- 总输出 token: {agg['total_completion_tokens']:,}",
        f"- 缓存命中 token: {agg['total_cache_hit_tokens']:,}",
        f"- 缓存命中率: {agg['total_cache_hit_tokens']/max(1,agg['total_prompt_tokens'])*100:.1f}%",
        f"- 估算成本: **${agg['total_cost_usd']:.6f}**",
        f"- 错误次数: {agg['total_errors']}",
    ]
    if agg["models"]:
        lines.append("\n### 各模型成本分布:")
        for m in agg["models"][:top_models]:
            lines.append(f"- `{m['model']}`: {m['calls']} 次调用, ${m['cost_usd']:.6f}")
    if agg["sessions"]:
        lines.append("\n### 各会话成本:")
        for s in agg["sessions"][:5]:
            lines.append(f"- `{s['session_id']}`: ${s['cost_usd']:.6f} ({s['runs']} 次运行)")
    if agg["daily"]:
        lines.append("\n### 最近每日成本:")
        for d in agg["daily"][:7]:
            lines.append(f"- {d['date']}: ${d['cost']:.6f} ({d['calls']} 次调用)")
    return "\n".join(lines)


@tool("cost_daily_trend", "查看最近 N 天的成本趋势数据", timeout=5)
def cost_daily_trend(
    days: int = Field(14, description="天数,默认 14"),
) -> dict:
    """返回每日成本折线数据。"""
    events = _load_traces()
    cutoff = time.time() - days * 86400
    filtered = [e for e in events if (e.get("ts", 0) or 0) >= cutoff]
    agg = _aggregate(filtered)
    return {
        "days": days,
        "trend": agg.get("daily", []),
        "total_cost_usd": agg["total_cost_usd"],
    }
