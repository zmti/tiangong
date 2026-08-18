"""Prometheus 格式指标端点:暴露 LLM/工具/成本/缓存等核心指标。

对标主流 AIOps 监控需求:
- /v1/metrics         Prometheus text format (可以被 Prometheus scrape)
- /v1/metrics/json    JSON 格式 (方便前端展示或 curl 调试)

指标分类:
  # HELP llm_calls_total 累计 LLM 调用次数
  # TYPE llm_calls_total counter
  llm_calls_total{model="deepseek-chat",provider="deepseek"} 42

  # HELP tool_calls_total 累计工具调用次数
  # TYPE tool_calls_total counter
  tool_calls_total{name="rag_search"} 15

  # HELP tool_errors_total 工具错误次数
  # TYPE tool_errors_total counter
  tool_errors_total{name="run_terminal"} 2

  # HELP agent_run_total 累计 Agent 运行次数
  # TYPE agent_run_total counter
  agent_run_total{status="success"} 20

  # HELP agent_cost_usd_total 累计成本(美元)
  # TYPE agent_cost_usd_total gauge
  agent_cost_usd_total 0.034

  # HELP embedding_cache_size 嵌入缓存条目数
  # TYPE embedding_cache_size gauge
  embedding_cache_size 1234

  # HELP rag_chunks_total RAG 向量库总 chunk 数
  # TYPE rag_chunks_total gauge
  rag_chunks_total 5678
"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Query

from app.config import settings
from app.rag.retriever import get_store

router = APIRouter(prefix="/v1", tags=["metrics"])


def _format_prometheus(lines: list[str]) -> str:
    """将指标行列表格式化为 Prometheus text format。"""
    return "\n".join(lines) + "\n"


def _collect_metrics() -> list[tuple[str, dict]]:
    """从 telemetry 和系统状态收集所有指标数据。"""
    metrics: list[tuple[str, dict]] = []

    # 1. Telemetry 聚合数据
    try:
        from app.core.telemetry import tracer as _tracer
        stats = _tracer.stats(limit=10000)
        metrics.append(("llm_calls_total", {"total": stats.get("llm_calls", 0)}))
        metrics.append(("tool_calls_total", {"total": stats.get("tool_calls", 0)}))
        metrics.append(("llm_errors_total", {"total": stats.get("errors", 0)}))
        metrics.append(("total_tokens", {
            "prompt": stats.get("prompt_tokens", 0),
            "completion": stats.get("completion_tokens", 0),
            "total": stats.get("total_tokens", 0),
        }))
        metrics.append(("cache_hit_tokens", {
            "total": stats.get("cache_hit_tokens", 0),
            "rate": stats.get("cache_hit_rate", 0.0),
        }))
        metrics.append(("cost_usd", {"total": stats.get("estimated_cost_usd", 0.0)}))
    except Exception:  # noqa: BLE001
        pass

    # 2. RAG 统计
    try:
        store = get_store()
        metrics.append(("rag_chunks_total", {"total": store.count()}))
    except Exception:  # noqa: BLE001
        pass

    # 3. Embedding 缓存统计
    try:
        from app.rag.cache import get_embedding_cache
        cache = get_embedding_cache()
        s = cache.stats()
        metrics.append(("embedding_cache_size", {"size": s.get("size", 0)}))
        metrics.append(("embedding_cache_max_size", {"max": s.get("max_size", 0)}))
    except Exception:  # noqa: BLE001
        pass

    # 4. 运行时统计
    try:
        from app.core.telemetry import tracer as _tracer
        recent = _tracer.recent(limit=100)
        now = time.time()
        llm_calls_1min = sum(1 for e in recent
                             if e.get("type") == "llm_call" and (now - e.get("ts", 0)) < 60)
        tool_calls_1min = sum(1 for e in recent
                              if e.get("type") == "tool" and (now - e.get("ts", 0)) < 60)
        metrics.append(("llm_calls_per_min", {"count": llm_calls_1min}))
        metrics.append(("tool_calls_per_min", {"count": tool_calls_1min}))
    except Exception:  # noqa: BLE001
        pass

    # 5. 后台任务状态
    try:
        from app.core.tasks import get_task_manager as _get_tm
        mgr = _get_tm()
        all_tasks = mgr.list(limit=100)
        metrics.append(("active_tasks_total", {
            "total": len(all_tasks),
            "running": sum(1 for t in all_tasks if t.get("status") == "running"),
            "queued": sum(1 for t in all_tasks if t.get("status") == "queued"),
        }))
    except Exception:  # noqa: BLE001
        pass

    return metrics


def _to_prometheus_text(metrics: list[tuple[str, dict]]) -> str:
    """转换为 Prometheus text format。"""
    lines = []
    lines.append("# HELP agent_service_uptime_seconds Service uptime in seconds.")
    lines.append("# TYPE agent_service_uptime_seconds gauge")
    lines.append(f"agent_service_uptime_seconds {time.time() - _get_start_time()}")
    lines.append("")

    for name, labels_data in metrics:
        for label_key, label_val in labels_data.items():
            if isinstance(label_val, float):
                val_str = f"{label_val:.6f}"
            elif isinstance(label_val, bool):
                val_str = "1" if label_val else "0"
            else:
                val_str = str(label_val)
            label_str = f'{{{label_key}="{label_val}"}}' if label_key else ""
            lines.append(f"agent_{name}{label_str} {val_str}")
        lines.append("")

    return "\n".join(lines)


def _to_json(metrics: list[tuple[str, dict]]) -> dict:
    """转换为 JSON 格式。"""
    result = {}
    for name, labels_data in metrics:
        if len(labels_data) == 1 and "" in labels_data:
            result[name] = labels_data[""]
        else:
            result[name] = dict(labels_data)
    result["_uptime_seconds"] = time.time() - _get_start_time()
    return result


# 模块级启动时间
_START_TIME = time.time()


def _get_start_time() -> float:
    return _START_TIME


@router.get("/metrics")
async def get_metrics(
    format: str = Query("prometheus", description="prometheus | json"),
) -> dict:
    """Prometheus 格式指标端点。

    format=prometheus: 标准 Prometheus scrape format (text/plain)
    format=json:       结构化 JSON (application/json)
    """
    metrics = _collect_metrics()
    if format.lower() == "json":
        return {"metrics": _to_json(metrics), "count": len(metrics)}
    return {"content": _to_prometheus_text(metrics), "format": "prometheus", "count": len(metrics)}


@router.get("/metrics/json")
async def get_metrics_json() -> dict:
    """JSON 格式指标(别名端点)。"""
    metrics = _collect_metrics()
    return _to_json(metrics)
