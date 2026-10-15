"""MCP Server 端点:将 Agent 暴露为 MCP 服务,供 Claude Code / Cursor / other MCP clients 调用。

MCP (Model Context Protocol) 是 Anthropic 定义的开放协议,允许任意 AI 客户端通过标准 JSON-RPC
与工具服务交互。本模块实现一个轻量 MCP Server,暴露以下能力:
- agent_chat: 通过 Agent 运行完整对话(等价于 /v1/chat)
- agent_tools: 列出 Agent 可用的工具描述
- agent_cost: 查询成本报告(等价于 cost_tracker 工具)

协议: stdio 传输,JSON-RPC 2.0。启动时由 main.py 的 lifespan 触发,
监听 stdin/stdout 流式 JSON-RPC 消息。也可通过 HTTP SSE 方式访问(见 /mcp/ 路由)。
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, AsyncGenerator


def _build_tools_list() -> list[dict]:
    """返回 Agent 工具的 MCP 格式列表。"""
    from app.tools.registry import get_registry
    from app.tools.categories import SYSTEM_TOOLS

    registry = get_registry()
    tools = []
    for t in registry.list_sync(enabled_only=True):
        schema = t.to_openai_schema()
        tools.append({
            "name": t.name,
            "description": t.description,
            "inputSchema": t.json_schema,
            "metadata": {
                "terminal": t.terminal,
                "timeout": t.timeout,
            },
        })
    # 额外暴露 Agent 级别的元工具
    tools.extend([
        {
            "name": "agent_chat",
            "description": "运行一次 Agent 对话(完整 ReAct 循环),返回最终答案与执行步骤",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "用户消息"},
                    "session_id": {"type": "string", "description": "会话 ID,默认 mcp_session"},
                    "mode": {"type": "string", "enum": ["auto", "single", "review", "ensemble"], "description": "协作模式"},
                },
                "required": ["message"],
            },
        },
        {
            "name": "list_agent_tools",
            "description": "列出 Agent 当前可用的所有工具名称和描述",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "agent_cost_report",
            "description": "查询 Agent 运行成本报告(token 用量/美元估算)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "按会话过滤,留空=全部"},
                    "days": {"type": "integer", "description": "最近 N 天,默认 7"},
                },
            },
        },
    ])
    return tools


class MCPServer:
    """轻量 MCP Server,处理 JSON-RPC 2.0 请求。"""

    def __init__(self):
        self._tools = _build_tools_list()

    async def handle_request(self, req: dict) -> dict | None:
        """处理单个 JSON-RPC 请求,返回响应。"""
        method = req.get("method", "")
        params = req.get("params") or {}
        msg_id = req.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-service", "version": "1.0.0"},
                },
            }
        if method == "initialized":
            return None
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self._tools},
            }
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self._call_tool(name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]},
            }
        if method == "notifications/initialized":
            return None
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    async def _call_tool(self, name: str, args: dict) -> dict:
        """分派到对应的 handler。"""
        if name == "list_agent_tools":
            return {"tools": [{"name": t["name"], "description": t["description"]} for t in self._tools]}
        if name == "agent_cost_report":
            return self._cost_report(args.get("session_id", ""), args.get("days", 7))
        if name == "agent_chat":
            return await self._agent_chat(args.get("message", ""), args.get("session_id", "mcp_session"),
                                           args.get("mode", "auto"))
        # 直接转发到注册器中的工具
        from app.tools.registry import get_registry

        registry = get_registry()
        tool_obj = registry.get(name)
        if tool_obj is None:
            return {"error": f"工具不存在: {name}"}
        try:
            result = await tool_obj.run(json.dumps(args, ensure_ascii=False))
            return json.loads(result) if isinstance(result, str) else result
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _cost_report(session_id: str, days: int) -> dict:
        from app.tools.cost_tool import cost_summary
        return cost_summary(session_id=session_id, days=days)

    async def _agent_chat(self, message: str, session_id: str, mode: str) -> dict:
        """运行一次 Agent 对话,返回答案与步骤。"""
        from app.core.agent import Agent

        agent = Agent(session_id=session_id, mode=mode)
        result = await agent.run(message)
        return {
            "answer": result.answer,
            "model": result.model,
            "steps": result.steps,
            "interrupted": result.interrupted,
        }


def make_request_iterator() -> AsyncGenerator[dict, None]:
    """从 stdin 逐行读取 JSON-RPC 请求,yield 解析后的 dict。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop.run_until_complete(loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer))
    transport = None

    async def _read_line():
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                try:
                    yield json.loads(text)
                except json.JSONDecodeError:
                    continue

    return _read_line()


