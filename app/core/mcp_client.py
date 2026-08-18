"""MCP (Model Context Protocol) 客户端:连接外部 MCP 服务器,调用远程工具。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import httpx

from app.config import settings
from app.core.mcp_stdio import MCPStdioClient


class MCPClient:
    """MCP HTTP 客户端,支持与 MCP 服务器通信。"""
    
    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 30.0):
        """
        初始化 MCP 客户端。
        
        Args:
            base_url: MCP 服务器地址
            api_key: API 密钥(可选)
            timeout: 请求超时时间
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._tools_cache: list[dict] = []
        self._connected = False
    
    def _get_headers(self) -> dict:
        """获取请求头。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    async def connect(self) -> dict:
        """
        连接到 MCP 服务器并获取初始化信息。
        
        Returns:
            服务器信息
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                info = response.json()
                self._connected = True
                return {"status": "connected", "server": info}
        except Exception as e:
            return {"status": "error", "error": f"连接失败: {e}"}
    
    async def list_tools(self) -> list[dict]:
        """
        获取 MCP 服务器提供的工具列表。
        
        Returns:
            工具列表
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/tools",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
                self._tools_cache = data.get("tools", [])
                return self._tools_cache
        except Exception as e:
            return [{"error": f"获取工具列表失败: {e}"}]
    
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        """
        调用 MCP 服务器上的工具。
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
        
        Returns:
            工具执行结果
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/tools/{tool_name}/call",
                    headers=self._get_headers(),
                    json={"arguments": arguments},
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP 错误: {e.response.status_code}", "detail": str(e)}
        except Exception as e:
            return {"error": f"调用工具失败: {e}"}
    
    async def get_tool_schema(self, tool_name: str) -> Optional[dict]:
        """
        获取指定工具的 schema。
        
        Args:
            tool_name: 工具名称
        
        Returns:
            工具 schema
        """
        if not self._tools_cache:
            await self.list_tools()
        
        for tool in self._tools_cache:
            if tool.get("name") == tool_name:
                return tool
        return None
    
    def to_openai_schemas(self) -> list[dict]:
        """
        将 MCP 工具列表转换为 OpenAI function calling 格式。
        
        Returns:
            OpenAI 格式的工具列表
        """
        schemas = []
        for tool in self._tools_cache:
            schema = {
                "type": "function",
                "function": {
                    "name": f"mcp_{tool.get('name', 'unknown')}",
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            }
            schemas.append(schema)
        return schemas


class MCPManager:
    """MCP 客户端管理器:管理多个 MCP 服务器连接。"""
    
    def __init__(self):
        self._clients: dict[str, MCPClient] = {}
        self._tools_map: dict[str, tuple[str, MCPClient]] = {}  # tool_name -> (mcp_name, client)
    
    def add_client(self, name: str, client: MCPClient) -> None:
        """添加 MCP 客户端。"""
        self._clients[name] = client

    async def reset(self) -> None:
        """清空全部 MCP 客户端与工具映射(用于重新加载配置)。"""
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._tools_map.clear()

    async def remove_client(self, name: str) -> None:
        """移除 MCP 客户端。"""
        if name in self._clients:
            client = self._clients[name]
            # 移除该客户端的所有工具映射
            to_remove = [k for k, v in self._tools_map.items() if v[0] == name]
            for k in to_remove:
                del self._tools_map[k]
            del self._clients[name]
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
    
    def get_client(self, name: str) -> Optional[MCPClient]:
        """获取指定名称的客户端。"""
        return self._clients.get(name)
    
    async def connect_all(self) -> dict:
        """连接所有配置的 MCP 服务器。"""
        results = {}
        for name, client in self._clients.items():
            result = await client.connect()
            results[name] = result
            if result.get("status") == "connected":
                # 连接成功后获取工具列表
                tools = await client.list_tools()
                for tool in tools:
                    tool_name = tool.get("name", "")
                    if tool_name:
                        self._tools_map[tool_name] = (name, client)
        return results
    
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """
        调用工具(自动路由到对应的 MCP 服务器)。
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
        
        Returns:
            工具执行结果
        """
        if tool_name in self._tools_map:
            mcp_name, client = self._tools_map[tool_name]
            return await client.call_tool(tool_name, arguments)
        return {"error": f"未找到工具: {tool_name}"}
    
    def list_all_tools(self) -> list[dict]:
        """列出所有可用的 MCP 工具。"""
        tools = []
        for tool_name, (mcp_name, client) in self._tools_map.items():
            for tool in client._tools_cache:
                if tool.get("name") == tool_name:
                    tools.append({
                        "name": tool_name,
                        "mcp_server": mcp_name,
                        "description": tool.get("description", ""),
                        "inputSchema": tool.get("inputSchema", {}),
                    })
                    break
        return tools
    
    def to_openai_schemas(self) -> list[dict]:
        """将所有 MCP 工具转换为 OpenAI 格式。"""
        schemas = []
        seen = set()
        for client in self._clients.values():
            for schema in client.to_openai_schemas():
                func_name = schema["function"]["name"]
                if func_name not in seen:
                    seen.add(func_name)
                    schemas.append(schema)
        return schemas


# 全局 MCP 管理器
_mcp_manager: Optional[MCPManager] = None


def get_mcp_manager() -> MCPManager:
    """获取全局 MCP 管理器。"""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager


async def init_mcp_from_config() -> dict:
    """
    从配置初始化 MCP 连接(先重置,再按配置重建)。

    配置格式(在 .env 中):
      HTTP 版(自研 /mcp 协议):
        MCP_SERVERS={"server1": {"url": "http://localhost:3000", "api_key": "xxx"}}
      stdio 版(标准 MCP 服务器,npx/uvx/python 等):
        MCP_SERVERS={"filesystem": {"type": "stdio", "command": "npx",
                                     "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/path"]}}
        MCP_SERVERS={"github": {"type": "stdio", "command": "uvx", "args": ["mcp-server-git"]}}

    Returns:
        初始化结果(每个服务器的连接状态)
    """
    import os

    manager = get_mcp_manager()
    manager.reset()
    servers_config = os.environ.get("MCP_SERVERS", "{}")

    try:
        servers = json.loads(servers_config) if servers_config else {}
    except json.JSONDecodeError:
        return {"error": "MCP_SERVERS 配置格式错误"}

    for name, config in servers.items():
        if not isinstance(config, dict):
            continue
        mcp_type = config.get("type", "http")
        if mcp_type == "stdio" and config.get("command"):
            client = MCPStdioClient(
                command=config["command"],
                args=config.get("args") or [],
                env=config.get("env") or {},
                timeout=float(config.get("timeout", 60)),
                cwd=config.get("cwd"),
                auto_reconnect=True,
            )
        else:
            url = config.get("url", "")
            if not url:
                continue
            client = MCPClient(
                base_url=url,
                api_key=config.get("api_key"),
                timeout=float(config.get("timeout", 30)),
            )
        manager.add_client(name, client)

    return await manager.connect_all()
