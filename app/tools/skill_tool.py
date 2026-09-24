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
async def run_skill(
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

    from app.tools.registry import get_registry
    executor = SkillExecutor()
    tool_exec = _get_tool_executor()
    result = await executor.execute(skill, user_params, tool_exec)
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
    from app.tools.project_context import get_project_path
    return ToolExecutor(registry=get_registry(), project_path=get_project_path())


@tool("discover_skills", "根据任务描述推荐相关技能(关键词匹配技能描述和步骤工具)", timeout=5)
def discover_skills(
    task: str = Field(..., description="任务描述,用于匹配相关技能"),
    top_k: int = Field(5, description="返回前 K 个推荐"),
) -> list[dict]:
    """推荐与任务相关的技能。"""
    from app.core.skills import discover_skills_for_task
    skills = discover_skills_for_task(task, top_k)
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


@tool("compose_skills", "将多个技能组合为新技能(顺序执行,参数透传)", timeout=5)
def compose_skills_tool(
    skill_names: str = Field(..., description="要组合的技能名称列表,JSON 数组格式"),
    composed_name: str = Field(..., description="组合后的新技能名称"),
    composed_description: str = Field(..., description="组合后的技能描述"),
) -> dict:
    """组合多个技能为新技能。"""
    import json
    from app.core.skills import compose_skills, get_skill_registry
    try:
        names = json.loads(skill_names)
    except json.JSONDecodeError:
        return {"error": "skill_names 必须是 JSON 数组"}
    if not isinstance(names, list) or not names:
        return {"error": "skill_names 必须是非空数组"}
    skill = compose_skills(names, composed_name, composed_description)
    get_skill_registry().register(skill)
    return {"name": skill.name, "description": skill.description, "steps": len(skill.steps), "params": len(skill.params)}


@tool("skill_versions", "列出技能的版本历史", timeout=5)
def skill_versions(
    skill_name: str = Field(..., description="技能名称"),
) -> list[dict]:
    """查看技能版本历史。"""
    from app.core.skills import get_skill_version_manager
    mgr = get_skill_version_manager()
    return mgr.list_versions(skill_name)


@tool("skill_rollback", "将技能回滚到指定版本", timeout=5)
def skill_rollback(
    skill_name: str = Field(..., description="技能名称"),
    version: int = Field(..., description="目标版本号"),
) -> dict:
    """回滚技能到指定版本。"""
    from app.core.skills import get_skill_version_manager
    mgr = get_skill_version_manager()
    ok = mgr.rollback(skill_name, version)
    return {"ok": ok, "skill_name": skill_name, "version": version}


@tool("save_skill_version", "保存当前技能为新版本(便于后续回滚)", timeout=5)
def save_skill_version(
    skill_name: str = Field(..., description="技能名称"),
    changelog: str = Field("", description="版本变更说明"),
) -> dict:
    """保存技能当前状态为新版本。"""
    from app.core.skills import get_skill_version_manager
    mgr = get_skill_version_manager()
    new_version = mgr.save_version(skill_name, changelog)
    return {"ok": new_version > 0, "skill_name": skill_name, "version": new_version, "changelog": changelog}