async def run_mcp_server() -> None:
    """以 stdio 模式运行 MCP Server(由外部进程通过 stdin/stdout 调用)。"""
    server = MCPServer()
    # 发送 initialize 通知
    init_resp = await server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "stdin", "version": "1.0"}},
    })
    if init_resp:
        sys.stdout.write(json.dumps(init_resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    # 发送 tools/list
    tools_resp = await server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    if tools_resp:
        sys.stdout.write(json.dumps(tools_resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async for req in make_request_iterator():
        resp = await server.handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# ---- HTTP SSE 端点(供 browser-based MCP clients) ----

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

router = APIRouter(prefix="/mcp", tags=["mcp"])
_mcp_server_instance: MCPServer | None = None


def _get_server() -> MCPServer:
    global _mcp_server_instance
    if _mcp_server_instance is None:
        _mcp_server_instance = MCPServer()
    return _mcp_server_instance


@router.post("/call")
async def mcp_call(request: Request) -> dict:
    """HTTP 单请求入口:接收 JSON-RPC 请求,返回 JSON-RPC 响应。"""
    body = await request.json()
    server = _get_server()
    resp = await server.handle_request(body)
    return resp or {"jsonrpc": "2.0", "id": body.get("id"), "result": {}}


# ---- 标准 MCP Streamable HTTP 传输端点(2025-03 规范,供标准 MCP 客户端直连) ----
# 规范核心:同一个端点同时接 POST 与 GET;客户端发 JSON-RPC(单条或 batch),
# 服务端按 Accept 头返回 application/json 或 text/event-stream;
# 纯通知/响应(无待回 request)返回 202。MCPServer 是无状态的(不维护会话),
# 故此处不实现 Mcp-Session-Id(规范允许省略),GET 长连返回 405(规范允许)。
# 端点路径 = 本 router prefix("/mcp") + "" = "/mcp"。

def _needs_response(obj: Any) -> bool:
    """该 JSON-RPC 对象是否需要服务端回一个带 id 的 response。"""
    return (
        isinstance(obj, dict)
        and obj.get("method") is not None
        and obj.get("id") is not None
    )


def _sse_encode(resp: dict) -> str:
    """把一个 JSON-RPC response 编码成 MCP 约定的 SSE 帧(event: message)。"""
    return f"event: message\ndata: {json.dumps(resp, ensure_ascii=False)}\n\n"


@router.api_route("", methods=["POST", "GET"], name="mcp_streamable")
async def mcp_streamable(request: Request) -> Response:
    """标准 MCP Streamable HTTP 端点(外部 MCP 客户端经 HTTP 直连 TianGong)。

    - POST:处理单条/批量 JSON-RPC(initialize / tools/list / tools/call /
      notifications),按 Accept 返回 application/json 或 text/event-stream;
      若本请求不含任何"需要回应的 request",返回 202 空 body。
    - GET:本服务端无状态、不提供 server→client 长连通知流,按规范返回 405。
    """
    if request.method == "GET":
        # 规范:GET 要么开 SSE 流,要么 405。无状态工具服务无需 server→client 推送。
        return Response(status_code=405, headers={"Allow": "POST"})

    server = _get_server()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)

    items = body if isinstance(body, list) else [body]
    # 逐个处理"需要回应的 request"(initialize/tools/list/tools/call),
    # 通知类(notifications/*、纯 response)handle_request 返回 None,自然被丢弃。
    responses = []
    for item in items:
        if not _needs_response(item):
            continue
        r = await server.handle_request(item)
        if r is not None:
            responses.append(r)

    # 无任何需回应的 request(全是通知/响应) → 202 Accepted 空 body
    if not responses:
        return Response(status_code=202)

    accept = request.headers.get("accept", "").lower()
    use_sse = ("text/event-stream" in accept) and ("application/json" not in accept)

    if use_sse:
        async def _gen():
            for resp in responses:
                yield _sse_encode(resp)
        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # application/json:单条返回对象,batch 返回数组
    if isinstance(body, list):
        return JSONResponse(responses)
    return JSONResponse(responses[0])


@router.get("/tools")
async def mcp_tools() -> dict:
    """列出 Agent 暴露的所有工具。"""
    server = _get_server()
    return {"tools": server._tools}


@router.get("/call")
async def mcp_call_get(
    tool: str = "",
    args: str = "",
) -> dict:
    """GET 简写:直接调用单个工具(方便 curl 测试)。"""
    server = _get_server()
    try:
        arguments = json.loads(args) if args else {}
    except json.JSONDecodeError:
        arguments = {}
    result = await server._call_tool(tool, arguments)
    return {"tool": tool, "result": result}


@router.get("/chat")
async def mcp_chat(message: str = "", session_id: str = "mcp_session", mode: str = "auto") -> dict:
    """GET 简写:运行一次 Agent 对话。"""
    server = _get_server()
    result = await server._agent_chat(message, session_id, mode)
    return result


@router.get("/cost")
async def mcp_cost(session_id: str = "", days: int = 7) -> dict:
    """GET 简写:查询成本报告。"""
    server = _get_server()
    return server._cost_report(session_id, days)


@router.get("/health")
async def mcp_health() -> dict:
    return {"status": "ok", "tools_count": len(_get_server()._tools)}
