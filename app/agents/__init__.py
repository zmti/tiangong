"""多智能体编排:角色化团队 + 委派/交接。

对标 CrewAI / Claude Code Subagents 的思路,但零第三方依赖:
- AgentRole:一个带系统提示、工具白名单、模型、步数上限的角色
- AgentTeam:角色的集合,可按角色名创建"独立上下文的子智能体"执行任务
- handoff:主 Agent 通过工具把任务交给某个角色,获得结果摘要后继续

角色运行复用 app/core/agent.py 的图运行时(含 checkpoint、反思、记忆注入),
只是换系统提示与工具子集,因此天然支持"主 Agent -> 角色"的多层协作。
"""
from app.agents.roles import (
    AgentRole,
    AgentTeam,
    RoleRegistry,
    get_role_registry,
    register_roles,
)
from app.agents.handoff_tool import get_team

__all__ = [
    "AgentRole",
    "AgentTeam",
    "RoleRegistry",
    "get_role_registry",
    "register_roles",
    "get_team",
]
