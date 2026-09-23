"""输出中断修复测试:"回答没写完"检测 + 自动续写。

复现场景(2026-09-20 实测):
1. agnes-3.0-flash 等网关把 max_tokens 截断 / 上游自身截断归一化成
   `finish_reason="stop"`,llm.py 原有的 `finish_reason=="length"` 检测失效,
   agent 把半句答案当最终答案落库 → 用户看到"输出中断"
   (实例:答案停在 "正式应用 2 项 edit：",后续 edit_file 调用再没发生)。
2. 流中途被 ReadTimeout/限流杀死时,已实时显示给用户的内容被整轮重生成覆盖。

修复后行为:
- `_answer_looks_truncated` 本地判定(悬空标点/未闭合代码块/触顶 max_output_tokens);
- 命中即自动续写:已输出内容作为 assistant 前缀回灌 + "从断点继续"提示,
  返回拼接后的完整文本,且**不发 answer_reset**(前端无需清屏,内容追加显示);
- 流中途失败把已输出片段写入 `metadata["partial_answer"]`,供上层断点续写。

注:本仓 venv 未安装 pytest-asyncio,故异步场景统一用 `asyncio.run` 驱动,
不依赖额外插件即可运行。
"""
import asyncio

import pytest

from app.core.agent import (
    Agent,
    _CONTINUE_DONE_MARKERS,
    _CONTINUE_PROMPT,
    _answer_looks_truncated,
    _strip_continuation_overlap,
)
from app.core.llm import ChatResult, ToolCall, LLMError
from app.runtime.state import AgentState
from tests.test_agent import MockLLM


# ---- 纯函数:截断判定 ----


def test_complete_answer_not_flagged():
    """正常结束的答案不应被判为截断(否则每次回答都多一次续写调用)。"""
    for text in [
        "好的,已经完成。",
        "## 结论\n\n全部改完并验证通过。",
        "- **A**：已完成\n- **B**：已完成",
        "3. quantity 符号：流水记正量,靠 movement_type 枚举区分方向,库存实扣在 Inventory.quantity,自洽",
        "```python\nprint(1)\n```\n执行成功。",
        "是否继续?",
    ]:
        ok, why = _answer_looks_truncated(text)
        assert ok is False, f"误判为截断({why}): {text[:60]!r}"


def test_dangling_tail_flagged():
    """结尾是冒号/逗号/连接词等"话没说完"的信号 → 判为截断。"""
    for text in ["答案如下：", "正式应用 2 项 edit：", "接下来我会读取 material.py,", "修改完成，"]:
        ok, _ = _answer_looks_truncated(text)
        assert ok is True, f"漏判截断: {text!r}"


def test_unclosed_code_fence_flagged():
    ok, why = _answer_looks_truncated("说明:\n\n```python\nprint(1)\n")
    assert ok is True and why == "code_fence_unclosed"


def test_max_output_tokens_budget_flagged():
    """估算输出 tokens 触顶 → 判为截断(上游回 stop 时的兜底信号)。"""
    long_text = "这是一段足够长的中文内容用来把 token 估算顶到上限。" * 200
    ok, why = _answer_looks_truncated(long_text, max_output_tokens=500)
    assert ok is True and why == "hit_max_output_tokens"
    # 配额充足时不判定
    ok2, _ = _answer_looks_truncated(long_text, max_output_tokens=100000)
    assert ok2 is False


def test_empty_answer_not_flagged():
    assert _answer_looks_truncated("")[0] is False
    assert _answer_looks_truncated("   \n ")[0] is False


# ---- 纯函数:续写重叠裁剪 ----


def test_strip_continuation_overlap():
    # prev 后缀 == cont 前缀(模型复述) → 裁掉
    assert _strip_continuation_overlap("正式应用 2 项 edit：", "正式应用 2 项 edit：已应用。") == "已应用。"
    # 完全重复
    assert _strip_continuation_overlap("重复内容重复内容", "重复内容重复内容") == ""
    # 无重叠 → 原样返回
    assert _strip_continuation_overlap("上一段结束。", "全新内容。") == "全新内容。"


def test_continue_prompt_requires_no_repeat():
    assert "不要重复" in _CONTINUE_PROMPT


# ---- 异步:续写主流程 ----


class _FakeStreamLLM:
    """文本模式假流:按 pass 依次吐出 chunk;pass 里的异常会直接抛出。"""

    def __init__(self, passes):
        self.passes = [list(p) for p in passes]
        self.calls: list[list[dict]] = []

    async def chat_stream(self, messages, temperature=0.7):
        self.calls.append(list(messages))
        if not self.passes:
            raise AssertionError("多请求了一次 LLM(passes 已耗尽)")
        for chunk in self.passes.pop(0):
            if isinstance(chunk, Exception):
                raise chunk
            await asyncio.sleep(0)
            yield chunk


