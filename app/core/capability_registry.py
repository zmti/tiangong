"""统一能力注册表:整合 MCP Tools、Agent Skills、内置工具。

核心能力:
1. 统一接口: 所有能力(工具/技能/MCP)通过同一注册表查询/调用
2. 双向导出: Skill <-> MCP Tool 互转
3. 能力发现: 自然语言查询、标签过滤、依赖分析
3. 生命周期管理: 安装/启用/禁用/版本/审计
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings
from app.core.mcp_client import get_mcp_manager, MCPManager
from app.core.skill_marketplace import get_skill_marketplace, SkillMarketplace, InstalledSkill, SkillStatus
from app.tools.registry import get_registry, ToolRegistry
from app.core.observability import get_observability
from app.core.guardrails_semantic import get_security_guard

logger = logging.getLogger(__name__)


# ============================================================
# 统一能力模型
# ============================================================

class CapabilityType(str):
    BUILTIN_TOOL = "builtin_tool"       # 内置工具(run_terminal, write_file 等)
    MCP_TOOL = "mcp_tool"               # MCP 服务器工具
    MCP_PROMPT = "mcp_prompt"           # MCP 提示词模板
    MCP_RESOURCE = "mcp_resource"       # MCP 资源
    AGENT_SKILL = "agent_skill"         # Agent 技能(skills.py)
    MARKETPLACE_SKILL = "marketplace_skill"  # 市场技能(skill_marketplace.py)


@dataclass
class Capability:
    """统一能力描述。"""
    id: str                           # 全局唯一 ID: type:name@version
    name: str                         # 显示名称
    type: CapabilityType              # 能力类型
    description: str = ""             # 描述
    version: str = "1.0.0"            # 版本
    source: str = ""                  # 来源: mcp_server_name / skill_registry / builtin
    
    # Schema 信息
    parameters: dict = field(default_factory=dict)  # JSON Schema
    required_params: list[str] = field(default_factory=list)
    
    # 运行时信息
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    
    # 执行器引用(不序列化)
    _executor: Any = field(default=None, repr=False)
    
    def to_openai_schema(self) -> dict:
        """转换为 OpenAI function schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.id.replace(":", "_").replace("@", "_"),
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required_params,
                    "additionalProperties": False,
                },
            },
        }
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value if hasattr(self.type, 'value') else self.type
        d.pop("_executor", None)
        return d


# ============================================================
# 统一能力注册表
# ============================================================

