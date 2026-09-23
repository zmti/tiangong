"""人工确认后"续跑"必须真流式(2026-09-20 "会话卡顿"根因锁定)。

现场:截图里确认卡片弹出来后点"批准",界面卡住几十分钟级不动,最后跳出红条
    「续跑超时,请稍后在运行记录面板重试」(desktop/src/ui/js/confirmation.js:332)。
取证(两个截图 + checkpoints/confirmations 库):
    - confirmations.db 里 ed883128/d05a628d 都是 status=approved,说明决定**已**写回;
    - checkpoints 里对应 run 都停在 status=running 且 interrupt.resume 已写入
      {"ed883128": {"approved": true}} → 续跑**确实启动了**,只是前端一直没内容;
    - 前端 5 分钟硬 abort(client 侧看门狗)命中后弹红条,而服务端还在继续跑。

根因:`chat_resume` 里 `isinstance(gen, types.AsyncGeneratorType)` 的 SSE 分支
**是死代码** —— `Agent.resume()` 无论 stream 传谁都走 `_run_state` → `graph.arun`
(非流式),返回的是跑完的 AgentResult。于是一次续跑(常含多轮 LLM + 工具,
5~20 分钟)期间前端收不到**任何一个字节**,只有打字指示器。

本文件锁定的行为:
    1. resume(stream=True) 返回异步生成器(SSE 分支可达);
    2. 事件**边跑边出**,不是跑完再一股脑给 —— 用"闸门 LLM"反证:
       续跑走到答案生成那一步才需要闸门,而闸门只在测试收到首个事件后才放行;
       若实现是批量的,首事件会等到闸门超时 → 测试失败(见反向对照用例);
    3. /v1/chat/resume 的响应是 text/event-stream,帧顺序 tool → token → done;
    4. 落库:工具消息逐条落,正文只落一次;interrupted 时不落半截正文;
    5. 客户端断开(只读一帧就走)后,后台续跑仍在跑并把结果落库 —— 这条正是
       "用户刷新/看门狗超时后不该丢结果"的保障。
"""
import asyncio
import json
import os
import types

import pytest

from app.core.agent import Agent
from app.core.llm import LLMClient
from app.runtime.checkpoint import CheckpointManager
from app.tools.registry import Tool, ToolRegistry, _build_params_model


def run(coro):
    return asyncio.run(coro)


class GatedLLM(LLMClient):
    """闸门 LLM:第 2 次及以后的 chat 调用会等 gate 放行。

    用来证明"事件是边跑边出":续跑要先执行完被批准的工具才会走到这次 chat,
    而 gate 只在测试**收到首个事件之后**才 set。批量(非流式)实现会在这里死等。
    """

    def __init__(self, responses, gate=None, gate_timeout=8.0):
        self._responses = list(responses)
        self._i = 0
        self.gate = gate
        self.gate_timeout = gate_timeout
        self.chat_calls = 0
        self.provider = "mock"
        self.model = "mock-model"
        self._custom = None  # 流式路径会读它(判自定义 provider)
        self._reasoning = ""
        self._finish_reason = ""

    async def chat(self, messages, temperature=0.7):
        self.chat_calls += 1
        if self.gate is not None and self.chat_calls > 1 and not self.gate.is_set():
            await asyncio.wait_for(self.gate.wait(), timeout=self.gate_timeout)
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp

    async def chat_stream(self, messages, temperature=0.7):
        """流式路径(agent 文本模式走这个):逐字吐,闸门在取答案前生效。"""
        text = await self.chat(messages, temperature)
        for ch in text:
            yield ch


def make_registry():
    reg = ToolRegistry()

    def add(a: int, b: int):
        return {"sum": a + b}

    model, schema = _build_params_model(add)
    reg.register(Tool(name="add", description="加法", func=add, params_model=model, json_schema=schema))
    return reg


