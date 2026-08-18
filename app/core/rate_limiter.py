"""API 速率限制:防止滥用，确保公平使用

支持多种限流策略:
1. 固定窗口限流 (Fixed Window)
2. 滑动窗口限流 (Sliding Window)
3. 令牌桶限流 (Token Bucket)
4. 漏桶限流 (Leaky Bucket)

支持维度:
- IP 地址限流
- API Key 限流
- 用户 ID 限流
- 全局限流
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RateLimitStrategy(str, Enum):
    """限流策略"""
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"
    TOKEN_BUCKET = "token_bucket"
    LEAKY_BUCKET = "leaky_bucket"


@dataclass
class RateLimitConfig:
    """限流配置"""
    strategy: RateLimitStrategy = RateLimitStrategy.SLIDING_WINDOW
    max_requests: int = 100  # 最大请求数
    window_seconds: int = 60  # 窗口时间（秒）
    burst_size: int = 10  # 突发大小（令牌桶）
    refill_rate: float = 1.0  # 令牌补充速率（个/秒）
    key_prefix: str = ""  # 键前缀


@dataclass
class RateLimitEntry:
    """限流条目"""
    key: str
    timestamps: list = field(default_factory=list)
    tokens: float = 0.0
    last_refill: float = 0.0
    request_count: int = 0


class RateLimiter:
    """速率限制器"""

    def __init__(self):
        self._entries: dict[str, RateLimitEntry] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = 60  # 清理间隔（秒）
        self._last_cleanup = time.time()

    def is_allowed(
        self,
        key: str,
        config: RateLimitConfig,
    ) -> tuple[bool, dict]:
        """检查请求是否允许
        
        Args:
            key: 限流键（如 IP、API Key）
            config: 限流配置
        
        Returns:
            (是否允许, 限流信息)
        """
        # 定期清理
        self._maybe_cleanup()
        
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = RateLimitEntry(key=key, tokens=config.burst_size)
                self._entries[key] = entry
            
            now = time.time()
            
            if config.strategy == RateLimitStrategy.FIXED_WINDOW:
                return self._check_fixed_window(entry, config, now)
            elif config.strategy == RateLimitStrategy.SLIDING_WINDOW:
                return self._check_sliding_window(entry, config, now)
            elif config.strategy == RateLimitStrategy.TOKEN_BUCKET:
                return self._check_token_bucket(entry, config, now)
            elif config.strategy == RateLimitStrategy.LEAKY_BUCKET:
                return self._check_leaky_bucket(entry, config, now)
            else:
                return True, {}

    def _check_fixed_window(
        self,
        entry: RateLimitEntry,
        config: RateLimitConfig,
        now: float,
    ) -> tuple[bool, dict]:
        """固定窗口限流"""
        window_start = now - (now % config.window_seconds)
        
        # 清理旧时间戳
        entry.timestamps = [t for t in entry.timestamps if t >= window_start]
        
        current_count = len(entry.timestamps)
        allowed = current_count < config.max_requests
        
        if allowed:
            entry.timestamps.append(now)
        
        return allowed, {
            "strategy": "fixed_window",
            "limit": config.max_requests,
            "remaining": max(0, config.max_requests - current_count - (1 if allowed else 0)),
            "reset_at": window_start + config.window_seconds,
        }

    def _check_sliding_window(
        self,
        entry: RateLimitEntry,
        config: RateLimitConfig,
        now: float,
    ) -> tuple[bool, dict]:
        """滑动窗口限流"""
        window_start = now - config.window_seconds
        
        # 清理旧时间戳
        entry.timestamps = [t for t in entry.timestamps if t > window_start]
        
        current_count = len(entry.timestamps)
        allowed = current_count < config.max_requests
        
        if allowed:
            entry.timestamps.append(now)
        
        return allowed, {
            "strategy": "sliding_window",
            "limit": config.max_requests,
            "remaining": max(0, config.max_requests - current_count - (1 if allowed else 0)),
            "reset_at": now + config.window_seconds,
        }

    def _check_token_bucket(
        self,
        entry: RateLimitEntry,
        config: RateLimitConfig,
        now: float,
    ) -> tuple[bool, dict]:
        """令牌桶限流"""
        # 补充令牌
        if entry.last_refill > 0:
            elapsed = now - entry.last_refill
            refill = elapsed * config.refill_rate
            entry.tokens = min(config.burst_size, entry.tokens + refill)
        else:
            entry.tokens = config.burst_size
        
        entry.last_refill = now
        
        allowed = entry.tokens >= 1.0
        
        if allowed:
            entry.tokens -= 1.0
        
        return allowed, {
            "strategy": "token_bucket",
            "limit": config.burst_size,
            "remaining": int(entry.tokens),
            "refill_rate": config.refill_rate,
        }

    def _check_leaky_bucket(
        self,
        entry: RateLimitEntry,
        config: RateLimitConfig,
        now: float,
    ) -> tuple[bool, dict]:
        """漏桶限流"""
        # 计算漏出
        if entry.last_refill > 0:
            elapsed = now - entry.last_refill
            leaked = elapsed * config.refill_rate
            entry.tokens = max(0, entry.tokens - leaked)
        
        entry.last_refill = now
        
        allowed = entry.tokens < config.max_requests
        
        if allowed:
            entry.tokens += 1.0
        
        return allowed, {
            "strategy": "leaky_bucket",
            "limit": config.max_requests,
            "remaining": max(0, config.max_requests - int(entry.tokens)),
            "leak_rate": config.refill_rate,
        }

    def _maybe_cleanup(self) -> None:
        """定期清理过期条目"""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        self._last_cleanup = now
        cutoff = now - 3600  # 保留 1 小时内的条目
        
        with self._lock:
            to_delete = [
                key for key, entry in self._entries.items()
                if entry.timestamps and max(entry.timestamps) < cutoff
            ]
            for key in to_delete:
                del self._entries[key]

    def get_usage(self, key: str, window_seconds: int = 60) -> dict:
        """获取使用情况"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return {"count": 0, "limit": 0, "remaining": 0}
            
            now = time.time()
            window_start = now - window_seconds
            recent_count = sum(1 for t in entry.timestamps if t > window_start)
            
            return {
                "count": recent_count,
                "tokens": entry.tokens,
            }

    def reset(self, key: Optional[str] = None) -> None:
        """重置限流状态"""
        with self._lock:
            if key:
                self._entries.pop(key, None)
            else:
                self._entries.clear()


