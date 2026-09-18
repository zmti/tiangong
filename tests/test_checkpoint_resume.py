"""Agent checkpoint 断点续跑测试。"""
import asyncio
import json
import os

from app.core.agent import Agent
from app.core.llm import LLMClient
from app.runtime.checkpoint import CheckpointManager
from app.runtime.state import AgentState
from app.tools.registry import ToolRegistry, _build_params_model, Tool


def run(coro):
    return asyncio.run(coro)


class CountLLM(LLMClient):
    """计数 LLM:记录 chat 调用次数。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.provider = "mock"
        self.model = "mock-model"

    async def chat(self, messages, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


def make_registry():
    reg = ToolRegistry()

    def add(a: int, b: int):
        return {"sum": a + b}

    model, schema = _build_params_model(add)
    reg.register(Tool(name="add", description="加法", func=add, params_model=model, json_schema=schema))
    return reg


def test_agent_run_creates_checkpoint(tmp_path):
    reg = make_registry()
    mock = CountLLM([json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "结果是 3"])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    agent._checkpoint = CheckpointManager(db_path=os.path.join(tmp_path, "ck.db"))

    result = run(agent.run("计算"))
    assert result.answer == "结果是 3"
    cps = run(agent.checkpoints())
    assert len(cps) >= 1
    assert cps[0]["status"] == "done"


def test_agent_resume_continues_from_node(tmp_path):
    """从中间状态恢复:prepare 已完成,从 llm 节点继续,不重跑 prepare。"""
    reg = make_registry()
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "ck2.db"))

    # 手工构造一个"中断"的运行现场:prepare 已完成,messages 已构建,node=llm
    state = AgentState(session_id="s1", user_input="计算")
    state.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "计算"},
    ]
    state.node = "llm"
    state.metadata["history"] = []
    run(cm.save(state))
    assert state.run_id

    mock = CountLLM(["最终答案 42"])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    agent._checkpoint = cm

    result = run(agent.resume(state.run_id))
    assert result.answer == "最终答案 42"
    # 只调用了一次 LLM(llm 节点),prepare 没有被重跑
    assert mock._i == 1


def test_agent_resume_missing_run_raises(tmp_path):
    reg = make_registry()
    mock = CountLLM(["ok"])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    agent._checkpoint = CheckpointManager(db_path=os.path.join(tmp_path, "ck3.db"))
    try:
        run(agent.resume("no-such-run"))
        assert False, "应当抛出异常"
    except RuntimeError as e:
        assert "未找到运行记录" in str(e)


def test_agent_resume_replays_latest_checkpoint(tmp_path):
    """多次 save 后 load 取最新快照。"""
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "ck4.db"))
    s1 = AgentState(session_id="a", user_input="q1")
    s1.node = "llm"
    run(cm.save(s1))
    s1.answer = "中间"
    s1.node = "tools"
    run(cm.save(s1))
    loaded = run(cm.load(s1.run_id))
    assert loaded.answer == "中间"
    assert loaded.node == "tools"


def test_agent_interrupt_confirmation_pauses_then_resumes(tmp_path):
    """interrupt 式确认流:危险工具先暂停(不执行),批准后 resume 从原节点继续。"""
    from app.config import settings

    old_interrupt = settings.agent_confirm_interrupt
    old_confirm = settings.agent_tool_confirm
    old_dangerous = settings.agent_dangerous_tools
    old_confirm_enabled = settings.agent_confirm_enabled
    try:
        settings.agent_confirm_interrupt = True
        settings.agent_tool_confirm = True
        settings.agent_confirm_enabled = False  # 只用 agent_tool_confirm 机制,便于精确控制
        settings.agent_dangerous_tools = "add"

        reg = make_registry()
        mock = CountLLM([json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "结果是 3"])
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        agent._checkpoint = CheckpointManager(db_path=os.path.join(tmp_path, "ck_interrupt.db"))

        # 第一次运行:tools 节点预扫描到 add 需要确认 -> 中断,不执行任何工具
        result = run(agent.run("计算"))
        assert result.interrupted
        assert result.interrupt["type"] == "confirmation"
        assert any(r["tool"] == "add" for r in result.interrupt["requests"])
        # 工具未执行
        assert not any(s["type"] == "tool" for s in result.steps)
        # checkpoint 存在且处于 interrupted
        cps = run(agent.checkpoints(status="interrupted"))
        assert cps, "应存在 interrupted 状态的 checkpoint"
        run_id = result.interrupt["run_id"]

        # 批准后恢复:add 放行执行,最终给出答案
        agent2 = Agent(llm=mock, registry=reg, max_steps=5)
        agent2._checkpoint = CheckpointManager(db_path=os.path.join(tmp_path, "ck_interrupt.db"))
        result2 = run(agent2.resume(run_id, resume_value=["add"]))
        assert not result2.interrupted
        assert result2.answer == "结果是 3"
        assert any(s["type"] == "tool" and s["tool"] == "add" for s in result2.steps)
    finally:
        settings.agent_confirm_interrupt = old_interrupt
        settings.agent_tool_confirm = old_confirm
        settings.agent_dangerous_tools = old_dangerous
        settings.agent_confirm_enabled = old_confirm_enabled


def test_agent_interrupt_confirmation_reject_blocks_tool(tmp_path):
    """拒绝后恢复:工具被硬拦截,不执行。"""
    from app.config import settings

    old_interrupt = settings.agent_confirm_interrupt
    old_confirm = settings.agent_tool_confirm
    old_dangerous = settings.agent_dangerous_tools
    old_confirm_enabled = settings.agent_confirm_enabled
    try:
        settings.agent_confirm_interrupt = True
        settings.agent_tool_confirm = True
        settings.agent_confirm_enabled = False
        settings.agent_dangerous_tools = "add"

        reg = make_registry()
        mock = CountLLM([json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "完成"])
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        agent._checkpoint = CheckpointManager(db_path=os.path.join(tmp_path, "ck_reject.db"))

        result = run(agent.run("计算"))
        assert result.interrupted
        run_id = result.interrupt["run_id"]

        # 不批准 -> add 被拦截,返回需确认错误,模型给出兜底回答
        agent2 = Agent(llm=mock, registry=reg, max_steps=5)
        agent2._checkpoint = CheckpointManager(db_path=os.path.join(tmp_path, "ck_reject.db"))
        result2 = run(agent2.resume(run_id, resume_value=[]))
        assert not result2.interrupted
        blocked = [s for s in result2.steps if s["type"] == "tool" and "需要用户确认" in s["result"]]
        assert len(blocked) == 1
    finally:
        settings.agent_confirm_interrupt = old_interrupt
        settings.agent_tool_confirm = old_confirm
        settings.agent_dangerous_tools = old_dangerous
        settings.agent_confirm_enabled = old_confirm_enabled
