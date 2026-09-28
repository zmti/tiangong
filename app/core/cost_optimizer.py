"""成本优化引擎 - 企业级成本管理与优化

功能:
- Token 使用追踪与成本计算
- 预算管理(会话级/用户级/项目级/租户级)
- 成本优化策略(模型路由/缓存/提示词压缩)
- 成本报告与告警
- 成本异常检测
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Callable, Awaitable, Tuple

import logging

logger = logging.getLogger(__name__)


class BudgetPeriod(Enum):
    """预算周期"""
    SESSION = "session"       # 单次会话
    HOURLY = "hourly"         # 小时
    DAILY = "daily"           # 每日
    WEEKLY = "weekly"         # 每周
    MONTHLY = "monthly"       # 每月
    PROJECT = "project"       # 项目级


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class ModelPricing:
    """模型定价"""
    provider: str
    model: str
    # 单价 (USD per 1K tokens)
    input_price_per_1k: float
    output_price_per_1k: float
    # 缓存 Token 价格 (通常更便宜)
    cache_input_price_per_1k: float = 0.0
    # 最大上下文长度
    max_context: int = 4096
    # 是否支持流式
    supports_streaming: bool = True
    # 是否支持工具调用
    supports_tools: bool = False
    
    def calculate_cost(self, input_tokens: int, output_tokens: int, cache_tokens: int = 0) -> float:
        """计算成本"""
        input_cost = (input_tokens - cache_tokens) / 1000.0 * self.input_price_per_1k
        cache_cost = cache_tokens / 1000.0 * self.cache_input_price_per_1k
        output_cost = output_tokens / 1000.0 * self.output_price_per_1k
        return input_cost + cache_cost + output_cost


# 预置模型定价表 (2024 年参考价格，需定期更新)
DEFAULT_MODEL_PRICING: Dict[str, ModelPricing] = {
    "openai:gpt-4o": ModelPricing(
        provider="openai",
        model="gpt-4o",
        input_price_per_1k=0.005,
        output_price_per_1k=0.015,
        cache_input_price_per_1k=0.0025,
        max_context=128000,
        supports_tools=True,
    ),
    "openai:gpt-4o-mini": ModelPricing(
        provider="openai",
        model="gpt-4o-mini",
        input_price_per_1k=0.00015,
        output_price_per_1k=0.0006,
        cache_input_price_per_1k=0.000075,
        max_context=128000,
        supports_tools=True,
    ),
    "openai:gpt-4-turbo": ModelPricing(
        provider="openai",
        model="gpt-4-turbo",
        input_price_per_1k=0.01,
        output_price_per_1k=0.03,
        max_context=128000,
        supports_tools=True,
    ),
    "anthropic:claude-3-5-sonnet": ModelPricing(
        provider="anthropic",
        model="claude-3-5-sonnet",
        input_price_per_1k=0.003,
        output_price_per_1k=0.015,
        cache_input_price_per_1k=0.0015,
        max_context=200000,
        supports_tools=True,
    ),
    "anthropic:claude-3-haiku": ModelPricing(
        provider="anthropic",
        model="claude-3-haiku",
        input_price_per_1k=0.00025,
        output_price_per_1k=0.00125,
        max_context=200000,
        supports_tools=True,
    ),
    "deepseek:deepseek-chat": ModelPricing(
        provider="deepseek",
        model="deepseek-chat",
        input_price_per_1k=0.00014,
        output_price_per_1k=0.00028,
        max_context=32768,
        supports_tools=True,
    ),
    "qwen:qwen-max": ModelPricing(
        provider="qwen",
        model="qwen-max",
        input_price_per_1k=0.0004,
        output_price_per_1k=0.0012,
        max_context=32768,
        supports_tools=True,
    ),
    "ollama:local": ModelPricing(
        provider="ollama",
        model="local",
        input_price_per_1k=0.0,
        output_price_per_1k=0.0,
        max_context=4096,
        supports_tools=False,
    ),
}


@dataclass
class TokenUsage:
    """Token 使用记录"""
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    user_id: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    
    @property
    def input_tokens_effective(self) -> int:
        return self.input_tokens - self.cache_tokens
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Budget:
    """预算配置"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    name: str = ""
    scope: str = "user"  # session/user/project/tenant
    scope_id: str = ""   # 对应的 session_id/user_id/project_id/tenant_id
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    limit_usd: float = 100.0
    # 告警阈值 (0-1)
    warning_threshold: float = 0.8    # 80% 告警
    critical_threshold: float = 0.95  # 95% 严重告警
    # 硬限制: 超过后阻止请求
    hard_limit: bool = True
    # 自动重置
    auto_reset: bool = True
    # 当前周期统计
    spent_usd: float = 0.0
    request_count: int = 0
    token_count: int = 0
    period_start: float = field(default_factory=time.time)
    period_end: Optional[float] = None
    # 状态
    is_active: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["period"] = self.period.value
        return d
    
    @property
    def usage_ratio(self) -> float:
        if self.limit_usd <= 0:
            return 0.0
        return self.spent_usd / self.limit_usd
    
    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)
    
    @property
    def is_warning(self) -> bool:
        return self.usage_ratio >= self.warning_threshold
    
    @property
    def is_critical(self) -> bool:
        return self.usage_ratio >= self.critical_threshold
    
    @property
    def is_exceeded(self) -> bool:
        return self.spent_usd >= self.limit_usd
    
    def check_thresholds(self) -> List[Tuple[AlertLevel, str]]:
        """检查阈值，返回告警列表"""
        alerts = []
        if self.is_critical:
            alerts.append((AlertLevel.CRITICAL, f"预算 '{self.name}' 已用 {self.usage_ratio:.1%} (${self.spent_usd:.2f}/${self.limit_usd:.2f})"))
        elif self.is_warning:
            alerts.append((AlertLevel.WARNING, f"预算 '{self.name}' 已用 {self.usage_ratio:.1%} (${self.spent_usd:.2f}/${self.limit_usd:.2f})"))
        return alerts


