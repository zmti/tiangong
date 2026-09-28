"""结构化可观测性平台:OpenTelemetry + 指标收集 + Trace查询 + 告警。

核心能力:
1. 统一追踪:基于 OpenTelemetry,自动采集 LLM/工具/图节点调用链
2. 结构化指标:计数器/直方图/仪表盘指标,支持 Prometheus/Grafana
3. Trace 查询 API:按 run_id/session_id/user_id 检索完整调用链
4. 实时告警:预算超限/错误率飙升/延迟异常自动告警
5. 成本/质量仪表盘数据源:Token成本/成功率/评审分数时序
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

# 当前追踪上下文变量
_current_trace_context: contextvars.ContextVar[Optional["TraceContext"]] = contextvars.ContextVar(
    "current_trace_context", default=None
)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class TraceContext:
    """追踪上下文。"""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    session_id: str = ""
    task_id: str = ""
    user_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class Span:
    """单个 Span(调用片段)。"""
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    name: str
    kind: str  # llm / tool / graph_node / pec_role / verification / memory / cache
    start_time: float
    end_time: float = 0
    duration_ms: int = 0
    status: str = "ok"  # ok / error / timeout / cancelled
    error: str = ""
    attributes: dict = field(default_factory=dict)  # 标签: model, tokens, cost, tool_name, etc.
    events: list[dict] = field(default_factory=list)  # 事件: log, exception, etc.
    
    def finish(self, status: str = "ok", error: str = "") -> None:
        self.end_time = time.time()
        self.duration_ms = int((self.end_time - self.start_time) * 1000)
        self.status = status
        self.error = error
    
    def add_event(self, name: str, attributes: dict = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })
    
    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value
    
    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
            "events": self.events,
        }


@dataclass
class Trace:
    """完整追踪(一组关联 Span)。"""
    trace_id: str
    session_id: str = ""
    task_id: str = ""
    user_id: str = ""
    root_span: Optional[Span] = None
    spans: list[Span] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: float = 0
    total_duration_ms: int = 0
    status: str = "running"
    
    def add_span(self, span: Span) -> None:
        self.spans.append(span)
        if self.root_span is None or span.parent_span_id is None:
            self.root_span = span
    
    def finish(self, status: str = "ok") -> None:
        self.end_time = time.time()
        self.total_duration_ms = int((self.end_time - self.start_time) * 1000)
        self.status = status
    
    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "user_id": self.user_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_duration_ms": self.total_duration_ms,
            "status": self.status,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }


# ============================================================
# 指标收集器
# ============================================================

class MetricsCollector:
    """指标收集器:计数器/直方图/仪表盘。"""
    
    def __init__(self):
        self._counters: dict[str, float] = defaultdict(float)
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._gauges: dict[str, float] = {}
        self._lock = threading.RLock()
        self._max_histogram_samples = 10000
    
    def increment(self, name: str, value: float = 1.0, labels: dict = None) -> None:
        """增加计数器。"""
        key = self._make_key(name, labels)
        with self._lock:
            self._counters[key] += value
    
    def observe(self, name: str, value: float, labels: dict = None) -> None:
        """记录直方图观测值。"""
        key = self._make_key(name, labels)
        with self._lock:
            hist = self._histograms[key]
            hist.append(value)
            if len(hist) > self._max_histogram_samples:
                # 保留最近的样本
                self._histograms[key] = hist[-self._max_histogram_samples:]
    
    def gauge(self, name: str, value: float, labels: dict = None) -> None:
        """设置仪表盘值。"""
        key = self._make_key(name, labels)
        with self._lock:
            self._gauges[key] = value
    
    def _make_key(self, name: str, labels: dict = None) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"
    
    def get_counters(self) -> dict[str, float]:
        with self._lock:
            return dict(self._counters)
    
    def get_histograms(self) -> dict[str, dict]:
        with self._lock:
            result = {}
            for key, values in self._histograms.items():
                if not values:
                    continue
                sorted_vals = sorted(values)
                n = len(sorted_vals)
                result[key] = {
                    "count": n,
                    "sum": sum(sorted_vals),
                    "min": sorted_vals[0],
                    "max": sorted_vals[-1],
                    "mean": sum(sorted_vals) / n,
                    "p50": sorted_vals[n // 2],
                    "p90": sorted_vals[int(n * 0.9)],
                    "p95": sorted_vals[int(n * 0.95)],
                    "p99": sorted_vals[int(n * 0.99)],
                }
            return result
    
    def get_gauges(self) -> dict[str, float]:
        with self._lock:
            return dict(self._gauges)
    
    def get_all_metrics(self) -> dict:
        """获取所有指标(用于 Prometheus 导出)。"""
        return {
            "counters": self.get_counters(),
            "histograms": self.get_histograms(),
            "gauges": self.get_gauges(),
        }
    
    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()


# ============================================================
# 追踪器
# ============================================================

class Tracer:
    """分布式追踪器。"""
    
    def __init__(
        self,
        db_path: Optional[str] = None,
        backend: Optional[StorageBackend] = None,
        sample_rate: float = 1.0,
        max_spans_per_trace: int = 1000,
    ):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "traces.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._sample_rate = sample_rate
        self._max_spans_per_trace = max_spans_per_trace
        self._active_traces: dict[str, Trace] = {}
        self._init_db()
        
        # OpenTelemetry 集成(可选)
        self._otel_tracer = None
        self._init_otel()
    
    def _init_otel(self) -> None:
        """初始化 OpenTelemetry(如果配置了导出端点)。"""
        endpoint = getattr(settings, "otel_exporter_otlp_endpoint", "")
        if not endpoint:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            
            provider = TracerProvider(resource=Resource.create({"service.name": "tiangong-agent"}))
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            self._otel_tracer = trace.get_tracer("tiangong")
            logger.info("OpenTelemetry 初始化成功: %s", endpoint)
        except Exception as e:
            logger.warning("OpenTelemetry 初始化失败: %s", e)
    
    def _init_db(self) -> None:
        schema = {
            "traces": [
                {"name": "trace_id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL DEFAULT ''"},
                {"name": "task_id", "type": "TEXT", "constraints": ""},
                {"name": "user_id", "type": "TEXT", "constraints": "DEFAULT ''"},
                {"name": "root_span_name", "type": "TEXT", "constraints": ""},
                {"name": "start_time", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "end_time", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "total_duration_ms", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "status", "type": "TEXT", "constraints": "DEFAULT 'running'"},
                {"name": "span_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "spans_json", "type": "TEXT", "constraints": "NOT NULL"},
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_traces_task ON traces(task_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_traces_time ON traces(start_time)")
    
    def start_trace(
        self,
        name: str,
        session_id: str = "",
        task_id: str = "",
        user_id: str = "",
        kind: str = "agent_run",
    ) -> Trace:
        """开始新追踪。"""
        # 采样决策
        import random
        if random.random() > self._sample_rate:
            return None
        
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        
        trace = Trace(
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            user_id=user_id,
        )
        
        root_span = Span(
            span_id=span_id,
            trace_id=trace_id,
            parent_span_id=None,
            name=name,
            kind=kind,
            start_time=time.time(),
            attributes={"session_id": session_id, "task_id": task_id},
        )
        trace.root_span = root_span
        trace.spans.append(root_span)
        
        with self._lock:
            self._active_traces[trace_id] = trace
        
        # 设置上下文变量
        ctx = TraceContext(
            trace_id=trace_id,
            span_id=span_id,
            session_id=session_id,
            task_id=task_id,
            user_id=user_id,
        )
        _current_trace_context.set(ctx)
        
        # OpenTelemetry span
        if self._otel_tracer:
            otel_span = self._otel_tracer.start_span(name)
            otel_span.set_attribute("trace_id", trace_id)
            otel_span.set_attribute("session_id", session_id)
            if task_id:
                otel_span.set_attribute("task_id", task_id)
        
        return trace
    
    def start_span(
        self,
        name: str,
        kind: str,
        parent_span_id: Optional[str] = None,
        attributes: dict = None,
    ) -> Optional[Span]:
        """开始子 Span。"""
        ctx = _current_trace_context.get()
        if not ctx:
            return None
        
        trace = self._active_traces.get(ctx.trace_id)
        if not trace:
            return None
        
        if len(trace.spans) >= self._max_spans_per_trace:
            return None
        
        span_id = uuid.uuid4().hex[:16]
        parent_id = parent_span_id or ctx.span_id
        
        span = Span(
            span_id=span_id,
            trace_id=ctx.trace_id,
            parent_span_id=parent_id,
            name=name,
            kind=kind,
            start_time=time.time(),
            attributes=attributes or {},
        )
        
        trace.add_span(span)
        
        # 更新上下文
        ctx.span_id = span_id
        ctx.parent_span_id = parent_id
        
        # OpenTelemetry
        if self._otel_tracer:
            # 这里简化,实际需要 span context 传递
            pass
        
        return span
    
    def end_span(self, span: Span, status: str = "ok", error: str = "") -> None:
        """结束 Span。"""
        if not span:
            return
        span.finish(status, error)
        
        # OpenTelemetry
        # 这里需要维护 otel span 栈,简化省略
    
    def end_trace(self, trace_id: str, status: str = "ok") -> Optional[Trace]:
        """结束追踪并持久化。"""
        with self._lock:
            trace = self._active_traces.pop(trace_id, None)
        
        if not trace:
            return None
        
        trace.finish(status)
        
        # 清理上下文
        ctx = _current_trace_context.get()
        if ctx and ctx.trace_id == trace_id:
            _current_trace_context.set(None)
        
        # 持久化
        self._persist_trace(trace)
        
        return trace
    
    def _persist_trace(self, trace: Trace) -> None:
        """持久化追踪到存储后端。"""
        try:
            spans_json = json.dumps([s.to_dict() for s in trace.spans], ensure_ascii=False)
            self._backend.insert(
                "traces",
                {
                    "trace_id": trace.trace_id,
                    "session_id": trace.session_id,
                    "task_id": trace.task_id,
                    "user_id": trace.user_id,
                    "root_span_name": trace.root_span.name if trace.root_span else "",
                    "start_time": trace.start_time,
                    "end_time": trace.end_time,
                    "total_duration_ms": trace.total_duration_ms,
                    "status": trace.status,
                    "span_count": len(trace.spans),
                    "spans_json": spans_json,
                },
            )
        except Exception as e:
            logger.warning("追踪持久化失败: %s", e)
    
    def get_trace(self, trace_id: str) -> Optional[Trace]:
        """获取追踪详情。"""
        # 先查活跃追踪
        with self._lock:
            trace = self._active_traces.get(trace_id)
        if trace:
            return trace
        
        # 查数据库
        rows = self._backend.select("traces", "trace_id = ?", (trace_id,), limit=1)
        if not rows:
            return None
        
        row = rows[0]
        trace = Trace(
            trace_id=row["trace_id"],
            session_id=row["session_id"],
            task_id=row["task_id"],
            user_id=row["user_id"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            total_duration_ms=row["total_duration_ms"],
            status=row["status"],
        )
        try:
            spans_data = json.loads(row["spans_json"])
            trace.spans = [Span(**s) for s in spans_data]
            trace.root_span = trace.spans[0] if trace.spans else None
        except Exception:
            pass
        return trace
    
    def query_traces(
        self,
        session_id: str = None,
        task_id: str = None,
        user_id: str = None,
        status: str = None,
        start_time: float = None,
        end_time: float = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trace]:
        """查询追踪列表。"""
        conditions = []
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if start_time:
            conditions.append("start_time >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("start_time <= ?")
            params.append(end_time)
        
        where = " AND ".join(conditions) if conditions else "1=1"
        
        rows = self._backend.select(
            "traces",
            where,
            tuple(params),
            order_by="start_time DESC",
            limit=limit,
            offset=offset,
        )
        
        traces = []
        for row in rows:
            trace = Trace(
                trace_id=row["trace_id"],
                session_id=row["session_id"],
                task_id=row["task_id"],
                user_id=row["user_id"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                total_duration_ms=row["total_duration_ms"],
                status=row["status"],
            )
            traces.append(trace)
        return traces
    
    def get_trace_summary(self, trace_id: str) -> dict:
        """获取追踪摘要(用于仪表盘)。"""
        trace = self.get_trace(trace_id)
        if not trace:
            return {}
        
        # 统计各类 span
        by_kind = defaultdict(int)
        by_status = defaultdict(int)
        total_tokens = 0
        total_cost = 0.0
        errors = []
        
        for span in trace.spans:
            by_kind[span.kind] += 1
            by_status[span.status] += 1
            if span.attributes.get("output_tokens"):
                total_tokens += span.attributes.get("output_tokens", 0)
            if span.attributes.get("cost_usd"):
                total_cost += span.attributes.get("cost_usd", 0)
            if span.status == "error":
                errors.append({"span": span.name, "error": span.error})
        
        return {
            "trace_id": trace.trace_id,
            "session_id": trace.session_id,
            "task_id": trace.task_id,
            "duration_ms": trace.total_duration_ms,
            "status": trace.status,
            "span_count": len(trace.spans),
            "by_kind": dict(by_kind),
            "by_status": dict(by_status),
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost,
            "error_count": len(errors),
            "errors": errors[:5],
        }


# ============================================================
# 告警引擎
# ============================================================

@dataclass
class AlertRule:
    """告警规则。"""
    name: str
    metric: str  # 指标名
    condition: str  # > / < / >= / <= / ==
    threshold: float
    labels: dict = field(default_factory=dict)
    severity: str = "warning"  # info / warning / critical
    cooldown_seconds: int = 300  # 冷却期
    enabled: bool = True


@dataclass
class Alert:
    """告警实例。"""
    rule_name: str
    metric: str
    value: float
    threshold: float
    severity: str
    message: str
    timestamp: float = field(default_factory=time.time)
    labels: dict = field(default_factory=dict)
    acknowledged: bool = False


class AlertEngine:
    """告警引擎:基于指标实时评估规则。"""
    
    DEFAULT_RULES = [
        AlertRule("token_budget_exceeded", "token_usage.session_used_pct", ">", 90, severity="critical"),
        AlertRule("token_budget_warning", "token_usage.session_used_pct", ">", 80, severity="warning"),
        AlertRule("error_rate_high", "error_rate", ">", 0.1, severity="critical"),
        AlertRule("latency_p99_high", "latency.p99_ms", ">", 30000, severity="warning"),
        AlertRule("llm_failure_rate", "llm.failure_rate", ">", 0.2, severity="critical"),
        AlertRule("tool_failure_rate", "tool.failure_rate", ">", 0.15, severity="warning"),
        AlertRule("critic_score_low", "critic.avg_score", "<", 0.6, severity="warning"),
        AlertRule("cache_hit_rate_low", "cache.hit_rate", "<", 0.3, severity="info"),
    ]
    
    def __init__(self, metrics: MetricsCollector, rules: list[AlertRule] = None):
        self.metrics = metrics
        self.rules = rules or self.DEFAULT_RULES
        self._last_fired: dict[str, float] = {}
        self._alerts: list[Alert] = []
        self._lock = threading.RLock()
        self._handlers: list[Callable[[Alert], None]] = []
    
    def register_handler(self, handler: Callable[[Alert], None]) -> None:
        """注册告警处理器(如发送钉钉/Slack/邮件)。"""
        self._handlers.append(handler)
    
    def evaluate(self) -> list[Alert]:
        """评估所有规则,返回触发的告警。"""
        triggered = []
        all_metrics = self.metrics.get_all_metrics()
        
        # 扁平化指标
        flat_metrics = {}
        for category, data in all_metrics.items():
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, (int, float)):
                        flat_metrics[f"{category}.{key}"] = value
                    elif isinstance(value, dict) and "mean" in value:
                        flat_metrics[f"{category}.{key}.mean"] = value["mean"]
                    elif isinstance(value, dict) and "p99" in value:
                        flat_metrics[f"{category}.{key}.p99"] = value["p99"]
        
        now = time.time()
        for rule in self.rules:
            if not rule.enabled:
                continue
            
            # 检查冷却期
            last = self._last_fired.get(rule.name, 0)
            if now - last < rule.cooldown_seconds:
                continue
            
            # 获取指标值
            value = flat_metrics.get(rule.metric)
            if value is None:
                continue
            
            # 评估条件
            triggered_now = False
            if rule.condition == ">" and value > rule.threshold:
                triggered_now = True
            elif rule.condition == "<" and value < rule.threshold:
                triggered_now = True
            elif rule.condition == ">=" and value >= rule.threshold:
                triggered_now = True
            elif rule.condition == "<=" and value <= rule.threshold:
                triggered_now = True
            elif rule.condition == "==" and value == rule.threshold:
                triggered_now = True
            
            if triggered_now:
                alert = Alert(
                    rule_name=rule.name,
                    metric=rule.metric,
                    value=value,
                    threshold=rule.threshold,
                    severity=rule.severity,
                    message=f"{rule.name}: {rule.metric}={value:.2f} {rule.condition} {rule.threshold}",
                    labels=rule.labels,
                )
                triggered.append(alert)
                self._last_fired[rule.name] = now
                
                with self._lock:
                    self._alerts.append(alert)
                
                # 触发处理器
                for handler in self._handlers:
                    try:
                        handler(alert)
                    except Exception as e:
                        logger.error("告警处理器异常: %s", e)
        
        return triggered
    
    def get_active_alerts(self, unacknowledged_only: bool = True) -> list[Alert]:
        with self._lock:
            alerts = list(self._alerts)
        if unacknowledged_only:
            return [a for a in alerts if not a.acknowledged]
        return alerts
    
    def acknowledge(self, rule_name: str) -> bool:
        with self._lock:
            for alert in self._alerts:
                if alert.rule_name == rule_name and not alert.acknowledged:
                    alert.acknowledged = True
                    return True
        return False


# ============================================================
# 统一可观测性门面
# ============================================================

class Observability:
    """统一可观测性门面:整合追踪、指标、告警。"""
    
    def __init__(self):
        self.tracer = Tracer()
        self.metrics = MetricsCollector()
        self.alert_engine = AlertEngine(self.metrics)
        self._setup_default_alert_handlers()
    
    def _setup_default_alert_handlers(self) -> None:
        """默认告警处理器:记录日志。"""
        def log_alert(alert: Alert):
            level = {
                "info": logging.INFO,
                "warning": logging.WARNING,
                "critical": logging.ERROR,
            }.get(alert.severity, logging.WARNING)
            logger.log(level, "ALERT: %s", alert.message)
        
        self.alert_engine.register_handler(log_alert)
    
    # --- 追踪便捷方法 ---
    
    def start_trace(self, name: str, session_id: str = "", task_id: str = "", user_id: str = "", kind: str = "agent_run") -> Trace:
        return self.tracer.start_trace(name, session_id, task_id, user_id, kind)
    
    def start_span(self, name: str, kind: str, attributes: dict = None) -> Optional[Span]:
        return self.tracer.start_span(name, kind, attributes=attributes)
    
    def end_span(self, span: Span, status: str = "ok", error: str = "") -> None:
        self.tracer.end_span(span, status, error)
    
    def end_trace(self, trace_id: str, status: str = "ok") -> Optional[Trace]:
        return self.tracer.end_trace(trace_id, status)
    
    def get_trace(self, trace_id: str) -> Optional[Trace]:
        return self.tracer.get_trace(trace_id)
    
    def query_traces(self, **kwargs) -> list[Trace]:
        return self.tracer.query_traces(**kwargs)
    
    def get_trace_summary(self, trace_id: str) -> dict:
        return self.tracer.get_trace_summary(trace_id)
    
    # --- 指标便捷方法 ---
    
    def inc(self, name: str, value: float = 1.0, labels: dict = None) -> None:
        self.metrics.increment(name, value, labels)
    
    def obs(self, name: str, value: float, labels: dict = None) -> None:
        self.metrics.observe(name, value, labels)
    
    def gauge(self, name: str, value: float, labels: dict = None) -> None:
        self.metrics.gauge(name, value, labels)
    
    def get_metrics(self) -> dict:
        return self.metrics.get_all_metrics()
    
    # --- 告警便捷方法 ---
    
    def check_alerts(self) -> list[Alert]:
        return self.alert_engine.evaluate()
    
    def get_alerts(self, unacknowledged_only: bool = True) -> list[Alert]:
        return self.alert_engine.get_active_alerts(unacknowledged_only)
    
    def acknowledge_alert(self, rule_name: str) -> bool:
        return self.alert_engine.acknowledge(rule_name)
    
    # --- 专用记录方法 ---
    
    def record_llm_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        cost_usd: float,
        success: bool,
        session_id: str = "",
        task_id: str = "",
    ) -> None:
        """记录 LLM 调用指标。"""
        labels = {"provider": provider, "model": model, "session_id": session_id}
        self.inc("llm.calls.total", 1, labels)
        self.inc("llm.tokens.input", input_tokens, labels)
        self.inc("llm.tokens.output", output_tokens, labels)
        self.obs("llm.latency_ms", latency_ms, labels)
        self.obs("llm.cost_usd", cost_usd, labels)
        if not success:
            self.inc("llm.calls.failed", 1, labels)
    
    def record_tool_call(
        self,
        tool_name: str,
        latency_ms: int,
        success: bool,
        session_id: str = "",
        task_id: str = "",
    ) -> None:
        """记录工具调用指标。"""
        labels = {"tool": tool_name, "session_id": session_id}
        self.inc("tool.calls.total", 1, labels)
        self.obs("tool.latency_ms", latency_ms, labels)
        if not success:
            self.inc("tool.calls.failed", 1, labels)
    
    def record_verification(
        self,
        check_type: str,  # pytest / ruff / mypy
        success: bool,
        duration_ms: int,
        session_id: str = "",
    ) -> None:
        """记录验证钩子指标。"""
        labels = {"check": check_type, "session_id": session_id}
        self.inc("verification.runs.total", 1, labels)
        self.obs("verification.duration_ms", duration_ms, labels)
        if not success:
            self.inc("verification.runs.failed", 1, labels)
    
    def record_memory_consolidation(
        self,
        memories_extracted: int,
        duration_ms: int,
        session_id: str = "",
    ) -> None:
        """记录记忆固化指标。"""
        labels = {"session_id": session_id}
        self.inc("memory.consolidated", memories_extracted, labels)
        self.obs("memory.consolidation_duration_ms", duration_ms, labels)
    
    def record_pec_role(
        self,
        role: str,  # planner / executor / critic
        action: str,  # create_plan / execute_step / review
        duration_ms: int,
        success: bool,
        session_id: str = "",
    ) -> None:
        """记录 PEC 角色指标。"""
        labels = {"role": role, "action": action, "session_id": session_id}
        self.inc("pec.actions.total", 1, labels)
        self.obs("pec.duration_ms", duration_ms, labels)
        if not success:
            self.inc("pec.actions.failed", 1, labels)
    
    def record_cost(self, session_id: str, task_id: str, tokens: int, cost_usd: float) -> None:
        """记录成本指标。"""
        labels = {"session_id": session_id}
        self.inc("cost.tokens", tokens, labels)
        self.obs("cost.usd", cost_usd, labels)
        self.gauge("cost.session_tokens", tokens, labels)
        self.gauge("cost.session_usd", cost_usd, labels)


# 全局单例
_observability: Optional[Observability] = None


def get_observability() -> Observability:
    global _observability
    if _observability is None:
        _observability = Observability()
    return _observability


# ============================================================
# 装饰器:自动追踪
# ============================================================

def traced(name: str = None, kind: str = "function"):
    """装饰器:自动为函数创建追踪 Span。"""
    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            obs = get_observability()
            span_name = name or f"{func.__module__}.{func.__name__}"
            span = obs.start_span(span_name, kind)
            try:
                result = await func(*args, **kwargs)
                obs.end_span(span, "ok")
                return result
            except Exception as e:
                obs.end_span(span, "error", str(e))
                raise
        
        def sync_wrapper(*args, **kwargs):
            obs = get_observability()
            span_name = name or f"{func.__module__}.{func.__name__}"
            span = obs.start_span(span_name, kind)
            try:
                result = func(*args, **kwargs)
                obs.end_span(span, "ok")
                return result
            except Exception as e:
                obs.end_span(span, "error", str(e))
                raise
        
        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator


# ============================================================
# 上下文管理器:手动追踪
# ============================================================

class TraceContextManager:
    """追踪上下文管理器。"""
    
    def __init__(self, name: str, kind: str = "block", attributes: dict = None):
        self.name = name
        self.kind = kind
        self.attributes = attributes
        self.span = None
    
    def __enter__(self):
        self.span = get_observability().start_span(self.name, self.kind, self.attributes)
        return self.span
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            get_observability().end_span(self.span, "error", str(exc_val))
        else:
            get_observability().end_span(self.span, "ok")
        return False


def trace_block(name: str, kind: str = "block", attributes: dict = None) -> TraceContextManager:
    """创建追踪块上下文管理器。"""
    return TraceContextManager(name, kind, attributes)