"""可观测性:轻量 trace 记录,零外部依赖。

事件三写:
- 内存环缓冲(最近 max_buffer 条),供 /v1/traces 实时查询
- JSONL 文件 data/traces/<yyyy-mm-dd>.jsonl,供离线分析
- (可选)OTLP 导出:配置 OTEL_EXPORTER_OTLP_ENDPOINT 后,事件同步转发
  到 OTel Collector / Langfuse / 自建网关,接入外部可观测平台。

事件类型:
- run_start / run_end :一次 Agent 运行的边界(含步骤数、耗时、token)
- llm_call            :每次 LLM 调用(chat/chat_with_tools/chat_json/chat_stream),
                       含 provider/model/token/耗时/是否成功
- tool                :每次工具执行(含参数预览、结果长度、耗时/错误)

用 _current_run_id contextvar 把某次运行的所有事件(含路由/规划/记忆等
子调用)串成一条 trace。关闭开关或写入失败都不影响主流程。
OTLP 导出在导入 SDK 失败/端点异常时静默降级,不影响本地记录。
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time

from app.config import settings

# 当前 Agent 运行 id:LLM/工具埋点据此归属到对应 trace
_current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_run_id", default="")

# 常见模型 token 单价(美元/千 token),用于成本估算;未知模型记 0
TOKEN_PRICES = {
    "deepseek-chat": {"prompt": 0.00027, "completion": 0.0011},
    "qwen-plus": {"prompt": 0.0008, "completion": 0.002},
    "qwen3.7-flash": {"prompt": 0.0002, "completion": 0.0006},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按常见模型单价估算成本(美元);未知模型返回 0。"""
    price = TOKEN_PRICES.get((model or "").lower())
    if not price:
        return 0.0
    return (prompt_tokens * price["prompt"] + completion_tokens * price["completion"]) / 1000.0