class _FakeNativeLLM:
    """原生工具模式假流:每次 pass 产出 (文本, tool_calls)。"""

    def __init__(self, passes):
        self.passes = [list(p) for p in passes]
        self.calls: list[list[dict]] = []

    async def chat_stream_with_tools(self, messages, tools=None, temperature=0.7, result_holder=None):
        self.calls.append(list(messages))
        if not self.passes:
            raise AssertionError("多请求了一次 LLM(passes 已耗尽)")
        for text, tool_calls in [self.passes.pop(0)]:
            for ch in text:
                await asyncio.sleep(0)
                yield ch
            if result_holder is not None:
                result_holder["result"] = ChatResult(content=text, tool_calls=list(tool_calls or []))


class _StubAgent(Agent):
    """只装配 _stream_round / _stream_pass 需要的最小字段,避免拉起整条 Agent 依赖链。"""

    def __init__(self, llm, native=False, max_output_tokens=0):
        self.llm = llm
        self.max_output_tokens = max_output_tokens
        self.temperature = 0.7
        self._native_enabled = native
        self._schemas_cache = []  # _schemas 是只读 property,直接喂缓存


def _make_state() -> AgentState:
    st = AgentState(session_id="t", user_input="继续")
    st.metadata["stream"] = True
    st.messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "继续"}]
    return st


def _token_text(state: AgentState) -> str:
    return "".join(e.get("content", "") for e in state.events if e.get("type") == "token")


def test_dangling_answer_triggers_continuation_and_appends():
    """答案停在"正式应用 2 项 edit：" → 自动续写并把剩余内容追加在后面。"""

    async def _case():
        llm = _FakeStreamLLM([["正式应用 2 项 edit："], ["已应用 2 项 edit。\n\n完成。"]])
        agent = _StubAgent(llm)
        state = _make_state()
        res = await agent._stream_round(state.messages, state, native=False)
        return llm, state, res

    llm, state, (mode, text, tool_calls) = asyncio.run(_case())

    assert mode == "text" and tool_calls == []
    assert text == "正式应用 2 项 edit：已应用 2 项 edit。\n\n完成。"
    assert len(llm.calls) == 2, "应恰好续写一次"
    # 续写请求带上:已输出内容(assistant 前缀) + 续写提示
    assert llm.calls[1][-2] == {"role": "assistant", "content": "正式应用 2 项 edit："}
    assert llm.calls[1][-1]["role"] == "user" and "从断点继续" in llm.calls[1][-1]["content"]
    # 界面事件:两段 token 都是追加式,且不得出现清屏事件
    kinds = [e["type"] for e in state.events]
    assert "answer_reset" not in kinds
    assert kinds.count("status") == 1
    assert _token_text(state) == text
    assert state.metadata["answer_continue_count"] == 1


def test_complete_answer_makes_single_pass():
    """正常结束的答案只请求一次 LLM。"""

    async def _case():
        llm = _FakeStreamLLM([["好的,全部完成。"]])
        agent = _StubAgent(llm)
        state = _make_state()
        res = await agent._stream_round(state.messages, state, native=False)
        return llm, state, res

    llm, state, (_, text, _) = asyncio.run(_case())

    assert text == "好的,全部完成。"
    assert len(llm.calls) == 1
    assert "answer_continue_count" not in state.metadata


def test_done_marker_is_not_appended():
    """模型回"(已完成)"哨兵时,不追加内容且不再续写。"""

    async def _case():
        llm = _FakeStreamLLM([["结论如下："], ["(已完成)"]])
        agent = _StubAgent(llm)
        state = _make_state()
        res = await agent._stream_round(state.messages, state, native=False)
        return llm, res

    llm, (_, text, _) = asyncio.run(_case())

    assert text == "结论如下："
    assert len(llm.calls) == 2
    assert "(已完成)" in _CONTINUE_DONE_MARKERS


def test_continuation_strips_repeated_prefix():
    """续写轮复述了前文时,重复部分不重复显示。"""

    async def _case():
        llm = _FakeStreamLLM([["第一步：读文件，"], ["第一步：读文件，第二步：改代码。"]])
        agent = _StubAgent(llm)
        state = _make_state()
        return await agent._stream_round(state.messages, state, native=False)

    _, text, _ = asyncio.run(_case())

    assert text == "第一步：读文件，第二步：改代码。"


def test_continuation_respects_max_rounds(monkeypatch):
    """模型一直不写完时,续写次数受 agent_answer_continue_max 限制。"""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_answer_continue_max", 1)

    async def _case():
        llm = _FakeStreamLLM([["还在写，"], ["依然没完，"]])
        agent = _StubAgent(llm)
        state = _make_state()
        res = await agent._stream_round(state.messages, state, native=False)
        return llm, res

    llm, (_, text, _) = asyncio.run(_case())

    assert len(llm.calls) == 2  # 首轮 + 1 次续写
    assert text == "还在写，依然没完，"


