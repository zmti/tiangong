"""技能(Skill)注册器 + 执行器 端到端测试。

覆盖:
- SkillRegistry: register / get / list / to_openai_schema
- SkillExecutor: 用真实已注册工具(get_current_time)验证执行器与工具真正接通
- 参数合并 + ${param} 占位解析
- 错误处理: stop_on_error 中断 / 未知工具报错
- run_skill 工具: 未知技能名返回错误列表(轻量)
"""
import asyncio
import json

import pytest

from app.core.skills import (
    Skill,
    SkillParam,
    SkillStep,
    SkillRegistry,
    SkillExecutor,
)
from app.tools.skill_tool import run_skill


# ---- 轻量假执行器: 验证执行器自身逻辑(参数合并 / ${param} / stop_on_error) ----

class _FakeExecutor:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def execute_tool(self, name: str, arguments: str) -> str:
        self.calls.append((name, arguments))
        args = json.loads(arguments) if arguments else {}
        if name == "boom":
            raise RuntimeError("step failed on purpose")
        return json.dumps({"ok": True, "echo": args, "tool": name}, ensure_ascii=False)


def _make_skill() -> Skill:
    return Skill(
        name="demo",
        description="demo skill",
        steps=[
            SkillStep(tool="echo", args={"msg": "hello", "who": "${who}"}),
            SkillStep(tool="echo", args={"msg": "done"}),
        ],
        params=[SkillParam(name="who", type_="string", description="谁", default="world", required=False)],
    )


def test_registry_register_and_list():
    reg = SkillRegistry()
    reg.register(_make_skill())
    assert reg.get("demo") is not None
    assert reg.get("missing") is None
    assert len(reg.list(enabled_only=True)) == 1
    reg.unregister("demo")
    assert reg.get("demo") is None


def test_registry_to_openai_schema():
    reg = SkillRegistry()
    reg.register(_make_skill())
    schemas = reg.to_openai_schemas()
    assert len(schemas) == 1
    s = schemas[0]
    assert s["type"] == "function"
    assert s["function"]["name"] == "demo"
    assert "who" in s["function"]["parameters"]["properties"]


def test_executor_param_merge_and_placeholder():
    reg = SkillRegistry()
    reg.register(_make_skill())
    # 覆盖默认参数
    ex = SkillExecutor(_FakeExecutor())
    res = asyncio.run(ex.execute(reg.get("demo"), {"who": "alice"}))
    assert res.success is True
    first_args = json.loads(ex._tool_executor.calls[0][1])
    assert first_args["who"] == "alice"
    # 不传则用默认
    ex2 = SkillExecutor(_FakeExecutor())
    res2 = asyncio.run(ex2.execute(reg.get("demo"), {}))
    assert res2.success is True
    first_args2 = json.loads(ex2._tool_executor.calls[0][1])
    assert first_args2["who"] == "world"


def test_executor_stop_on_error():
    skill = Skill(name="fail", description="f", steps=[
        SkillStep(tool="boom", args={}),
        SkillStep(tool="echo", args={"msg": "unreachable"}),
    ])
    ex = SkillExecutor(_FakeExecutor())
    res = asyncio.run(ex.execute(skill, {}))
    assert res.success is False
    # 失败后不应执行第二步
    assert len(ex._tool_executor.calls) == 1


def test_executor_unknown_tool_error():
    from app.core.tool_executor import ToolExecutor
    from app.tools.registry import get_registry

    # 真实执行器对未知工具返回错误 JSON;SkillExecutor 现在应据此判步骤失败
    skill = Skill(name="bad", description="f", steps=[
        SkillStep(tool="no_such_tool_xyz", args={}),
    ])
    ex = SkillExecutor(ToolExecutor(registry=get_registry(), project_path=""))
    res = asyncio.run(ex.execute(skill, {}))
    assert res.success is False
    assert res.error is not None
    assert res.steps[0].success is False


# ---- 真实工具链路: 用已注册 get_current_time 验证执行器与工具真正接通 ----

def test_executor_real_tool_end_to_end():
    from app.core.tool_executor import ToolExecutor
    from app.tools.registry import get_registry

    skill = Skill(name="now", description="当前时间", steps=[
        SkillStep(tool="get_current_time", args={"tz": "local"}, label="取当前时间"),
    ])
    ex = SkillExecutor(ToolExecutor(registry=get_registry(), project_path=""))
    res = asyncio.run(ex.execute(skill, {}))
    assert res.success is True
    # 真实工具的返回值应进入合并输出
    assert "datetime" in res.merged_output


def test_run_skill_unknown_returns_error():
    # run_skill 被 @tool 包装为 Tool 对象,其底层协程在 .func
    out = asyncio.run(run_skill.func("definitely_not_a_skill", "{}"))
    assert "error" in out
    assert "definitely_not_a_skill" in out["error"]
