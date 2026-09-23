"""网关诊断文案"跟有效工具调用一起回来"时的处置 + resume 重复落库回归(2026-09-20 二轮)。

现场(截图 + 会话 desktop-mu2rgdkv 的 messages 224~245):
    13:29:51 那一秒内连着 11 轮,每轮都是
        assistant「empty content block is not allowed」 → tool 真实执行成功
    也就是错误文案**和有效 tool_calls 同轮到达**。run c0939734 的
    checkpoints.state.steps = ['tool']*11 + ['answer'](共 12),而库里恰好是
    11 条 tool + 12 条内容完全相同的 assistant —— 两条结论:

    ① 机制:模型这一轮只产出 tool_use、没有正文,provider 适配层没有省略这个
       空 text 块,而是把校验诊断当正文填了回来。请求是被接受的(否则拿不到
       tool_calls),所以**不是**"我们发了空内容块被 400"(一轮的旧假设,已推翻)。
    ② 重复:app/api/chat.py 的 chat_resume 把 `result.answer` 写在 `for step in
       result.steps` 循环体内 → 一个 N 步的运行把同一段 answer 重复落库 N 次。

本文件锁定的行为:
    - 有 tool_calls  → 丢弃文案,保留工具,不抛错(抛错会连整轮工具一起杀);
    - 无 tool_calls  → 抛可重试 LLMError,交给 agent 瞬态重试;
    - resume 落库    → 正文只写一次,且诊断文案永不落库;
    - 读历史         → 已落库的噪音不再回放给界面/模型。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.llm import (
    LLMClient,
    LLMError,
    _is_retryable,
    _looks_like_gateway_error,
    _strip_gateway_error_lines,
    _StreamHeadGuard,
)

NOISE = "empty content block is not allowed"
TOOL_ARGS = '{"query": "class ProductionStatus", "path": "E:\\\\dev\\\\TianGong\\\\MES-System\\\\src"}'


# ---------------- 行级清洗 ----------------

def test_strip_leading_error_line_keeps_body():
    """诊断文案独占首行时删掉该行,正文一字不动。"""
    src = "empty message\n\n现在开始分析 process.py 的 NEW-8 改动。"
    assert _strip_gateway_error_lines(src) == "现在开始分析 process.py 的 NEW-8 改动。"


def test_strip_middle_error_line():
    """夹在正文中间的诊断行同样删掉,前后段落保留。"""
    src = "第一段结论。\nempty message\n第二段结论。"
    assert _strip_gateway_error_lines(src) == "第一段结论。\n第二段结论。"


def test_strip_keeps_inline_quote():
    """行内引用不能误删(否则会把"解释这句话是什么"的正常回答吃掉)。"""
    src = "结论:这句 empty content block is not allowed 是网关的校验文案。"
    assert _strip_gateway_error_lines(src) == src


def test_strip_is_noop_on_clean_text():
    src = "process.py L60-87 已确认 NEW-8 改动落盘。"
    assert _strip_gateway_error_lines(src) == src
    assert _strip_gateway_error_lines("") == ""


# ---------------- 流式守卫:放行时也清洗 ----------------

def test_head_guard_scrubs_on_release():
    """文案+正文混在一条回包、且总长超过 64 字符触发放行时,文案行必须被剔除。"""
    g = _StreamHeadGuard()
    out = ""
    for piece in ["empty message", "\n\n现在开始分析 process.py,先读 schemas.py 确认 Create 分支的两处改动。"]:
        out += g.feed(piece)
    assert "empty message" not in out, f"放行文本里仍有诊断文案: {out!r}"
    assert out.startswith("\n\n") or out.lstrip().startswith("现在开始分析"), out
    assert "process.py" in out


def test_head_guard_discard_drops_buffer_without_error():
    """有 tool_calls 时用 discard:缓冲丢弃、不报错、flush 也不再吐出文案。"""
    g = _StreamHeadGuard()
    assert g.feed(NOISE) == ""
    assert g.is_gateway_error(), "纯文案必须被判为网关回包"
    g.discard()
    assert g.flush() == ""
    assert not g.is_gateway_error(), "discard 后不得再被判定为报错(否则会误抛)"
    assert g.feed("后续正文") == "后续正文"


def test_head_guard_flush_scrubs_short_answer():
    """短回包(<64 字符)里的诊断行在 flush 时也要被剔掉。"""
    g = _StreamHeadGuard()
    assert g.feed("empty message\n你好。") == ""
    assert not g.is_gateway_error()
    assert g.flush() == "你好。"


# ---------------- 流式带工具:两路处置 ----------------

def _tc_delta(index, tc_id=None, name=None, args=None):
    fn = SimpleNamespace(name=name, arguments=args)
    return SimpleNamespace(index=index, id=tc_id, function=fn)


def _chunk(content=None, tool_calls=None, finish_reason=None):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, reasoning_content=None, tool_calls=tool_calls),
            finish_reason=finish_reason,
        )],
    )


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _client_returning(chunks):
    client = LLMClient(provider="compat", model="fake")
    client._require_key = lambda: None
    client._trace = lambda *a, **kw: None

    async def _fake_invoke(_fn, _kind, **_kw):
        return _FakeStream(chunks)

    client._invoke = _fake_invoke
    return client


def _no_stream_timeout(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "agent_llm_stream_idle_timeout", 0, raising=False)
    monkeypatch.setattr(settings, "agent_llm_stream_total_timeout", 0, raising=False)


def test_stream_with_tools_noise_plus_toolcalls_keeps_tools(monkeypatch):
    """核心回归:诊断文案 + 有效 tool_calls → 不抛错、工具保留、正文为空。"""
    _no_stream_timeout(monkeypatch)
    chunks = [
        _chunk(content="empty content "),
        _chunk(content="block is not allowed"),
        _chunk(tool_calls=[_tc_delta(0, "call_1", "code_search", "")]),
        _chunk(tool_calls=[_tc_delta(0, None, None, TOOL_ARGS)]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def _run():
        client = _client_returning(chunks)
        holder = {}
        text, tc_names = "", []
        async for item in client.chat_stream_with_tools(
            [{"role": "user", "content": "继续"}],
            tools=[{"type": "function", "function": {"name": "code_search"}}],
            result_holder=holder,
        ):
            if isinstance(item, str):
                text += item
            elif item.get("type") == "tool_call_delta" and item.get("name"):
                tc_names.append(item["name"])
        return text, tc_names, holder["result"]

    text, tc_names, result = asyncio.run(_run())
    assert text == "", f"诊断文案不得流向前端,实际: {text!r}"
    assert "empty content" not in text
    # 每个 tool_call 增量都会发射一次(name 先到、参数随后),所以 name 可能出现多次
    assert tc_names and set(tc_names) == {"code_search"}, f"工具调用增量必须照常透出: {tc_names}"
    assert result is not None and len(result.tool_calls) == 1, "工具调用不能在守卫里被丢掉"
    assert result.tool_calls[0].name == "code_search"
    assert result.tool_calls[0].arguments == TOOL_ARGS
    assert result.content == "", "同轮噪音必须被清空,不能进 state.messages"


def test_stream_with_tools_noise_only_raises_retryable(monkeypatch):
    """没有工具调用、整段就是诊断文案 → 抛可重试错误,不产出正文。"""
    _no_stream_timeout(monkeypatch)
    chunks = [_chunk(content="empty "), _chunk(content="message"), _chunk(finish_reason="stop")]

    async def _run():
        client = _client_returning(chunks)
        got = []
        with pytest.raises(LLMError) as exc:
            async for item in client.chat_stream_with_tools(
                [{"role": "user", "content": "hi"}], tools=[], result_holder={},
            ):
                if isinstance(item, str):
                    got.append(item)
        return got, exc

    got, exc = asyncio.run(_run())
    assert got == [], f"不得产出正文: {got}"
    assert "网关回包异常" in str(exc.value)
    assert _is_retryable(str(exc.value)), "必须走 agent 的瞬态重试分支"


def test_stream_with_tools_normal_answer_untouched(monkeypatch):
    """正常回答(含工具轮)不受影响:前 64 字符也不丢。"""
    _no_stream_timeout(monkeypatch)
    pieces = [
        "process.py L60-87 已确认 NEW-8 改动落盘。",
        "现在读 schemas.py 确认 2 处(Create 加 completed_count + Response 回填)。",
    ]
    chunks = [_chunk(content=p) for p in pieces] + [
        _chunk(tool_calls=[_tc_delta(0, "c1", "read_file", "{}")]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def _run():
        client = _client_returning(chunks)
        holder = {}
        text = ""
        async for item in client.chat_stream_with_tools(
            [{"role": "user", "content": "继续"}], tools=[], result_holder=holder,
        ):
            if isinstance(item, str):
                text += item
        return text, holder["result"]

    text, result = asyncio.run(_run())
    assert text == "".join(pieces), f"正常回答必须完整透传: {text!r}"
    assert result.content == "".join(pieces)


# ---------------- 非流式带工具:同构两路 ----------------

def _client_chat_result(content, tool_calls=None, finish_reason="stop"):
    client = LLMClient(provider="compat", model="fake")
    client._require_key = lambda: None
    client._trace = lambda *a, **kw: None

    msg = SimpleNamespace(content=content, tool_calls=tool_calls or [], reasoning_content=None)
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
        usage=None,
    )

    async def _fake_invoke(_fn, _kind, **_kw):
        return resp

    client._invoke = _fake_invoke
    return client


def test_chat_with_tools_noise_plus_toolcalls_keeps_tools():
    """非流式路径同构:有工具调用时清空正文而不是抛错。"""
    tc = SimpleNamespace(id="call_1", function=SimpleNamespace(name="read_file", arguments=TOOL_ARGS))

    async def _run():
        client = _client_chat_result(NOISE, [tc], finish_reason="tool_calls")
        return await client.chat_with_tools([{"role": "user", "content": "继续"}], tools=[{}])

    result = asyncio.run(_run())
    assert result.content == "", f"噪音文案不得当正文返回: {result.content!r}"
    assert len(result.tool_calls) == 1 and result.tool_calls[0].name == "read_file"


def test_chat_with_tools_noise_only_raises_retryable():
    """无工具调用 + 整段文案 → 可重试错误。"""

    async def _run():
        client = _client_chat_result(NOISE)
        return await client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])

    with pytest.raises(LLMError) as exc:
        asyncio.run(_run())
    assert "网关回包异常" in str(exc.value)
    assert _is_retryable(str(exc.value))


def test_chat_with_tools_scrubs_leading_error_line():
    """正文首行是诊断文案时剔掉该行,其余内容保留。"""
    body = "empty message\n\n先把 NEW-8 的 Response 段回填补上。"

    async def _run():
        client = _client_chat_result(body)
        return await client.chat_with_tools([{"role": "user", "content": "继续"}], tools=[])

    result = asyncio.run(_run())
    assert "empty message" not in result.content
    assert "NEW-8" in result.content


# ---------------- resume 重复落库 ----------------

class _FakeStore:
    def __init__(self):
        self.calls = []

    async def add_message(self, session_id, role, kind="text", content=None, **kw):
        self.calls.append({"role": role, "content": content})
        return {"messages": [{"id": "x"}]}


def _resume_result(steps, answer):
    return SimpleNamespace(interrupted=False, answer=answer, steps=steps, model="fake", run_id="run-1")


def _run_resume(monkeypatch, steps, answer):
    """驱动 chat_resume 的非流式分支,返回 store 收到的落库调用。"""
    from app.api import chat as chat_api

    store = _FakeStore()
    monkeypatch.setattr(chat_api, "_store", lambda: store)

    fake_agent = SimpleNamespace()

    async def _resume(*_a, **_kw):
        return _resume_result(steps, answer)

    fake_agent.resume = _resume
    monkeypatch.setattr(chat_api, "_build_agent", lambda _req: fake_agent)

    req = chat_api.ChatResumeRequest(run_id="run-1", session_id="s1", resume_value=["read_file"])
    asyncio.run(chat_api.chat_resume(req))
    return store.calls


TOOL_STEPS = [
    {"type": "tool", "tool": "code_search", "arguments": "{}", "result": "ok"},
    {"type": "tool", "tool": "read_file", "arguments": "{}", "result": "ok"},
    {"type": "tool", "tool": "list_notes", "arguments": "{}", "result": "{}"},
]
ANSWER_STEP = {"type": "answer", "text": "done"}


def test_resume_writes_answer_once_not_per_step(monkeypatch):
    """核心回归:12 步的运行只能落 1 条 assistant,不能每步都写一遍 answer。

    旧实现把 add_message(assistant) 放在 `for step in result.steps` 循环体内,
    run c0939734(11×tool + 1×answer)因此在库里留下 12 条内容完全相同的
    assistant 消息 —— 就是截图里成排重复气泡的来源。
    """
    steps = TOOL_STEPS + [ANSWER_STEP]
    calls = _run_resume(monkeypatch, steps, "NEW-8 已落盘并验证通过。")
    assistants = [c for c in calls if c["role"] == "assistant"]
    tools = [c for c in calls if c["role"] == "tool"]
    assert len(tools) == 3, f"每个 tool step 记一条,实际: {tools}"
    assert len(assistants) == 1, f"正文只能落一次,实际写了 {len(assistants)} 次: {assistants}"
    assert assistants[0]["content"] == "NEW-8 已落盘并验证通过。"


def test_resume_never_persists_gateway_noise(monkeypatch):
    """答案就是网关诊断文案时,一条 assistant 都不该落库。"""
    steps = TOOL_STEPS + [ANSWER_STEP]
    calls = _run_resume(monkeypatch, steps, NOISE)
    assistants = [c for c in calls if c["role"] == "assistant"]
    assert assistants == [], f"诊断文案不得落库,实际: {assistants}"
    assert len([c for c in calls if c["role"] == "tool"]) == 3, "工具过程消息仍要保留"


def test_resume_skips_empty_answer(monkeypatch):
    """空答案不落库(避免空气泡)。"""
    calls = _run_resume(monkeypatch, TOOL_STEPS, "   ")
    assert [c for c in calls if c["role"] == "assistant"] == []


# ---------------- 读历史时清洗 ----------------

def _sqlite_store(db_path):
    """指向临时库的 SessionStore(不动 data/sessions.db)。"""
    from app.memory.session import SessionStore

    return SessionStore(db_path=str(db_path))


def test_get_history_filters_noise_assistant_but_keeps_tools(tmp_path, monkeypatch):
    """历史里已落库的噪音 assistant 消息不再回放,工具与正常回复保留。"""
    store = _sqlite_store(tmp_path / "sessions.db")
    sid = "noise-filter-test"

    async def _seed():
        await store.clear(sid)
        await store.add_message(sid, "user", content="继续")
        await store.add_message(sid, "assistant", content=NOISE)
        await store.add_message(sid, "tool", content=json.dumps({"name": "read_file"}))
        await store.add_message(sid, "assistant", content="empty message")
        await store.add_message(sid, "assistant", content="NEW-8 已落盘,语法校验通过。")
        return await store.get_history(sid)

    history = asyncio.run(_seed())
    roles = [m["role"] for m in history]
    contents = [m["content"] for m in history]
    assert roles == ["user", "tool", "assistant"], f"噪音 assistant 应被滤掉: {list(zip(roles, contents))}"
    assert contents[-1] == "NEW-8 已落盘,语法校验通过。"
    # 引用这句话的正常回答不能被误杀
    assert not any(_looks_like_gateway_error(c) for c in contents)
