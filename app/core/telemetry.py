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

import asyncio
import contextvars
import json
import logging
import os
import threading
import time

from app.config import settings

logger = logging.getLogger(__name__)

# 当前 Agent 运行 id:LLM/工具埋点据此归属到对应 trace
_current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_run_id", default="")

# 常见模型 token 单价(美元/千 token),用于成本估算;未知模型记 0
TOKEN_PRICES = {
    "deepseek-chat": {"prompt": 0.00027, "completion": 0.0011},
    "deepseek-r1": {"prompt": 0.00055, "completion": 0.00219},
    "qwen-plus": {"prompt": 0.0008, "completion": 0.002},
    "qwen3.7-flash": {"prompt": 0.0002, "completion": 0.0006},
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
    "gpt-4o": {"prompt": 0.0025, "completion": 0.01},
    "claude-3.5-sonnet": {"prompt": 0.003, "completion": 0.015},
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按真实 usage + 静态价格估算成本(美元)。

    优先从模型名匹配已知价格表;未知模型使用默认兜底价格(0.001/千 token)。
    缓存命中 token 会减少实际计费,此处按 50% 折扣估算。
    """
    price = TOKEN_PRICES.get((model or "").lower())
    if not price:
        # 未知模型使用通用估算价格
        default_rate = 0.001  # $/千 token
        return (prompt_tokens + completion_tokens) * default_rate / 1000.0
    # 缓存命中 token 通常享受 50% 折扣
    cache_hit = int(prompt_tokens * 0.5)  # 简化估算:50% 命中视为折扣
    effective_prompt = prompt_tokens - cache_hit
    return (effective_prompt * price["prompt"] + completion_tokens * price["completion"]) / 1000.0


class Tracer:
    """进程内 trace 缓冲 + JSONL 落盘 + 可选 OTLP 导出。线程安全,失败静默。"""

    def __init__(self, max_buffer: int = 1000, retention_days: float = 7.0):
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self._max_buffer = max_buffer
        self._retention_days = retention_days
        self._otlp = self._init_otlp()
        self._last_cleanup = time.time()
        self._cleanup_interval = 3600  # 每小时清理一次
        self._cleanup_task = None

    def start_background_cleanup(self) -> None:
        """启动后台清理任务。"""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(self._cleanup_loop())
            logger.info("Telemetry 后台清理已启动 (retention=%d天, interval=1小时)", int(self._retention_days))
        except RuntimeError:
            logger.debug("没有运行中的事件循环，跳过后台清理任务创建")

    def stop_background_cleanup(self) -> None:
        """停止后台清理任务。"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
            logger.info("Telemetry 后台清理已停止")

    async def _cleanup_loop(self) -> None:
        """后台清理循环。"""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self.cleanup_old_events()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Telemetry 清理任务出错: %s", e)

    async def cleanup_old_events(self) -> int:
        """清理过期的事件记录。
        
        Returns:
            清理的事件数量
        """
        cutoff = time.time() - (self._retention_days * 86400)
        
        with self._lock:
            original_count = len(self._buffer)
            self._buffer = [e for e in self._buffer if e.get("ts", 0) > cutoff]
            cleaned = original_count - len(self._buffer)
        
        # 清理过期的 JSONL 文件
        cleaned_files = await self._cleanup_old_files(cutoff)
        
        if cleaned > 0 or cleaned_files > 0:
            logger.info("Telemetry 清理完成: 内存 %d 条, 文件 %d 个", cleaned, cleaned_files)
        
        return cleaned + cleaned_files

    async def _cleanup_old_files(self, cutoff: float) -> int:
        """清理过期的 JSONL 文件。
        
        Returns:
            清理的文件数量
        """
        import os as _os
        base = _os.path.join(settings.resolved_data_dir, "traces")
        if not _os.path.exists(base):
            return 0
        
        cleaned = 0
        try:
            for filename in _os.listdir(base):
                if not filename.endswith('.jsonl'):
                    continue
                filepath = _os.path.join(base, filename)
                # 文件名格式: yyyy-mm-dd.jsonl
                try:
                    file_date_str = filename.replace('.jsonl', '')
                    file_date = time.mktime(time.strptime(file_date_str, '%Y-%m-%d'))
                    if file_date < cutoff:
                        _os.remove(filepath)
                        cleaned += 1
                        logger.debug("已删除过期 trace 文件: %s", filename)
                except ValueError:
                    continue
        except Exception as e:
            logger.warning("清理 trace 文件时出错: %s", e)
        
        return cleaned

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


async def cleanup_traces() -> int:
    """清理过期的 trace 记录（供管理接口调用）。"""
    return await tracer.cleanup_old_events()
