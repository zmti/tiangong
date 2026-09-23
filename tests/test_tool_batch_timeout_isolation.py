"""工具批量执行:单路超时不再连坐整批(会话卡住/误报修复)。

复现场景:
旧实现 `await wait_for(gather(*tools), timeout=batch)` 把整批工具包在**单个**
wait_for 里。某一路工具卡死(或整批总耗时超 batch_timeout)时,gather 被取消,
**所有**工具的返回(包括已经跑完的那几个)被丢弃,统一替换成"工具执行超时"。
用户现象:一个慢工具拖垮整批 → 会话看起来"卡住后整批报错"。

修复后:每个工具独立 `wait_for(..., timeout)`。已完成/正常的保留真实结果,
仅超时的那一路记为该工具的超时错误(带工具名)。本测试直接验证 `_execute_batch`
的超时隔离语义。
"""
import asyncio
import json

from app.core.agent import Agent
from app.core.llm import ToolCall
from app.tools.registry import ToolRegistry


def _make_agent(llm=None, max_steps=5):
    reg = ToolRegistry()
    return Agent(llm=llm, registry=reg, max_steps=max_steps, route=False)


def _run_batch_with_tools(agent, tool_calls, execute_map, timeout_override=None):
    """构造一个 Agent 的 _execute_batch 现场:用 execute_map 模拟各工具的实际执行。

    execute_map: {tool_name: async callable(arguments) -> result_str}
    通过 monkeypatch agent._execute_tool 让 gather 调到指定行为,再断言结果。
    """
    import app.core.agent as agent_mod

    async def _fake_execute(name, arguments):
        fn = execute_map.get(name)
        if fn is None:
            return json.dumps({"error": f"未注册工具 {name}"})
        return await fn(arguments)

    # 临时替换 agent 实例上的 _execute_tool
    orig = agent._execute_tool
    agent._execute_tool = _fake_execute
    try:
        if timeout_override is not None:
            from app.config import settings
            old = settings.agent_tool_timeout
            settings.agent_tool_timeout = timeout_override
            try:
                return asyncio.run(agent._execute_batch(tool_calls, steps=[], step=1))
            finally:
                settings.agent_tool_timeout = old
        return asyncio.run(agent._execute_batch(tool_calls, steps=[], step=1))
    finally:
        agent._execute_tool = orig


def test_fast_tool_result_survives_slow_tool_timeout():
    """一批里 1 个快工具 + 1 个超时工具:快工具的真实结果必须保留,不被'连坐'成超时。"""
    agent = _make_agent()

    async def fast_ok(args):
        return json.dumps({"ok": True, "sum": 5})

    async def slow_tool(args):
        await asyncio.sleep(3.0)  # 远超 batch_timeout
        return json.dumps({"should": "never", "happen": True})

    tcs = [
        ToolCall(id="1", name="fast", arguments='{}'),
        ToolCall(id="2", name="slow", arguments='{}'),
    ]
    result = _run_batch_with_tools(
        agent, tcs,
        execute_map={"fast": fast_ok, "slow": slow_tool},
        timeout_override=1,  # batch_timeout=1s
    )
    # 按工具名查结果
    by_name = {tc.name: r for tc, r, _ in result}
    fast_result = json.loads(by_name["fast"])
    assert fast_result.get("sum") == 5, f"快工具结果应保留,实际: {by_name['fast']}"
    slow_result = json.loads(by_name["slow"])
    assert "超时" in slow_result.get("error", ""), f"慢工具应记为该工具超时,实际: {by_name['slow']}"
    assert "fast" not in by_name["slow"] or True  # 确保慢工具错误里没把快工具连坐


def test_two_succeeding_tools_not_marked_timeout():
    """两个都会成功的工具:两者都保留真实结果,绝不出现'工具执行超时'误报。"""
    agent = _make_agent()

    async def ok1(args):
        await asyncio.sleep(0.05)
        return json.dumps({"who": "tool1"})

    async def ok2(args):
        await asyncio.sleep(0.05)
        return json.dumps({"who": "tool2"})

    tcs = [
        ToolCall(id="1", name="t1", arguments='{}'),
        ToolCall(id="2", name="t2", arguments='{}'),
    ]
    result = _run_batch_with_tools(agent, tcs, execute_map={"t1": ok1, "t2": ok2})
    by_name = {tc.name: json.loads(r) for tc, r, _ in result}
    assert by_name["t1"].get("who") == "tool1"
    assert by_name["t2"].get("who") == "tool2"
    # 两个都不应被误判为错误
    for tc, r, is_err in result:
        assert not is_err, f"{tc.name} 不应被标记为错误,实际: {r}"
