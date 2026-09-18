"""流式卡死修复测试:验证 LLM 流空闲超时/总时长上限 + 正常流不受影响。

复现场景:
1. provider 建连后长时间不吐字 -> 旧实现 `async for chunk in resp` 永久挂起,
   前端表现为"发送后一直卡"且无错误提示。修复后 `LLMClient._stream_with_idle_timeout`
   在流内停顿超过 agent_llm_stream_idle_timeout 时抛 LLMError(含可操作提示),
   事件流经 _bg_agent_run 的 error 分支送达前端。
2. 正常流不受影响:idle_timeout<=0 时透传行为与旧版一致。
"""
import asyncio

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
