"""性能监控 Metrics。

集成 Prometheus metrics，提供:
- 请求耗时直方图
- Token 消耗计数器
- 工具调用统计
- LLM 调用统计
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

# 延迟导入 prometheus_client
try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False


# ---- Metrics 定义 ----

# 请求耗时（秒）
_REQUEST_DURATION = None
# 请求计数
_REQUEST_COUNT = None
# Token 消耗
_TOKEN_USAGE = None
# LLM 调用计数
_LLM_CALL_COUNT = None
# LLM 调用失败计数
_LLM_ERROR_COUNT = None
# 工具调用计数
_TOOL_CALL_COUNT = None
# 活跃会话数
_ACTIVE_SESSIONS = None


def _init_metrics() -> None:
    """初始化 Prometheus metrics（延迟初始化）。"""
    global _REQUEST_DURATION, _REQUEST_COUNT, _TOKEN_USAGE
    global _LLM_CALL_COUNT, _LLM_ERROR_COUNT
    global _TOOL_CALL_COUNT, _ACTIVE_SESSIONS

    if not _PROM_AVAILABLE:
        return

    _REQUEST_DURATION = Histogram(
        'agent_request_duration_seconds',
        '请求耗时',
        ['provider', 'model'],
    )
    _REQUEST_COUNT = Counter(
        'agent_request_total',
        '请求总数',
        ['status'],  # success/error
    )
    _TOKEN_USAGE = Counter(
        'agent_token_usage_total',
        'Token 消耗',
        ['provider', 'model', 'type'],  # prompt/completion
    )
    _LLM_CALL_COUNT = Counter(
        'agent_llm_call_total',
        'LLM 调用次数',
        ['provider', 'model', 'kind'],  # chat/chat_with_tools/chat_json
    )
    _LLM_ERROR_COUNT = Counter(
        'agent_llm_error_total',
        'LLM 调用错误',
        ['provider', 'model', 'error_type'],
    )
    _TOOL_CALL_COUNT = Counter(
        'agent_tool_call_total',
        '工具调用次数',
        ['tool_name', 'success'],
    )
    _ACTIVE_SESSIONS = Gauge(
        'agent_active_sessions',
        '活跃会话数',
    )


def get_metrics() -> str:
    """获取 Prometheus metrics 输出。"""
    if not _PROM_AVAILABLE:
        return "# Prometheus metrics not available (install prometheus-client)"
    return generate_latest().decode('utf-8')


@contextmanager
def measure_request_duration(provider: str = "", model: str = ""):
    """请求耗时测量上下文管理器。"""
    if not _PROM_AVAILABLE:
        yield
        return

    with _REQUEST_DURATION.labels(provider=provider, model=model).time():
        yield


def inc_request_count(status: str = "success") -> None:
    """增加请求计数。"""
    if not _PROM_AVAILABLE:
        return
    _REQUEST_COUNT.labels(status=status).inc()


def inc_token_usage(provider: str, model: str, token_type: str, count: int) -> None:
    """增加 Token 使用计数。"""
    if not _PROM_AVAILABLE:
        return
    _TOKEN_USAGE.labels(provider=provider, model=model, type=token_type).inc(count)


def inc_llm_call_count(provider: str, model: str, kind: str = "chat") -> None:
    """增加 LLM 调用计数。"""
    if not _PROM_AVAILABLE:
        return
    _LLM_CALL_COUNT.labels(provider=provider, model=model, kind=kind).inc()


def inc_llm_error_count(provider: str, model: str, error_type: str = "unknown") -> None:
    """增加 LLM 错误计数。"""
    if not _PROM_AVAILABLE:
        return
    _LLM_ERROR_COUNT.labels(provider=provider, model=model, error_type=error_type).inc()


def inc_tool_call_count(tool_name: str, success: bool = True) -> None:
    """增加工具调用计数。"""
    if not _PROM_AVAILABLE:
        return
    _TOOL_CALL_COUNT.labels(tool_name=tool_name, success=str(success)).inc()


def set_active_sessions(count: int) -> None:
    """设置活跃会话数。"""
    if not _PROM_AVAILABLE:
        return
    _ACTIVE_SESSIONS.set(count)


# 模块加载时初始化
_init_metrics()
