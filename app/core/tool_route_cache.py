"""工具路由缓存:基于查询特征缓存路由结果,避免重复 LLM 调用。

缓存策略:
1. 精确缓存:相同 query 直接返回缓存结果
2. 模糊缓存:基于 query 的 hash 匹配相似结果
3. TTL 过期:缓存结果 5 分钟后过期
4. 容量限制:最多缓存 1000 条记录

使用方法:
    from app.core.tool_route_cache import get_tool_route_cache
    cache = get_tool_route_cache()
    
    # 尝试从缓存获取
    cached = cache.get(query)
    if cached:
        return cached
    
    # 执行路由并缓存结果
    result = await select_tools_enhanced(...)
    cache.set(query, result)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RouteCacheEntry:
    """缓存的路由结果。"""
    query: str
    tools: list[str]
    confidence: float
    reason: str
    timestamp: float
    query_hash: str  # 用于模糊匹配


class ToolRouteCache:
    """工具路由缓存管理器。"""
    
    def __init__(self, max_size: int = 1000, ttl_seconds: float = 300.0):
        """初始化缓存。
        
        Args:
            max_size: 最大缓存条目数
            ttl_seconds: TTL 过期时间(秒),默认 5 分钟
        """
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._cache: dict[str, RouteCacheEntry] = {}
        self._lock = asyncio.Lock()
        self._stats = {"hits": 0, "misses": 0, "evictions": 0}
    
    def _compute_hash(self, query: str) -> str:
        """计算查询的 hash(用于模糊匹配)。"""
        # 简化 hash:取 query 的前 100 字符的 md5
        simplified = query[:100].strip().lower()
        return hashlib.md5(simplified.encode('utf-8')).hexdigest()[:16]
    
    async def get(self, query: str) -> Optional[RouteCacheEntry]:
        """尝试从缓存获取路由结果。
        
        Args:
            query: 用户查询文本
            
        Returns:
            缓存结果,如果未命中或已过期则返回 None
        """
        async with self._lock:
            # 1. 精确匹配
            if query in self._cache:
                entry = self._cache[query]
                if self._is_valid(entry):
                    self._stats["hits"] += 1
                    logger.debug("工具路由缓存命中(精确): %s", query[:50])
                    return entry
            
            # 2. 模糊匹配:基于 hash 查找相似查询
            query_hash = self._compute_hash(query)
            for entry in list(self._cache.values()):
                if entry.query_hash == query_hash and self._is_valid(entry):
                    self._stats["hits"] += 1
                    logger.debug("工具路由缓存命中(模糊): %s -> %s", query[:50], entry.query[:50])
                    return entry
            
            self._stats["misses"] += 1
            return None
    
    async def set(self, query: str, tools: list[str], confidence: float, reason: str) -> None:
        """缓存路由结果。
        
        Args:
            query: 用户查询
            tools: 选中的工具列表
            confidence: 置信度分数
            reason: 路由原因
        """
        async with self._lock:
            # 容量检查
            if len(self._cache) >= self._max_size:
                await self._evict_old()
            
            entry = RouteCacheEntry(
                query=query,
                tools=tools,
                confidence=confidence,
                reason=reason,
                timestamp=time.time(),
                query_hash=self._compute_hash(query),
            )
            self._cache[query] = entry
            logger.debug("工具路由缓存写入: %s (tools=%d)", query[:50], len(tools))
    
    def _is_valid(self, entry: RouteCacheEntry) -> bool:
        """检查缓存条目是否仍然有效。"""
        return time.time() - entry.timestamp < self._ttl
    
    async def _evict_old(self) -> None:
        """淘汰最旧的缓存条目。"""
        if not self._cache:
            return
        
        # 按时间排序，淘汰最旧的 10%
        sorted_entries = sorted(self._cache.values(), key=lambda e: e.timestamp)
        evict_count = max(1, len(sorted_entries) // 10)
        
        for entry in sorted_entries[:evict_count]:
            if entry.query in self._cache:
                del self._cache[entry.query]
        
        self._stats["evictions"] += evict_count
        logger.debug("工具路由缓存淘汰: %d 条", evict_count)
    
    def clear(self) -> None:
        """清空缓存。"""
        self._cache.clear()
        logger.info("工具路由缓存已清空")
    
    def get_stats(self) -> dict:
        """获取缓存统计信息。"""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total if total > 0 else 0.0
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "hit_rate": round(hit_rate, 3),
            "evictions": self._stats["evictions"],
        }


# 全局单例
_cache_instance: Optional[ToolRouteCache] = None
_cache_lock = asyncio.Lock()


def get_tool_route_cache() -> ToolRouteCache:
    """获取工具路由缓存单例。"""
    global _cache_instance
    # 简单的懒加载，实际生产环境建议使用更可靠的单例模式
    if _cache_instance is None:
        _cache_instance = ToolRouteCache()
    return _cache_instance


async def clear_tool_route_cache() -> None:
    """清空工具路由缓存(供管理接口使用)。"""
    global _cache_instance
    if _cache_instance is not None:
        _cache_instance.clear()
        _cache_instance = None
        logger.info("工具路由缓存已重置")