@pytest.fixture(autouse=True)
def isolated_confirmation_store(tmp_path, monkeypatch):
    """把"确认请求"落到临时库。

    测试会真的走 interrupt 流(危险工具 add)→ 会往**真实**的
    data/confirmations.db 里写 pending 记录,而桌面端每 3s 轮询
    /v1/confirmations/pending 并把它们渲染成确认卡片 —— 跑一次测试就给用户
    弹一堆假卡片(2026-09-20 16:0x 实测污染了 9 条 pending)。这里换成临时库,
    测试不再触碰用户数据。
    """
    from app.tools import collaboration_tool as ct
    from app.tools.collaboration_tool import ConfirmationStore

    store = ConfirmationStore(db_path=os.path.join(str(tmp_path), "confirmations.db"))
    monkeypatch.setattr(ct, "_store", store)
    yield store


@pytest.fixture()
def interrupt_settings():
    """危险工具=add,开启 interrupt 式确认流(与真实 run_terminal 同一条机制)。"""
    from app.config import settings

    old = (settings.agent_confirm_interrupt, settings.agent_tool_confirm,
           settings.agent_dangerous_tools, settings.agent_confirm_enabled)
    settings.agent_confirm_interrupt = True
    settings.agent_tool_confirm = True
    settings.agent_confirm_enabled = False  # 只用 agent_tool_confirm 机制,便于精确控制
    settings.agent_dangerous_tools = "add"
    try:
        yield settings
    finally:
        (settings.agent_confirm_interrupt, settings.agent_tool_confirm,
         settings.agent_dangerous_tools, settings.agent_confirm_enabled) = old


def _stage_interrupted_run(tmp_path, db_name, responses):
    """跑一次被 interrupt 暂停的运行,返回 (run_id, db_path, confirm_args)。"""
    db_path = os.path.join(tmp_path, db_name)
    reg = make_registry()
    mock = GatedLLM(responses)
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    agent._checkpoint = CheckpointManager(db_path=db_path)
    result = run(agent.run("计算"))
    assert result.interrupted, "危险工具应先暂停等待人工确认"
    run_id = result.interrupt["run_id"]
    confirm_arg = result.interrupt["requests"][0]["confirmation_id"]
    return run_id, db_path, confirm_arg, mock


# ---------------- 1. SSE 分支可达:返回的是异步生成器 ----------------

def test_resume_stream_returns_async_generator(tmp_path, interrupt_settings):
    run_id, db_path, conf_id, mock = _stage_interrupted_run(
        tmp_path, "ck_stream_gen.db",
        [json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "结果是 3"],
    )
    reg = make_registry()
    # 复用同一个 mock:它的响应游标已前进,续跑直接拿到答案("结果是 3")
    agent2 = Agent(llm=mock, registry=reg, max_steps=5)
    agent2._checkpoint = CheckpointManager(db_path=db_path)

    async def go():
        gen = await agent2.resume(run_id, resume_value={conf_id: {"approved": True}}, stream=True)
        # 旧实现这里拿到的是已跑完的 AgentResult → chat_resume 的 SSE 分支是死代码
        assert isinstance(gen, types.AsyncGeneratorType), "resume(stream=True) 必须返回异步生成器"
        assert not asyncio.iscoroutine(gen)
        events = []
        async for evt in gen:
            events.append(evt)
        return events

    events = run(go())
    types_ = [e.get("type") for e in events]
    assert "tool" in types_, "被批准的工具应产出 tool 事件"
    assert "token" in types_, "续跑的答案应逐 token 产出"
    assert types_[-1] == "done"
    done = events[-1]
    assert any(e.get("type") == "tool" and e.get("name") == "add" for e in events)
    assert any(s.get("type") == "answer" and s.get("content") for s in done["steps"])
    assert done.get("run_id") == run_id

    # 非流式分支不受影响:stream=False 仍返回 AgentResult
    async def go_plain():
        agent3 = Agent(llm=mock, registry=reg, max_steps=5)
        agent3._checkpoint = CheckpointManager(db_path=db_path)
        return await agent3.resume(run_id, resume_value={conf_id: {"approved": True}})

    result = run(go_plain())
    assert not hasattr(result, "__anext__")
    assert result.answer == "结果是 3"


# ---------------- 2. 事件边跑边出(不是跑完再给) ----------------

