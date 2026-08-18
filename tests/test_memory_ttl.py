"""记忆 TTL 管理器测试。"""
import time

import pytest

from app.core.memory_ttl import MemoryTTLManager


@pytest.fixture
def ttl_manager():
    """创建 TTL 管理器(使用默认后端)。"""
    manager = MemoryTTLManager()
    manager._ttl_days = 7  # 缩短 TTL 便于测试
    manager._cleanup_interval = 60
    yield manager
    manager.stop_background_cleanup()


def test_memory_score_freshness(ttl_manager):
    """新鲜度评分:越新的记忆得分越高。"""
    now = time.time()
    old = {"created_at": now - 86400 * 10, "hit_count": 0}
    recent = {"created_at": now - 3600, "hit_count": 0}
    score_old = ttl_manager.get_memory_score(old)
    score_recent = ttl_manager.get_memory_score(recent)
    assert score_recent > score_old


def test_memory_score_hit_count(ttl_manager):
    """访问频率加分:高频访问的记忆得分更高。"""
    now = time.time()
    low = {"created_at": now - 3600, "hit_count": 1}
    high = {"created_at": now - 3600, "hit_count": 50}
    score_low = ttl_manager.get_memory_score(low)
    score_high = ttl_manager.get_memory_score(high)
    assert score_high > score_low


def test_should_prune_expired_memory(ttl_manager):
    """超过 TTL 的记忆应被剪枝。"""
    now = time.time()
    expired = {"created_at": now - 86400 * 10, "hit_count": 0}
    recent = {"created_at": now - 3600, "hit_count": 0}
    assert ttl_manager.should_prune(expired, current_count=100) is True
    assert ttl_manager.should_prune(recent, current_count=100) is False


def test_should_prune_low_quality(ttl_manager):
    """低质量记忆(旧 + 低访问)应被剪枝。"""
    now = time.time()
    # 10天前，无访问记录，应被剪枝
    stale = {"created_at": now - 86400 * 10, "hit_count": 0}
    # 2天前，有一定访问量，不应被剪枝
    good = {"created_at": now - 86400 * 2, "hit_count": 20}
    assert ttl_manager.should_prune(stale, current_count=100) is True
    assert ttl_manager.should_prune(good, current_count=100) is False


def test_background_cleanup_property(ttl_manager):
    """后台清理任务属性可正常读取。"""
    assert ttl_manager._running is False
    assert ttl_manager._task is None
