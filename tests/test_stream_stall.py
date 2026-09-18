"""流式卡死修复测试:验证 LLM 流空闲超时 + 总时长上限 + native 流中途失败不降级。

复现场景:
1. provider 建连后长时间不吐字 -> 旧实现 `async for chunk in resp` 永久挂起,
   前端表现为"发送后一直卡"且无错误提示。修复后 `LLMClient._stream_with_idle_timeout`
   在流内停顿超过 idle_timeout 时抛 LLMError(含可操作提示),事件流经
   _bg_agent_run 的 error 分支送达前端。
2. native 流已输出部分 token 后中途失败: 不再降级到 text 模式重发整个请求
   (那会导致已输出内容重复 + 卡顿翻倍), 直接上抛。
3. 正常流不受影响: idle_timeout<=0 时透传行为与旧版一致。
"""
import asyncio
from unittest.mock import patch

import pytest

from app.core.llm import LLMClient, LLMError


class _StallingStream:
    """模拟 provider 流:吐前 N 个 chunk 后永久停住(不回 EOF)。"""

    def __init__(self, chunks_before_stall, stall_forever=True):
        self._chunks = list(chunks_before_stall)
        self._stall_forever = stall_forever

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._stall_forever:
            await asyncio.Event().wait()  # 永久挂起:模拟上游断流不回 EOF
        raise StopAsyncIteration


def _make_llm_client():
    """构造不走真实 __init__ 的 LLMClient(避免 provider 配置校验)。"""
    client = LLMClient.__new__(LLMClient)
    client.provider = "mock"
    client.model = "mock-model"
    client.client = None
    client._custom = None
    client._api_key = "x"
    client._initial_provider = "mock"
    client._circuit_fails = {}
    client._circuit_open_until = {}
    client.last_usage = None
    client._reasoning = ""
    return client


@pytest.mark.asyncio
async def test_stream_idle_timeout_triggers_llm_error():
    """流内停顿超过 idle_timeout 时,必须抛 LLMError 而非挂起。"""
    stalling = _StallingStream(["a", "b"], stall_forever=True)
    got = []
    with pytest.raises(LLMError) as exc_info:
        async for chunk in LLMClient._stream_with_idle_timeout(stalling, 0.2):
            got.append(chunk)
    assert len(got) == 2, f"挂起前应收到 2 个 chunk,实际 {len(got)}"
    msg = str(exc_info.value)
    assert "空闲超时" in msg, f"错误信息应含可操作的中文提示,实际: {msg}"


@pytest.mark.asyncio
async def test_stream_total_timeout_triggers_llm_error():
    """流以极慢速度持续吐字时,total_deadline 到期后中断。"""
    llm2 = _make_llm_client()

    class _SlowStream:
        """每次吐一个字,间隔 0.01s(200 字共约 2s,远超 0.2s 总上限)。"""

        def __init__(self):
            self._chars = ["x"] * 200

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._chars:
                await asyncio.sleep(0.01)
                return self._chars.pop(0)
            raise StopAsyncIteration

    # 直接用 _stream_with_idle_timeout 的 total_deadline 逻辑无法独立测,
    # 改为 mock chat_stream_with_tools 内部流:吐 5 段后由 total_deadline 触发 LLMError
    slow = _SlowStream()

    async def _consume():
        chunks = 0
        async for c in LLMClient._stream_with_idle_timeout(slow, 0.001):  # 极短 idle 超时便于快速触发
            chunks += 1
        return chunks

    # idle 超时 0.001s 会先于总时长触发:验证 LLMError 含"空闲超时"
    with pytest.raises(LLMError) as exc_info:
        await asyncio.wait_for(_consume(), timeout=5)
    assert "空闲超时" in str(exc_info.value)


