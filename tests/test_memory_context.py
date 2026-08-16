"""Token 级上下文预算与渐进摘要测试。"""
import asyncio

from app.memory.context import (
    budget_messages,
    estimate_tokens,
    layered_compress,
    progressive_summarize,
    split_history,
)


def test_estimate_tokens_cjk_and_ascii():
    assert estimate_tokens("你好世界") == 4          # 4 个 CJK 字 ≈ 4 token
    assert estimate_tokens("hello") in (1, 2)        # 5 ASCII ≈ 1~2 token
    assert estimate_tokens("") == 0
    # 混合: 2 CJK + 9 ASCII -> 2 + ceil(9/4) = 5
    assert estimate_tokens("中文abc123xyz") == 5


def test_budget_messages_keeps_recent():
    msgs = [{"role": "user", "content": "甲"}, {"role": "user", "content": "乙"}] * 5
    # 预算只够 2 条
    kept = budget_messages(msgs, max_tokens=2, min_keep=1)
    assert len(kept) == 2
    # 保留的是最后 2 条(顺序保持)
    assert kept[0]["content"] == "甲"
    assert kept[1]["content"] == "乙"


def test_budget_messages_respects_head_tokens():
    msgs = [{"role": "user", "content": "AAA"}, {"role": "user", "content": "BBB"}]
    kept = budget_messages(msgs, max_tokens=10, head_tokens=10, min_keep=0)
    assert kept == []


def test_split_history():
    history = [{"role": "user", "content": "旧"}] * 10
    keep, old = split_history(history, max_tokens=4, min_keep=4)
    assert len(keep) == 4
    assert len(old) == 6


class FakeLLM:
    async def chat_json(self, messages, temperature=0.2):
        return {"summary": "这是压缩后的摘要"}

    async def chat(self, messages, temperature=0.2):
        return '{"summary": "这是压缩后的摘要"}'


def test_progressive_summarize_returns_summary_plus_recent():
    history = [{"role": "user", "content": "旧消息"}] * 10 + \
              [{"role": "user", "content": "最近消息"}]
    out = asyncio.run(progressive_summarize(FakeLLM(), history, max_tokens=10, min_keep=3))
    # 首条为 system 摘要,其余为保留的最近消息
    assert out[0]["role"] == "system"
    assert "摘要" in out[0]["content"]
    assert any(m["content"] == "最近消息" for m in out)
    assert len(out) < len(history)


def test_progressive_summarize_short_history_unchanged():
    history = [{"role": "user", "content": "x"}] * 3
    out = asyncio.run(progressive_summarize(FakeLLM(), history, max_tokens=1000, min_keep=3))
    assert out == history


class LayeredFakeLLM:
    """支持 chat_json(抽事实)与 chat(摘要)的分层压缩假 LLM。"""

    provider = "fake"

    async def chat_json(self, messages, temperature=0.2):
        return {"facts": ["用户偏好事实A", "决策B", "未完成任务C"]}

    async def chat(self, messages, temperature=0.2):
        return '{"summary": "早期背景摘要"}'


class FailingLLM:
    provider = "fake"

    async def chat_json(self, messages, temperature=0.2):
        raise RuntimeError("network down")

    async def chat(self, messages, temperature=0.2):
        raise RuntimeError("network down")


def test_layered_compress_short_history_unchanged():
    history = [{"role": "user", "content": "x"}] * 3
    out, stats = asyncio.run(
        layered_compress(LayeredFakeLLM(), history, max_tokens=10000, min_keep=3)
    )
    assert out == history
    assert stats["strategy"] == "none"


def test_layered_compress_facts_and_summary():
    history = [{"role": "user", "content": "旧消息"}] * 10 + \
              [{"role": "user", "content": "最近消息"}]
    out, stats = asyncio.run(
        layered_compress(LayeredFakeLLM(), history, max_tokens=10, min_keep=3)
    )
    assert stats["strategy"] == "layered"
    assert stats["summary"] is True
    assert stats["fact_count"] >= 1
    joined = "\n".join(m.get("content", "") for m in out)
    assert "早期背景摘要" in joined
    assert "事实A" in joined
    assert any(m["content"] == "最近消息" for m in out)


def test_layered_compress_all_fail_falls_back_to_truncate():
    """LLM 不可用(摘要/抽事实全失败)时,应降级为普通截断且不抛异常。"""
    history = [{"role": "user", "content": "旧消息"}] * 10 + \
              [{"role": "user", "content": "最近消息"}]
    out, stats = asyncio.run(
        layered_compress(FailingLLM(), history, max_tokens=10, min_keep=3)
    )
    assert stats["strategy"] == "fallback_keep"
    assert any(m["content"] == "最近消息" for m in out)
    assert len(out) <= len(history)
