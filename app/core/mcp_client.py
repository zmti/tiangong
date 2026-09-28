"""MCP (Model Context Protocol) 客户端:连接外部 MCP 服务器,调用远程工具/提示词/资源。

支持完整 MCP 协议三大能力:
- Tools: 工具调用 (已有)
- Prompts: 提示词模板 (新增)
- Resources: 资源访问 (新增)

传输层: HTTP + stdio + SSE (新增)
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.core.mcp_stdio import MCPStdioClient

logger = logging.getLogger(__name__)


# ============================================================
# MCP 核心数据模型
# ============================================================

@dataclass
class MCPPrompt:
    """MCP 提示词模板。"""
    name: str
    description: str
    arguments: list[dict] = None  # [{"name": "", "description": "", "required": bool}]


@dataclass
class MCPResource:
    """MCP 资源。"""
    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"


@dataclass
class MCPResourceContent:
    """资源内容。"""
    uri: str
    mime_type: str
    text: str = ""
    blob: str = ""  # base64


# ============================================================
# HTTP 客户端增强
# ============================================================

class MCPClient:
    """MCP HTTP 客户端:支持 Tools/Prompts/Resources + SSE 流式。"""
    
    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 30.0,
                 tenant_id: str = "default"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.tenant_id = tenant_id
        self._tools_cache: list[dict] = []
        self._prompts_cache: list[MCPPrompt] = []
        self._resources_cache: list[MCPResource] = []
        self._connected = False
        self._sse_task: Optional[asyncio.Task] = None
        self._sse_client: Optional[httpx.AsyncClient] = None
    
    def _get_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Tenant-ID": self.tenant_id,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    async def connect(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                info = response.json()
                self._connected = True
                # 预加载缓存
                await asyncio.gather(
                    self.list_tools(),
                    self.list_prompts(),
                    self.list_resources(),
                    return_exceptions=True,
                )
                return {"status": "connected", "server": info}
        except Exception as e:
            return {"status": "error", "error": f"连接失败: {e}"}
    
    # ---- Tools ----
    async def list_tools(self) -> list[dict]:
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
    
    # ---- Prompts (新增) ----
    async def list_prompts(self) -> list[MCPPrompt]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/prompts",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
                prompts = []
                for p in data.get("prompts", []):
                    prompts.append(MCPPrompt(
                        name=p.get("name", ""),
                        description=p.get("description", ""),
                        arguments=p.get("arguments", []),
                    ))
                self._prompts_cache = prompts
                return prompts
        except Exception as e:
            logger.warning("获取 prompts 失败: %s", e)
            return []
    
    async def get_prompt(self, name: str, arguments: dict = None) -> dict:
        """获取渲染后的提示词。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/prompts/{name}/get",
                    headers=self._get_headers(),
                    json={"arguments": arguments or {}},
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            return {"error": f"获取提示词失败: {e}"}
    
    # ---- Resources (新增) ----
    async def list_resources(self) -> list[MCPResource]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/resources",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()
                resources = []
                for r in data.get("resources", []):
                    resources.append(MCPResource(
                        uri=r.get("uri", ""),
                        name=r.get("name", ""),
                        description=r.get("description", ""),
                        mime_type=r.get("mimeType", "text/plain"),
                    ))
                self._resources_cache = resources
                return resources
        except Exception as e:
            logger.warning("获取 resources 失败: %s", e)
            return []
    
    async def read_resource(self, uri: str) -> MCPResourceContent:
        """读取资源内容。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/resources/read",
                    headers=self._get_headers(),
                    json={"uri": uri},
                )
                response.raise_for_status()
                data = response.json()
                contents = data.get("contents", [])
                if contents:
                    c = contents[0]
                    return MCPResourceContent(
                        uri=c.get("uri", uri),
                        mime_type=c.get("mimeType", "text/plain"),
                        text=c.get("text", ""),
                        blob=c.get("blob", ""),
                    )
                return MCPResourceContent(uri=uri, mime_type="text/plain", text="")
        except Exception as e:
            return MCPResourceContent(uri=uri, mime_type="text/plain", text=f"Error: {e}")
    
    async def subscribe_resource(self, uri: str) -> bool:
        """订阅资源变更通知。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/resources/subscribe",
                    headers=self._get_headers(),
                    json={"uri": uri},
                )
                response.raise_for_status()
                return True
        except Exception as e:
            logger.warning("订阅资源失败: %s", e)
            return False
    
    # ---- SSE 流式 (新增) ----
    async def stream_tool(self, tool_name: str, arguments: dict) -> AsyncGenerator[dict, None]:
        """SSE 流式调用工具(需服务端支持)。"""
        if not self._sse_client:
            self._sse_client = httpx.AsyncClient(timeout=None)
        
        async with self._sse_client.stream(
            "POST",
            f"{self.base_url}/tools/{tool_name}/stream",
            headers=self._get_headers(),
            json={"arguments": arguments},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        pass
    
    async def stream_prompt(self, name: str, arguments: dict) -> AsyncGenerator[str, None]:
        """SSE 流式获取提示词。"""
        if not self._sse_client:
            self._sse_client = httpx.AsyncClient(timeout=None)
        
        async with self._sse_client.stream(
            "POST",
            f"{self.base_url}/prompts/{name}/stream",
            headers=self._get_headers(),
            json={"arguments": arguments},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    yield data
    
    async def close(self) -> None:
        if self._sse_client:
            await self._sse_client.aclose()
            self._sse_client = None
        if self._sse_task:
            self._sse_task.cancel()
            self._sse_task = None
    
    # ---- 缓存访问 ----
    def get_cached_tools(self) -> list[dict]:
        return self._tools_cache
    
    def get_cached_prompts(self) -> list[MCPPrompt]:
        return self._prompts_cache
    
    def get_cached_resources(self) -> list[MCPResource]:
        return self._resources_cache
    
    def to_openai_schemas(self) -> list[dict]:
        schemas = []
        for tool in self._tools_cache:
            schemas.append({
                "type": "function",
                "function": {
                    "name": f"mcp_{tool.get('name', 'unknown')}",
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            })
        return schemas


class MCPManager:
    """MCP 客户端管理器:管理多个 MCP 服务器连接,支持 Tools/Prompts/Resources/SSE。"""
    
    def __init__(self, cache_db_path: Optional[str] = None):
        self._clients: dict[str, MCPClient] = {}
        self._tools_map: dict[str, tuple[str, MCPClient]] = {}  # tool_name -> (mcp_name, client)
        self._prompts_map: dict[str, tuple[str, MCPClient]] = {}  # prompt_name -> (mcp_name, client)
        self._resources_map: dict[str, tuple[str, MCPClient]] = {}  # resource_uri -> (mcp_name, client)
        self._cache_db = cache_db_path
        self._init_cache()
    
    def _init_cache(self) -> None:
        """初始化工具缓存数据库。"""
        if not self._cache_db:
            return
        try:
            from app.core.storage_backend import get_storage_backend
            backend = get_storage_backend(db_path=self._cache_db)
            schema = {
                "mcp_tool_cache": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "mcp_name", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "tool_json", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "cached_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
                "mcp_prompt_cache": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "mcp_name", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "prompt_json", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "cached_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
                "mcp_resource_cache": [
                    {"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"},
                    {"name": "mcp_name", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "resource_json", "type": "TEXT", "constraints": "NOT NULL"},
                    {"name": "cached_at", "type": "REAL", "constraints": "NOT NULL"},
                ],
            }
            backend.init_db(schema)
        except Exception as e:
            logger.warning("MCP 缓存初始化失败: %s", e)
            self._cache_db = None
    
    def add_client(self, name: str, client: MCPClient) -> None:
        """添加 MCP 客户端。"""
        self._clients[name] = client
        # 尝试恢复缓存
        self._restore_cache(name)

    def _restore_cache(self, mcp_name: str) -> None:
        """从数据库恢复缓存。"""
        if not self._cache_db:
            return
        try:
            from app.core.storage_backend import get_storage_backend
            backend = get_storage_backend(db_path=self._cache_db)
            
            # 恢复工具
            rows = backend.select("mcp_tool_cache", "mcp_name = ?", (mcp_name,))
            for row in rows:
                tool = json.loads(row["tool_json"])
                tool_name = tool.get("name", "")
                if tool_name:
                    self._tools_map[tool_name] = (mcp_name, self._clients[mcp_name])
            
            # 恢复 prompts
            rows = backend.select("mcp_prompt_cache", "mcp_name = ?", (mcp_name,))
            for row in rows:
                prompt = json.loads(row["prompt_json"])
                p_name = prompt.get("name", "")
                if p_name:
                    self._prompts_map[p_name] = (mcp_name, self._clients[mcp_name])
            
            # 恢复 resources
            rows = backend.select("mcp_resource_cache", "mcp_name = ?", (mcp_name,))
            for row in rows:
                resource = json.loads(row["resource_json"])
                uri = resource.get("uri", "")
                if uri:
                    self._resources_map[uri] = (mcp_name, self._clients[mcp_name])
        except Exception as e:
            logger.warning("恢复 MCP 缓存失败: %s", e)
    
    def _save_cache(self, mcp_name: str, client: MCPClient) -> None:
        """保存缓存到数据库。"""
        if not self._cache_db:
            return
        try:
            from app.core.storage_backend import get_storage_backend
            backend = get_storage_backend(db_path=self._cache_db)
            now = time.time()
            
            # 保存工具
            for tool in client._tools_cache:
                backend.upsert(
                    "mcp_tool_cache",
                    {
                        "id": f"{mcp_name}:{tool.get('name', '')}",
                        "mcp_name": mcp_name,
                        "tool_json": json.dumps(tool, ensure_ascii=False),
                        "cached_at": now,
                    },
                    conflict_columns=["id"],
                )
            
            # 保存 prompts
            for prompt in client._prompts_cache:
                backend.upsert(
                    "mcp_prompt_cache",
                    {
                        "id": f"{mcp_name}:{prompt.name}",
                        "mcp_name": mcp_name,
                        "prompt_json": json.dumps({
                            "name": prompt.name,
                            "description": prompt.description,
                            "arguments": prompt.arguments,
                        }, ensure_ascii=False),
                        "cached_at": now,
                    },
                    conflict_columns=["id"],
                )
            
            # 保存 resources
            for resource in client._resources_cache:
                backend.upsert(
                    "mcp_resource_cache",
                    {
                        "id": f"{mcp_name}:{resource.uri}",
                        "mcp_name": mcp_name,
                        "resource_json": json.dumps({
                            "uri": resource.uri,
                            "name": resource.name,
                            "description": resource.description,
                            "mime_type": resource.mime_type,
                        }, ensure_ascii=False),
                        "cached_at": now,
                    },
                    conflict_columns=["id"],
                )
        except Exception as e:
            logger.warning("保存 MCP 缓存失败: %s", e)

    async def reset(self) -> None:
        """清空全部 MCP 客户端与工具映射(用于重新加载配置)。"""
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._tools_map.clear()
        self._prompts_map.clear()
        self._resources_map.clear()

    async def remove_client(self, name: str) -> None:
        """移除 MCP 客户端。"""
        if name in self._clients:
            client = self._clients[name]
            # 移除该客户端的所有映射
            to_remove = [k for k, v in self._tools_map.items() if v[0] == name]
            for k in to_remove:
                del self._tools_map[k]
            to_remove = [k for k, v in self._prompts_map.items() if v[0] == name]
            for k in to_remove:
                del self._prompts_map[k]
            to_remove = [k for k, v in self._resources_map.items() if v[0] == name]
            for k in to_remove:
                del self._resources_map[k]
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
                # 连接成功后获取所有能力
                tools = await client.list_tools()
                for tool in tools:
                    tool_name = tool.get("name", "")
                    if tool_name:
                        self._tools_map[tool_name] = (name, client)
                
                prompts = await client.list_prompts()
                for prompt in prompts:
                    self._prompts_map[prompt.name] = (name, client)
                
                resources = await client.list_resources()
                for resource in resources:
                    self._resources_map[resource.uri] = (name, client)
                
                # 保存缓存
                self._save_cache(name, client)
        return results
    
    # ---- Tools ----
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        if tool_name in self._tools_map:
            mcp_name, client = self._tools_map[tool_name]
            return await client.call_tool(tool_name, arguments)
        return {"error": f"未找到工具: {tool_name}"}
    
    async def stream_tool(self, tool_name: str, arguments: dict):
        """SSE 流式调用工具。"""
        if tool_name in self._tools_map:
            mcp_name, client = self._tools_map[tool_name]
            async for chunk in client.stream_tool(tool_name, arguments):
                yield chunk
        else:
            yield {"error": f"未找到工具: {tool_name}"}
    
    def list_all_tools(self) -> list[dict]:
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
    
    # ---- Prompts (新增) ----
    async def get_prompt(self, prompt_name: str, arguments: dict = None) -> dict:
        if prompt_name in self._prompts_map:
            mcp_name, client = self._prompts_map[prompt_name]
            return await client.get_prompt(prompt_name, arguments)
        return {"error": f"未找到提示词: {prompt_name}"}
    
    async def stream_prompt(self, prompt_name: str, arguments: dict):
        if prompt_name in self._prompts_map:
            mcp_name, client = self._prompts_map[prompt_name]
            async for chunk in client.stream_prompt(prompt_name, arguments):
                yield chunk
        else:
            yield f"Error: 未找到提示词: {prompt_name}"
    
    def list_all_prompts(self) -> list[dict]:
        prompts = []
        for prompt_name, (mcp_name, client) in self._prompts_map.items():
            for prompt in client._prompts_cache:
                if prompt.name == prompt_name:
                    prompts.append({
                        "name": prompt_name,
                        "mcp_server": mcp_name,
                        "description": prompt.description,
                        "arguments": prompt.arguments,
                    })
                    break
        return prompts
    
    # ---- Resources (新增) ----
    async def read_resource(self, uri: str) -> MCPResourceContent:
        if uri in self._resources_map:
            mcp_name, client = self._resources_map[uri]
            return await client.read_resource(uri)
        return MCPResourceContent(uri=uri, mime_type="text/plain", text=f"未找到资源: {uri}")
    
    async def subscribe_resource(self, uri: str) -> bool:
        if uri in self._resources_map:
            mcp_name, client = self._resources_map[uri]
            return await client.subscribe_resource(uri)
        return False
    
    def list_all_resources(self) -> list[dict]:
        resources = []
        for uri, (mcp_name, client) in self._resources_map.items():
            for resource in client._resources_cache:
                if resource.uri == uri:
                    resources.append({
                        "uri": uri,
                        "mcp_server": mcp_name,
                        "name": resource.name,
                        "description": resource.description,
                        "mime_type": resource.mime_type,
                    })
                    break
        return resources
    
    def to_openai_schemas(self) -> list[dict]:
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
_mcp_manager_lock = threading.Lock()


def get_mcp_manager(cache_db_path: Optional[str] = None) -> MCPManager:
    """获取全局 MCP 管理器。"""
    global _mcp_manager
    if _mcp_manager is None:
        with _mcp_manager_lock:
            if _mcp_manager is None:
                _mcp_manager = MCPManager(cache_db_path=cache_db_path)
    return _mcp_manager


async def init_mcp_from_config(cache_db_path: Optional[str] = None) -> dict:
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

    manager = get_mcp_manager(cache_db_path=cache_db_path)
    await manager.reset()
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
            # 支持 tenant_id 配置
            tenant_id = config.get("tenant_id", "default")
            client = MCPClient(
                base_url=url,
                api_key=config.get("api_key"),
                timeout=float(config.get("timeout", 30)),
                tenant_id=tenant_id,
            )
        manager.add_client(name, client)

    return await manager.connect_all()
