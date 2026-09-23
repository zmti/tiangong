"""MCP 工具包装器:将远程 MCP 工具注册为本地工具。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import Field

from app.tools.registry import Tool, ToolRegistry, get_registry, tool


class MCPToolWrapper:
    """将 MCP 远程工具包装为本地工具。"""
    
    def __init__(
        self,
        name: str,
        description: str,
        mcp_client,
        mcp_tool_name: str,
        input_schema: dict,
        timeout: float = 30.0,
    ):
        self.name = name
        self.description = description
        self.mcp_client = mcp_client
        self.mcp_tool_name = mcp_tool_name
        self.input_schema = input_schema
        self.timeout = timeout
        self.enabled = True
        self.terminal = False
        self.is_async = True
    
    def to_openai_schema(self) -> dict:
        """转换为 OpenAI function calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            }
        }
    
    async def run(self, arguments: str | dict) -> str:
        """执行远程 MCP 工具。"""
        import json
        
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"参数解析失败: {e}"}, ensure_ascii=False)
        else:
            args = arguments
        
        result = await self.mcp_client.call_tool(self.mcp_tool_name, args)
        
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)


def register_mcp_tools(mcp_manager=None) -> int:
    """
    将所有 MCP 工具注册为本地工具。
    
    Args:
        mcp_manager: MCP 管理器实例,如果为 None 则使用全局管理器
    
    Returns:
        注册的工具数量
    """
    if mcp_manager is None:
        from app.core.mcp_client import get_mcp_manager
        mcp_manager = get_mcp_manager()
    
    registry = get_registry()
    count = 0
    
    for mcp_tool in mcp_manager.list_all_tools():
        tool_name = mcp_tool.get("name", "")
        if not tool_name:
            continue
        
        # 跳过已存在的工具
        if registry.get(f"mcp_{tool_name}") is not None:
            continue
        
        # 创建工具包装器
        wrapper = MCPToolWrapper(
            name=f"mcp_{tool_name}",
            description=mcp_tool.get("description", f"MCP 工具: {tool_name}"),
            mcp_client=mcp_manager._tools_map[tool_name][1],
            mcp_tool_name=tool_name,
            input_schema=mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
        )

        async def bridge(**kwargs):
            """把参数整体作为 dict 转发给 MCP 包装器。"""
            return await wrapper.run(kwargs)

        # 注册到全局注册器
        tool_obj = Tool(
            name=wrapper.name,
            description=wrapper.description,
            func=bridge,
            timeout=wrapper.timeout,
            params_model=None,
            json_schema=wrapper.input_schema,
            enabled=True,
            terminal=False,
        )
        registry.register(tool_obj)
        count += 1
    
    return count


def unregister_mcp_tools(registry: Optional[ToolRegistry] = None) -> int:
    """
    移除所有 MCP 工具。
    
    Args:
        registry: 工具注册器,如果为 None 则使用全局注册器
    
    Returns:
        移除的工具数量
    """
    if registry is None:
        registry = get_registry()
    
    mcp_tools = [t for t in registry.list_sync(enabled_only=False) if t.name.startswith("mcp_")]
    
    for tool in mcp_tools:
        registry.unregister(tool.name)
    
    return len(mcp_tools)
