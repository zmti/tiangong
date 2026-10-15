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
    
    async def _connect_one(self, name: str, client) -> dict:
        """连接单个 MCP 服务器并把其工具/提示词/资源挂进映射。返回该服务器结果。"""
        result = await client.connect()
        result.setdefault("connected", result.get("status") == "connected")
        if result.get("status") == "connected":
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
            self._save_cache(name, client)
        else:
            # 连接失败:记入内存,供 UI 展示原因(不再"静默 60s 后无反馈")
            client._connect_error = result.get("error", "连接失败")  # noqa: SLF001
        return result

    async def _auto_disable_failed(self, results: dict) -> None:
        """自动禁用连不上的服务器(写 enabled:false,下次启动不再尝试 → 消除反复 60s 卡顿)。
        可在桌面端「MCP 服务器」页重新启用,或在 mcp_servers.json 手改。"""
        auto_disable = True
        try:
            auto_disable = bool(getattr(settings, "mcp_auto_disable_on_failure", True))
        except Exception:  # noqa: BLE001
            pass
        if not auto_disable:
            return
        for name, res in results.items():
            if not isinstance(res, dict) or res.get("status") == "connected":
                continue
            try:
                set_mcp_server_enabled(name, False)
                res["auto_disabled"] = True
                res["reason"] = res.get("error", "连接失败")
                logger.warning("MCP 服务器 %s 连接失败,已自动禁用(可重新启用): %s",
                               name, res.get("error", ""))
            except Exception as e:  # noqa: BLE001
                logger.debug("自动禁用 %s 失败: %s", name, e)

    async def connect_all(self) -> dict:
        """并行连接所有配置的 MCP 服务器。

        并行是关键:此前是 for 循环逐个 `await`,某个 server 卡 60s 超时会把
        排在后面的全部堵住(实测 postgres 冷下载+连不上拖慢整个后端启动)。
        改 `asyncio.gather` 后各 server 同时连,最慢的那个不再拖累其他。
        """
        results: dict = {}

        async def _one(name: str, client) -> None:
            try:
                results[name] = await self._connect_one(name, client)
            except Exception as e:  # noqa: BLE001
                results[name] = {"status": "error", "error": str(e)}

        await asyncio.gather(*(_one(n, c) for n, c in self._clients.items()),
                             return_exceptions=True)
        await self._auto_disable_failed(results)
        return results

    async def connect_named(self, pairs: list[tuple[str, object]]) -> dict:
        """增量连接:只连 pairs 里指定的服务器(其余已连接的保持不变)。

        供「MCP 保存/增删/启停」的增量刷新用:未变化的服务器不重连,避免每次
        保存都把全部 stdio 服务器重新 initialize(冷 npx/超时叠加,实测 ~50s)。
        """
        results: dict = {}

        async def _one(name: str, client) -> None:
            try:
                results[name] = await self._connect_one(name, client)
            except Exception as e:  # noqa: BLE001
                results[name] = {"status": "error", "error": str(e)}

        await asyncio.gather(*(_one(n, c) for n, c in pairs), return_exceptions=True)
        await self._auto_disable_failed(results)
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


def _load_mcp_servers_config() -> Optional[dict]:
    """读取 MCP 服务器配置(2026-10-01)。

    优先级:
      1. 数据目录下的 mcp_servers.json(桌面端最方便入口;settings.resolved_data_dir 同级)
      2. 环境变量 MCP_SERVERS(JSON 字符串)

    容忍两种外壳:
      - 扁平:  {"name": {"type": "http", "url": ...}, ...}
      - Claude Desktop 风格:  {"mcpServers": {"name": {...}, ...}}  → 自动拍平
      - 双层同名嵌套:  {"name": {"name": {"url": ...}}}          → 自动下钻
      解析失败返回 None(调用方回 "配置格式错误")。
    """
    import os
    from pathlib import Path

    raw: dict = {}
    p = Path(settings.resolved_data_dir) / "mcp_servers.json"
    if p.is_file():
        try:
            raw = json.loads(p.read_text("utf-8"))
            logger.info("MCP 配置从文件加载: %s", p)
        except Exception as e:  # noqa: BLE001
            logger.error("读取 mcp_servers.json 失败: %s", e)
            return None
    else:
        env_cfg = os.environ.get("MCP_SERVERS", "").strip()
        if env_cfg:
            try:
                raw = json.loads(env_cfg)
                logger.info("MCP 配置从环境变量加载: MCP_SERVERS")
            except json.JSONDecodeError:
                logger.error("环境变量 MCP_SERVERS 不是合法 JSON: %r", env_cfg[:200])
                return None
        else:
            return {}

    if not isinstance(raw, dict):
        logger.error("MCP 配置顶层必须是对象, 实际: %s", type(raw).__name__)
        return None

    # 拍平 mcpServers 外壳(Claude Desktop 风格)
    if "mcpServers" in raw and isinstance(raw.get("mcpServers"), dict):
        raw = raw["mcpServers"]

    servers: dict = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            logger.warning("MCP 服务器 %r 配置必须是对象, 跳过", name)
            continue
        # 双层同名嵌套: {"toutiao": {"toutiao": {"url": ...}}} → 下钻
        if cfg.get(name) is not None and isinstance(cfg.get(name), dict):
            cfg = cfg[name]
        servers[name] = cfg
    return servers


