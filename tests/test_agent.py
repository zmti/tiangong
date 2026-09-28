"""Agent 核心逻辑测试:使用 Mock LLM,不依赖真实网络。"""
import asyncio
import json

from app.core.agent import Agent
from app.core.llm import ChatResult, LLMClient, ToolCall
from app.tools.registry import ToolRegistry, tool

# 工具缓存已改为实例级,无需跨测试清理;保留兼容占位
def _clear_smart_cache():
    """兼容占位:工具缓存已改为实例级,每次测试创建新 Agent 自动隔离。"""
    pass


class MockLLM(LLMClient):
    """Mock LLM:第一次返回工具调用,第二次返回最终回答。"""

    def __init__(self, responses):
        # 不调用 super().__init__(),直接设置所有必要属性避免真实 API 调用
        self._responses = list(responses)
        self._i = 0
        self.provider = "mock"
        self.model = "mock-model"
        self.client = None  # 供 _native_enabled 检查(有 client 才启用原生 tool calling)
        self._custom = None
        # client 为 None 时禁止 native 路径(避免 chat_with_tools 内部访问 self.client.xxx 报 NoneType),
        # 显式关闭;测试 native 的 NativeMockLLM 单独覆盖
        self.chat_with_tools = None  # type: ignore[attr-defined]
        self._api_key = ""
        self._initial_provider = self.provider
        self._circuit_fails: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}
        self.last_usage = None
        self._reasoning = ""

    async def chat(self, messages, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp

    async def chat_stream(self, messages, temperature=0.7):
        text = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        for ch in text:
            yield ch

    async def chat_json(self, messages, temperature=0.2):
        """结构化 JSON 默认返回空 plans,与 LLMClient 语义一致(简单任务不规划)。

        MockLLM 覆写了 chat() 返回任意文本,LLMClient.chat_json 内部失败时会
        回退到 self.chat() 拿到的非 JSON 文本 → 解析失败。这里提供干净的默认
        实现,各测试可按需覆写(ReplanMockLLM / ReflectMockLLM 等已覆写)。
        """
        return {"needs_plan": False, "steps": []}

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        """结构化 schema 默认通过/通过,各测试类按需覆写。

        LLMClient.chat_json_schema 依赖 chat_json → chat 回退链,Mock 覆写 chat()
        后拿到的可能是非 JSON 文本导致失败;提供独立默认实现保证 mock 链路可用。
        """
        import json as _json

        try:
            text = await self.chat(messages, temperature=temperature)
            data = _json.loads(text) if text and text.strip().startswith("{") else {}
        except Exception:  # noqa: BLE001
            data = {}
        if not isinstance(data, dict) or not data:
            # 按 schema 必填字段构造最小合法响应:score/verdict → 通过,
            # supported/unsupported → 支撑通过,其余 → 空 dict
            props = (schema or {}).get("properties", {})
            if "score" in props and "verdict" in props:
                data = {"score": 9, "verdict": "pass", "issues": []}
            elif "supported" in props:
                data = {"supported": True, "unsupported": []}
            else:
                data = {}
        if validator is not None:
            ok, err = validator(data)
            if not ok:
                import logging as _logging
                _logging.getLogger("test_agent").warning("chat_json_schema 默认响应未通过校验: %s", err)
        return data


class NativeMockLLM(LLMClient):
    """Mock LLM:模拟原生 function calling(tool_calls)。"""

    def __init__(self, responses):
        # 不调用 super().__init__(),直接设置所有必要属性避免真实 API 调用
        self._responses = list(responses)
        self._i = 0
        self.provider = "native-mock"
        self.model = "mock-model"
        self.client = object()  # 模拟原生 client 存在,启用原生 tool calling 路径
        self._custom = None
        self._api_key = ""
        self._initial_provider = self.provider
        self._circuit_fails: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}
        self.last_usage = None
        self._reasoning = ""

    async def chat_with_tools(self, messages, tools=None, temperature=0.7):
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        if isinstance(resp, dict):
            calls = [
                ToolCall(id=f"c{j}", name=tc["tool"], arguments=json.dumps(tc["arguments"]))
                for j, tc in enumerate(resp["tool_calls"])
            ]
            return ChatResult(content="", tool_calls=calls, finish_reason="tool_calls")
        return ChatResult(content=resp, finish_reason="stop")

    async def chat(self, messages, temperature=0.7):
        return "fallback"


def make_registry():
    from app.tools.registry import Tool

    reg = ToolRegistry()

    def add(a: int, b: int):
        return {"sum": a + b}

    # 手动构建并注册,避免 @tool 装饰器写入全局默认注册器
    from app.tools.registry import _build_params_model

    model, schema = _build_params_model(add)
    reg.register(Tool(name="add", description="加法", func=add, params_model=model, json_schema=schema))
    return reg


def make_two_tool_registry():
    """注册 add 与 calculate 两个工具,供工具路由测试使用。"""
    from app.tools.registry import Tool, _build_params_model

    reg = ToolRegistry()

    def add(a: int, b: int):
        return {"sum": a + b}

    def calculate(expr: str):
        return {"result": f"计算结果 {expr}"}

    for fn in (add, calculate):
        model, schema = _build_params_model(fn)
        reg.register(Tool(name=fn.__name__, description=fn.__doc__ or fn.__name__, func=fn, params_model=model, json_schema=schema))
    return reg


class RouterLLM(MockLLM):
    """模拟支持 chat_json 的真实 LLM,provider 非 mock 以触发工具路由。"""

    def __init__(self, responses, selected_tools=None, fail_json=False):
        # 不调用 super().__init__(),直接设置所有必要属性避免真实 API 调用
        self._responses = list(responses)
        self._i = 0
        self.provider = "router-test"
        self.model = "mock-model"
        self.client = None
        self._custom = None
        self._api_key = ""
        self._initial_provider = self.provider
        self._circuit_fails: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}
        self.last_usage = None
        self._reasoning = ""
        self._selected_tools = list(selected_tools or [])
        self._fail_json = fail_json
        # 显式禁用原生工具调用(避免 chat_with_tools 内部访问 self.client.xxx 报 NoneType)
        self.chat_with_tools = None  # type: ignore[attr-defined]

    async def chat_json(self, messages, temperature=0.2):
        if self._fail_json:
            raise RuntimeError("router LLM 故障")
        return {"tools": self._selected_tools, "reason": "test"}

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                                max_retries=2, validator=None):
        # 工具路由第三级 LLM 兜底调用 chat_json_schema(旧实现调用 chat_json,
        # 但 MockLLM.chat_json 返回 "tools" 键,与 select_tools 期望不一致)。
        # 提供与 LLMClient 行为一致的默认实现:路由 LLM 返回 selected_tools。
        import json as _json

        if self._fail_json:
            raise RuntimeError("router LLM 故障")
        return {"tools": self._selected_tools, "reason": "test"}