class CapabilityRegistry:
    """统一能力注册表:管理所有类型的能力。"""
    
    def __init__(
        self,
        tool_registry: ToolRegistry = None,
        mcp_manager: MCPManager = None,
        skill_marketplace: SkillMarketplace = None,
    ):
        self._tool_registry = tool_registry or get_registry()
        self._mcp_manager = mcp_manager or get_mcp_manager()
        self._skill_marketplace = skill_marketplace or get_skill_marketplace()
        self._skill_registry = get_skill_registry()
        
        self._capabilities: dict[str, Capability] = {}
        self._lock = threading.RLock()
        self._initialized = False
    
    def initialize(self) -> None:
        """初始化:加载所有能力源。"""
        if self._initialized:
            return
        
        with self._lock:
            # 1. 内置工具
            self._load_builtin_tools()
            
            # 2. MCP 能力
            self._load_mcp_capabilities()
            
            # 3. Agent Skills
            self._load_agent_skills()
            
            # 4. Marketplace Skills
            self._load_marketplace_skills()
            
            self._initialized = True
            logger.info("能力注册表初始化完成: 共 %d 个能力", len(self._capabilities))
    
    def _load_builtin_tools(self) -> None:
        """加载内置工具。"""
        for tool in self._tool_registry.list_sync():
            cap = Capability(
                id=f"builtin_tool:{tool.name}",
                name=tool.name,
                type=CapabilityType.BUILTIN_TOOL,
                description=tool.description or "",
                version="1.0.0",
                source="builtin",
                parameters=tool.inputSchema or {"type": "object", "properties": {}},
                required_params=tool.inputSchema.get("required", []) if tool.inputSchema else [],
                tags=["builtin", tool.category] if hasattr(tool, 'category') else ["builtin"],
                _executor=tool,
            )
            self._capabilities[cap.id] = cap
    
    def _load_mcp_capabilities(self) -> None:
        """加载 MCP 工具/Prompts/Resources。"""
        # Tools
        for tool in self._mcp_manager.list_all_tools():
            cap = Capability(
                id=f"mcp_tool:{tool['mcp_server']}:{tool['name']}",
                name=tool['name'],
                type=CapabilityType.MCP_TOOL,
                description=tool.get('description', ''),
                version="1.0.0",
                source=tool['mcp_server'],
                parameters=tool.get('inputSchema', {"type": "object", "properties": {}}),
                required_params=tool.get('inputSchema', {}).get('required', []),
                tags=["mcp", tool['mcp_server']],
                metadata={"mcp_server": tool['mcp_server']},
            )
            self._capabilities[cap.id] = cap
        
        # Prompts
        for prompt in self._mcp_manager.list_all_prompts():
            cap = Capability(
                id=f"mcp_prompt:{prompt['mcp_server']}:{prompt['name']}",
                name=prompt['name'],
                type=CapabilityType.MCP_PROMPT,
                description=prompt.get('description', ''),
                version="1.0.0",
                source=prompt['mcp_server'],
                parameters={"arguments": prompt.get('arguments', [])},
                required_params=[],
                tags=["mcp", "prompt", prompt['mcp_server']],
                metadata={"mcp_server": prompt['mcp_server']},
            )
            self._capabilities[cap.id] = cap
        
        # Resources
        for resource in self._mcp_manager.list_all_resources():
            cap = Capability(
                id=f"mcp_resource:{resource['mcp_server']}:{resource['uri']}",
                name=resource['name'],
                type=CapabilityType.MCP_RESOURCE,
                description=resource.get('description', ''),
                version="1.0.0",
                source=resource['mcp_server'],
                parameters={"uri": resource['uri']},
                required_params=["uri"],
                tags=["mcp", "resource", resource['mcp_server']],
                metadata={"mcp_server": resource['mcp_server'], "uri": resource['uri']},
            )
            self._capabilities[cap.id] = cap
    
    def _load_agent_skills(self) -> None:
        """加载 Agent Skills (skills.py)。"""
        from app.core.skills import get_skill_registry
        skill_registry = get_skill_registry()
        for skill in skill_registry.list():
            cap = Capability(
                id=f"agent_skill:{skill.name}",
                name=skill.name,
                type=CapabilityType.AGENT_SKILL,
                description=skill.description,
                version=str(skill.version),
                source="skill_registry",
                parameters=skill.to_openai_schema()["function"]["parameters"],
                required_params=skill.to_openai_schema()["function"]["parameters"].get("required", []),
                tags=["skill", "agent"] + [s.tool for s in skill.steps],
                metadata={"timeout": skill.timeout, "steps": len(skill.steps)},
            )
            self._capabilities[cap.id] = cap
    
    def _load_marketplace_skills(self) -> None:
        """加载 Marketplace Skills (skill_marketplace.py)。"""
        for skill in self._skill_marketplace.list_installed():
            if skill.status != SkillStatus.ENABLED:
                continue
            cap = Capability(
                id=f"marketplace_skill:{skill.name}@{skill.version}",
                name=skill.name,
                type=CapabilityType.MARKETPLACE_SKILL,
                description=skill.manifest.description if skill.manifest else "",
                version=skill.version,
                source=skill.skill_id,
                parameters={"payload": {"type": "object", "properties": {}}},
                required_params=["payload"],
                tags=["marketplace", "skill"] + (skill.manifest.tags if skill.manifest else []),
                metadata={"skill_id": skill.skill_id, "status": skill.status.value},
            )
            self._capabilities[cap.id] = cap
    
    # ============================================================
    # 查询接口
    # ============================================================
    
    def get(self, capability_id: str) -> Optional[Capability]:
        """获取单个能力。"""
        with self._lock:
            return self._capabilities.get(capability_id)
    
    def list(
        self,
        capability_type: CapabilityType = None,
        enabled_only: bool = True,
        tags: list[str] = None,
        limit: int = 100,
    ) -> list[Capability]:
        """列出能力。"""
        with self._lock:
            results = []
            for cap in self._capabilities.values():
                if enabled_only and not cap.enabled:
                    continue
                if capability_type and cap.type != capability_type:
                    continue
                if tags and not any(t in cap.tags for t in tags):
                    continue
                results.append(cap)
                if len(results) >= limit:
                    break
            return results
    
    def search(
        self,
        query: str,
        capability_type: CapabilityType = None,
        limit: int = 20,
    ) -> list[Capability]:
        """关键词搜索能力。"""
        query_lower = query.lower()
        results = []
        with self._lock:
            for cap in self._capabilities.values():
                if capability_type and cap.type != capability_type:
                    continue
                if not cap.enabled:
                    continue
                if (query_lower in cap.name.lower() or 
                    query_lower in cap.description.lower() or
                    any(query_lower in tag.lower() for tag in cap.tags)):
                    results.append(cap)
                    if len(results) >= limit:
                        break
        return results
    
    def discover_for_task(self, task_description: str, top_k: int = 10) -> list[Capability]:
        """根据任务描述推荐相关能力。"""
        # 简单关键词匹配，可扩展为语义搜索
        return self.search(task_description, limit=top_k)
    
    # ============================================================
    # 执行接口
    # ============================================================
    
    async def execute(
        self,
        capability_id: str,
        arguments: dict,
        context: dict = None,
        tool_executor=None,
    ) -> dict:
        """统一执行接口。"""
        cap = self.get(capability_id)
        if not cap:
            return {"success": False, "error": f"能力不存在: {capability_id}"}
        
        if not cap.enabled:
            return {"success": False, "error": f"能力已禁用: {capability_id}"}
        
        start = time.time()
        
        try:
            if cap.type == CapabilityType.BUILTIN_TOOL:
                return await self._execute_builtin_tool(cap, arguments, tool_executor)
            
            elif cap.type == CapabilityType.MCP_TOOL:
                return await self._execute_mcp_tool(cap, arguments)
            
            elif cap.type == CapabilityType.MCP_PROMPT:
                return await self._execute_mcp_prompt(cap, arguments)
            
            elif cap.type == CapabilityType.MCP_RESOURCE:
                return await self._execute_mcp_resource(cap, arguments)
            
            elif cap.type == CapabilityType.AGENT_SKILL:
                return await self._execute_agent_skill(cap, arguments, tool_executor)
            
            elif cap.type == CapabilityType.MARKETPLACE_SKILL:
                return await self._execute_marketplace_skill(cap, arguments)
            
            else:
                return {"success": False, "error": f"不支持的能力类型: {cap.type}"}
        
        except Exception as e:
            logger.error("执行能力 %s 失败: %s", capability_id, e)
            return {"success": False, "error": str(e), "duration_ms": int((time.time() - start) * 1000)}
    
    async def _execute_builtin_tool(self, cap: Capability, arguments: dict, tool_executor) -> dict:
        tool = cap._executor
        if tool is None:
            return {"success": False, "error": "工具执行器未绑定"}
        
        import json as _json
        result_str = await tool_executor.execute_tool(cap.name, _json.dumps(arguments, ensure_ascii=False))
        return {"success": True, "result": result_str, "duration_ms": int((time.time() - time.time()) * 1000)}
    
    async def _execute_mcp_tool(self, cap: Capability, arguments: dict) -> dict:
        mcp_manager = get_mcp_manager()
        result = await mcp_manager.call_tool(cap.name, arguments)
        return {"success": "error" not in result, "result": result.get("result"), "error": result.get("error")}
    
    async def _execute_mcp_prompt(self, cap: Capability, arguments: dict) -> dict:
        mcp_manager = get_mcp_manager()
        result = await mcp_manager.get_prompt(cap.name, arguments)
        return {"success": "error" not in result, "result": result, "error": result.get("error")}
    
    async def _execute_mcp_resource(self, cap: Capability, arguments: dict) -> dict:
        mcp_manager = get_mcp_manager()
        uri = arguments.get("uri", cap.metadata.get("uri", ""))
        result = await mcp_manager.read_resource(uri)
        return {"success": True, "result": {"text": result.text, "blob": result.blob, "mime_type": result.mime_type}}
    
    async def _execute_agent_skill(self, cap: Capability, arguments: dict, tool_executor) -> dict:
        from app.core.skills import get_skill_registry, SkillExecutor
        skill = get_skill_registry().get(cap.name)
        if not skill:
            return {"success": False, "error": f"Agent Skill 未找到: {cap.name}"}
        
        executor = SkillExecutor(tool_executor)
        result = await executor.execute(skill, arguments)
        return result.to_dict()
    
    async def _execute_marketplace_skill(self, cap: Capability, arguments: dict) -> dict:
        skill_name = cap.name
        result = self._skill_marketplace.execute_skill(skill_name, arguments.get("payload", {}))
        return result
    
    # ============================================================
    # 双向导出: Skill <-> MCP Tool
    # ============================================================
    
    def skill_to_mcp_tool(self, skill_name: str) -> dict:
        """将 Agent Skill 导出为 MCP Tool 格式。"""
        from app.core.skills import get_skill_registry, StepType
        skill = get_skill_registry().get(skill_name)
        if not skill:
            raise ValueError(f"Skill 未找到: {skill_name}")
        
        # 提取依赖工具
        tool_names = set()
        for step in skill.steps:
            if step.type == StepType.TOOL:
                tool_names.add(step.tool)
        
        return {
            "name": skill.name,
            "description": skill.description,
            "inputSchema": skill.to_openai_schema()["function"]["parameters"],
            "mcp_meta": {
                "source_skill": skill.name,
                "version": skill.version,
                "required_tools": list(tool_names),
                "steps": len(skill.steps),
            },
        }
    
    def mcp_tool_to_skill(self, mcp_name: str, tool_names: list[str]):
        """将 MCP Tool(s) 封装为 Agent Skill。"""
        from app.core.skills import Skill, SkillStep, SkillParam, StepType
        from app.core.mcp_client import get_mcp_manager
        mcp_manager = get_mcp_manager()
        steps = []
        all_params = {}
        
        for tool_name in tool_names:
            tool = None
            for t in mcp_manager.list_all_tools():
                if t["name"] == tool_name:
                    tool = t
                    break
            if not tool:
                continue
            
            steps.append(SkillStep(
                type=StepType.TOOL,
                tool=f"mcp_{tool_name}",
                args={},
                label=tool_name,
            ))
            # 合并参数 schema
            schema = tool.get("inputSchema", {})
            for prop_name, prop_def in schema.get("properties", {}).items():
                if prop_name not in all_params:
                    all_params[prop_name] = SkillParam(
                        name=prop_name,
                        type_=prop_def.get("type", "string"),
                        description=prop_def.get("description", ""),
                    )
        
        return Skill(
            name=f"mcp_combo_{mcp_name}",
            description=f"组合 MCP 工具: {', '.join(tool_names)}",
            steps=steps,
            params=list(all_params.values()),
            timeout=120.0,
        )
    
    def skill_to_marketplace(self, skill_name: str) -> InstalledSkill:
        """将 Agent Skill 发布到 Marketplace。"""
        return get_skill_marketplace().install_skill(get_skill_registry().get(skill_name))
    
    def marketplace_to_skill(self, skill_id: str):
        """从 Marketplace 加载 Skill 到 Agent 注册表。"""
        from app.core.skills import Skill, get_skill_registry
        skill = get_skill_marketplace().get_installed(skill_id=skill_id)
        if not skill:
            raise ValueError(f"Marketplace Skill 未找到: {skill_id}")
        
        skill_file = Path(skill.install_path) / f"{skill.name}.json"
        if not skill_file.exists():
            raise ValueError(f"技能定义文件丢失: {skill_file}")
        
        with open(skill_file, "r", encoding="utf-8") as f:
            skill_data = json.load(f)
        
        skill_obj = Skill.from_dict(skill_data)
        get_skill_registry().register(skill_obj)
        return skill_obj
    
    # ============================================================
    # 生命周期管理
    # ============================================================
    
    def enable(self, capability_id: str) -> bool:
        cap = self.get(capability_id)
        if not cap:
            return False
        cap.enabled = True
        return True
    
    def disable(self, capability_id: str) -> bool:
        cap = self.get(capability_id)
        if not cap:
            return False
        cap.enabled = False
        return True
    
    def get_stats(self) -> dict:
        """获取注册表统计。"""
        with self._lock:
            stats = {
                "total": len(self._capabilities),
                "by_type": {},
                "by_source": {},
                "enabled": 0,
                "disabled": 0,
            }
            for cap in self._capabilities.values():
                t = cap.type.value if hasattr(cap.type, 'value') else cap.type
                stats["by_type"][t] = stats["by_type"].get(t, 0) + 1
                stats["by_source"][cap.source] = stats["by_source"].get(cap.source, 0) + 1
                if cap.enabled:
                    stats["enabled"] += 1
                else:
                    stats["disabled"] += 1
            return stats