def _mcp_config_path():
    """mcp_servers.json 的绝对路径(数据目录同级)。"""
    from pathlib import Path
    return Path(settings.resolved_data_dir) / "mcp_servers.json"


def load_mcp_servers_raw() -> dict:
    """读取并规范化为扁平 {name: config}(纯读文件,不连接、不鉴权)。

    每个 config 可能带 enabled 标志(缺省视为 True)。供桌面端「MCP 服务器管理」
    展示全部已配置(含被禁用)的服务器,以及增删改查用。
    """
    servers = _load_mcp_servers_config()
    return servers or {}


def _write_mcp_servers(servers: dict) -> None:
    p = _mcp_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(servers, ensure_ascii=False, indent=2), "utf-8")


def upsert_mcp_server(name: str, config: dict) -> dict:
    """新增或覆盖一个 MCP 服务器配置(写 mcp_servers.json)。"""
    if not name or not str(name).strip():
        raise ValueError("MCP 服务器名不能为空")
    servers = load_mcp_servers_raw()
    servers[str(name).strip()] = config or {}
    _write_mcp_servers(servers)
    return servers[str(name).strip()]


def remove_mcp_server(name: str) -> bool:
    """从 mcp_servers.json 移除一个 MCP 服务器配置。返回是否删除成功。"""
    servers = load_mcp_servers_raw()
    if name not in servers:
        return False
    del servers[name]
    _write_mcp_servers(servers)
    return True


def set_mcp_server_enabled(name: str, enabled: bool) -> bool:
    """置某个已配置 MCP 服务器的 enabled 标志(禁用后 refresh 会跳过连接)。"""
    servers = load_mcp_servers_raw()
    if name not in servers:
        return False
    if not isinstance(servers[name], dict):
        servers[name] = {}
    servers[name]["enabled"] = bool(enabled)
    _write_mcp_servers(servers)
    return True


def list_mcp_servers_configured() -> list[dict]:
    """列出全部已配置 MCP 服务器(含禁用),附 connected/tools_count 实时状态。

    桌面端据此渲染 MCP 管理列表:未连接的显示禁用态,连接的显示工具数。
    """
    from app.core.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    configured = load_mcp_servers_raw()
    out = []
    for name, cfg in configured.items():
        client = manager.get_client(name)
        connected = bool(getattr(client, "_connected", False)) if client else False
        tools = getattr(client, "_tools_cache", []) or [] if client else []
        # 连接失败原因(连接失败时由 connect_all 写入 client._connect_error)
        conn_err = getattr(client, "_connect_error", "") if (client and not connected) else ""
        out.append({
            "name": name,
            "type": cfg.get("type", "http"),
            "enabled": cfg.get("enabled", True),
            "connected": connected,
            "tools_count": len(tools),
            "error": conn_err,   # 未连接时给前端提示根因
            "config": cfg,
        })
    return out


async def init_mcp_from_config(cache_db_path: Optional[str] = None) -> dict:
    """
    从配置初始化 MCP 连接(先重置,再按配置重建)。

    配置来源(优先级从高到低):
      1. 数据目录下的 mcp_servers.json —— 桌面端最方便的入口
         (见 _load_mcp_servers_config)。文件存在时优先于环境变量。
      2. 环境变量 MCP_SERVERS(JSON 字符串)。
      注:.env 里的 MCP_SERVERS 不会被 pydantic-settings 写进 os.environ,
         所以"写进 .env 后不生效"是已知坑,统一用文件入口解决。

    配置格式(扁平,顶层 key 即服务器名,不认 mcpServers 外壳——但加载器会容忍并拍平):
      HTTP 版(自研 /mcp REST 协议,GET {base}/tools、POST {base}/tools/{name}/call):
        {"toutiao": {"type": "http", "url": "http://localhost:9000/mcp", "api_key": "xxx"}}
      stdio 版(标准 MCP JSON-RPC 服务器,npx/uvx/python 等):
        {"windows-mcp": {"type": "stdio",
                         "command": "C:\\...\\uvx.exe",
                         "args": ["windows-mcp", "serve"],
                         "env": {"WINDOWS_MCP_DISABLE_FLASH": "1"}}}

    Returns:
        初始化结果(每个服务器的连接状态)
    """
    import os

    manager = get_mcp_manager(cache_db_path=cache_db_path)
    await manager.reset()
    servers = _load_mcp_servers_config()
    if servers is None:
        return {"error": "MCP_SERVERS 配置格式错误"}

    for name, config in servers.items():
        if not isinstance(config, dict):
            continue
        # enabled 标志(桌面端「MCP 服务器管理」可禁用):禁用后跳过连接
        if config.get("enabled") is False:
            continue
        client = _build_mcp_client(config)
        if client is None:
            continue
        # 记录"连接定义"签名:增量刷新(refresh_mcp_tools_incremental)靠它判断
        # 哪些服务器配置没变、可跳过重连(否则首次保存会误判全量重连)
        client._cfg_key = _mcp_config_key(name, config)  # noqa: SLF001
        manager.add_client(name, client)

    return await manager.connect_all()