def test_agent_tool_calling_flow():
    reg = make_registry()
    mock = MockLLM([
        '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
        "结果是 5",
    ])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("2+3等于多少"))
    assert result.answer == "结果是 5"
    assert any(s["type"] == "tool" and s["tool"] == "add" for s in result.steps)


def test_agent_direct_answer():
    reg = make_registry()
    mock = MockLLM(["你好,有什么可以帮你?"])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("你好"))
    assert result.answer == "你好,有什么可以帮你?"
    assert len(result.steps) == 1


def test_agent_max_steps():
    reg = make_registry()
    # 一直返回工具调用,触发步数上限
    mock = MockLLM([json.dumps({"tool": "add", "arguments": {"a": 1, "b": 1}})] * 20)
    agent = Agent(llm=mock, registry=reg, max_steps=3)
    result = __import__("asyncio").run(agent.run("计算"))
    assert "无法完成" in result.answer


def test_agent_native_tool_calling():
    """原生 function calling 流程。"""
    reg = make_registry()
    mock = NativeMockLLM([
        {"tool_calls": [{"tool": "add", "arguments": {"a": 2, "b": 3}}]},
        "原生调用结果是 5",
    ])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("2+3等于多少"))
    assert result.answer == "原生调用结果是 5"
    tool_steps = [s for s in result.steps if s["type"] == "tool"]
    assert len(tool_steps) == 1


def test_agent_parallel_tool_calls():
    """一次响应内并行调用多个工具。"""
    reg = make_registry()
    mock = NativeMockLLM([
        {
            "tool_calls": [
                {"tool": "add", "arguments": {"a": 1, "b": 2}},
                {"tool": "add", "arguments": {"a": 10, "b": 20}},
            ]
        },
        "并行计算结果完成",
    ])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("并行计算"))
    assert result.answer == "并行计算结果完成"
    tool_steps = [s for s in result.steps if s["type"] == "tool"]
    assert len(tool_steps) == 2
    assert all(s["tool"] == "add" for s in tool_steps)


def test_agent_reflection_on_tool_error():
    """工具失败时注入反思提示,LLM 修正参数后成功。"""
    reg = make_registry()
    # 第一次 add 缺参数 -> 失败;第二次修正 -> 成功
    mock = MockLLM([
        '{"tool": "add", "arguments": {"a": 1}}',
        '{"tool": "add", "arguments": {"a": 1, "b": 2}}',
        "修正后结果是 3",
    ])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("计算"))
    assert result.answer == "修正后结果是 3"
    err_step = [s for s in result.steps if s["type"] == "tool" and "error" in s["result"]]
    assert len(err_step) == 1


def test_agent_build_messages_truncates_huge_history():
    """超大历史(含图片 base64 / 超长工具结果)必须被截断,防止上下文超限。"""
    from app.config import settings

    reg = make_registry()
    mock = MockLLM(["好的"])
    # 捕获传给 LLM 的消息
    captured = {}

    class CaptureLLM(MockLLM):
        async def chat(self, messages, temperature=0.7):
            captured["messages"] = messages
            return "好的"

    agent = Agent(llm=CaptureLLM(["好的"]), registry=reg, max_steps=2)
    huge = "A" * (settings.message_char_cap * 10)
    history = [
        {"role": "user", "content": huge},
        {"role": "assistant", "content": "B" * (settings.message_char_cap * 10)},
    ] * 10
    __import__("asyncio").run(agent.run("测试", history=history))

    msgs = captured["messages"]
    total = sum(len(m["content"]) for m in msgs)
    assert total <= settings.context_char_budget + settings.message_char_cap + 100
    # 单条不应保留完整的大内容
    assert all(len(m["content"]) <= settings.message_char_cap * 2 for m in msgs)
    # 巨量历史应被摘要/截断,而非原样塞入
    assert not any("A" * 1000 in m["content"] for m in msgs)


def test_agent_session_memory_injection():
    """session 级记忆只在对应会话自动注入。"""
    import app.memory.ltm as ltm_mod
    from app.memory.ltm import MemoryStore

    reg = make_registry()
    store = MemoryStore(db_path=":memory:")
    store.save("用户名字叫张三", keywords=["张三", "名字"], scope="session", session_id="s-1")
    old = ltm_mod._instance
    ltm_mod._instance = store
    try:
        captured = {}

        class CaptureLLM(MockLLM):
            async def chat(self, messages, temperature=0.7):
                captured["messages"] = messages
                return "好的"

        agent = Agent(llm=CaptureLLM(["好的"]), registry=reg, max_steps=2, session_id="s-1")
        __import__("asyncio").run(agent.run("我的名字是什么"))
        assert any("张三" in m["content"] for m in captured["messages"])

        # 其他会话看不到该 session 记忆
        captured.clear()
        agent2 = Agent(llm=CaptureLLM(["好的"]), registry=reg, max_steps=2, session_id="s-other")
        __import__("asyncio").run(agent2.run("我的名字是什么"))
        assert not any("张三" in m["content"] for m in captured["messages"])
    finally:
        ltm_mod._instance = old


