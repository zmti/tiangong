"""网关校验错误被当正文回包的防御测试(2026-09-20 "回答变成一句报错")。

现象:
    assistant 气泡正文是 "empty content block is not allowed" / "empty message",
    并被写进会话历史。

取证一(机制,已确证):
    data/traces/2026-09-20.jsonl 有 5 个 run 的 run_end answer_len 正好是 13 / 34
    == 这两句话的长度;checkpoints.db 中对应 run 的 metadata.resp 就是这两句话,
    且 finish_reason=stop、tool_calls=0 —— **上游 200 正常流,只是正文是它自己的
    校验错误**。这两句话在仓库源码里搜不到,不是本机生成的。

取证二(排除"我们发了空内容块"):
    重放 3 个失败 run 的真实消息体(75~127KB、含 content="" 的 assistant、
    158 个 tools)到同一网关,全部返回正常回答;run e9714360 的请求里完全没有
    空内容块,当时却同样回了 "empty message"。
    => 触发条件在 provider 侧,是偶发抖动;本机缺陷是"把网关错误当答案忠实渲染"。

本文件锁定的两道防线:
    ① _StreamHeadGuard / _looks_like_gateway_error(主修):整段回包就是这类错误时
       不对外产出任何正文,抛可重试 LLMError,交给 agent 瞬态重试重发;
    ② _normalize_outgoing_messages(加固):出站前消除空内容块。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.llm import (
    LLMClient,
    LLMError,
    _is_retryable,
    _looks_like_gateway_error,
    _normalize_outgoing_messages,
    _StreamHeadGuard,
)


# ---------------- 防线① 出站规范化 ----------------

def test_assistant_tool_call_empty_content_drops_key():
    """带 tool_calls 的空正文 assistant 消息必须省略 content 键,不能发 ""。"""
    out = _normalize_outgoing_messages([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
    ])
    assert len(out) == 1
    assert "content" not in out[0], f"content 键应被移除,实际: {out[0]}"
    assert out[0]["tool_calls"] == [{"id": "a"}]


def test_assistant_tool_call_whitespace_content_drops_key():
    """纯空白正文同样算空内容块(网关会 400)。"""
    out = _normalize_outgoing_messages([
        {"role": "assistant", "content": "   \n\t ", "tool_calls": [{"id": "a"}]},
    ])
    assert "content" not in out[0]


def test_tool_empty_result_gets_placeholder():
    """tool 消息 content 是必填字符串:空结果填占位,不留空块。"""
    out = _normalize_outgoing_messages([
        {"role": "tool", "tool_call_id": "a", "content": ""},
    ])
    assert out[0]["content"].strip(), "tool 空结果必须替换为非空占位"
    assert out[0]["tool_call_id"] == "a"


def test_assistant_empty_without_tools_dropped():
    """无 tool_calls 的空 assistant 消息无信息量,整条丢弃而不是补占位。"""
    out = _normalize_outgoing_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "在吗"},
    ])
    assert [m["role"] for m in out] == ["user", "user"]


def test_multimodal_empty_text_blocks_pruned():
    """多模态数组里的空 text 块要剔除,非空块保留。"""
    out = _normalize_outgoing_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "   "},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]},
    ])
    assert out[0]["content"] == [{"type": "image_url", "image_url": {"url": "x"}}]


def test_multimodal_all_empty_becomes_no_content_or_placeholder():
    """多模态块全为空:退化为非空文本或省略,绝不留下空内容块。"""
    out = _normalize_outgoing_messages([
        {"role": "user", "content": [{"type": "text", "text": ""}]},
    ])
    content = out[0].get("content")
    assert content and str(content).strip()


def test_non_empty_messages_untouched():
    """正常消息原样透传,规范化不能改动正文。"""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "正常回答 L97-103"},
        {"role": "assistant", "content": "有工具也有正文", "tool_calls": [{"id": "a"}]},
    ]
    out = _normalize_outgoing_messages(msgs)
    assert out == msgs


def test_prepare_messages_is_the_choke_point():
    """_prepare_messages(所有请求入口的统一出口)必须先做规范化。"""
    sent = {}

    class _Probe(LLMClient):
        def _cache_control_messages(self, messages):  # noqa: D102
            sent["messages"] = messages
            return messages

    client = _Probe(provider="compat", model="probe")
    client._prepare_messages([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
    ])
    assert "content" not in sent["messages"][0]


# ---------------- 防线② 网关报错识别 ----------------

def test_gateway_error_exact_match():
    """就是这句报错 → 命中。"""
    assert _looks_like_gateway_error("empty content block is not allowed")
    assert _looks_like_gateway_error("Empty content block is not allowed.")
    assert _looks_like_gateway_error("  empty message  ")


def test_gateway_error_not_fooled_by_prose():
    """正常回答里**提到**这句话时不能误判为网关报错(否则会吞掉真实答案)。"""
    assert not _looks_like_gateway_error(
        "这个报错 empty content block is not allowed 的含义是:请求里存在空内容块,"
        "需要我们在出站前把它清掉。"
    )
    assert not _looks_like_gateway_error("empty message 是网关的校验提示。")
    assert not _looks_like_gateway_error("")


def test_gateway_error_long_text_rejected():
    """超过 200 字符的回包不可能是网关校验文案,一律不拦。"""
    assert not _looks_like_gateway_error("empty message" + " 补充" * 100)


# ---------------- 防线② 流式守卫 ----------------

def test_head_guard_holds_short_head_then_releases():
    """不足 64 字符先缓冲(网关报错只有几十字符),命中即判定为报错。"""
    g = _StreamHeadGuard()
    assert g.feed("empty content") == ""
    assert g.feed(" block is not allowed") == ""
    assert g.is_gateway_error(), "整段就是网关报错,必须命中"
    assert g.sample() == "empty content block is not allowed"


def test_head_guard_releases_after_64_chars():
    """超过 64 字符即放行,正常回答只承受极短延迟。"""
    g = _StreamHeadGuard()
    released = g.feed("x" * 65)
    assert released == "x" * 65
    assert not g.is_gateway_error()
    assert g.feed("后续内容") == "后续内容"


def test_head_guard_flush_releases_short_normal_answer():
    """短正常回答(<64 字符)在流结束时由 flush 放行,内容不丢失。"""
    g = _StreamHeadGuard()
    assert g.feed("短回答") == ""
    assert not g.is_gateway_error()
    assert g.flush() == "短回答"


# ---------------- 端到端:chat_stream 不得把网关报错当正文 ----------------

def _fake_chunk(text):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=None),
            finish_reason=None,
        )],
    )


class _FakeStream:
    """把若干文本片段当成 provider 的流式 chunk 逐个吐出。"""

    def __init__(self, pieces):
        self._pieces = list(pieces)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._pieces:
            raise StopAsyncIteration
        return _fake_chunk(self._pieces.pop(0))


def _client_returning(pieces):
    client = LLMClient(provider="compat", model="fake")
    client._require_key = lambda: None
    client._trace = lambda *a, **kw: None

    async def _fake_invoke(_fn, _kind, **_kw):
        return _FakeStream(pieces)

    client._invoke = _fake_invoke
    return client


def _no_stream_timeout(monkeypatch):
    """关掉流式超时,让测试只考察守卫逻辑(本仓库当前未装 pytest-asyncio,
    因此这里的异步用例统一用 asyncio.run 驱动,不依赖插件)。"""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_llm_stream_idle_timeout", 0, raising=False)
    monkeypatch.setattr(settings, "agent_llm_stream_total_timeout", 0, raising=False)


def test_chat_stream_rejects_gateway_error_toxin(monkeypatch):
    """整段回包就是网关校验错误 → 抛可重试 LLMError,不产出任何正文给前端。"""
    _no_stream_timeout(monkeypatch)

    # 分片到达(网关常把报错拆成多个 chunk),也必须被识别
    async def _run():
        client = _client_returning(["empty content", " block is not allowed"])
        got = []
        with pytest.raises(LLMError) as exc_info:
            async for piece in client.chat_stream([{"role": "user", "content": "hi"}]):
                got.append(piece)
        return got, exc_info

    got, exc_info = asyncio.run(_run())
    assert got == [], f"网关报错不得流向用户区域,实际收到: {got}"
    assert "网关回包异常" in str(exc_info.value), "错误信息需带可重试标记(网关回包异常)"
    # 必须被判为可重试,否则 agent 不会重发,用户只会看到一次失败
    assert _is_retryable(str(exc_info.value)), "网关回包异常必须走 agent 的瞬态重试分支"


def test_chat_stream_normal_answer_passes_through(monkeypatch):
    """正常回答(含短回答)不受守卫影响,内容一字不差。"""
    _no_stream_timeout(monkeypatch)
    pieces = ["process.py L60-87 已确认改动落盘。", "现在读 schemas.py 确认 2 处。"]

    async def _run():
        client = _client_returning(pieces)
        return [p async for p in client.chat_stream([{"role": "user", "content": "hi"}])]

    got = asyncio.run(_run())
    assert "".join(got) == "".join(pieces), "正常回答必须完整透传(含前 64 字符)"