# 预定义限流配置
DEFAULT_CONFIGS = {
    "api_default": RateLimitConfig(
        strategy=RateLimitStrategy.SLIDING_WINDOW,
        max_requests=100,
        window_seconds=60,
    ),
    "api_strict": RateLimitConfig(
        strategy=RateLimitStrategy.SLIDING_WINDOW,
        max_requests=10,
        window_seconds=60,
    ),
    "api_relaxed": RateLimitConfig(
        strategy=RateLimitStrategy.SLIDING_WINDOW,
        max_requests=1000,
        window_seconds=60,
    ),
    "chat": RateLimitConfig(
        strategy=RateLimitStrategy.TOKEN_BUCKET,
        max_requests=30,
        window_seconds=60,
        burst_size=5,
        refill_rate=0.5,
    ),
}

# 单例
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """获取速率限制器单例"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def rate_limit(
    key: str,
    config_name: str = "api_default",
    config: Optional[RateLimitConfig] = None,
) -> tuple[bool, dict]:
    """速率限制检查
    
    Args:
        key: 限流键
        config_name: 预定义配置名
        config: 自定义配置（优先使用）
    
    Returns:
        (是否允许, 限流信息)
    """
    limiter = get_rate_limiter()
    if config is None:
        config = DEFAULT_CONFIGS.get(config_name, DEFAULT_CONFIGS["api_default"])
    return limiter.is_allowed(key, config)