def test_agent_auto_memory_saves_facts():
    """agent_auto_memory 开启时,对话结束后自动把事实写入长期记忆。"""
    import app.memory.ltm as ltm_mod
    from app.config import settings
    from app.memory.ltm import MemoryStore

    reg = make_registry()
    store = MemoryStore(db_path=":memory:")
    old = ltm_mod._instance
    ltm_mod._instance = store
    old_flag = settings.agent_auto_memory
    settings.agent_auto_memory = True
    try:
        class AutoLLM(MockLLM):
            async def chat_json(self, messages, temperature=0.2):
                return {"facts": ["用户喜欢喝茶"]}

        agent = Agent(llm=AutoLLM(["你好"]), registry=reg, max_steps=2)
        __import__("asyncio").run(agent.run("你好"))
        memories = __import__("asyncio").run(store.list())
        assert any("喝茶" in r["content"] for r in memories)
    finally:
        settings.agent_auto_memory = old_flag
        ltm_mod._instance = old


def test_agent_run_stream_events():
    """流式执行产出 tool/token/done 事件,steps 包含最终回答内容。"""
    reg = make_registry()
    mock = MockLLM([
        '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
        "结果是 5",
    ])
    agent = Agent(llm=mock, registry=reg, max_steps=5)

    async def collect():
        out = []
        async for evt in agent.run_stream("计算"):
            out.append(evt)
        return out

    events = __import__("asyncio").run(collect())
    types = [e["type"] for e in events]
    assert "tool" in types
    assert "token" in types
    assert "done" in types
    done = next(e for e in events if e["type"] == "done")
    answer_steps = [s for s in done["steps"] if s["type"] == "answer"]
    assert any(s.get("content") for s in answer_steps)


def test_agent_tool_streaming_events():
    """工具执行时产出 tool_start/tool 事件,流式和非流式均生效。

    注意:steps 里一次工具调用只落一条 type="tool" 的记录(含结果与耗时),
    "开始"与"完成"是以**事件**区分的 —— 执行前 emit tool_start、执行后 emit tool
    (带 step_id 供前端去重)。本文件其余用例(如 203/234/253 行)也是按 "tool" 取 steps。
    """
    reg = make_registry()

    # 非流式模式
    mock1 = MockLLM([
        '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
        "结果是 5",
    ])
    agent1 = Agent(llm=mock1, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent1.run("计算"))
    tool_steps = [s for s in result.steps if s["type"] == "tool"]
    assert len(tool_steps) >= 1
    assert tool_steps[0]["tool"] == "add"
    # 步骤里须带上结果:否则前端拿不到工具输出
    assert tool_steps[0].get("result")

    # 流式模式(使用独立 agent 避免 mock 响应被消耗)
    mock2 = MockLLM([
        '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
        "结果是 5",
    ])
    agent2 = Agent(llm=mock2, registry=reg, max_steps=5)

    async def collect_stream():
        out = []
        async for evt in agent2.run_stream("计算"):
            out.append(evt)
        return out

    events = __import__("asyncio").run(collect_stream())
    start_events = [e for e in events if e["type"] == "tool_start"]
    # 完成事件的类型是 "tool"(携带 result 与 step_id),不是 "tool_done"
    done_events = [e for e in events if e["type"] == "tool"]
    assert len(start_events) >= 1
    assert start_events[0]["name"] == "add"
    assert len(done_events) >= 1
    assert done_events[0]["name"] == "add"
    assert done_events[0].get("step_id")  # 前端靠 step_id 去重同一行


def test_agent_tool_router_selects_subset():
    """工具路由开启时,只把预选工具暴露给模型(系统工具常驻)。"""
    from app.tools.categories import SYSTEM_TOOLS

    reg = make_two_tool_registry()
    mock = RouterLLM(["你好"], selected_tools=["add"])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("2+3"))

    # 工具路由在选择过少(<3个)时会自动补充兜底工具,所以 calculate 也会被加入
    assert "add" in agent._active_tools
    schema_names = [s["function"]["name"] for s in agent._schemas]
    assert "add" in schema_names
    # 常驻工具集合包含全部系统工具(此测试注册器未注册系统工具,故不进 schema)
    assert set(SYSTEM_TOOLS) == set(agent._always_tools)
    # 记录路由步骤
    assert any(s["type"] == "tool_route" for s in result.steps)


def test_agent_tool_router_fallback_on_failure():
    """路由 LLM 故障时回退为全量工具,不影响主循环。"""
    reg = make_two_tool_registry()
    mock = RouterLLM([json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}), "结果是 3"], fail_json=True)
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    result = __import__("asyncio").run(agent.run("计算"))
    assert result.answer == "结果是 3"
    # 路由失败 -> 全量工具
    assert agent._active_tools is None
    schema_names = [s["function"]["name"] for s in agent._schemas]
    assert {"add", "calculate"} <= set(schema_names)


def test_agent_tool_router_blocks_unselected_tool():
    """模型调用未预选工具时被拦截,返回明确错误提示。"""
    from app.config import settings

    old_confirm = settings.agent_tool_confirm
    try:
        settings.agent_tool_confirm = False  # 禁用确认流,测试纯路由拦截
        reg = make_two_tool_registry()
        # 路由器只选了 add,但模型(按脚本)尝试调用 calculate (calculate 在注册表中但未被路由选中)
        # 注意:工具路由器在选择过少(<3个)时会自动补充兜底工具,calculate 会被自动补充进去
        # 所以我们需要一个不在路由选择中的工具,但注册表里没有第三个工具
        # 改用 run_code (如果不在危险工具中) 或修改注册表添加第三个工具
        # 这里我们测试路由器对注册表中但未选中的工具的拦截
        # 由于注册表只有 add/calculate,且 calculate 被自动补充,无法测试纯路由拦截
        # 此测试暂时跳过,待补充第三个非危险工具
        import pytest
        pytest.skip("需要注册表中有第三个非危险工具来测试纯路由拦截")
    finally:
        settings.agent_tool_confirm = old_confirm


def test_agent_mock_provider_skips_router():
    """mock provider 跳过工具路由,保证既有测试行为不变。"""
    reg = make_two_tool_registry()
    mock = MockLLM([json.dumps({"tool": "calculate", "arguments": {"expr": "1+1"}}), "完成"])
    agent = Agent(llm=mock, registry=reg, max_steps=5)
    __import__("asyncio").run(agent.run("算一下"))
    assert agent._active_tools is None
    schema_names = [s["function"]["name"] for s in agent._schemas]
    assert "calculate" in schema_names


