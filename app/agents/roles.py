"""角色与团队定义:多智能体协作的基础单元。

一个 AgentRole 描述"一个专业子智能体":
- system_prompt:角色定位与工作守则(决定行为)
- tools:允许使用的工具白名单(None = 全量)
- model:可选的模型覆盖("provider:model" 或仅 provider,继承父模型)
- max_steps:步数上限

AgentTeam 是角色的集合。用 roles.py 默认注册一批开箱即用的角色,
也可在业务代码里 register_roles 覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.config import settings


@dataclass
class AgentRole:
    name: str
    system_prompt: str
    description: str = ""
    tools: Optional[list[str]] = None  # 工具白名单;None = 全量
    model: str = ""  # "provider:model" 或 "provider" 或 ""(继承父模型)
    max_steps: int = 8

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "model": self.model,
            "max_steps": self.max_steps,
        }


class AgentTeam:
    """角色集合:按角色名查找,并创建角色子智能体。"""

    def __init__(self, roles: Optional[list[AgentRole]] = None):
        self._roles: dict[str, AgentRole] = {}
        for r in roles or []:
            self.add(r)

    def add(self, role: AgentRole) -> None:
        self._roles[role.name] = role

    def get(self, name: str) -> Optional[AgentRole]:
        return self._roles.get(name)

    def list(self) -> list[AgentRole]:
        return list(self._roles.values())

    def names(self) -> list[str]:
        return list(self._roles.keys())

    def summary(self) -> list[dict]:
        return [r.to_dict() for r in self._roles.values()]


class RoleRegistry:
    """全局角色注册表(团队)。"""

    def __init__(self, team: Optional[AgentTeam] = None):
        self._team = team or AgentTeam()

    @property
    def team(self) -> AgentTeam:
        return self._team

    def set_team(self, team: AgentTeam) -> None:
        self._team = team


_registry: Optional[RoleRegistry] = None


def get_role_registry() -> RoleRegistry:
    global _registry
    if _registry is None:
        _registry = RoleRegistry()
    return _registry


def register_roles(roles: list[AgentRole]) -> None:
    """注册(或覆盖)一批角色。"""
    reg = get_role_registry()
    for r in roles:
        reg.team.add(r)


def default_roles() -> list[AgentRole]:
    """开箱即用的默认角色团队。"""
    return [
        AgentRole(
            name="researcher",
            description="网络调研/资料收集:检索、抓取、提炼信息",
            system_prompt=(
                "你是一名研究员(researcher)子智能体,由主智能体委派做信息调研。"
                "只做调研与信息收集,不要修改代码或文件。"
                "使用 web 检索/网页抓取/知识库等工具收集事实,输出:结论摘要、关键证据(带来源)、"
                "以及不确定或缺失的信息。用简洁中文汇报。"
            ),
            tools=["fetch_webpage", "extract_text", "web_search", "rag_search", "memory_search"],
            max_steps=10,
        ),
        AgentRole(
            name="coder",
            description="代码编写/修改:写代码、改 bug、跑测试",
            system_prompt=(
                "你是一名软件工程师(coder)子智能体。负责编写/修改代码、修复 bug、运行测试。"
                "流程:先理解需求与现有代码 -> 定位修改点 -> 修改 -> 运行测试验证。"
                "遵循仓库现有风格与约定,不做无关重构。完成后汇报改动文件、测试结果与结论。"
            ),
            tools=None,  # 全量工具
            max_steps=15,
        ),
        AgentRole(
            name="code_reviewer",
            description="代码审查:检查质量、安全、潜在 bug",
            system_prompt=(
                "你是一名资深代码审查员(code_reviewer)子智能体。对给定代码/改动做审查,"
                "重点:正确性、边界条件、安全风险、性能、可维护性、与既有约定的偏差。"
                "只审查,不修改代码。输出:问题清单(按严重程度)、每个问题的位置与理由、修改建议。"
            ),
            tools=["read_document", "search_files", "list_directory", "code_context", "code_search", "git_diff", "git_log"],
            max_steps=10,
        ),
        AgentRole(
            name="analyst",
            description="数据分析:查询数据库/表格、计算、画图",
            system_prompt=(
                "你是一名数据分析师(analyst)子智能体。负责数据查询、统计计算、图表生成。"
                "使用数据库工具/SQLite/计算工具/图表工具。给出:分析结论、关键数字、图表(如生成)。"
            ),
            tools=["query_sqlite", "list_sqlite_tables", "describe_table", "calculate",
                   "generate_chart", "generate_bar_chart", "generate_line_chart", "generate_pie_chart"],
            max_steps=10,
        ),
        AgentRole(
            name="writer",
            description="写作润色:撰写/改写文档与文案",
            system_prompt=(
                "你是一名写作助手(writer)子智能体。负责撰写、润色、改写中文文档与文案。"
                "保持事实准确、条理清晰、语气一致。完成后输出成稿与要点说明。"
            ),
            tools=["read_document", "write_file", "list_directory", "search_files"],
            max_steps=6,
        ),
        AgentRole(
            name="security_reviewer",
            description="安全审查:检查命令/代码/配置的安全风险",
            system_prompt=(
                "你是一名安全审查员(security_reviewer)子智能体。审查命令、代码、配置、SQL 的安全风险:"
                "注入、越权、敏感信息泄露、危险操作、依赖风险。只审查,不执行危险操作。"
                "输出风险清单:严重程度、风险说明、修复建议。"
            ),
            tools=["code_context", "code_search", "read_document", "search_files", "list_directory"],
            max_steps=10,
        ),
    ]


def ensure_default_roles() -> None:
    """若注册表为空,注册默认角色团队。"""
    reg = get_role_registry()
    if not reg.team.names():
        register_roles(default_roles())
