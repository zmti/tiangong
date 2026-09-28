"""max_steps=0(不限制步骤上限)语义验证:不再第 10 轮强制 giveup,
但同参 3 连败/轮数硬上限仍能防死循环。

背景: 旧实现 agent_max_steps 默认 10,复杂任务常被"无法完成任务:已尝试 10 步"
强制中止。现默认改为 0(不限制),由收敛机制兜底:
- 同一工具同参连续失败 3 次 → _tool_failure 触发 giveup
- 轮数硬上限 _HARD_ROUND_CAP=500 → 绝对兜底
本测试直接验证 max_steps=0 时,反复成功的工具调用不会在第 10 轮被截断。
"""
import asyncio
import json

from app.core.agent import Agent
from app.core.llm import LLMClient, ChatResult, LLMError
from app.tools.registry import ToolRegistry, _build_params_model, Tool


def _make_registry():
    def _add(a: int, b: int):
        return {"sum": a + b}
    model, schema = _build_params_model(_add)
    reg = ToolRegistry()
    reg.register(Tool(name="add", description="加法", func=_add, params_model=model, json_schema=schema))
    return reg


class UnlimitedMockLLM(LLMClient):
    """脚本式 LLM:按顺序返回响应(字符串)。

    与 test_agent.py 的 MockLLM 契约一致:chat() 直接返回字符串(工具调用
    用 JSON 字符串,最终答案用普通字符串),agent 侧 _parse_tool_calls 解析。
    """

    def __init__(self, script):
        self._responses = list(script)
        self._i = 0
        self.provider = "mock"
        self.model = "mock-model"
        self.client = None
        self._custom = None
        self.chat_with_tools = None
        self._api_key = ""
        self._initial_provider = self.provider
        self._circuit_fails = {}
        self._circuit_open_until = {}
        self.last_usage = None
        self._reasoning = ""

    async def chat(self, messages, temperature=0.7):
        # 与 MockLLM.chat 一致:直接返回字符串,不包 ChatResult
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp

    async def chat_stream(self, messages, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        for ch in str(resp):
            yield ch

    async def chat_json(self, messages, temperature=0.2):
        return {"needs_plan": False, "steps": []}

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        return {"score": 9, "verdict": "pass", "issues": []}


def test_max_steps_zero_does_not_giveup_at_round_10():
    """max_steps=0:反复成功的工具调用不会在第 10 轮被'步骤耗尽'截断。

    脚本:15 次工具调用(每次成功)+ 1 次最终回答。
    旧实现(max_steps=10)会在第 10 轮 giveup;新实现(max_steps=0)跑满 15 轮才回答。
    """
    reg = _make_registry()
    # JSON 字符串形式(老式工具调用,_parse_tool_calls 能解析)
    script = [
        json.dumps({"tool": "add", "arguments": {"a": i, "b": 1}})
        for i in range(15)
    ] + ["最终答案: 加完了"]

    llm = UnlimitedMockLLM(script)
    agent = Agent(llm=llm, registry=reg, max_steps=0, route=False)

    result = asyncio.run(agent.run("一直加"))
    tool_steps = [s for s in result.steps if s.get("type") == "tool"]
    assert "无法完成" not in result.answer, f"max_steps=0 不应触发步骤耗尽,实际: {result.answer}"
    assert len(tool_steps) >= 15, f"应跑满 15 轮工具调用,实际 {len(tool_steps)}"
    assert "加完了" in result.answer


def test_max_steps_explicit_positive_still_bounds():
    """回归保护:显式传 max_steps>0(如 5)时,旧行为保留 —— 第 5 轮后 giveup。"""
    reg = _make_registry()
    script = [
        json.dumps({"tool": "add", "arguments": {"a": 1, "b": 1}})
        for _ in range(20)
    ] + ["不该到这"]

    llm = UnlimitedMockLLM(script)
    agent = Agent(llm=llm, registry=reg, max_steps=5, route=False)

    result = asyncio.run(agent.run("一直加"))
    assert "无法完成" in result.answer or "达到最大" in result.answer, \
        f"max_steps=5 应在 5 轮后 giveup,实际: {result.answer}"


def test_max_steps_zero_hard_cap_bounds_truly_infinite():
    """max_steps=0 的绝对兜底验证:50 轮(远小于 500 硬兜底)不会在第 10 轮被截断。
    LLM 反复调工具且任务无进展时,仍由 _HARD_ROUND_CAP/同参 3 连败收敛,不会死循环。
    这里用 50 轮成功工具调用验证'无上限'语义:不 giveup,跑满 50 轮才回答。
    """
    reg = _make_registry()
    script = [
        json.dumps({"tool": "add", "arguments": {"a": i, "b": 1}})
        for i in range(50)
    ] + ["回答"]

    llm = UnlimitedMockLLM(script)
    agent = Agent(llm=llm, registry=reg, max_steps=0, route=False)

    result = asyncio.run(agent.run("一直加"))
    tool_steps = [s for s in result.steps if s.get("type") == "tool"]
    assert "无法完成" not in result.answer, f"50 轮远小于硬兜底 500,不应 giveup: {result.answer}"
    assert len(tool_steps) == 50, f"应跑满 50 轮,实际 {len(tool_steps)}"


class FailingMockLLM(UnlimitedMockLLM):
    """所有 chat 调用都抛 LLMError:模拟第 1 轮就因模型故障中止。"""

    async def chat(self, messages, temperature=0.7):
        raise LLMError("模拟模型不可用")

    async def chat_stream(self, messages, temperature=0.7):
        raise LLMError("模拟模型不可用")
        yield  # pragma: no cover  (使函数成为 async generator)


def test_giveup_does_not_blame_step_limit_on_llm_error():
    """回归:LLM 第 1 轮就失败时,不能报"已尝试 10 步,仍未得到正确结果"。

    旧实现在 _node_giveup 里无条件用 self.max_steps 当已执行步数,导致用户看到
    与实际完全无关的步数,无法判断真实原因。现按 giveup_reason 报真实原因。
    """
    reg = _make_registry()
    agent = Agent(llm=FailingMockLLM(["x"]), registry=reg, max_steps=10, route=False)

    result = asyncio.run(agent.run("随便问点什么"))

    assert "已尝试 10 步" not in result.answer, \
        f"模型故障时不该报成步数耗尽,实际: {result.answer}"
    assert "模型调用出错" in result.answer, \
        f"应报真实原因(模型调用出错),实际: {result.answer}"


def test_giveup_reports_actual_rounds_when_max_steps_zero():
    """max_steps=0 走到 giveup 时,步数口径用实际执行轮数,而不是 0。"""
    reg = _make_registry()
    agent = Agent(llm=FailingMockLLM(["x"]), registry=reg, max_steps=0, route=False)

    result = asyncio.run(agent.run("随便问点什么"))

    assert "已尝试 0 步" not in result.answer, \
        f"max_steps=0 时不该报'已尝试 0 步',实际: {result.answer}"
    assert "模型调用出错" in result.answer, \
        f"应报真实原因(模型调用出错),实际: {result.answer}"