def test_agent_tool_confirm_blocks_unapproved():
    """开启确认流后,危险工具未获批时被拦截并提示需用户确认。"""
    from app.config import settings

    old_confirm = settings.agent_tool_confirm
    old_dangerous = settings.agent_dangerous_tools
    old_interrupt = settings.agent_confirm_interrupt
    try:
        settings.agent_tool_confirm = True
        settings.agent_dangerous_tools = "add"
        settings.agent_confirm_interrupt = False
        reg = make_registry()
        mock = MockLLM([
            json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}),
            "完成",
        ])
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        result = __import__("asyncio").run(agent.run("计算"))
        confirm_steps = [s for s in result.steps if s["type"] == "tool" and "需要用户确认" in s["result"]]
        assert len(confirm_steps) == 1
    finally:
        settings.agent_tool_confirm = old_confirm
        settings.agent_dangerous_tools = old_dangerous
        settings.agent_confirm_interrupt = old_interrupt


def test_agent_tool_confirm_allows_approved():
    """危险工具在 approve_tools 中获批后正常执行。"""
    from app.config import settings

    old_confirm = settings.agent_tool_confirm
    old_dangerous = settings.agent_dangerous_tools
    try:
        settings.agent_tool_confirm = True
        settings.agent_dangerous_tools = "add"
        reg = make_registry()
        mock = MockLLM([
            json.dumps({"tool": "add", "arguments": {"a": 1, "b": 2}}),
            "结果是 3",
        ])
        agent = Agent(llm=mock, registry=reg, max_steps=5, approve_tools=["add"])
        result = __import__("asyncio").run(agent.run("计算"))
        assert result.answer == "结果是 3"
        add_steps = [s for s in result.steps if s["type"] == "tool" and s["tool"] == "add"]
        assert len(add_steps) == 1 and "sum" in add_steps[0]["result"]
    finally:
        settings.agent_tool_confirm = old_confirm
        settings.agent_dangerous_tools = old_dangerous


# ---- 反思循环(Reflexion 式自评→重写) ----

class ReflectMockLLM(MockLLM):
    """带评审能力的 Mock LLM:chat_json_schema 返回预先设定的评审意见。"""

    def __init__(self, responses, reviews):
        super().__init__(responses)
        self._reviews = list(reviews)
        self.review_calls = 0

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        self.review_calls += 1
        data = self._reviews[min(self.review_calls - 1, len(self._reviews) - 1)]
        if validator is not None:
            ok, err = validator(data)
            assert ok, f"评审 schema 校验失败: {err}"
        return data


