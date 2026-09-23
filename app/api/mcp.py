"""MCP (Model Context Protocol) 服务器:将本地工具暴露为标准 MCP 服务。"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tools.registry import get_registry


router = APIRouter(prefix="/mcp", tags=["MCP"])


class ToolCallRequest(BaseModel):
    """工具调用请求。"""
    arguments: dict[str, Any] = {}


class ToolCallResponse(BaseModel):
    """工具调用响应。"""
    result: Any
    error: Optional[str] = None


@router.get("/")
async def mcp_info():
    """
    MCP 服务器信息。
    
    符合 MCP 规范的根端点。
    """
    return {
        "name": "agent-service",
        "version": "1.0.0",
        "description": "通用对话式智能体 MCP 服务",
        "protocol": "mcp",
        "endpoints": {
            "tools": "/mcp/tools",
            "call": "/mcp/tools/{tool_name}/call",
        }
    }


@router.get("/tools")
async def list_tools():
    """
    列出所有可用的 MCP 工具。
    
    返回符合 MCP 规范的工具列表。
    """
    registry = get_registry()
    tools = []

    async for tool in registry.list(enabled_only=True):
        tool_info = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.json_schema,
            "annotations": {
                "timeout": tool.timeout,
                "terminal": tool.terminal,
            }
        }
        tools.append(tool_info)
    
    return {"tools": tools}


@router.get("/tools/{tool_name}")
async def get_tool_schema(tool_name: str):
    """
    获取指定工具的 schema。
    
    Args:
        tool_name: 工具名称
    """
    registry = get_registry()
    tool = registry.get(tool_name)
    
    if tool is None:
        raise HTTPException(status_code=404, detail=f"工具不存在: {tool_name}")
    
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.json_schema,
        "annotations": {
            "timeout": tool.timeout,
            "terminal": tool.terminal,
        }
    }


@router.post("/tools/{tool_name}/call")
async def call_tool(tool_name: str, request: ToolCallRequest):
    """
    调用指定工具。
    
    Args:
        tool_name: 工具名称
        request: 工具调用请求
    """
    registry = get_registry()
    tool = registry.get(tool_name)
    
    if tool is None:
        raise HTTPException(status_code=404, detail=f"工具不存在: {tool_name}")
    
    if not tool.enabled:
        raise HTTPException(status_code=400, detail=f"工具已禁用: {tool_name}")
    
    try:
        result = await tool.run(request.arguments)
        return {"result": result}
    except Exception as e:
        return {"result": None, "error": str(e)}


@router.post("/tools/batch")
async def batch_call_tools(calls: list[dict]):
    """
    批量调用多个工具。
    
    请求格式:
    [
        {"tool": "tool_name", "arguments": {...}},
        ...
    ]
    """
    registry = get_registry()
    results = []
    
    for call in calls:
        tool_name = call.get("tool", "")
        arguments = call.get("arguments", {})
        
        tool = registry.get(tool_name)
        if tool is None:
            results.append({"tool": tool_name, "error": f"工具不存在: {tool_name}"})
            continue
        
        try:
            result = await tool.run(arguments)
            results.append({"tool": tool_name, "result": result})
        except Exception as e:
            results.append({"tool": tool_name, "error": str(e)})
    
    return {"results": results}


@router.get("/health")
async def mcp_health():
    """MCP 服务健康检查。"""
    registry = get_registry()
    tools = registry.list_sync(enabled_only=True)
    
    return {
        "status": "ok",
        "tools_count": len(tools),
        "tools": [t.name for t in tools]
    }