@dataclass
class CostRecord:
    """成本记录"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    user_id: str = ""
    model: str = ""
    provider: str = ""
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    # 优化信息
    optimization_applied: List[str] = field(default_factory=list)
    cached: bool = False
    # 元数据
    metadata: Dict = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        d["token_usage"] = self.token_usage.to_dict()
        return d


class CostTracker:
    """成本追踪器 - 核心追踪引擎"""
    
    def __init__(self, storage_dir: str = "data/cost"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        # 模型定价表
        self.pricing: Dict[str, ModelPricing] = dict(DEFAULT_MODEL_PRICING)
        
        # 预算管理
        self._budgets: Dict[str, Budget] = {}  # budget_id -> Budget
        self._budgets_by_scope: Dict[str, Set[str]] = defaultdict(set)  # scope_id -> budget_ids
        
        # 成本记录
        self._cost_records: List[CostRecord] = []
        self._cost_by_session: Dict[str, List[CostRecord]] = defaultdict(list)
        self._cost_by_user: Dict[str, List[CostRecord]] = defaultdict(list)
        
        # 缓存
        self._response_cache: Dict[str, Tuple[str, float]] = {}  # prompt_hash -> (response, cost)
        self._cache_ttl = 3600  # 1小时
        
        # 统计
        self._stats: Dict[str, float] = defaultdict(float)
        
        # 告警回调
        self._alert_handlers: List[Callable] = []
        
        # 锁
        self._lock = asyncio.Lock()
    
    def register_pricing(self, pricing: ModelPricing) -> None:
        """注册/更新模型定价"""
        key = f"{pricing.provider}:{pricing.model}"
        self.pricing[key] = pricing
        logger.info(f"Registered pricing: {key}")
    
    def get_pricing(self, provider: str, model: str) -> Optional[ModelPricing]:
        key = f"{provider}:{model}"
        return self.pricing.get(key)
    
    def calculate_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
    ) -> float:
        """计算成本"""
        pricing = self.get_pricing(provider, model)
        if not pricing:
            # 默认估算
            return (input_tokens + output_tokens) / 1000.0 * 0.001
        return pricing.calculate_cost(input_tokens, output_tokens, cache_tokens)
    
    async def track_usage(
        self,
        session_id: str,
        user_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
        request_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> CostRecord:
        """记录 Token 使用并计算成本"""
        cost = self.calculate_cost(provider, model, input_tokens, output_tokens, cache_tokens)
        
        token_usage = TokenUsage(
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_tokens=cache_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
            session_id=session_id,
            user_id=user_id,
            request_id=request_id or uuid.uuid4().hex[:16],
        )
        
        record = CostRecord(
            session_id=session_id,
            user_id=user_id,
            model=model,
            provider=provider,
            token_usage=token_usage,
            cost_usd=cost,
            metadata=metadata or {},
        )
        
        async with self._lock:
            self._cost_records.append(record)
            self._cost_by_session[session_id].append(record)
            self._cost_by_user[user_id].append(record)
            
            # 更新预算
            await self._update_budgets(user_id, cost)
            
            # 更新统计
            self._stats["total_cost_usd"] += cost
            self._stats["total_tokens"] += token_usage.total_tokens
            self._stats["total_requests"] += 1
            
            # 缓存响应
            if input_tokens > 0:
                prompt_hash = hashlib.sha256(f"{provider}:{model}:{request_id}".encode()).hexdigest()[:32]
                self._response_cache[prompt_hash] = (model, cost)
        
        return record
    
    async def _update_budgets(self, user_id: str, cost: float) -> None:
        """更新相关预算"""
        scope_ids = [user_id]  # 可以扩展为 project_id, tenant_id 等
        for scope_id in scope_ids:
            for budget_id in self._budgets_by_scope.get(scope_id, set()):
                budget = self._budgets.get(budget_id)
                if budget and budget.is_active:
                    budget.spent_usd += cost
                    budget.request_count += 1
                    budget.updated_at = time.time()
                    
                    # 检查阈值
                    for alert_level, message in budget.check_thresholds():
                        await self._emit_alert(alert_level, message, budget.id)
    
    async def check_budget(self, user_id: str, estimated_cost: float = 0.0) -> Tuple[bool, List[str]]:
        """检查预算是否允许请求"""
        allowed = True
        messages = []
        
        for budget_id in self._budgets_by_scope.get(user_id, set()):
            budget = self._budgets.get(budget_id)
            if not budget or not budget.is_active:
                continue
            
            if budget.hard_limit and budget.spent_usd + estimated_cost > budget.limit_usd:
                allowed = False
                messages.append(f"预算 '{budget.name}' 已超限 (${budget.spent_usd:.2f}/${budget.limit_usd:.2f})")
            elif budget.is_critical:
                messages.append(f"预算 '{budget.name}' 接近上限 ({budget.usage_ratio:.1%})")
            elif budget.is_warning:
                messages.append(f"预算 '{budget.name}' 已用 {budget.usage_ratio:.1%}")
        
        return allowed, messages
    
    def create_budget(
        self,
        name: str,
        scope: str,
        scope_id: str,
        limit_usd: float,
        period: BudgetPeriod = BudgetPeriod.MONTHLY,
        **kwargs,
    ) -> Budget:
        """创建预算"""
        budget = Budget(
            name=name,
            scope=scope,
            scope_id=scope_id,
            limit_usd=limit_usd,
            period=period,
            **kwargs,
        )
        self._budgets[budget.id] = budget
        self._budgets_by_scope[scope_id].add(budget.id)
        return budget
    
    def get_budget(self, budget_id: str) -> Optional[Budget]:
        return self._budgets.get(budget_id)
    
    def get_budgets_by_scope(self, scope_id: str) -> List[Budget]:
        return [self._budgets[bid] for bid in self._budgets_by_scope.get(scope_id, set())]
    
    async def get_cache(self, prompt_hash: str) -> Optional[Tuple[str, float]]:
        """获取缓存的响应和成本"""
        if prompt_hash in self._response_cache:
            response, cost = self._response_cache[prompt_hash]
            # 简单的 TTL 检查 (实际应用中需要存储时间戳)
            return response, cost
        return None
    
    async def set_cache(self, prompt_hash: str, response: str, cost: float) -> None:
        """设置响应缓存"""
        self._response_cache[prompt_hash] = (response, cost)
    
    # 优化策略
    def estimate_cost_savings(
        self,
        original_model: str,
        cheaper_model: str,
        estimated_tokens: int,
    ) -> float:
        """估算模型切换的成本节省"""
        orig_pricing = self.get_pricing(*original_model.split(":", 1))
        cheap_pricing = self.get_pricing(*cheaper_model.split(":", 1))
        
        if not orig_pricing or not cheap_pricing:
            return 0.0
        
        orig_cost = orig_pricing.calculate_cost(estimated_tokens // 2, estimated_tokens // 2)
        cheap_cost = cheap_pricing.calculate_cost(estimated_tokens // 2, estimated_tokens // 2)
        
        return max(0.0, orig_cost - cheap_cost)
    
    def recommend_cheaper_model(
        self,
        current_model: str,
        required_capabilities: List[str],
    ) -> Optional[str]:
        """推荐更便宜的替代模型"""
        # 解析 provider:model 格式
        parts = current_model.split(":", 1)
        if len(parts) != 2:
            return None
        provider, model = parts
        
        current = self.get_pricing(provider, model)
        if not current:
            return None
        
        candidates = []
        for key, pricing in self.pricing.items():
            if pricing.provider == current.provider and pricing.model != current.model:
                # 检查能力匹配
                if pricing.supports_tools or "tools" not in required_capabilities:
                    if pricing.max_context >= current.max_context:
                        candidates.append((pricing, key))
        
        if not candidates:
            return None
        
        # 按价格排序，选择最便宜的
        candidates.sort(key=lambda x: x[0].input_price_per_1k + x[0].output_price_per_1k)
        return candidates[0][1]
    
    # 缓存管理
    def clear_cache(self) -> int:
        count = len(self._response_cache)
        self._response_cache.clear()
        return count
    
    def get_cache_stats(self) -> Dict:
        return {
            "entries": len(self._response_cache),
            "estimated_size_mb": sum(len(v[0]) for v in self._response_cache.values()) / 1024 / 1024,
        }
    
    # 统计与报告
    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "cache_entries": len(self._response_cache),
            "total_budgets": len(self._budgets),
            "active_budgets": sum(1 for b in self._budgets.values() if b.is_active),
        }
    
    def get_session_cost(self, session_id: str) -> Dict:
        records = self._cost_by_session.get(session_id, [])
        total_cost = sum(r.cost_usd for r in records)
        total_tokens = sum(r.token_usage.total_tokens for r in records)
        by_model = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "count": 0})
        
        for r in records:
            by_model[r.model]["cost"] += r.cost_usd
            by_model[r.model]["tokens"] += r.token_usage.total_tokens
            by_model[r.model]["count"] += 1
        
        return {
            "session_id": session_id,
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "request_count": len(records),
            "by_model": dict(by_model),
        }
    
    def get_user_cost(self, user_id: str, days: int = 30) -> Dict:
        cutoff = time.time() - days * 86400
        records = [r for r in self._cost_by_user.get(user_id, []) if r.timestamp > cutoff]
        
        total_cost = sum(r.cost_usd for r in records)
        total_tokens = sum(r.token_usage.total_tokens for r in records)
        
        by_model = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "count": 0})
        by_day = defaultdict(float)
        
        for r in records:
            by_model[r.model]["cost"] += r.cost_usd
            by_model[r.model]["tokens"] += r.token_usage.total_tokens
            by_model[r.model]["count"] += 1
            
            day = datetime.fromtimestamp(r.timestamp).strftime("%Y-%m-%d")
            by_day[day] += r.cost_usd
        
        return {
            "user_id": user_id,
            "period_days": days,
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "request_count": len(records),
            "by_model": dict(by_model),
            "daily_costs": dict(sorted(by_day.items())),
        }
    
    # 告警系统
    def on_alert(self, handler: Callable) -> None:
        self._alert_handlers.append(handler)
    
    async def _emit_alert(self, level: AlertLevel, message: str, budget_id: str) -> None:
        alert_data = {
            "level": level.value,
            "message": message,
            "budget_id": budget_id,
            "timestamp": time.time(),
        }
        for handler in self._alert_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(alert_data)
                else:
                    handler(alert_data)
            except Exception as e:
                logger.error(f"Alert handler error: {e}")
    
    # 成本优化建议
    def generate_optimization_report(self, user_id: str, days: int = 30) -> Dict:
        """生成成本优化建议报告"""
        user_cost = self.get_user_cost(user_id, days)
        
        suggestions = []
        
        # 1. 检查是否有昂贵模型可以替换
        for model, stats in user_cost.get("by_model", {}).items():
            if stats["cost"] > user_cost["total_cost"] * 0.3:  # 占比超过 30%
                cheaper = self.recommend_cheaper_model(model, [])
                if cheaper and cheaper != model:
                    savings = self.estimate_cost_savings(model, cheaper, stats["tokens"])
                    if savings > 0.01:  # 节省超过 1 美分
                        suggestions.append({
                            "type": "model_switch",
                            "current_model": model,
                            "recommended_model": cheaper,
                            "estimated_savings_usd": savings,
                            "reason": f"模型 {model} 占成本 {stats['cost']/user_cost['total_cost']:.1%}，可切换到更便宜的 {cheaper}",
                        })
        
        # 2. 检查缓存利用率
        cache_stats = self.get_cache_stats()
        if cache_stats["entries"] == 0:
            suggestions.append({
                "type": "enable_cache",
                "reason": "未启用响应缓存，启用后可节省重复查询成本",
                "estimated_savings": "取决于重复查询频率",
            })
        
        # 3. 检查是否有失败重试导致的额外成本
        # (需要更详细的记录)
        
        return {
            "user_id": user_id,
            "period_days": days,
            "total_cost": user_cost["total_cost"],
            "total_tokens": user_cost["total_tokens"],
            "suggestions": suggestions,
            "potential_monthly_savings": sum(s.get("estimated_savings_usd", 0) for s in suggestions) * 30 / days,
        }


class CostOptimizer:
    """成本优化器 - 自动应用优化策略"""
    
    def __init__(self, tracker: CostTracker):
        self.tracker = tracker
        self._rules: List[Callable] = []
        self._enabled = True
    
    def add_rule(self, rule: Callable) -> None:
        self._rules.append(rule)
    
    async def apply_optimizations(
        self,
        provider: str,
        model: str,
        input_text: str,
        context: Dict,
    ) -> Tuple[str, str, Dict]:
        """应用优化规则，返回优化后的模型和参数"""
        if not self._enabled:
            return provider, model, {}
        
        optimized_provider = provider
        optimized_model = model
        optimization_info = {}
        
        for rule in self._rules:
            try:
                result = await rule(provider, model, input_text, context)
                if result:
                    optimized_provider, optimized_model, info = result
                    optimization_info.update(info)
            except Exception as e:
                logger.warning(f"Optimization rule failed: {e}")
        
        return optimized_provider, optimized_model, optimization_info
    
    def enable(self) -> None:
        self._enabled = True
    
    def disable(self) -> None:
        self._enabled = False


# ===== 内置优化规则 =====

async def rule_prefer_cheaper_model(
    provider: str,
    model: str,
    input_text: str,
    context: Dict,
) -> Optional[Tuple[str, str, Dict]]:
    """优先使用更便宜的模型 (如果任务不需要高能力)"""
    # 简单任务检测
    simple_indicators = ["翻译", "总结", "摘要", "格式化", "提取"]
    complex_indicators = ["代码", "推理", "分析", "架构", "设计", "调试", "重构"]
    
    is_simple = any(ind in input_text for ind in simple_indicators)
    is_complex = any(ind in input_text for ind in complex_indicators)
    
    if is_simple and not is_complex:
        # 简单任务尝试使用更便宜的模型
        tracker = get_cost_tracker()
        cheaper = tracker.recommend_cheaper_model(model, [])
        if cheaper and cheaper != model:
            return provider, cheaper, {"optimization": "simple_task_cheaper_model", "original_model": model}
    
    return None


async def rule_enable_cache_for_repetitive(
    provider: str,
    model: str,
    input_text: str,
    context: Dict,
) -> Optional[Tuple[str, str, Dict]]:
    """重复任务启用缓存"""
    # 检查是否有相似的历史请求
    tracker = get_cost_tracker()
    cache_stats = tracker.get_cache_stats()
    
    if cache_stats["entries"] > 100:
        # 有大量缓存，提示使用缓存
        return provider, model, {"optimization": "cache_hint", "cache_size": cache_stats["entries"]}
    
    return None


async def rule_batch_requests(
    provider: str,
    model: str,
    input_text: str,
    context: Dict,
) -> Optional[Tuple[str, str, Dict]]:
    """批量请求优化"""
    # 如果上下文中有批量标记
    if context.get("batch_mode"):
        return provider, model, {"optimization": "batch_mode", "batch_size": context.get("batch_size", 10)}
    
    return None


# 全局成本追踪器
_global_cost_tracker: Optional[CostTracker] = None
_tracker_lock = asyncio.Lock()


async def get_cost_tracker(storage_dir: str = "data/cost") -> CostTracker:
    global _global_cost_tracker
    if _global_cost_tracker is None:
        async with _tracker_lock:
            if _global_cost_tracker is None:
                _global_cost_tracker = CostTracker(storage_dir)
    return _global_cost_tracker


# ===== 便捷函数 =====

async def track_llm_usage(
    session_id: str,
    user_id: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int = 0,
    **kwargs,
) -> CostRecord:
    """追踪 LLM 使用"""
    tracker = await get_cost_tracker()
    return await tracker.track_usage(
        session_id=session_id,
        user_id=user_id,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        **kwargs,
    )


async def check_budget(user_id: str, estimated_cost: float = 0.0) -> Tuple[bool, List[str]]:
    tracker = await get_cost_tracker()
    return await tracker.check_budget(user_id, estimated_cost)


async def create_budget(
    name: str,
    user_id: str,
    limit_usd: float,
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    **kwargs,
) -> Budget:
    tracker = await get_cost_tracker()
    return tracker.create_budget(
        name=name,
        scope="user",
        scope_id=user_id,
        limit_usd=limit_usd,
        period=period,
        **kwargs,
    )


async def get_cost_report(user_id: str, days: int = 30) -> Dict:
    tracker = await get_cost_tracker()
    return tracker.get_user_cost(user_id, days)


async def get_optimization_suggestions(user_id: str, days: int = 30) -> Dict:
    tracker = await get_cost_tracker()
    return tracker.generate_optimization_report(user_id, days)


# ===== 内置优化器实例 =====

_global_optimizer: Optional[CostOptimizer] = None


async def get_cost_optimizer() -> CostOptimizer:
    global _global_optimizer
    if _global_optimizer is None:
        tracker = await get_cost_tracker()
        _global_optimizer = CostOptimizer(tracker)
        # 注册默认规则
        _global_optimizer.add_rule(rule_prefer_cheaper_model)
        _global_optimizer.add_rule(rule_enable_cache_for_repetitive)
        _global_optimizer.add_rule(rule_batch_requests)
    return _global_optimizer


# 导出
__all__ = [
    "BudgetPeriod",
    "AlertLevel",
    "ModelPricing",
    "DEFAULT_MODEL_PRICING",
    "TokenUsage",
    "Budget",
    "CostRecord",
    "CostTracker",
    "CostOptimizer",
    "Budget",
    "get_cost_tracker",
    "get_cost_optimizer",
    "track_llm_usage",
    "check_budget",
    "create_budget",
    "get_cost_report",
    "get_optimization_suggestions",
    "rule_prefer_cheaper_model",
    "rule_enable_cache_for_repetitive",
    "rule_batch_requests",
]