class Tracer:
    """进程内 trace 缓冲 + JSONL 落盘 + 可选 OTLP 导出。线程安全,失败静默。"""

    def __init__(self, max_buffer: int = 1000):
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._max_buffer = max_buffer
        self._otlp = self._init_otlp()

    @staticmethod
    def _init_otlp():
        """按配置初始化 OTLP 导出器;未配置/失败返回 None(静默)。

        初始化前先做轻量连通性探测,端点不可达直接禁用,
        避免 BatchSpanProcessor 后台线程无限重试拖慢进程。
        """
        endpoint = (settings.otel_exporter_otlp_endpoint or "").strip()
        if not endpoint:
            return None
        try:
            # 连通性探测:只认 http(s);不可达/超时直接放弃 OTLP
            import urllib.parse
            import urllib.request

            parsed = urllib.parse.urlparse(endpoint)
            if parsed.scheme in ("http", "https"):
                probe = urllib.request.urlopen(endpoint, timeout=2)  # noqa: S310
                probe.close()

            from opentelemetry import trace as otel_trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider()
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
            otel_trace.set_tracer_provider(provider)
            return otel_trace.get_tracer("agent-service")
        except Exception:  # noqa: BLE001  # SDK 缺失/端点异常,静默降级
            return None

    def record(self, event: dict) -> None:
        if not settings.agent_tracing_enabled:
            return
        event = dict(event)
        event.setdefault("ts", time.time())
        event.setdefault("run_id", _current_run_id.get())
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[-self._max_buffer:]
            try:
                self._append_file(event)
            except Exception:  # noqa: BLE001  写盘失败不影响主流程
                pass
        if self._otlp is not None:
            try:
                self._export_otlp(event)
            except Exception:  # noqa: BLE001  OTLP 失败静默
                pass

    def _export_otlp(self, event: dict) -> None:
        """把事件映射为 OTel span 导出。"""
        t = self._otlp
        etype = event.get("type", "")
        ts = event.get("ts", time.time())
        start_ns = int(ts * 1_000_000_000)
        run_id = event.get("run_id", "")

        if etype == "run_start":
            span = t.start_span(
                f"agent.run {run_id[:8]}",
                attributes={
                    "run_id": run_id,
                    "session_id": event.get("session_id", ""),
                    "input": event.get("input", "")[:200],
                    "max_steps": event.get("max_steps", 0),
                },
                start_time=start_ns,
            )
            span.end(end_time=start_ns)  # 仅记录属性,真实耗时由 run_end 反映
            return
        if etype == "llm_call":
            attrs = {
                "provider": event.get("provider", ""),
                "model": event.get("model", ""),
                "prompt_tokens": event.get("prompt_tokens", 0),
                "completion_tokens": event.get("completion_tokens", 0),
                "cache_hit_tokens": event.get("cache_hit_tokens", 0),
                "cache_hit_rate": event.get("cache_hit_rate", 0.0),
                "cost": event.get("cost", 0.0),
                "duration": event.get("duration", 0),
                "ok": bool(event.get("ok", True)),
                "tools_n": event.get("tools_n", 0),
            }
            if not settings.otel_export_llm_details:
                attrs = {k: v for k, v in attrs.items() if k not in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_hit_rate", "cost", "tools_n")}
            span = t.start_span(
                f"llm.{event.get('kind', 'call')}",
                attributes=attrs,
                start_time=start_ns,
            )
            span.end(end_time=start_ns)
            return
        if etype == "tool":
            attrs = {
                "tool": event.get("name", ""),
                "ok": bool(event.get("ok", True)),
                "result_len": event.get("result_len", 0),
                "duration": event.get("duration", 0),
            }
            if event.get("args_preview"):
                attrs["args_preview"] = event["args_preview"]
            span = t.start_span(
                f"tool.{event.get('name', '')}",
                attributes=attrs,
                start_time=start_ns,
            )
            span.end(end_time=start_ns)
            return
        if etype == "run_end":
            t.start_span(
                f"agent.run {run_id[:8]} (end)",
                attributes={
                    "run_id": run_id,
                    "ok": bool(event.get("ok", True)),
                    "steps": event.get("steps", 0),
                    "answer_len": event.get("answer_len", 0),
                    "duration": event.get("duration", 0),
                },
                start_time=start_ns,
            ).end(end_time=start_ns)

    def _append_file(self, event: dict) -> None:
        base = os.path.join(settings.resolved_data_dir, "traces")
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, time.strftime("%Y-%m-%d") + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def recent(self, limit: int = 200, run_id: str = "", type_: str = "") -> list[dict]:
        with self._lock:
            items = list(self._buffer)
        if run_id:
            items = [e for e in items if e.get("run_id") == run_id]
        if type_:
            items = [e for e in items if e.get("type") == type_]
        return items[-limit:]

    def runs(self, limit: int = 50) -> list[dict]:
        """最近运行摘要:每条 run_id 一行,含 LLM 调用数、token、耗时、是否成功。"""
        with self._lock:
            items = list(self._buffer)
        by_run: dict[str, dict] = {}
        for e in items:
            rid = e.get("run_id")
            if not rid:
                continue
            r = by_run.setdefault(rid, {"run_id": rid, "llm_calls": 0, "tools": 0, "tokens": 0, "cost": 0.0})
            if e.get("type") == "llm_call":
                r["llm_calls"] += 1
                r["tokens"] += int(e.get("tokens", 0) or 0)
                r["cost"] += float(e.get("cost", 0) or 0)
                if not e.get("ok"):
                    r["error"] = e.get("error", "llm_error")
            elif e.get("type") == "tool":
                r["tools"] += 1
                if not e.get("ok"):
                    r["error"] = e.get("error", "tool_error")
            elif e.get("type") == "run_start":
                r["input"] = e.get("input", "")
            elif e.get("type") == "run_end":
                r["ok"] = bool(e.get("ok", True))
                r["steps"] = e.get("steps")
                r["answer_len"] = e.get("answer_len")
                r["duration"] = e.get("duration")
        return list(by_run.values())[-limit:]

    def stats(self, limit: int = 1000) -> dict:
        """最近 limit 条事件的聚合统计(LLM 调用数、token、成本、工具错误等)。"""
        with self._lock:
            items = list(self._buffer)[-limit:]
        llm_calls = tools = errors = 0
        prompt_tokens = completion_tokens = cache_hit_tokens = 0
        cost = 0.0
        for e in items:
            if e.get("type") == "llm_call":
                llm_calls += 1
                prompt_tokens += int(e.get("prompt_tokens", 0) or 0)
                completion_tokens += int(e.get("completion_tokens", 0) or 0)
                cache_hit_tokens += int(e.get("cache_hit_tokens", 0) or 0)
                cost += float(e.get("cost", 0) or 0)
                if not e.get("ok"):
                    errors += 1
            elif e.get("type") == "tool":
                tools += 1
                if not e.get("ok"):
                    errors += 1
        return {
            "llm_calls": llm_calls,
            "tool_calls": tools,
            "errors": errors,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cache_hit_tokens": cache_hit_tokens,
            "cache_hit_rate": round(cache_hit_tokens / prompt_tokens, 4) if prompt_tokens else 0.0,
            "estimated_cost_usd": round(cost, 6),
        }


tracer = Tracer()
