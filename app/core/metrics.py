"""性能监控 Metrics。

集成 Prometheus metrics，提供:
- 请求耗时直方图
- Token 消耗计数器
- 工具调用统计
- LLM 调用统计
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Optional

# 延迟导入 prometheus_client
try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False


# ---- Metrics 定义 ----

# 请求耗时（秒）
REQUEST_DURATION = None
# 请求计数
REQUEST_COUNT = None
# Token 消耗
TOKEN_USAGE = None
# LLM 调用计数
LLM_CALL_COUNT = None
# LLM 调用失败计数
LLM_ERROR_COUNT = None
# 工具调用计数
TOOL_CALL_COUNT = None
# 活跃会话数
ACTIVE_SESSIONS = None


def _init_metrics():
    """初始化 Prometheus metrics（延迟初始化）。"""
    global REQUEST_DURATION, REQUEST_COUNT, TOKEN_USAGE
    global LLM_CALL_COUNT, LLM_ERROR_COUNT
    global TOOL_CALL_COUNT, ACTIVE_SESSIONS
    
    if not _PROM_AVAILABLE:
        return
    
    REQUEST_DURATION = Histogram(
        'agent_request_duration_seconds',
        '请求耗时',
        ['provider', 'model'],
    )
    REQUEST_COUNT = Counter(
        'agent_request_total',
        '请求总数',
        ['status'],  # success/error
    )
    TOKEN_USAGE = Counter(
        'agent_token_usage_total',
        'Token 消耗',
        ['provider', 'model', 'type'],  # prompt/completion
    )
    LLM_CALL_COUNT = Counter(
        'agent_llm_call_total',
        'LLM 调用次数',
        ['provider', 'model', 'kind'],  # chat/chat_with_tools/chat_json
    )
    LLM_ERROR_COUNT = Counter(
        'agent_llm_error_total',
        'LLM 调用错误',
        ['provider', 'model', 'error_type'],
    )
    TOOL_CALL_COUNT = Counter(
        'agent_tool_call_total',
        '工具调用次数',
        ['tool_name', 'success'],
    )
    ACTIVE_SESSIONS = Gauge(
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
    
    with REQUEST_DURATION.labels(provider=provider, model=model).time():
        yield


def inc_request_count(status: str = "success"):
    """增加请求计数。"""
    if not _PROM_AVAILABLE:
        return
    REQUEST_COUNT.labels(status=status).inc()


def inc_token_usage(provider: str, model: str, token_type: str, count: int):
    """增加 Token 使用计数。"""
    if not _PROM_AVAILABLE:
        return
    TOKEN_USAGE.labels(provider=provider, model=model, type=token_type).inc(count)


def inc_llm_call_count(provider: str, model: str, kind: str = "chat"):
    """增加 LLM 调用计数。"""
    if not _PROM_AVAILABLE:
        return
    LLM_CALL_COUNT.labels(provider=provider, model=model, kind=kind).inc()


def inc_llm_error_count(provider: str, model: str, error_type: str = "unknown"):
    """增加 LLM 错误计数。"""
    if not _PROM_AVAILABLE:
        return
    LLM_ERROR_COUNT.labels(provider=provider, model=model, error_type=error_type).inc()


def inc_tool_call_count(tool_name: str, success: bool = True):
    """增加工具调用计数。"""
    if not _PROM_AVAILABLE:
        return
    TOOL_CALL_COUNT.labels(tool_name=tool_name, success=str(success)).inc()


def set_active_sessions(count: int):
    """设置活跃会话数。"""
    if not _PROM_AVAILABLE:
        return
    ACTIVE_SESSIONS.set(count)


# 模块加载时初始化
_init_metrics()