def _mcp_config_key(name: str, cfg: dict) -> tuple:
    """某服务器的"连接定义"签名:只有这些字段变化才需要重连。"""
    if (cfg.get("type") or "http") == "stdio":
        return ("stdio", cfg.get("command", ""), tuple(cfg.get("args") or []),
                json.dumps(cfg.get("env") or {}, sort_keys=True),
                float(cfg.get("timeout", 60)), cfg.get("cwd"))
    return ("http", cfg.get("url", ""), cfg.get("api_key", ""),
            float(cfg.get("timeout", 30)), cfg.get("tenant_id", "default"))


def _build_mcp_client(cfg: dict):
    """按配置构造一个 MCP 客户端(stdio/http),不连接。返回 None 表示配置不可连。"""
    mcp_type = cfg.get("type", "http")
    if mcp_type == "stdio" and cfg.get("command"):
        return MCPStdioClient(
            command=cfg["command"],
            args=cfg.get("args") or [],
            env=cfg.get("env") or {},
            timeout=float(cfg.get("timeout", 60)),
            cwd=cfg.get("cwd"),
            auto_reconnect=True,
        )
    url = cfg.get("url", "")
    if not url:
        return None
    return MCPClient(
        base_url=url,
        api_key=cfg.get("api_key"),
        timeout=float(cfg.get("timeout", 30)),
        tenant_id=cfg.get("tenant_id", "default"),
    )


async def refresh_mcp_tools_incremental() -> dict:
    """增量刷新 MCP 工具(2026-10-04):只重连"配置实际变化"的服务器,未变化的
    保留现有连接与工具映射。

    背景:「MCP 服务器管理」的 保存/增删/启停 此前走全量 _refresh_mcp_tools
    (unregister → reset+init → register),每次把所有 stdio 服务器重新 initialize
    一遍(冷 npx 下载/30-60s 超时叠加,实测 ~50s),表现为"保存 MCP 很慢"。
    增量刷新把未变化的服务器直接跳过,常见"只改一个配置"的保存从 ~50s → ~2s。
    失败语义与全量一致:本次尝试连接且失败的服务器自动禁用(可重新启用)。
    """
    from app.tools.mcp_tool import register_mcp_tools, unregister_mcp_tools

    manager = get_mcp_manager()
    configured = _load_mcp_servers_config() or {}

    # 1) 移除:配置里已删除(或被禁用)的服务器 → 断开并注销其本地工具
    for name in [n for n in list(manager._clients)]:  # noqa: SLF001
        cfg = configured.get(name)
        if cfg is None or cfg.get("enabled") is False:
            await manager.remove_client(name)

    # 2) 重连:配置变化、未启用→启用、尚未连接、或上次连接失败的服务器
    to_connect: list[tuple[str, object]] = []
    for name, cfg in configured.items():
        if not isinstance(cfg, dict) or cfg.get("enabled") is False:
            continue
        existing = manager.get_client(name)
        # getattr 兜底:旧连接(未记 _cfg_key)一律视为"需重连"
        changed = existing is None or getattr(existing, "_cfg_key", None) != _mcp_config_key(name, cfg)
        # 上次连接失败(_connect_error 非空)的服务器:配置未变也要重试连接。
        # 覆盖"自动禁用后用户在 UI 重新启用"——若只看 _cfg_key,该 server 会永远
        # 跳不过去,启用了也连不上(全量刷新时代每次都会重试,这里对齐旧语义)。
        failed_before = getattr(existing, "_connect_error", None) is not None
        if existing is not None and (changed or failed_before):
            await manager.remove_client(name)
        if changed or failed_before:
            client = _build_mcp_client(cfg)
            if client is not None:
                client._cfg_key = _mcp_config_key(name, cfg)  # noqa: SLF001
                manager.add_client(name, client)
                to_connect.append((name, client))
    results = await manager.connect_named(to_connect) if to_connect else {}

    # 3) 工具注册表重刷:注销旧 mcp_ 工具后按新 _tools_map 重建(工厂闭包绑定)
    unregister_mcp_tools()
    registered = register_mcp_tools()
    return {
        "init": results,
        "registered": registered,
        "reconnected": [n for n, _ in to_connect],
        "kept": [n for n in manager._clients if n not in dict(to_connect)],  # noqa: SLF001
    }
