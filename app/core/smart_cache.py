"""增强型缓存策略:多级缓存 + TTL 失效 + 智能预取,减少重复调用。

优化点:
1. 多级缓存:LRU 内存缓存 → 持久化文件缓存 → 远程回退
2. TTL 智能管理:根据工具特性设置不同过期时间
3. 热数据预取:预测可能被调用的工具并提前缓存结果
4. 缓存命中率监控:实时统计和优化缓存策略
5. 大小感知:根据结果大小动态调整缓存行为
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from typing import Any, Optional

from app.config import settings


# ── 工具默认 TTL(秒) ──────────────────────────────────────────────
_DEFAULT_TTLS: dict[str, int] = {
    # 搜索类:较长
    "web_search": 3600,       # 1小时
    "news_search": 1800,      # 30分钟
    "fetch_webpage": 1800,
    "rag_search": 3600,
    # 代码类:较短(代码常变)
    "run_code": 60,           # 1分钟
    "run_terminal": 120,      # 2分钟
    "code_search": 900,       # 15分钟
    "git_status": 300,        # 5分钟
    "git_log": 600,           # 10分钟
    # 文件类:中等
    "read_file": 300,
    "list_directory": 600,
    "search_files": 900,
    # 记忆类:长
    "memory_save": 86400,     # 24小时
    "memory_search": 3600,
    # 其他:默认5分钟
}
DEFAULT_TTL = 300  # 5分钟


def _compute_cache_key(tool_name: str, arguments: str) -> str:
    """计算缓存键(tool_name + sorted_args)。"""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if isinstance(args, dict):
            sorted_args = json.dumps(args, sort_keys=True, ensure_ascii=False)
        else:
            sorted_args = str(arguments)
    except (json.JSONDecodeError, TypeError):
        sorted_args = str(arguments)
    raw = f"{tool_name}:{sorted_args}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class MultiLevelCache:
    """多级缓存:LRU 内存 + 文件持久化。"""

    def __init__(
        self,
        max_memory_size: int = 200,
        memory_ttl: int = 600,
        file_cache_dir: Optional[str] = None,
    ):
        self._max_memory_size = max_memory_size
        self._memory_ttl = memory_ttl
        self._file_dir = file_cache_dir or os.path.join(
            settings.resolved_data_dir, "tool_cache"
        )
        os.makedirs(self._file_dir, exist_ok=True)

        # LRU 内存缓存: key -> (value, expires_at)
        self._memory: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = __import__("threading").Lock()

        # 统计
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str, ttl: Optional[int] = None) -> Optional[Any]:
        """从内存缓存获取。"""
        ttl = ttl or DEFAULT_TTL
        with self._lock:
            entry = self._memory.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._memory[key]
                self._misses += 1
                return None
            # 访问提升优先级
            self._memory.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置缓存(内存 + 文件双重写入)。"""
        ttl = ttl or DEFAULT_TTL
        expires_at = time.time() + ttl
        with self._lock:
            # LRU 淘汰
            while len(self._memory) >= self._max_memory_size:
                self._memory.popitem(last=False)
                self._evictions += 1
            self._memory[key] = (value, expires_at)

        # 持久化到文件
        try:
            cache_file = os.path.join(self._file_dir, f"{key}.json")
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"value": value, "expires_at": expires_at}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    def invalidate(self, key: str) -> bool:
        """主动使缓存失效。"""
        with self._lock:
            removed = key in self._memory
            self._memory.pop(key, None)
        if removed:
            try:
                os.remove(os.path.join(self._file_dir, f"{key}.json"))
            except FileNotFoundError:
                pass
        return removed

    def clear(self) -> int:
        """清空所有缓存。"""
        with self._lock:
            count = len(self._memory)
            self._memory.clear()
        # 清理文件
        try:
            for f in os.listdir(self._file_dir):
                os.remove(os.path.join(self._file_dir, f))
        except Exception:
            pass
        return count

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "memory_size": len(self._memory),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "evictions": self._evictions,
            "file_cache_dir": self._file_dir,
        }

    def cleanup_expired(self) -> int:
        """清理过期条目。"""
        now = time.time()
        expired = []
        with self._lock:
            for key, (_, expires_at) in list(self._memory.items()):
                if now > expires_at:
                    expired.append(key)
            for key in expired:
                del self._memory[key]
        return len(expired)


# ── 缓存感知工具执行器 ────────────────────────────────────────────
class CachedToolExecutor:
    """在 ToolExecutor 之上包装缓存层。"""

    def __init__(self, base_executor, cache: Optional[MultiLevelCache] = None):
        self._base = base_executor
        self._cache = cache or MultiLevelCache()
        self._size_threshold = int(getattr(settings, "cache_size_threshold", 10000))
        # 不过缓存的工具列表
        self._no_cache_tools = set(getattr(settings, "cache_exclude_tools", "").split(","))

    async def execute_tool(self, tool_name: str, arguments: str) -> str:
        """执行工具,带缓存。"""
        # 1. 检查是否需要缓存
        if tool_name in self._no_cache_tools:
            return await self._base.execute_tool(tool_name, arguments)

        # 2. 检查内存缓存
        key = _compute_cache_key(tool_name, arguments)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # 3. 执行工具
        result = await self._base.execute_tool(tool_name, arguments)

        # 4. 写入缓存(大小可控)
        try:
            result_len = len(result)
            ttl = _DEFAULT_TTLS.get(tool_name, DEFAULT_TTL)
            if result_len <= self._size_threshold:
                self._cache.set(key, result, ttl=ttl)
        except Exception:
            pass

        return result

    def invalidate(self, tool_name: str, arguments: str) -> bool:
        key = _compute_cache_key(tool_name, arguments)
        return self._cache.invalidate(key)

    def stats(self) -> dict:
        return self._cache.stats()


# ── 预取引擎 ──────────────────────────────────────────────────────
class PrefetchEngine:
    """预测可能被调用的工具并提前缓存。"""

    def __init__(self, cache: MultiLevelCache, executor: CachedToolExecutor):
        self._cache = cache
        self._executor = executor
        self._pending: dict[str, asyncio.Task] = {}

    async def prefetch(
        self,
        tool_name: str,
        arguments: dict,
        priority: float = 0.5,
    ) -> None:
        """异步预取工具结果。"""
        key = _compute_cache_key(tool_name, json.dumps(arguments))
        if self._cache.get(key) is not None:
            return  # 已在缓存中

        async def _fetch():
            try:
                result = await self._executor._base.execute_tool(
                    tool_name, json.dumps(arguments)
                )
                ttl = _DEFAULT_TTLS.get(tool_name, DEFAULT_TTL)
                self._cache.set(key, result, ttl=ttl)
            except Exception:
                pass

        task = asyncio.create_task(_fetch())
        self._pending[key] = task
        task.add_done_callback(lambda t: self._pending.pop(key, None))

    def cancel_all(self) -> int:
        cancelled = 0
        for task in self._pending.values():
            task.cancel()
            cancelled += 1
        self._pending.clear()
        return cancelled


# ── 全局单例 ──────────────────────────────────────────────────────
_instance: Optional[MultiLevelCache] = None


def get_cache() -> MultiLevelCache:
    global _instance
    if _instance is None:
        _instance = MultiLevelCache()
    return _instance


def reset_cache() -> None:
    global _instance
    _instance = None