def test_tool_call_stops_continuation():
    """有工具调用时按正常流程返回,不做续写(文本是工具调用的铺垫)。"""

    async def _case():
        tool_calls = [ToolCall(id="1", name="edit_file", arguments="{}")]
        llm = _FakeNativeLLM([("准备调用工具：", tool_calls)])
        agent = _StubAgent(llm, native=True, max_output_tokens=100000)
        state = _make_state()
        res = await agent._stream_round(state.messages, state, native=True)
        return llm, res

    llm, (mode, text, tcs) = asyncio.run(_case())

    assert mode == "native" and len(tcs) == 1 and tcs[0].name == "edit_file"
    assert text == "准备调用工具："
    assert len(llm.calls) == 1


def test_mid_stream_failure_records_partial_answer():
    """流中途失败:已输出片段写入 metadata['partial_answer'],供上层断点续写。"""

    async def _case():
        llm = _FakeStreamLLM([["前文已输出一半,", LLMError("LLM 流式响应中断: ReadTimeout")]])
        agent = _StubAgent(llm)
        state = _make_state()
        with pytest.raises(LLMError):
            await agent._stream_round(state.messages, state, native=False)
        return state

    state = asyncio.run(_case())

    assert state.metadata["partial_answer"] == "前文已输出一半,"
    assert _token_text(state) == "前文已输出一半,"


def test_prefix_is_kept_when_continuation_round_fails():
    """续写轮再次断流时,partial_answer 必须包含续写前的全部已显示内容。"""

    async def _case():
        llm = _FakeStreamLLM([["第一段,", LLMError("断流 A")], ["第二段,", LLMError("断流 B")]])
        agent = _StubAgent(llm)
        state = _make_state()
        first = ""
        second = ""
        with pytest.raises(LLMError):
            await agent._stream_round(state.messages, state, native=False, prefix="零段, ")
        first = state.metadata["partial_answer"]
        with pytest.raises(LLMError):
            await agent._stream_round(state.messages, state, native=False, prefix=first)
        second = state.metadata["partial_answer"]
        return first, second

    first, second = asyncio.run(_case())

    assert first == "零段, 第一段,"
    assert second == "零段, 第一段,第二段,"


# ---- 端到端:图运行时层(断流 → 断点续写,而不是整轮重来) ----


class _FlakyStreamLLM(MockLLM):
    """第一次流式调用吐一半后断流,第二次返回剩余内容。"""

    def __init__(self, first_part: str, rest: str):
        super().__init__([first_part + rest])
        self.first_part = first_part
        self.rest = rest
        self.calls: list[list[dict]] = []

    async def chat_stream(self, messages, temperature=0.7):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            for ch in self.first_part:
                await asyncio.sleep(0)
                yield ch
            raise LLMError("LLM 流式响应中断: ReadTimeout @ default.py:270|__aiter__")
        for ch in self.rest:
            await asyncio.sleep(0)
            yield ch


def test_graph_recovers_stream_break_by_continuing(monkeypatch):
    """流中途被 ReadTimeout 杀死时:保留已显示前文,续写剩余内容,不清屏重来。"""
    from app.core import agent as agent_mod
    from tests.test_agent import make_registry  # noqa: F401

    _real_sleep = asyncio.sleep

    async def _fast_sleep(delay, *args, **kwargs):
        # 只跳过 5s/15s 退避;小 sleep 必须保留,否则图内的轮询循环会空转卡死
        return await _real_sleep(0 if (delay or 0) >= 5 else delay)

    monkeypatch.setattr(agent_mod.asyncio, "sleep", _fast_sleep)

    async def _case():
        llm = _FlakyStreamLLM("前文已输出一半,", "后半句补齐了。")
        agent = Agent(llm=llm, registry=make_registry(), max_steps=5)
        events = []
        async for evt in agent.run_stream("继续"):
            events.append(evt)
        return llm, events

    llm, events = asyncio.run(_case())

    kinds = [e["type"] for e in events]
    tokens = "".join(e.get("content", "") for e in events if e["type"] == "token")
    status = [e.get("content", "") for e in events if e["type"] == "status"]

    assert len(llm.calls) == 2, f"应重试/续写一次,实际 {len(llm.calls)} 次"
    # 已有前文时不得发 answer_reset(否则用户看到的内容被清掉)
    assert "answer_reset" not in kinds
    assert any("续写" in s for s in status), f"缺少续写状态提示: {status}"
    # 续写请求把已输出内容作为 assistant 前缀回灌
    assert llm.calls[1][-2] == {"role": "assistant", "content": "前文已输出一半,"}
    assert "从断点继续" in llm.calls[1][-1]["content"]
    # 界面上前半段保留、后半段追加
    assert tokens == "前文已输出一半,后半句补齐了。"
    assert "error" not in kinds