# ============================================================
# 全局单例
# ============================================================

_capability_registry: Optional[CapabilityRegistry] = None
_registry_lock = threading.Lock()


def get_capability_registry() -> CapabilityRegistry:
    global _capability_registry
    if _capability_registry is None:
        with _registry_lock:
            if _capability_registry is None:
                _capability_registry = CapabilityRegistry()
                _capability_registry.initialize()
    return _capability_registry


# ============================================================
# 便捷函数
# ============================================================

async def execute_capability(
    capability_id: str,
    arguments: dict,
    context: dict = None,
    tool_executor=None,
) -> dict:
    """便捷函数:执行能力。"""
    registry = get_capability_registry()
    return await registry.execute(capability_id, arguments, context, tool_executor)


def list_capabilities(
    capability_type: CapabilityType = None,
    enabled_only: bool = True,
    tags: list[str] = None,
    limit: int = 100,
) -> list[Capability]:
    """便捷函数:列出能力。"""
    registry = get_capability_registry()
    return registry.list(capability_type, enabled_only, tags, limit)


def search_capabilities(
    query: str,
    capability_type: CapabilityType = None,
    limit: int = 20,
) -> list[Capability]:
    """便捷函数:搜索能力。"""
    registry = get_capability_registry()
    return registry.search(query, capability_type, limit)


def discover_for_task(task_description: str, top_k: int = 10) -> list[Capability]:
    """便捷函数:根据任务推荐能力。"""
    registry = get_capability_registry()
    return registry.discover_for_task(task_description, top_k)