def test_resume_stream_emits_events_before_run_completes(tmp_path, interrupt_settings):
    """首事件必须在"答案生成"之前送达 —— 否则界面就是长时间卡住。"""
    run_id, db_path, conf_id, mock = _stage_interrupted_run(
        tmp_path, "ck_progressive.db",
        [json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "结果是 3"],
    )
    reg = make_registry()

    async def go():
        gate = asyncio.Event()
        mock.gate = gate  # 共用 mock:续跑那次 chat 走闸门
        agent2 = Agent(llm=mock, registry=reg, max_steps=5)
        agent2._checkpoint = CheckpointManager(db_path=db_path)
        gen = await agent2.resume(run_id, resume_value={conf_id: {"approved": True}}, stream=True)
        first = await asyncio.wait_for(gen.__anext__(), timeout=10)
        # 走到这里说明:图跑到"执行完工具、要去问 LLM 要答案"之前就把事件送出来了。
        # 批量实现会卡在闸门上(闸门此刻还没放行)→ 上面的 wait_for 超时失败。
        gate.set()
        rest = []
        async for evt in gen:
            rest.append(evt)
        return first, rest

    first, rest = run(go())
    assert isinstance(first, dict)
    all_types = [first.get("type")] + [e.get("type") for e in rest]
    assert all_types[-1] == "done"
    assert any(t == "token" for t in all_types)
    # 首个事件必须是工具相关事件(tool_start/tool_call_start/tool),而不是答案的 token
    assert first.get("type") in ("tool_start", "tool_call_start", "tool"), first.get("type")


def test_reverse_control_batched_stream_stays_silent(tmp_path, interrupt_settings):
    """反向对照:旧实现下,闸门未放行前消费者拿到的事件数 = 0。

    证明上面那条闸门用例真的能区分"边跑边出"与"跑完再给"(而不是恒真)。
    """
    run_id, db_path, conf_id, mock = _stage_interrupted_run(
        tmp_path, "ck_reverse.db",
        [json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "结果是 3"],
    )
    reg = make_registry()

    async def go():
        gate = asyncio.Event()
        mock.gate = gate
        mock.gate_timeout = 30.0  # 不给它自己超时逃脱的机会
        agent2 = Agent(llm=mock, registry=reg, max_steps=5)
        agent2._checkpoint = CheckpointManager(db_path=db_path)
        collected = []

        # 旧行为等价物:先用非流式 graph.arun 把整轮跑完,再回放事件
        # (旧实现 resume(stream=True) 走的就是 _run_state → graph.arun)
        async def batched_stream_state(state, history, event_name="run_stream"):
            state.metadata["history"] = history
            state.metadata["stream"] = True
            graph = agent2._build_graph()
            _state, events = await graph.arun(state)  # ← 会卡在答案生成(闸门)上
            for evt in events:
                yield evt

        agent2._stream_state = batched_stream_state

        async def collect():
            gen = await agent2.resume(run_id, resume_value={conf_id: {"approved": True}}, stream=True)
            async for evt in gen:
                collected.append(evt)

        task = asyncio.create_task(collect())
        await asyncio.sleep(1.0)  # 旧实现:这 1 秒里一个事件也送不出来
        seen = len(collected)
        gate.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return seen

    assert run(go()) == 0, "闸门未放行前不应有任何事件(旧实现正是这样卡住界面)"


# ---------------- 3/4/5. HTTP 层:SSE 分帧 + 落库 + 断连不丢结果 ----------------

class _FakeStore:
    def __init__(self):
        self.calls = []

    async def add_message(self, session_id, role, kind="text", content=None, meta=None, **kw):
        self.calls.append({"session": session_id, "role": role, "content": content, "meta": meta})
        return {"messages": [{"id": "m%d" % len(self.calls)}]}

    async def update_message(self, *a, **kw):
        self.calls.append({"role": "update"})
        return True


def _fake_agent_stream(events, delay=0.0):
    """构造一个可注入 chat_resume 的续跑事件流。"""
    async def _gen():
        for evt in events:
            if delay:
                await asyncio.sleep(delay)
            yield evt
    return _gen()


