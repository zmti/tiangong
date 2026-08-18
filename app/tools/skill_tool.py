"""run_skill 工具:让 LLM 可以执行预定义的多步骤技能。

支持:
- 列出可用技能
- 执行技能(按步骤依次调用工具)
- 查看技能执行结果
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


@tool("run_skill", "执行预定义的多步骤技能(如部署、回归测试、安全扫描等)。"
      "返回每步执行结果和最终汇总。", timeout=120)
def run_skill(
    skill_name: str = Field(..., description="技能名称,用 list_skills 查看可用技能"),
    params: str = Field("{}", description="JSON 格式的参数,覆盖技能默认值"),
    stop_on_error: bool = Field(True, description="某步失败时是否停止后续步骤"),
) -> dict:
    """执行一个预定义技能。"""
    from app.core.skills import get_skill_registry, SkillExecutor

    registry = get_skill_registry()
    skill = registry.get(skill_name)
    if skill is None:
        available = [s.name for s in registry.list(enabled_only=True)]
        return {
            "error": f"技能 '{skill_name}' 不存在",
            "available_skills": available,
        }

    try:
        user_params = json.loads(params) if params else {}
    except json.JSONDecodeError:
        return {"error": f"参数 JSON 解析失败: {params}"}

    user_params["_stop_on_error"] = stop_on_error

    import asyncio
    from app.tools.registry import get_registry
    executor = SkillExecutor()
    tool_exec = _get_tool_executor()
    result = asyncio.get_event_loop().run_until_complete(
        executor.execute(skill, user_params, tool_exec)
    )
    return result.to_dict()


@tool("list_skills", "列出所有可用的多步骤技能及其描述", timeout=5)
def list_skills() -> list[dict]:
    """列出所有已注册技能。"""
    from app.core.skills import get_skill_registry

    registry = get_skill_registry()
    skills = registry.list(enabled_only=True)
    return [
        {
            "name": s.name,
            "description": s.description,
            "steps": len(s.steps),
            "params": [
                {"name": p.name, "type": p.type_, "default": p.default, "required": p.required}
                for p in s.params
            ],
        }
        for s in skills
    ]


def _get_tool_executor():
    """获取工具执行器实例。"""
    from app.core.tool_executor import ToolExecutor
    from app.tools.registry import get_registry
    return ToolExecutor(registry=get_registry())