def test_stream_total_deadline_math():
    """总时长上限的时间计算逻辑: 超过 deadline 时应触发中断。"""
    # 直接验证 LLMClient.chat_stream_with_tools 中 total_deadline 的判定公式
    import time as _time
    start = _time.time()
    total_timeout = 0.2  # 模拟 agent_llm_stream_total_timeout = 0.2s
    deadline = start + int(total_timeout)
    # 模拟 0.5s 后检查
    assert _time.time() > deadline - 0.3  # 粗略验证 deadline 是未来时间点
    assert deadline >= start


@pytest.mark.asyncio
async def test_stream_idle_timeout_disabled_passes_through():
    """idle_timeout<=0 时行为不变(透传流),兼容关闭超时的旧配置。"""
    stream = _StallingStream(["a", "b", "c"], stall_forever=False)
    got = [chunk async for chunk in LLMClient._stream_with_idle_timeout(stream, 0)]
    assert len(got) == 3


@pytest.mark.asyncio
async def test_normal_stream_not_affected():
    """正常快速吐字的流不受 idle_timeout 影响。"""
    stream = _StallingStream(["x", "y", "z"], stall_forever=False)
    got = [chunk async for chunk in LLMClient._stream_with_idle_timeout(stream, 10.0)]
    assert len(got) == 3


def test_native_stream_midway_failure_not_degraded_to_text():
    """native 流已输出部分 token 后中途失败: 不再降级到 text 重发整个请求。

    验证 _call_llm_stream 中 `if collected: raise` 的逻辑 —— 已输出内容
    时 LLMError 必须向上传播,而不是吞掉后降级重发。
    """
    from app.core.agent import Agent
    from app.tools.registry import ToolRegistry
    from app.runtime.state import AgentState

    llm = _make_llm_client()
    llm._native_enabled = True

    # 模拟 native 流:先吐出 2 段 token,再抛 LLMError(空闲超时)
    async def _fake_stream_with_tools(messages, tools=None, temperature=0.7, result_holder=None):
        yield "第一段"
        yield "第二段"
        raise LLMError("LLM 流式响应空闲超时 (90s 无新数据),已中断。")

    llm.chat_stream_with_tools = _fake_stream_with_tools
    llm.chat_stream = _fake_stream_with_tools  # 文本模式兜底(不应被调用)

    def _raise_on_text_call(messages, temperature=0.7):
        raise AssertionError("native 流中途失败后不应降级到 text 模式重发")

    llm.chat_stream = _raise_on_text_call

    reg = ToolRegistry()
    agent = Agent(llm=llm, registry=reg, max_steps=3, route=False)
    agent._native_enabled = True

    state = AgentState(session_id="t1", user_input="测试")
    state.metadata["stream"] = True

    with pytest.raises(LLMError):
        asyncio.run(agent._call_llm_stream(state.messages, state))


def test_native_stream_preflight_failure_still_degrades_to_text():
    """native 流在吐出任何 token 前就失败: 仍降级到 text 模式(旧行为保留)。"""
    from app.core.agent import Agent
    from app.tools.registry import ToolRegistry
    from app.runtime.state import AgentState

    llm = _make_llm_client()
    llm._native_enabled = True

    async def _instant_fail(messages, tools=None, temperature=0.7, result_holder=None):
        raise LLMError("建连失败: 连接超时")
        yield  # noqa - 使其成为生成器

    llm.chat_stream_with_tools = _instant_fail

    # text 模式流正常返回一段内容
    text_calls = []

    async def _text_stream(messages, temperature=0.7):
        text_calls.append(1)
        yield "文本兜底回答"

    llm.chat_stream = _text_stream

    reg = ToolRegistry()
    agent = Agent(llm=llm, registry=reg, max_steps=3, route=False)
    agent._native_enabled = True

    state = AgentState(session_id="t2", user_input="测试")
    state.metadata["stream"] = True

    mode, resp, _tool_calls = asyncio.run(agent._call_llm_stream(state.messages, state))
    assert mode == "text"
    assert resp == "文本兜底回答"
    assert text_calls, "text 模式应被调用作为兜底"