def _drive_resume_stream(monkeypatch, events, frames_to_read=None):
    """跑 chat_resume 的流式分支,返回 (response, 已读帧, store, running_tasks)。"""
    from app.api import chat as chat_api

    store = _FakeStore()
    monkeypatch.setattr(chat_api, "_store", lambda: store)
    fake_agent = __import__("types").SimpleNamespace()

    async def _resume(*_a, **_kw):
        return _fake_agent_stream(events)

    fake_agent.resume = _resume
    monkeypatch.setattr(chat_api, "_build_agent", lambda _req: fake_agent)

    req = chat_api.ChatResumeRequest(run_id="run-1", session_id="s1",
                                     resume_value={"c1": {"approved": True}}, stream=True)

    async def go():
        resp = await chat_api.chat_resume(req)
        frames = []
        n = 0
        async for chunk in resp.body_iterator:
            frames.append(chunk)
            n += 1
            if frames_to_read is not None and n >= frames_to_read:
                break  # 模拟客户端断开
        # 给后台 pump 一点时间收尾
        for _ in range(60):
            if chat_api._RUNNING_TASKS.get("s1") is None:
                break
            await asyncio.sleep(0.05)
        return resp, frames

    return run(go()) + (store,)


TOOL_EVT = {"type": "tool", "name": "run_terminal", "arguments": '{"command":"echo hi"}',
            "result": "hi"}
TOKEN_EVTS = [{"type": "token", "content": c} for c in ("结果", "是", " 3")]
DONE_EVT = {"type": "done", "model": "fake", "steps": [], "run_id": "run-1"}


def test_chat_resume_stream_returns_sse_frames(monkeypatch):
    from app.api import chat as chat_api

    resp, frames, store = _drive_resume_stream(monkeypatch, [TOOL_EVT] + TOKEN_EVTS + [DONE_EVT])
    assert resp.media_type == "text/event-stream"
    assert frames[0].startswith("data: "), "首帧就是事件,不是等整轮跑完"
    payloads = [json.loads(f[6:].strip()) for f in frames if f.startswith("data: ")]
    types = [p.get("type") for p in payloads]
    assert types[0] == "tool"
    assert "token" in types
    assert types[-1] == "done"
    # 工具消息落库、正文只落一次
    roles = [c["role"] for c in store.calls]
    assert roles.count("tool") == 1
    assert roles.count("assistant") == 1
    assert store.calls[[c["role"] for c in store.calls].index("assistant")]["content"] == "结果是 3"
    # 结束后台任务表清空,避免"停止生成"打到已结束的任务
    assert chat_api._RUNNING_TASKS.get("s1") is None


def test_chat_resume_stream_interrupted_writes_no_partial_answer(monkeypatch):
    from app.api import chat as chat_api

    done_intr = {"type": "done", "model": "fake", "steps": [], "run_id": "run-1",
                 "interrupt": {"type": "confirmation", "run_id": "run-1",
                               "requests": [{"confirmation_id": "c2", "tool": "run_terminal"}]}}
    _resp, frames, store = _drive_resume_stream(monkeypatch, [TOOL_EVT] + TOKEN_EVTS[:1] + [done_intr])
    payloads = [json.loads(f[6:].strip()) for f in frames if f.startswith("data: ")]
    assert payloads[-1]["interrupt"]["requests"][0]["confirmation_id"] == "c2"
    roles = [c["role"] for c in store.calls]
    assert "assistant" not in roles, "再次暂停时本轮没有最终答案,不能落半截正文"
    assert chat_api._RUNNING_TASKS.get("s1") is None


def test_chat_resume_stream_survives_client_disconnect(monkeypatch):
    """客户端只读一帧就走:后台续跑继续跑完并落库(刷新后能看到结果)。"""
    from app.api import chat as chat_api

    events = [TOOL_EVT] + TOKEN_EVTS + [DONE_EVT]
    _resp, frames, store = _drive_resume_stream(monkeypatch, events, frames_to_read=1)
    assert len(frames) == 1
    roles = [c["role"] for c in store.calls]
    assert roles.count("assistant") == 1, "断连后仍应把正文落库"
    assert chat_api._RUNNING_TASKS.get("s1") is None