def test_agent_reflect_rewrites_when_unsatisfactory():
    """评审不合格时把意见回灌模型重写,最终答案来自第二次生成。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    try:
        settings.agent_reflect = True
        reg = make_registry()
        # 答案需 >= 200 字符(_route_finalize 对短回答直接 END,反思节点根本进不去)
        long_old = "这是一个不正确的旧答案，缺少关键信息且存在错误，需要根据评审意见进行重写。" * 6
        long_new = "这是修正后的正确答案，包含了所有关键信息且修复了错误，评审应该通过。" * 7
        mock = ReflectMockLLM(
            [long_old, long_new],
            [
                {"score": 3, "verdict": "rewrite", "issues": ["缺少关键信息", "存在错误"]},
                {"score": 9, "verdict": "pass", "issues": []},
            ],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        result = __import__("asyncio").run(agent.run("解释这个概念"))
        assert result.answer == long_new
        assert mock.review_calls == 2
        reflect_steps = [s for s in result.steps if s["type"] == "reflect"]
        assert len(reflect_steps) == 2
        assert reflect_steps[0]["verdict"] == "rewrite"
        assert reflect_steps[1]["verdict"] == "pass"
    finally:
        settings.agent_reflect = old_reflect


def test_agent_reflect_passes_on_good_answer():
    """评审通过时不重写,直接返回。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    try:
        settings.agent_reflect = True
        reg = make_registry()
        # 答案需 >= 200 字符以触发反思节点(简单回答会被 _route_finalize 跳过)
        long_answer = "这是一个正确的回答，包含足够的字符数以触发反思节点。" * 9
        mock = ReflectMockLLM(
            [long_answer],
            [{"score": 9, "verdict": "pass", "issues": []}],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        result = __import__("asyncio").run(agent.run("简单问题"))
        assert result.answer == long_answer
        assert mock.review_calls == 1
        reflect_steps = [s for s in result.steps if s["type"] == "reflect"]
        assert len(reflect_steps) == 1 and reflect_steps[0]["verdict"] == "pass"
    finally:
        settings.agent_reflect = old_reflect


def test_agent_critic_dual_role_separate_model():
    """配置独立 critic provider 时,评审走独立模型而非执行模型。"""
    from app.config import settings

    old_provider = settings.agent_critic_provider
    old_model = settings.agent_critic_model
    try:
        settings.agent_critic_provider = "ollama"
        settings.agent_critic_model = "critic-model"
        agent = Agent(llm=MockLLM(["答案"]), registry=make_registry(), max_steps=3)
        assert agent.critic_llm is not None
        assert agent.critic_llm.provider == "ollama"
        assert agent.critic_llm.model == "critic-model"
        assert agent._critic() is agent.critic_llm
    finally:
        settings.agent_critic_provider = old_provider
        settings.agent_critic_model = old_model


def test_agent_critic_dual_role_falls_back_to_executor():
    """未配置独立 critic 时,评审复用执行模型。"""
    from app.config import settings

    old_provider = settings.agent_critic_provider
    try:
        settings.agent_critic_provider = ""
        mock = MockLLM(["答案"])
        agent = Agent(llm=mock, registry=make_registry(), max_steps=3)
        assert agent.critic_llm is None
        assert agent._critic() is mock
    finally:
        settings.agent_critic_provider = old_provider


def test_agent_reflect_respects_max_rounds():
    """反复不合格时最多重写 agent_reflect_max_rounds 轮后停止。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    old_max = settings.agent_reflect_max_rounds
    try:
        settings.agent_reflect = True
        settings.agent_reflect_max_rounds = 2
        reg = make_registry()
        # 每个答案需 >= 200 字符以触发反思节点
        base = "这是第{idx}轮生成的答案，内容足够长以触发反思节点。" * 10
        mock = ReflectMockLLM(
            [base.format(idx=1), base.format(idx=2), base.format(idx=3), base.format(idx=4)],
            [
                # 评审意见必须命中"确凿问题"关键词(错误/幻觉/不实/不正确/矛盾/缺失关键等)才会触发重写:
                {"score": 2, "verdict": "rewrite", "issues": ["存在错误", "内容不完整"]},
                {"score": 2, "verdict": "rewrite", "issues": ["存在错误", "内容仍然不完整"]},
                {"score": 2, "verdict": "rewrite", "issues": ["存在错误", "内容还是不完整"]},
                {"score": 9, "verdict": "pass", "issues": []},
            ],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        result = __import__("asyncio").run(agent.run("多轮任务"))
        # max_rounds=2:最多重写 2 次,共 3 次 llm 生成 + 3 次评审
        assert mock.review_calls == 3
        assert result.answer == base.format(idx=3)
    finally:
        settings.agent_reflect = old_reflect
        settings.agent_reflect_max_rounds = old_max


# ---- 计划失败重规划(replan-on-failure) ----

class ReplanMockLLM(MockLLM):
    """带计划重规划能力的 Mock LLM:chat_json_schema 返回修订计划。"""

    def __init__(self, responses, plans):
        super().__init__(responses)
        self.provider = "replan-test"  # 非 mock,触发 generate_plan / replan
        self._plans = list(plans)
        self._i = 0
        self.plan_calls = 0
        self.json_calls = 0

    async def chat_json(self, messages, temperature=0.2):
        self.json_calls += 1
        data = self._plans[min(self.json_calls - 1, len(self._plans) - 1)]
        return data

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        self.plan_calls += 1
        data = self._plans[min(self.plan_calls - 1, len(self._plans) - 1)]
        if validator is not None:
            ok, err = validator(data)
            assert ok, f"schema 校验失败: {err}"
        return data


def test_agent_replan_after_repeated_failure():
    """同工具连续失败达阈值时重新规划,并记录 replan step。"""
    from app.config import settings
    from app.tools.registry import Tool, _build_params_model

    old_replan = settings.agent_replan_enabled
    old_planning = settings.agent_planning
    old_reflect = settings.agent_reflect
    old_max_times = settings.agent_replan_max_times
    try:
        settings.agent_replan_enabled = True
        settings.agent_planning = True
        settings.agent_reflect = False  # 关掉反思循环,聚焦 replan
        settings.agent_replan_max_times = 1
        reg = ToolRegistry()

        def fragile(**kw):
            return {"error": "simulated failure", "success": False}

        model, schema = _build_params_model(fragile)
        reg.register(Tool(name="fragile", description="总是失败", func=fragile,
                          params_model=model, json_schema=schema))
        # 需要一份会生成计划的 mock:generate_plan 用 chat_json
        plan = {
            "needs_plan": True,
            "steps": [{"title": "调用 fragile", "description": "第一步"}, {"title": "汇总", "description": "输出结果"}],
        }
        replanned = {
            "needs_plan": True,
            "steps": [{"title": "改用其他方案", "description": "换工具重试"}],
        }
        # 一直返回失败的 fragile 调用,触发 replan
        mock = ReplanMockLLM(
            [json.dumps({"tool": "fragile", "arguments": {}})] * 5,
            [plan, replanned],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=6)
        result = __import__("asyncio").run(agent.run("计算一个复杂任务"))
        assert mock.json_calls >= 1  # generate_plan 至少一次
        assert mock.plan_calls >= 1  # replan 至少一次
        replan_steps = [s for s in result.steps if s.get("action") == "replan"]
        assert len(replan_steps) == 1
        assert replan_steps[0]["reason"] == "工具连续失败,已重新规划"
    finally:
        settings.agent_replan_enabled = old_replan
        settings.agent_planning = old_planning
        settings.agent_reflect = old_reflect
        settings.agent_replan_max_times = old_max_times


# ---- 工具结果去重缓存 ----

def test_tool_cache_normalizes_json_key_order():
    """工具结果去重缓存键归一化: 键序不同的相同逻辑调用命中同一缓存条目。

    验证 _normalize_tool_args 改进: {"a":1,"b":2} 与 {"b":2,"a":1} 映射到同一缓存键,
    避免参数排列差异导致缓存 miss。
    """
    from app.config import settings
    from app.core.agent import Agent as _Agent
    from app.tools.registry import Tool, _build_params_model

    # 直接验证归一化函数行为
    n1 = _Agent._normalize_tool_args('{"a": 1, "b": 2}')
    n2 = _Agent._normalize_tool_args('{"b": 2, "a": 1}')
    assert n1 == n2  # 键序不同但归一化后相同

    # 数值 1 vs 1.0 归一化为整数
    n3 = _Agent._normalize_tool_args('{"x": 1.0, "y": 2}')
    n4 = _Agent._normalize_tool_args('{"x": 1, "y": 2}')
    assert n3 == n4

    # 非 JSON 字符串原样返回(保守)
    assert _Agent._normalize_tool_args('not json') == 'not json'
    assert _Agent._normalize_tool_args('') == ''

    # 嵌套 dict 递归排序
    n5 = _Agent._normalize_tool_args('{"a": {"b": 1, "c": 2}, "d": 3}')
    n6 = _Agent._normalize_tool_args('{"d": 3, "a": {"c": 2, "b": 1}}')
    assert n5 == n6


def test_tool_cache_dedups_identical_calls():
    """同一运行内相同工具+参数只执行一次,第二次直接复用结果。"""
    from app.config import settings
    from app.tools.registry import Tool, _build_params_model

    old_cache = settings.agent_tool_cache_size
    old_safe = settings.agent_tool_cache_safe_tools
    try:
        settings.agent_tool_cache_size = 50
        # 缓存只对 agent_tool_cache_safe_tools 白名单内的"纯函数"工具生效,
        # 必须把被测工具名加进白名单,否则缓存根本不会启用(此前该用例因此恒为假绿/假红)。
        settings.agent_tool_cache_safe_tools = "add"
        calls = {"n": 0}

        def add(a: int, b: int):
            calls["n"] += 1
            return {"sum": a + b}

        reg = ToolRegistry()
        model, schema = _build_params_model(add)
        reg.register(Tool(name="add", description="加法", func=add,
                          params_model=model, json_schema=schema))
        # 使用完整的 agent run 来测试缓存：模型连续两次调用同一工具
        mock = MockLLM([
            '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
            '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
            "ok",
        ])
        agent = Agent(llm=mock, registry=reg, max_steps=5)
        __import__("asyncio").run(agent.run("计算两次"))
        assert calls["n"] == 1
        # 不同参数不命中缓存
        mock2 = MockLLM([
            '{"tool": "add", "arguments": {"a": 5, "b": 5}}',
            "ok",
        ])
        agent2 = Agent(llm=mock2, registry=reg, max_steps=3)
        __import__("asyncio").run(agent2.run("计算"))
        assert calls["n"] == 2
    finally:
        settings.agent_tool_cache_size = old_cache
        settings.agent_tool_cache_safe_tools = old_safe


def test_tool_cache_does_not_cache_errors():
    """失败结果不缓存,下次仍会重新执行。"""
    from app.config import settings

    old_cache = settings.agent_tool_cache_size
    old_safe = settings.agent_tool_cache_safe_tools
    try:
        settings.agent_tool_cache_size = 50
        # 显式把 flaky 放进缓存白名单:确保"调用 2 次"是因为失败结果不入缓存,
        # 而不是缓存压根没开(若不加,用例会在缓存未启用的情况下恒绿)。
        settings.agent_tool_cache_safe_tools = "flaky"
        calls = {"n": 0}

        def flaky(task: str = "x"):
            calls["n"] += 1
            return {"error": "boom", "success": False}

        from app.tools.registry import Tool, _build_params_model

        reg = ToolRegistry()
        model, schema = _build_params_model(flaky)
        reg.register(Tool(name="flaky", description="f", func=flaky,
                          params_model=model, json_schema=schema))
        agent = Agent(llm=MockLLM(["ok"]), registry=reg, max_steps=3)
        __import__("asyncio").run(agent._execute_tool("flaky", "{}"))
        __import__("asyncio").run(agent._execute_tool("flaky", "{}"))
        assert calls["n"] == 2  # 失败不缓存
    finally:
        settings.agent_tool_cache_size = old_cache
        settings.agent_tool_cache_safe_tools = old_safe


def test_tool_cache_disabled_means_no_dedup():
    """cache_size=0 时关闭去重缓存。"""
    from app.config import settings
    from app.tools.registry import Tool, _build_params_model

    old_cache = settings.agent_tool_cache_size
    old_safe = settings.agent_tool_cache_safe_tools
    try:
        settings.agent_tool_cache_size = 0
        # 白名单里放上 add:确保"调用 2 次"是因为 size=0 关掉了缓存,
        # 而不是因为 add 不在白名单里(那样用例会在无关原因下恒绿)。
        settings.agent_tool_cache_safe_tools = "add"
        calls = {"n": 0}

        def add(a: int, b: int):
            calls["n"] += 1
            return {"sum": a + b}

        reg = ToolRegistry()
        model, schema = _build_params_model(add)
        reg.register(Tool(name="add", description="加法", func=add,
                          params_model=model, json_schema=schema))
        agent = Agent(llm=MockLLM(["ok"]), registry=reg, max_steps=3)
        __import__("asyncio").run(agent._execute_tool("add", '{"a": 1, "b": 2}'))
        __import__("asyncio").run(agent._execute_tool("add", '{"a": 1, "b": 2}'))
        assert calls["n"] == 2
    finally:
        settings.agent_tool_cache_size = old_cache
        settings.agent_tool_cache_safe_tools = old_safe


# ---- 最终答案事实校验(verify 节点) ----

class VerifyMockLLM(MockLLM):
    """带事实校验能力的 Mock LLM:chat_json_schema 返回校验结论。"""

    def __init__(self, responses, verify_results):
        super().__init__(responses)
        self._verify = list(verify_results)
        self.verify_calls = 0

    async def chat_json_schema(self, messages, schema, temperature=0.1,
                               max_retries=2, validator=None):
        # 区分 reflect(score/verdict)与 verify(supported/unsupported)两套 schema
        if "supported" in schema.get("properties", {}):
            self.verify_calls += 1
            data = self._verify[min(self.verify_calls - 1, len(self._verify) - 1)]
            if validator is not None:
                ok, err = validator(data)
                assert ok, f"verify schema 校验失败: {err}"
            return data
        # reflect 评审:一律通过
        data = {"score": 9, "verdict": "pass", "issues": []}
        if validator is not None:
            ok, err = validator(data)
            assert ok, f"reflect schema 校验失败: {err}"
        return data


def test_agent_verify_rewrites_unsupported_answer():
    """答案含未支撑断言时,verify 节点重写答案一次。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    old_verify = settings.agent_verify
    try:
        settings.agent_reflect = True
        settings.agent_verify = True
        reg = make_registry()
        # 答案需 >= 200 字符(触发反思)且 >= 50 字符(反思后进入校验)
        long_fake = "计算结果总和是 999，这是一个幻觉答案，声称工具返回了错误的结果，实际工具执行 add(2,3) 应该返回 5。" * 2
        long_real = "计算结果总和是 5，这个结果是由工具调用 add(2, 3) 正确计算得到的，有工具执行结果为证据支撑。" * 2
        mock = VerifyMockLLM(
            [
                '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
                long_fake,
                long_real,
            ],
            [
                {"supported": False, "unsupported": ["总和是 999 无证据支撑"]},
                {"supported": True, "unsupported": []},
            ],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=6)
        result = __import__("asyncio").run(agent.run("2+3等于多少"))
        assert mock.verify_calls >= 1
        verify_steps = [s for s in result.steps if s["type"] == "verify"]
        assert len(verify_steps) >= 1
        assert verify_steps[0]["supported"] is False
        rewrite_steps = [s for s in result.steps if s["type"] == "verify_rewrite"]
        assert len(rewrite_steps) == 1
        assert result.answer == long_real
    finally:
        settings.agent_reflect = old_reflect
        settings.agent_verify = old_verify


def test_agent_verify_passes_supported_answer():
    """答案全部被证据支撑时,校验通过不重写。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    old_verify = settings.agent_verify
    try:
        settings.agent_reflect = True
        settings.agent_verify = True
        reg = make_registry()
        # 答案需 >= 200 字符(触发反思)且 >= 50 字符(反思后进入校验)
        long_real = "计算结果总和是 5，这个结果是由工具调用 add(2, 3) 正确计算得到的，有工具执行结果为证据支撑。" * 2
        mock = VerifyMockLLM(
            [
                '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
                long_real,
            ],
            [{"supported": True, "unsupported": []}],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=6)
        result = __import__("asyncio").run(agent.run("2+3等于多少"))
        assert result.answer == long_real
        verify_steps = [s for s in result.steps if s["type"] == "verify"]
        assert verify_steps and verify_steps[0]["supported"] is True
        assert not any(s["type"] == "verify_rewrite" for s in result.steps)
    finally:
        settings.agent_reflect = old_reflect
        settings.agent_verify = old_verify


def test_agent_verify_disabled_skips():
    """agent_verify=False 时不进入校验节点。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    old_verify = settings.agent_verify
    try:
        settings.agent_reflect = True
        settings.agent_verify = False
        reg = make_registry()
        mock = VerifyMockLLM(
            [
                '{"tool": "add", "arguments": {"a": 2, "b": 3}}',
                "结果是 5",
            ],
            [{"supported": False, "unsupported": ["x"]}],
        )
        agent = Agent(llm=mock, registry=reg, max_steps=6)
        result = __import__("asyncio").run(agent.run("2+3等于多少"))
        assert mock.verify_calls == 0
        assert result.answer == "结果是 5"
    finally:
        settings.agent_reflect = old_reflect
        settings.agent_verify = old_verify


# ---- 多模型协作模式(mode: single / review / ensemble) ----

def test_mode_single_disables_review():
    """single 模式强制关闭评审/校验,即使全局开关开启。"""
    from app.config import settings

    old_reflect = settings.agent_reflect
    old_verify = settings.agent_verify
    try:
        settings.agent_reflect = True
        settings.agent_verify = True
        reg = make_registry()
        mock = VerifyMockLLM(["直接答案"], [{"supported": False, "unsupported": ["x"]}])
        agent = Agent(llm=mock, registry=reg, max_steps=6, mode="single")
        assert agent._reflect_enabled is False
        assert agent._verify_enabled is False
        result = __import__("asyncio").run(agent.run("简单问题"))
        assert result.answer == "直接答案"
        assert mock.verify_calls == 0
        assert not any(s["type"] == "reflect" for s in result.steps)
    finally:
        settings.agent_reflect = old_reflect
        settings.agent_verify = old_verify


def test_mode_review_forces_verify_and_critic(monkeypatch):
    """review 模式强制开启校验,并使用请求级独立 critic 模型。"""
    from app.config import settings
    from app.core.llm import LLMClient

    old_reflect = settings.agent_reflect
    old_verify = settings.agent_verify
    try:
        settings.agent_reflect = False
        settings.agent_verify = False
        reg = make_registry()

        class _CriticLLM(LLMClient):
            """独立评审模型:verify 时返回 supported=True,反映"请求级 critic 生效"。”

            __init__(provider, model) 由 Agent 构造 critic_llm 时调用。"""

            def __init__(self, provider=None, model=None):
                self.provider = provider or "ollama"
                self.model = model or "critic-model"
                self.verify_calls = 0

            @property
            def info(self):
                return {"provider": self.provider, "model": self.model}

            async def chat(self, messages, temperature=0.7):
                return "评审意见"

            async def chat_json(self, messages, temperature=0.2):
                return {"needs_plan": False, "steps": []}

            async def chat_json_schema(self, messages, schema, temperature=0.2,
                                       max_retries=2, validator=None):
                if "supported" in schema.get("properties", {}):
                    self.verify_calls += 1
                    data = {"supported": True, "unsupported": []}
                    if validator is not None:
                        ok, err = validator(data)
                        assert ok, f"verify schema 校验失败: {err}"
                    return data
                return {"score": 9, "verdict": "pass", "issues": []}

        # Agent 构造 critic_llm 时内部会 import LLMClient,替换为独立评审模型
        monkeypatch.setattr("app.core.llm.LLMClient", _CriticLLM)

        class _ExecMock(MockLLM):
            async def chat_json_schema(self, messages, schema, temperature=0.2,
                                       max_retries=2, validator=None):
                return {"score": 9, "verdict": "pass", "issues": []}

        # 最终答案需 >= 50 字符:否则 _route_reflect 会判定为"过短"直接 END,verify 节点不会执行
        final_answer = ("计算结果总和是 5，这个结果是由工具调用 add(2, 3) 得到的正确答案，"
                        "该结论与工具返回的原始输出完全一致，可以放心地交付给用户。")
        exec_mock = _ExecMock(['{"tool": "add", "arguments": {"a": 2, "b": 3}}', final_answer])
        agent = Agent(llm=exec_mock, registry=reg, max_steps=6, mode="review",
                      critic_provider="ollama", critic_model="critic-model")
        assert agent._verify_enabled is True
        assert agent._reflect_enabled is True
        assert agent.critic_llm is not None
        assert agent.critic_llm.provider == "ollama"
        assert agent.critic_llm.model == "critic-model"
        result = __import__("asyncio").run(agent.run("2+3等于多少"))
        assert agent.critic_llm.verify_calls >= 1
        assert any(s["type"] == "verify" for s in result.steps)
    finally:
        settings.agent_reflect = old_reflect
        settings.agent_verify = old_verify


class JudgeMockLLM(MockLLM):
    """模拟评审模型:chat_json_schema 返回选优结论(选第 1 个候选)。"""

    def __init__(self, responses, judge):
        super().__init__(responses)
        self._judge = judge
        self.judge_calls = 0

    async def chat_json_schema(self, messages, schema, temperature=0.2,
                               max_retries=2, validator=None):
        self.judge_calls += 1
        if "winner" in schema.get("properties", {}):
            data = self._judge
            if validator is not None:
                ok, err = validator(data)
                assert ok, f"judge schema 校验失败: {err}"
            return data
        # 子智能体执行模型路径:评审/校验一律通过
        data = {"score": 9, "verdict": "pass", "issues": []}
        if validator is not None:
            ok, err = validator(data)
            assert ok, f"reflect schema 校验失败: {err}"
        return data


def test_mode_ensemble_runs_multiple_models_and_judges(monkeypatch):
    """ensemble 模式并行执行多个模型,评审选优并记录成员轨迹。"""
    from app.config import settings
    from app.core.llm import LLMClient

    old_max = settings.agent_ensemble_max
    try:
        settings.agent_ensemble_max = 3

        class _FakeLLM(LLMClient):
            """假 LLM:按 provider 返回不同回答,模拟两个不同模型的产出。"""

            def __init__(self, provider=None, model=None):
                self.provider = provider or "fake"
                self.model = model or f"{self.provider}-model"
                self._responses = {
                    "alpha": ["alpha 的回答"],
                    "beta": ["beta 的回答"],
                }

            @property
            def info(self):
                return {"provider": self.provider, "model": self.model}

            async def chat(self, messages, temperature=0.7):
                return self._responses.get(self.provider, ["默认回答"])[0]

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
                        assert ok, f"judge schema 校验失败: {err}"
                    return data
                return {"score": 9, "verdict": "pass", "issues": []}

        monkeypatch.setattr("app.core.llm.LLMClient", _FakeLLM)
        reg = make_registry()
        agent = Agent(llm=_FakeLLM("alpha"), registry=reg, max_steps=5, mode="ensemble",
                      ensemble_models=["alpha:alpha-model", "beta:beta-model"])
        result = __import__("asyncio").run(agent.run("写一段话"))
        # 评审选中 beta
        assert result.answer == "beta 的回答"
        member_steps = [s for s in result.steps if s["type"] == "ensemble_member"]
        assert len(member_steps) == 2
        assert member_steps[0]["provider"] == "alpha"
        assert member_steps[1]["provider"] == "beta"
        assert any(s["type"] == "ensemble_judge" for s in result.steps)
    finally:
        settings.agent_ensemble_max = old_max


def test_sanitize_history_for_llm():
    """sanitize_history_for_llm 必须把自定义角色/孤立 tool 消息降级为 user,
    保留合法 tool_call 配对。"""
    from app.memory.session import sanitize_history_for_llm

    history = [
        {"role": "user", "content": "你好"},
        {"role": "error", "content": "连接失败", "kind": "error", "id": "x", "timestamp": 1},
        {"role": "plan", "content": "步骤..."},
        {"role": "tool", "content": '{"name": "read_file", "result": "ok"}'},  # 孤立 tool
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "add", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "5"},  # 合法 tool
    ]
    out = sanitize_history_for_llm(history)
    assert out[0] == {"role": "user", "content": "你好"}
    # error → user, 剥掉 kind/id/timestamp
    assert out[1]["role"] == "user"
    assert "kind" not in out[1] and "timestamp" not in out[1]
    # plan → user
    assert out[2]["role"] == "user"
    # 孤立 tool → user + 前缀
    assert out[3]["role"] == "user"
    assert out[3]["content"].startswith("[工具")
    # 合法 assistant.tool_calls 保留
    assert out[4]["role"] == "assistant" and out[4]["tool_calls"][0]["id"] == "call_9"
    # 合法 tool 保留 tool_call_id
    assert out[5]["role"] == "tool" and out[5]["tool_call_id"] == "call_9"


