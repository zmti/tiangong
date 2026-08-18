"""用户反馈工具与 API 测试。"""
import os
import uuid

import pytest

from app.config import settings
from app.memory.ltm import MemoryStore
from app.tools.feedback_tool import like_feedback, dislike_feedback


def _mk_db() -> str:
    d = os.path.join(settings.resolved_data_dir, f"_test_fb_{uuid.uuid4().hex[:8]}")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "feedback.db")


@pytest.fixture
def feedback_store():
    db = _mk_db()
    store = MemoryStore(db_path=db)
    yield store
    try:
        import shutil
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)
    except OSError:
        pass


def test_like_feedback_stores_memory(feedback_store):
    import app.memory.ltm as ltm_mod
    orig = ltm_mod.get_memory_store
    ltm_mod.get_memory_store = lambda: feedback_store
    try:
        result = like_feedback.func(context="如何计算斐波那契数列", note="答案很清晰")
        assert result.get("id") is not None
        memories = feedback_store.list(limit=10)
        assert any("[用户反馈-认可]" in m["content"] for m in memories)
    finally:
        ltm_mod.get_memory_store = orig


def test_dislike_feedback_stores_memory(feedback_store):
    import app.memory.ltm as ltm_mod
    orig = ltm_mod.get_memory_store
    ltm_mod.get_memory_store = lambda: feedback_store
    try:
        result = dislike_feedback.func(context="如何排序列表", reason="给的例子太复杂")
        assert result.get("id") is not None
        memories = feedback_store.list(limit=10)
        assert any("[用户反馈-建议改进]" in m["content"] for m in memories)
    finally:
        ltm_mod.get_memory_store = orig


def test_like_without_note(feedback_store):
    import app.memory.ltm as ltm_mod
    orig = ltm_mod.get_memory_store
    ltm_mod.get_memory_store = lambda: feedback_store
    try:
        result = like_feedback.func(context="简单问题")
        assert result.get("id") is not None
    finally:
        ltm_mod.get_memory_store = orig


def test_dislike_without_reason(feedback_store):
    import app.memory.ltm as ltm_mod
    orig = ltm_mod.get_memory_store
    ltm_mod.get_memory_store = lambda: feedback_store
    try:
        result = dislike_feedback.func(context="有问题的回答")
        assert result.get("id") is not None
    finally:
        ltm_mod.get_memory_store = orig


def test_feedback_keyword_tags(feedback_store):
    import app.memory.ltm as ltm_mod
    orig = ltm_mod.get_memory_store
    ltm_mod.get_memory_store = lambda: feedback_store
    try:
        like_feedback.func(context="测试点赞", note="好")
        dislike_feedback.func(context="测试踩点", reason="不好")
        all_memories = feedback_store.list(limit=20)
        liked = [m for m in all_memories if "liked" in m.get("keywords", [])]
        disliked = [m for m in all_memories if "improve" in m.get("keywords", [])]
        assert len(liked) >= 1
        assert len(disliked) >= 1
    finally:
        ltm_mod.get_memory_store = orig
