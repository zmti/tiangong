"""记忆 TTL 自动过期机制。

解决记忆永久保留导致的问题:
1. 存储空间持续增长
2. 旧记忆干扰新检索
3. 语义漂移（过时的信息影响回答）
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from app.config import settings
from app.core.storage_backend import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)


class MemoryTTLManager:
    """记忆 TTL 管理器。

    功能:
    1. 定时清理过期记忆
    2. 自动衰减低频访问记忆的权重
    3. 记忆优先级评分（结合新鲜度 + 访问频率）
    """
    
    # 默认 TTL（天）
    DEFAULT_TTL_DAYS = 180
    # 衰减因子（每次清理时乘以）
    DECAY_FACTOR = 0.95
    # 最小访问间隔（秒）
    MIN_ACCESS_INTERVAL = 3600
    
    def __init__(self, backend: Optional[StorageBackend] = None):
        self._backend = backend or get_storage_backend()
        self._ttl_days = int(getattr(settings, "memory_ttl_days", self.DEFAULT_TTL_DAYS))
        self._decay_factor = float(getattr(settings, "memory_decay_factor", self.DECAY_FACTOR))
        self._cleanup_interval = int(getattr(settings, "memory_cleanup_interval", 3600))
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = threading.Lock()
    
    def start_background_cleanup(self):
        """启动后台定时清理任务。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info("Memory TTL manager started, cleanup interval=%ds, ttl=%ddays",
                    self._cleanup_interval, self._ttl_days)
    
    def stop_background_cleanup(self):
        """停止后台清理任务。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Memory TTL manager stopped")
    
    async def _cleanup_loop(self):
        """后台清理循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                if not self._running:
                    break
                await self._cleanup_expired()
                await self._decay_stale_memories()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Memory TTL cleanup error: %s", e)
    
    async def _cleanup_expired(self) -> int:
        """清理过期记忆。
        
        Returns:
            清理的记忆数量
        """
        cutoff = time.time() - (self._ttl_days * 86400)
        
        try:
            result = self._backend.execute(
                "DELETE FROM memories WHERE created_at < ? AND scope = 'global'",
                (cutoff,),
            )
            count = result.rowcount if result else 0
            if count > 0:
                logger.info("Cleaned up %d expired memories (age > %d days)", count, self._ttl_days)
            return count
        except Exception as e:
            logger.error("Failed to cleanup expired memories: %s", e)
            return 0
    
    async def _decay_stale_memories(self) -> int:
        """衰减低频访问的记忆。
        
        Returns:
            衰减的记忆数量
        """
        now = time.time()
        min_access_time = now - self._cleanup_interval * 2  # 超过2个清理周期的记忆
        
        try:
            # 查找最近未访问的记忆
            rows = self._backend.select(
                "memories",
                "last_accessed < ? AND hit_count > 0",
                (min_access_time,),
            )
            
            if not rows:
                return 0
            
            count = 0
            for row in rows:
                try:
                    # 衰减 hit_count
                    new_hits = max(0, (row.get("hit_count") or 0) * self._decay_factor)
                    self._backend.update(
                        "memories",
                        {"hit_count": int(new_hits), "last_accessed": now},
                        "id = ?",
                        (row["id"],),
                    )
                    count += 1
                except Exception:
                    pass
            
            if count > 0:
                logger.info("Decayed %d stale memories", count)
            return count
        except Exception as e:
            logger.error("Failed to decay stale memories: %s", e)
            return 0
    
    def get_memory_score(self, memory: dict) -> float:
        """计算记忆的综合评分。
        
        评分因素:
        1. 新鲜度（越新越好）
        2. 访问频率（越高越好）
        3. 时间衰减（越久越低）
        
        Returns:
            综合评分（0~1）
        """
        created_at = memory.get("created_at", 0)
        hit_count = memory.get("hit_count", 0) or 0
        now = time.time()
        
        # 新鲜度评分（0~1，越新越高）
        age_days = (now - created_at) / 86400
        freshness = max(0, 1 - age_days / self._ttl_days)
        
        # 访问频率评分（0~1，对数缩放）
        access_score = min(1, hit_count / 10) if hit_count > 0 else 0.1
        
        # 综合评分
        score = freshness * 0.6 + access_score * 0.4
        return round(score, 3)
    
    def should_prune(self, memory: dict, current_count: int) -> bool:
        """判断记忆是否需要裁剪。
        
        Args:
            memory: 记忆记录
            current_count: 当前记忆总数
        
        Returns:
            True 表示应该裁剪
        """
        # 过期记忆直接裁剪
        created_at = memory.get("created_at", 0)
        if time.time() - created_at > self._ttl_days * 86400:
            return True
        
        # 低频访问且记忆总数过多时裁剪
        if current_count > 1000 and (memory.get("hit_count") or 0) < 2:
            return True
        
        return False


# 单例
_ttl_manager: Optional[MemoryTTLManager] = None


def get_memory_ttl_manager() -> MemoryTTLManager:
    """获取记忆 TTL 管理器单例。"""
    global _ttl_manager
    if _ttl_manager is None:
        _ttl_manager = MemoryTTLManager()
    return _ttl_manager


def start_memory_ttl_cleanup():
    """启动记忆 TTL 后台清理。"""
    get_memory_ttl_manager().start_background_cleanup()


def stop_memory_ttl_cleanup():
    """停止记忆 TTL 后台清理。"""
    get_memory_ttl_manager().stop_background_cleanup()
