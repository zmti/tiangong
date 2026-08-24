"""记忆衰退测试。"""
import os
import time
import uuid

import pytest

from app.config import settings
from app.memory.decay import MemoryCluster, MemoryDecay


@pytest.fixture
def decay():
    return MemoryDecay(ttl_hours=1.0, decay_rate=0.5, min_score=0.05)


def test_score_decay_over_time(decay):
    now = time.time()
    old_created = now - 7200  # 2小时前 = 2个TTL周期, score = 0.5^2 = 0.25
    recent_created = now - 3600  # 1小时前 = 1个TTL周期, score = 0.5
    score_old = decay.calculate_score(old_created, old_created)
    score_recent = decay.calculate_score(recent_created, recent_created)
    assert score_old < score_recent


def test_should_evict_old_memory(decay):
    now = time.time()
    # 远超TTL: 10倍 = 0.5^10 ≈ 0.001 < 0.05
    old = now - 36000
    assert decay.should_evict(old, old, hit_count=0) is True
    # 接近现在,高活跃不应被清除
    recent = now - 100
    assert decay.should_evict(recent, recent, hit_count=5) is False


def test_hit_count_boosts_score(decay):
    now = time.time()
    age = now - 5000
    score_low_hits = decay.calculate_score(age, age, hit_count=0)
    score_high_hits = decay.calculate_score(age, age, hit_count=50)
    assert score_high_hits > score_low_hits


def test_cluster_find_similar_no_vector():
    """find_similar 在无向量时不应崩溃。"""
    cluster = MemoryCluster()
    # 不传 store(None),应返回空列表而非异常
    result = cluster.find_similar(None, "测试内容", ["测试", "关键词"])
    assert isinstance(result, list)


def test_memory_store_integration():
    from app.memory.ltm import MemoryStore

    db_path = os.path.join(
        settings.resolved_data_dir, f"_test_decay_{uuid.uuid4().hex[:8]}.db"
    )
    store = MemoryStore(db_path=db_path)
    decay = MemoryDecay(ttl_hours=1.0, decay_rate=0.5, min_score=0.05)

    mid = uuid.uuid4().hex
    now = time.time()
    store.save("这是一条测试记忆", keywords=["测试"], memory_id=mid)
    mem = store.get(mid)
    created_at = mem["created_at"]

    # 刚存入不应被清除(分数高于阈值)
    assert decay.should_evict(created_at, created_at, hit_count=0) is False

    # 手动设置一个很旧的时间戳(100倍TTL)
    old_ts = now - 360000  # 100小时
    store._backend.execute(
        "UPDATE memories SET created_at=?, updated_at=? WHERE id=?",
        (old_ts, old_ts, mid),
    )

    # 100倍TTL后分数应低于阈值
    assert decay.should_evict(old_ts, old_ts, hit_count=0) is True

    try:
        store.delete(mid)
    except Exception:
        pass
    try:
        import shutil
        shutil.rmtree(os.path.dirname(db_path), ignore_errors=True)
    except Exception:
        pass
