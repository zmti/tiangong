"""Token预算 + 模型分级路由 + 语义缓存系统。

核心能力:
1. Token预算会计: 请求级/会话级/任务级预算控制,超预算自动降级/拦截
2. 模型分级路由: simple/standard/complex 三级模型池,按任务复杂度自动分流
3. 语义缓存: 基于嵌入相似度(>0.95)的工具结果/LLM回答缓存,跨请求复用
4. 成本实时回显: Token/美元/延迟实时统计,注入上下文供模型感知
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ModelTier:
    """模型分级配置。"""
    name: str                    # tier 名称: simple / standard / complex
    provider: str                # provider 名称
    model: str                   # 模型名称
    max_tokens: int = 0          # 单次最大输出 token (0=不限制)
    cost_per_1k_input: float = 0.0   # 美元/1k input tokens
    cost_per_1k_output: float = 0.0  # 美元/1k output tokens
    latency_p50_ms: int = 5000   # 预估 P50 延迟(ms)
    capabilities: list[str] = field(default_factory=list)  # 支持能力标签


@dataclass
class TokenBudget:
    """Token 预算配置。"""
    request_limit: int = 0           # 单请求上限 (0=不限)
    session_limit: int = 0           # 会话累计上限 (0=不限)
    task_limit: int = 0              # 任务累计上限 (0=不限)
    warn_threshold: float = 0.8      # 告警阈值 (0-1)
    hard_stop: bool = True           # 超预算是否硬拦截


@dataclass
class UsageSnapshot:
    """使用量快照。"""
    request_id: str
    session_id: str
    task_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model_tier: str = ""
    provider: str = ""
    model: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class CacheEntry:
    """语义缓存条目。"""
    key: str                       # 语义键(查询嵌入哈希)
    query_embedding: list[float]   # 查询向量
    response: str                  # 缓存响应
    response_embedding: list[float] = field(default_factory=list)  # 响应向量
    tool_name: str = ""            # 关联工具名(工具缓存用)
    tool_args: str = ""            # 关联工具参数
    hit_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


# ============================================================
# 模型分级路由器
# ============================================================

class ModelRouter:
    """模型分级路由器:按任务复杂度选择合适模型。"""

    DEFAULT_TIERS = {
        "simple": ModelTier(
            name="simple",
            provider="deepseek",
            model="deepseek-chat",
            max_tokens=4096,
            cost_per_1k_input=0.00014,
            cost_per_1k_output=0.00028,
            latency_p50_ms=2000,
            capabilities=["chat", "simple_qa", "translation"],
        ),
        "standard": ModelTier(
            name="standard",
            provider="deepseek",
            model="deepseek-chat",
            max_tokens=8192,
            cost_per_1k_input=0.00014,
            cost_per_1k_output=0.00028,
            latency_p50_ms=4000,
            capabilities=["chat", "code", "analysis", "reasoning", "tool_use"],
        ),
        "complex": ModelTier(
            name="complex",
            provider="deepseek",
            model="deepseek-reasoner",
            max_tokens=16384,
            cost_per_1k_input=0.00055,
            cost_per_1k_output=0.00219,
            latency_p50_ms=15000,
            capabilities=["chat", "code", "analysis", "reasoning", "tool_use", "planning", "complex_reasoning"],
        ),
    }

    def __init__(self):
        self.tiers: dict[str, ModelTier] = {}
        self._load_config()
        self._complexity_classifier = None

    def _load_config(self) -> None:
        """从配置加载模型分级。"""
        # 从 settings 读取自定义分级
        custom = getattr(settings, "agent_model_tiers", {})
        if custom:
            for name, cfg in custom.items():
                self.tiers[name] = ModelTier(**cfg)
        else:
            self.tiers = self.DEFAULT_TIERS.copy()

        # 允许环境变量覆盖
        for tier_name in self.tiers:
            env_prefix = f"AGENT_MODEL_TIER_{tier_name.upper()}_"
            provider = os.environ.get(f"{env_prefix}PROVIDER")
            model = os.environ.get(f"{env_prefix}MODEL")
            if provider:
                self.tiers[tier_name].provider = provider
            if model:
                self.tiers[tier_name].model = model

    def select_tier(self, task_complexity: str, required_capabilities: list[str] = None) -> ModelTier:
        """选择模型分级。
        
        Args:
            task_complexity: simple / standard / complex
            required_capabilities: 必需能力标签
        """
        # 优先满足能力要求
        if required_capabilities:
            for tier_name in ["complex", "standard", "simple"]:
                tier = self.tiers.get(tier_name)
                if tier and all(cap in tier.capabilities for cap in required_capabilities):
                    return tier

        # 回退到复杂度匹配
        tier = self.tiers.get(task_complexity)
        if tier:
            return tier

        # 兜底:返回 standard
        return self.tiers.get("standard", list(self.tiers.values())[0])

    def estimate_cost(self, tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
        """估算成本(美元)。"""
        return (input_tokens / 1000) * tier.cost_per_1k_input + (output_tokens / 1000) * tier.cost_per_1k_output


# ============================================================
# Token 预算管理器
# ============================================================

class BudgetManager:
    """Token 预算管理器:请求/会话/任务三级预算控制。"""

    def __init__(self, db_path: Optional[str] = None, backend: Optional[StorageBackend] = None):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "token_budget.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._init_db()
        self._budget = TokenBudget(
            request_limit=getattr(settings, "agent_token_budget_request", 0),
            session_limit=getattr(settings, "agent_token_budget_session", 0),
            task_limit=getattr(settings, "agent_token_budget_task", 0),
            warn_threshold=getattr(settings, "agent_token_budget_warn_threshold", 0.8),
            hard_stop=getattr(settings, "agent_token_budget_hard_stop", True),
        )
        self._session_usage: dict[str, int] = {}
        self._task_usage: dict[str, int] = {}

    def _init_db(self) -> None:
        schema = {
            "token_usage": [
                {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "request_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "session_id", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "task_id", "type": "TEXT", "constraints": ""},
                {"name": "input_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "output_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "total_tokens", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "cost_usd", "type": "REAL", "constraints": "DEFAULT 0"},
                {"name": "latency_ms", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "model_tier", "type": "TEXT", "constraints": ""},
                {"name": "provider", "type": "TEXT", "constraints": ""},
                {"name": "model", "type": "TEXT", "constraints": ""},
                {"name": "timestamp", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "metadata", "type": "TEXT", "constraints": ""},
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_usage_session ON token_usage(session_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_usage_task ON token_usage(task_id)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_usage_time ON token_usage(timestamp)")

    def check_budget(self, session_id: str, task_id: str = "", estimated_tokens: int = 0) -> tuple[bool, str]:
        """检查预算是否允许执行。
        
        Returns:
            (allowed, reason)
        """
        with self._lock:
            # 请求级预算
            if self._budget.request_limit > 0 and estimated_tokens > self._budget.request_limit:
                return False, f"单请求预估 {estimated_tokens} tokens 超过限制 {self._budget.request_limit}"

            # 会话级预算
            if self._budget.session_limit > 0:
                session_used = self._get_session_usage(session_id)
                if session_used + estimated_tokens > self._budget.session_limit:
                    if self._budget.hard_stop:
                        return False, f"会话累计 {session_used} + 预估 {estimated_tokens} 超过限制 {self._budget.session_limit}"
                    # 软限制:仅告警
                    if session_used > self._budget.session_limit * self._budget.warn_threshold:
                        logger.warning("会话 %s token 使用接近上限: %d/%d", session_id[:8], session_used, self._budget.session_limit)

            # 任务级预算
            if task_id and self._budget.task_limit > 0:
                task_used = self._get_task_usage(task_id)
                if task_used + estimated_tokens > self._budget.task_limit:
                    if self._budget.hard_stop:
                        return False, f"任务累计 {task_used} + 预估 {estimated_tokens} 超过限制 {self._budget.task_limit}"
                    if task_used > self._budget.task_limit * self._budget.warn_threshold:
                        logger.warning("任务 %s token 使用接近上限: %d/%d", task_id[:8], task_used, self._budget.task_limit)

            return True, "ok"

    def record_usage(self, usage: UsageSnapshot) -> None:
        """记录使用量。"""
        with self._lock:
            self._backend.insert(
                "token_usage",
                {
                    "id": usage.request_id,
                    "request_id": usage.request_id,
                    "session_id": usage.session_id,
                    "task_id": usage.task_id,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                    "cost_usd": usage.cost_usd,
                    "latency_ms": usage.latency_ms,
                    "model_tier": usage.model_tier,
                    "provider": usage.provider,
                    "model": usage.model,
                    "timestamp": usage.timestamp,
                    "metadata": json.dumps(usage.metadata, ensure_ascii=False),
                },
            )
            # 更新内存缓存
            self._session_usage[usage.session_id] = self._session_usage.get(usage.session_id, 0) + usage.total_tokens
            if usage.task_id:
                self._task_usage[usage.task_id] = self._task_usage.get(usage.task_id, 0) + usage.total_tokens

    def _get_session_usage(self, session_id: str) -> int:
        if session_id in self._session_usage:
            return self._session_usage[session_id]
        rows = self._backend.select(
            "token_usage",
            "session_id = ?",
            (session_id,),
        )
        total = sum(r["total_tokens"] for r in rows)
        self._session_usage[session_id] = total
        return total

    def _get_task_usage(self, task_id: str) -> int:
        if task_id in self._task_usage:
            return self._task_usage[task_id]
        rows = self._backend.select(
            "token_usage",
            "task_id = ?",
            (task_id,),
        )
        total = sum(r["total_tokens"] for r in rows)
        self._task_usage[task_id] = total
        return total

    def get_session_summary(self, session_id: str) -> dict:
        """获取会话用量摘要。"""
        with self._lock:
            rows = self._backend.select(
                "token_usage",
                "session_id = ?",
                (session_id,),
                order_by="timestamp DESC",
                limit=100,
            )
        total_tokens = sum(r["total_tokens"] for r in rows)
        total_cost = sum(r["cost_usd"] for r in rows)
        by_tier = {}
        for r in rows:
            tier = r["model_tier"] or "unknown"
            by_tier[tier] = by_tier.get(tier, 0) + r["total_tokens"]
        return {
            "session_id": session_id,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost,
            "request_count": len(rows),
            "by_tier": by_tier,
            "budget_limit": self._budget.session_limit,
            "budget_used_pct": (total_tokens / self._budget.session_limit * 100) if self._budget.session_limit > 0 else 0,
        }

    def get_task_summary(self, task_id: str) -> dict:
        """获取任务用量摘要。"""
        with self._lock:
            rows = self._backend.select(
                "token_usage",
                "task_id = ?",
                (task_id,),
                order_by="timestamp DESC",
            )
        total_tokens = sum(r["total_tokens"] for r in rows)
        total_cost = sum(r["cost_usd"] for r in rows)
        return {
            "task_id": task_id,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost,
            "request_count": len(rows),
            "budget_limit": self._budget.task_limit,
            "budget_used_pct": (total_tokens / self._budget.task_limit * 100) if self._budget.task_limit > 0 else 0,
        }


# ============================================================
# 语义缓存
# ============================================================

class SemanticCache:
    """语义缓存:基于嵌入相似度的缓存系统。"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        backend: Optional[StorageBackend] = None,
        similarity_threshold: float = 0.95,
        max_entries: int = 10000,
    ):
        if backend:
            self._backend = backend
            self._db_path = None
        else:
            self._db_path = db_path or os.path.join(settings.resolved_data_dir, "semantic_cache.db")
            self._backend = get_storage_backend(db_path=self._db_path)
        self._lock = threading.RLock()
        self._similarity_threshold = similarity_threshold
        self._max_entries = max_entries
        self._init_db()
        self._embedder = None
        self._embedder_loaded = False

    def _init_db(self) -> None:
        schema = {
            "semantic_cache": [
                {"name": "key", "type": "TEXT", "constraints": "PRIMARY KEY"},
                {"name": "query_embedding", "type": "BLOB", "constraints": "NOT NULL"},
                {"name": "response", "type": "TEXT", "constraints": "NOT NULL"},
                {"name": "response_embedding", "type": "BLOB", "constraints": ""},
                {"name": "tool_name", "type": "TEXT", "constraints": ""},
                {"name": "tool_args", "type": "TEXT", "constraints": ""},
                {"name": "hit_count", "type": "INTEGER", "constraints": "DEFAULT 0"},
                {"name": "created_at", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "last_accessed", "type": "REAL", "constraints": "NOT NULL"},
                {"name": "metadata", "type": "TEXT", "constraints": ""},
            ],
        }
        self._backend.init_db(schema)
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_cache_tool ON semantic_cache(tool_name)")
        self._backend.execute("CREATE INDEX IF NOT EXISTS idx_cache_time ON semantic_cache(created_at)")

    def _get_embedder(self):
        if self._embedder_loaded:
            return self._embedder
        try:
            from app.rag.embeddings import get_embedder
            self._embedder = get_embedder()
            self._embedder_loaded = True
        except Exception as e:
            logger.warning("嵌入模型加载失败,语义缓存降级为精确匹配: %s", e)
            self._embedder = None
            self._embedder_loaded = True
        return self._embedder

    def _embed(self, text: str) -> Optional[list[float]]:
        embedder = self._get_embedder()
        if embedder is None:
            return None
        try:
            from app.rag.embeddings import run_embed
            vec = run_embed(embedder, [text])
            return vec[0] if vec else None
        except Exception:
            return None

    def _vector_to_blob(self, vec: list[float]) -> bytes:
        import struct
        return struct.pack(f"{len(vec)}f", *vec)

    def _blob_to_vector(self, blob: bytes) -> list[float]:
        import struct
        return list(struct.unpack(f"{len(blob)//4}f", blob))

    def _make_key(self, text: str, tool_name: str = "", tool_args: str = "") -> str:
        """生成缓存键:查询文本+工具名+参数的哈希。"""
        content = f"{tool_name}|{tool_args}|{text}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def get(self, query: str, tool_name: str = "", tool_args: str = "") -> Optional[str]:
        """获取缓存:精确键匹配 -> 语义相似度匹配。"""
        key = self._make_key(query, tool_name, tool_args)

        with self._lock:
            # 1) 精确键匹配
            row = self._backend.select_one("semantic_cache", "key = ?", (key,))
            if row:
                self._backend.execute(
                    "UPDATE semantic_cache SET hit_count = hit_count + 1, last_accessed = ? WHERE key = ?",
                    (time.time(), key),
                )
                return row["response"]

            # 2) 语义相似度匹配(仅当有嵌入模型时)
            if not tool_name:  # 工具缓存通常用精确匹配,LLM 回答用语义匹配
                q_vec = self._embed(query)
                if q_vec:
                    rows = self._backend.select("semantic_cache", "1=1", limit=500)
                    from app.rag.store import cosine_similarity
                    best_score = 0.0
                    best_response = None
                    best_key = None
                    for row in rows:
                        if row["query_embedding"]:
                            stored_vec = self._blob_to_vector(row["query_embedding"])
                            score = cosine_similarity(q_vec, stored_vec)
                            if score > best_score and score >= self._similarity_threshold:
                                best_score = score
                                best_response = row["response"]
                                best_key = row["key"]
                    if best_response:
                        self._backend.execute(
                            "UPDATE semantic_cache SET hit_count = hit_count + 1, last_accessed = ? WHERE key = ?",
                            (time.time(), best_key),
                        )
                        logger.debug("语义缓存命中: similarity=%.4f", best_score)
                        return best_response
        return None

    def set(self, query: str, response: str, tool_name: str = "", tool_args: str = "", metadata: dict = None) -> None:
        """存入缓存。"""
        key = self._make_key(query, tool_name, tool_args)
        q_vec = self._embed(query)
        r_vec = self._embed(response) if not tool_name else []

        with self._lock:
            # 检查容量,LRU 淘汰
            count = self._backend.count("semantic_cache")
            if count >= self._max_entries:
                # 删除最久未访问的 10%
                delete_count = max(1, self._max_entries // 10)
                old_rows = self._backend.select(
                    "semantic_cache", "1=1", order_by="last_accessed ASC", limit=delete_count
                )
                for row in old_rows:
                    self._backend.delete("semantic_cache", "key = ?", (row["key"],))

            q_blob = self._vector_to_blob(q_vec) if q_vec else b""
            r_blob = self._vector_to_blob(r_vec) if r_vec else b""

            self._backend.upsert(
                "semantic_cache",
                {
                    "key": key,
                    "query_embedding": q_blob,
                    "response": response,
                    "response_embedding": r_blob,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "hit_count": 0,
                    "created_at": time.time(),
                    "last_accessed": time.time(),
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                },
                conflict_columns=["key"],
            )

    def clear(self, tool_name: str = None) -> int:
        """清理缓存。"""
        with self._lock:
            if tool_name:
                return self._backend.delete("semantic_cache", "tool_name = ?", (tool_name,))
            else:
                return self._backend.delete("semantic_cache", "1=1")

    def stats(self) -> dict:
        with self._lock:
            total = self._backend.count("semantic_cache")
            rows = self._backend.select("semantic_cache", "1=1", order_by="hit_count DESC", limit=10)
            top = [{"key": r["key"][:8], "hits": r["hit_count"], "tool": r["tool_name"]} for r in rows]
            return {"total_entries": total, "top_hits": top}


# ============================================================
# 统一成本控制器 (对外主入口)
# ============================================================

class CostController:
    """统一成本控制器:整合路由、预算、缓存。"""

    def __init__(self):
        self.router = ModelRouter()
        self.budget = BudgetManager()
        self.cache = SemanticCache()
        self._request_context: dict = {}  # 当前请求上下文

    def begin_request(
        self,
        session_id: str,
        task_id: str = "",
        task_complexity: str = "standard",
        required_capabilities: list[str] = None,
    ) -> dict:
        """开始请求:选择模型、检查预算、返回上下文。"""
        tier = self.router.select_tier(task_complexity, required_capabilities)
        request_id = uuid.uuid4().hex

        # 预估 token (粗略:输入字符/3)
        estimated = 2000  # 默认预估

        allowed, reason = self.budget.check_budget(session_id, task_id, estimated)
        if not allowed:
            raise BudgetExceededError(reason)

        ctx = {
            "request_id": request_id,
            "session_id": session_id,
            "task_id": task_id,
            "tier": tier,
            "start_time": time.time(),
            "input_tokens": 0,
            "output_tokens": 0,
        }
        self._request_context = ctx
        return ctx

    def end_request(self, input_tokens: int, output_tokens: int, metadata: dict = None) -> UsageSnapshot:
        """结束请求:记录用量、计算成本。"""
        ctx = self._request_context
        if not ctx:
            return UsageSnapshot(request_id="", session_id="")

        latency_ms = int((time.time() - ctx["start_time"]) * 1000)
        tier = ctx["tier"]
        cost = self.router.estimate_cost(tier, input_tokens, output_tokens)

        usage = UsageSnapshot(
            request_id=ctx["request_id"],
            session_id=ctx["session_id"],
            task_id=ctx.get("task_id", ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            model_tier=tier.name,
            provider=tier.provider,
            model=tier.model,
            metadata=metadata or {},
        )
        self.budget.record_usage(usage)
        self._request_context = {}
        return usage

    def get_cached_response(self, query: str, tool_name: str = "", tool_args: str = "") -> Optional[str]:
        """获取语义缓存。"""
        return self.cache.get(query, tool_name, tool_args)

    def cache_response(self, query: str, response: str, tool_name: str = "", tool_args: str = "", metadata: dict = None) -> None:
        """存入语义缓存。"""
        self.cache.set(query, response, tool_name, tool_args, metadata)

    def build_cost_note(self, session_id: str, task_id: str = "") -> str:
        """构建成本提示注入上下文。"""
        session_summary = self.budget.get_session_summary(session_id)
        lines = [
            "## 成本预算实时状态",
            f"- 会话累计: {session_summary['total_tokens']:,} tokens, ${session_summary['total_cost_usd']:.4f}",
            f"- 预算限制: {session_summary['budget_limit']:,} tokens ({session_summary['budget_used_pct']:.1f}% 已用)",
        ]
        if task_id:
            task_summary = self.budget.get_task_summary(task_id)
            lines.append(f"- 任务累计: {task_summary['total_tokens']:,} tokens, ${task_summary['total_cost_usd']:.4f}")
        return "\n".join(lines)


class BudgetExceededError(Exception):
    """预算超限异常。"""
    pass


# 全局单例
_cost_controller: Optional[CostController] = None


def get_cost_controller() -> CostController:
    global _cost_controller
    if _cost_controller is None:
        _cost_controller = CostController()
    return _cost_controller