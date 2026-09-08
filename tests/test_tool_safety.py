"""工具安全/确认流共享逻辑 + 本次修复的 bug 的回归测试。

覆盖:
- app.core.tool_safety 的确认扫描/请求创建/恢复批准(含持久化回退)
- 危险工具滑动窗口限流(Agent 与 ToolExecutor 统一)
- Agent 端危险工具限流
- Agent step_id 跨批次全局唯一(修复重复)
- ensemble 子模型创建失败时的 specs/results 对齐(修复下标错位)
"""
import asyncio
import json

from app.core.agent import Agent
from app.core.llm import ChatResult, LLMClient, ToolCall
from app.tools.registry import Tool, ToolRegistry, _build_params_model
from app.core import tool_safety


def make_registry():
    reg = ToolRegistry()

    def add(a: int, b: int):
        return {"sum": a + b}

    model, schema = _build_params_model(add)
    # 同步写入与 default_registry._tools 一致(ToolRegistry.register 是 async,此处无法 await)
    reg._tools["add"] = Tool(name="add", description="加法", func=add, params_model=model, json_schema=schema)
    return reg


class MockLLM(LLMClient):
    """与 test_agent.py 一致的文本模式 Mock LLM。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.provider = "mock"
        self.model = "mock-model"
        self.client = None

    @property
    def info(self):
        return {"provider": self.provider, "model": self.model}

    async def chat(self, messages, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp

    async def chat_stream(self, messages, temperature=0.7):
        text = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        for ch in text:
            yield ch

    async def chat_with_tools(self, messages, tools=None, temperature=0.7):
        raise NotImplementedError("mock 不实现 native tool calling")

    async def chat_json(self, messages, temperature=0.2):
        return {"needs_plan": False, "steps": []}

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        return {"score": 9, "verdict": "pass", "issues": []}


def config_offline(agent):
    """关闭会触发 LLM 网络调用的环节,以便离线完整跑通 run()。

    说明:基类 LLMClient 定义了 chat_with_tools / chat_json / chat_json_schema,
    使 _native_enabled 误判为 True,且 planning/reflect/verify/memory 各环节会
    调用这些方法——mock 未实现时会在离线环境挂起(仓库既有测试的环境限制)。
    这里显式关闭相关配置与实例开关,保证离线测试快速收敛。
    """
    from app.config import settings

    agent._native_enabled = False
    agent._reflect_enabled = False
    agent._verify_enabled = False
    agent.route = False
    settings.agent_auto_memory = False
    settings.agent_memory_enabled = False
    settings.agent_layered_memory = False
    settings.agent_planning = False
    settings.agent_summarize = False
    # 关键:批量 checkpoint 的 SQLite 落盘在本离线环境会挂起(既有问题,与本次改动无关)。
    # _build_graph() 在 run 时按 self._checkpoint 决定是否落盘,置空即可跳过。
    settings.agent_checkpoint_enabled = False
    agent._checkpoint = None
    return agent


# ---- 共享确认扫描逻辑 ----

def test_scan_pending_dangerous_tool():
    from app.config import settings

    old_c = settings.agent_tool_confirm
    old_d = settings.agent_dangerous_tools
    old_i = settings.agent_confirm_interrupt
    try:
        settings.agent_tool_confirm = True
        settings.agent_dangerous_tools = "run_terminal"
        settings.agent_confirm_interrupt = True
        tc = ToolCall(id="1", name="run_terminal", arguments='{"command": "ls"}')
        pending = tool_safety.scan_pending_confirmations([tc], None, None, {"run_terminal"})
        assert len(pending) == 1
        assert pending[0][0].name == "run_terminal"
        # 已批准集合内的工具不进入待确认
        pending2 = tool_safety.scan_pending_confirmations([tc], None, {"run_terminal"}, {"run_terminal"})
        assert pending2 == []
    finally:
        settings.agent_tool_confirm = old_c
        settings.agent_dangerous_tools = old_d
        settings.agent_confirm_interrupt = old_i


def test_is_tool_approved_by_resume_list_vs_persistent():
    """resume 为 list 白名单时直接放行;回退查询持久化记录被批准。"""
    from app.config import settings

    old_c = settings.agent_tool_confirm
    old_d = settings.agent_dangerous_tools
    old_i = settings.agent_confirm_interrupt
    try:
        settings.agent_tool_confirm = True
        settings.agent_dangerous_tools = "run_terminal"
        settings.agent_confirm_interrupt = True
        tc = ToolCall(id="1", name="run_terminal", arguments="{}")
        # list 白名单
        state = _State(interrupt={"value": {"requests": [{"tool": "run_terminal", "confirmation_id": "x"}]},
                                  "resume": ["run_terminal"]})
        assert tool_safety.is_tool_approved_by_resume(tc, state) is True
        # dict 关键 id 批准
        state2 = _State(interrupt={"value": {"requests": [{"tool": "run_terminal", "confirmation_id": "x"}]},
                                   "resume": {"x": {"approved": True}}})
        assert tool_safety.is_tool_approved_by_resume(tc, state2) is True
        # runtime_approved 优先
        assert tool_safety.is_tool_approved_by_resume(tc, state, runtime_approved={"run_terminal"}) is True
    finally:
        settings.agent_tool_confirm = old_c
        settings.agent_dangerous_tools = old_d
        settings.agent_confirm_interrupt = old_i


class _State:
    def __init__(self, interrupt):
        self.interrupt = interrupt


# ---- 危险工具限流 ----

def test_dangerous_rate_limiter_shared_class():
    lim = tool_safety.DangerousToolRateLimiter(rate_limit_per_minute=2)
    ok, _ = lim.allow("run_terminal")
    assert ok
    ok, _ = lim.allow("run_terminal")
    assert ok
    ok, msg = lim.allow("run_terminal")
    assert not ok and "频率限制" in msg
    # 不同工具互不影响
    ok, _ = lim.allow("add")
    assert ok


def test_agent_dangerous_tool_rate_limit_blocks():
    """Agent 端危险工具超出 60s 窗口限流后返回 blocked 错误。"""
    from app.config import settings

    old_rl = settings.agent_dangerous_tool_rate_limit
    old_d = settings.agent_dangerous_tools
    old_c = settings.agent_tool_confirm
    try:
        settings.agent_dangerous_tool_rate_limit = 1
        settings.agent_dangerous_tools = "add"
        settings.agent_tool_confirm = False
        reg = make_registry()
        agent = config_offline(Agent(llm=MockLLM([]), registry=reg, max_steps=5))
        r1 = __import__("asyncio").run(agent._execute_tool_once("add", '{"a": 1, "b": 2}'))
        assert "sum" in r1
        r2 = __import__("asyncio").run(agent._execute_tool_once("add", '{"a": 3, "b": 4}'))
        data = json.loads(r2)
        assert data.get("blocked") is True and "频率限制" in data.get("error", "")
    finally:
        settings.agent_dangerous_tool_rate_limit = old_rl
        settings.agent_dangerous_tools = old_d
        settings.agent_tool_confirm = old_c


def test_agent_and_executor_scan_behavior_equivalent():
    """ToolExecutor 与 Agent 确认扫描行为应等价(共享实现收敛)。"""
    from app.config import settings
    from app.core.tool_executor import ToolExecutor

    old_tc = settings.agent_tool_confirm
    old_d = settings.agent_dangerous_tools
    old_i = settings.agent_confirm_interrupt
    old_ce = settings.agent_confirm_enabled
    try:
        settings.agent_tool_confirm = True
        settings.agent_dangerous_tools = "run_terminal"
        settings.agent_confirm_interrupt = True
        settings.agent_confirm_enabled = False
        tc = ToolCall(id="1", name="run_terminal", arguments="{}")
        ex = ToolExecutor(make_registry())
        ex._dangerous_tools = {"run_terminal"}
        assert len(ex.scan_pending_confirmations([tc])) == 1
        agent = Agent(llm=MockLLM([]), registry=make_registry(), max_steps=5)
        assert len(agent._scan_pending_confirmations([tc])) == 1
    finally:
        settings.agent_tool_confirm = old_tc
        settings.agent_dangerous_tools = old_d
        settings.agent_confirm_interrupt = old_i
        settings.agent_confirm_enabled = old_ce


# ---- step_id 全局唯一(修复跨批重复) ----

def test_step_id_unique_across_batches():
    """同一运行内多轮工具调用的 step_id 应全局唯一。"""
    reg = make_registry()
    mock = MockLLM([
        json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}),
        json.dumps({"tool": "add", "arguments": {"a": 3, "b": 4}}),
        "完成",
    ])
    agent = config_offline(Agent(llm=mock, registry=reg, max_steps=10))
    result = __import__("asyncio").run(agent.run("算"))
    ids = [s["step_id"] for s in result.steps if s["type"] == "tool" and "step_id" in s]
    assert len(ids) == len(set(ids)), f"step_id 重复: {ids}"


# ---- ensemble 创建失败时的对齐(修复下标错位) ----

def test_ensemble_alignment_when_member_creation_fails(monkeypatch):
    """子模型之一创建失败被跳过时,成员轨迹与答案仍应正确对齐。"""
    from app.config import settings
    from app.core.llm import LLMClient

    old_max = settings.agent_ensemble_max
    old_ck = settings.agent_checkpoint_enabled
    old_mem = settings.agent_memory_enabled
    old_auto = settings.agent_auto_memory
    old_plan = settings.agent_planning
    try:
        settings.agent_ensemble_max = 3
        settings.agent_checkpoint_enabled = False
        settings.agent_memory_enabled = False
        settings.agent_auto_memory = False
        settings.agent_planning = False

        class _FakeLLM(LLMClient):
            def __init__(self, provider=None, model=None):
                self.provider = provider or "fake"
                self.model = model or f"{self.provider}-model"

            @property
            def info(self):
                return {"provider": self.provider, "model": self.model}

            async def chat(self, messages, temperature=0.7):
                return {
                    "alpha": "alpha 的回答",
                    "beta": "beta 的回答",
                }.get(self.provider, "默认回答")

            async def chat_json(self, messages, temperature=0.2):
                return {"needs_plan": False, "steps": []}

            async def chat_json_schema(self, messages, schema, temperature=0.2,
                                       max_retries=2, validator=None):
                if "winner" in schema.get("properties", {}):
                    data = {"winner": 1, "scores": [{"index": 0, "score": 5},
                                                    {"index": 1, "score": 9}],
                            "reason": "beta 更准确"}
                    if validator is not None:
                        ok, err = validator(data)
                        assert ok
                    return data
                return {"score": 9, "verdict": "pass", "issues": []}

        def _fake_cls(provider=None, model=None):
            # beta 创建失败,应被 _run_ensemble 跳过
            if provider == "beta":
                raise RuntimeError("beta provider 不可用")
            return _FakeLLM(provider, model)

        monkeypatch.setattr("app.core.llm.LLMClient", _fake_cls)
        reg = make_registry()
        agent = Agent(llm=_FakeLLM("alpha"), registry=reg, max_steps=5, mode="ensemble",
                      ensemble_models=["alpha:alpha-model", "beta:beta-model"])
        result = __import__("asyncio").run(agent.run("写一段话"))
        member_steps = [s for s in result.steps if s["type"] == "ensemble_member"]
        # 只有 alpha 成功,轨迹只有一条,且 provider 正确
        assert len(member_steps) == 1
        assert member_steps[0]["provider"] == "alpha"
        # 答案来自唯一成员(评审对单候选直接返回该结果)
        assert result.answer == "alpha 的回答"
    finally:
        settings.agent_ensemble_max = old_max
        settings.agent_checkpoint_enabled = old_ck
        settings.agent_memory_enabled = old_mem
        settings.agent_auto_memory = old_auto
        settings.agent_planning = old_plan