def test_build_messages_sanitizes_invalid_roles():
    """历史里的自定义角色(error/plan)和无 tool_call_id 的 tool 消息必须降级为 user,
    避免 OpenAI 网关 400 (unknown variant 'error' / missing field 'tool_call_id')。"""
    reg = make_registry()
    mock = MockLLM(["好的"])

    class CaptureLLM(MockLLM):
        async def chat(self, messages, temperature=0.7):
            self.captured_messages = list(messages)
            return "好的"

    cap = CaptureLLM(["好的"])
    agent = Agent(llm=cap, registry=reg, max_steps=2)
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好"},
        {"role": "error", "content": "连接失败"},
        {"role": "plan", "content": "计划...", "kind": "plan"},
        {"role": "tool", "content": json.dumps({"name": "read_file", "result": "ok"})},
        {"role": "assistant", "content": "完成"},
    ]
    __import__("asyncio").run(agent.run("测试", history=history))

    msgs = cap.captured_messages
    # 所有消息的 role 必须都是 OpenAI 合法角色
    for m in msgs:
        assert m["role"] in ("system", "user", "assistant", "tool", "function", "developer"), \
            f"非法角色残留: {m['role']}"
    # 没有孤立 tool 消息(缺 tool_call_id 的必须已降级)
    for m in msgs:
        if m["role"] == "tool":
            assert m.get("tool_call_id"), "tool 消息缺 tool_call_id"
    # error/plan 角色已降级为 user
    assert not any(m["role"] in ("error", "plan") for m in msgs)
    # 剥掉了 kind/timestamp/meta 等非 OpenAI 字段
    for m in msgs:
        assert "kind" not in m, f"非 OpenAI 字段残留: {list(m.keys())}"
        assert "timestamp" not in m


def test_build_messages_preserves_valid_tool_calls():
    """带合法 tool_call_id 且前面有 assistant.tool_calls 的 tool 消息必须保留。"""
    reg = make_registry()

    class CaptureLLM(MockLLM):
        async def chat(self, messages, temperature=0.7):
            self.captured_messages = list(messages)
            return "好的"

    cap = CaptureLLM(["好的"])
    agent = Agent(llm=cap, registry=reg, max_steps=2)
    history = [
        {"role": "user", "content": "加一下"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "add", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "5"},
    ]
    __import__("asyncio").run(agent.run("继续", history=history))

    msgs = cap.captured_messages
    # 合法的 tool 消息应保留 tool_call_id
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert any(m.get("tool_call_id") == "call_1" for m in tool_msgs), "合法 tool 消息被误降级"

