"""工具管理接口:查看已注册工具。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import settings
from app.tools.registry import get_registry

router = APIRouter(prefix="/v1", tags=["tools"])


@router.get("/tools")
async def list_tools() -> dict:
    registry = get_registry()
    dangerous = {s.strip() for s in settings.agent_dangerous_tools.split(",") if s.strip()}
    tools = []
    for t in registry.list():
        tools.append({
            "name": t.name,
            "description": t.description,
            "parameters": t.json_schema,
            "enabled": t.enabled,
            "dangerous": t.name in dangerous,
        })
    return {
        "tools": tools,
        "count": len(tools),
        "tool_confirm_enabled": settings.agent_tool_confirm,
        "dangerous_tools": sorted(dangerous),
    }


@router.get("/mcp/servers")
async def list_mcp_servers() -> dict:
    """列出已配置的 MCP 服务器及其工具。"""
    from app.core.mcp_client import get_mcp_manager
    from app.core.mcp_stdio import MCPStdioClient

    manager = get_mcp_manager()
    servers = []
    for name, client in manager._clients.items():  # noqa: SLF001
        tools = getattr(client, "_tools_cache", []) or []
        servers.append({
            "name": name,
            "type": "stdio" if isinstance(client, MCPStdioClient) else "http",
            "connected": bool(getattr(client, "_connected", False)),
            "tools_count": len(tools),
            "tools": [t.get("name", "") for t in tools if isinstance(t, dict)],
        })
    return {"servers": servers, "count": len(servers)}


@router.post("/mcp/refresh")
async def refresh_mcp() -> dict:
    """从配置重新加载并注册 MCP 工具。"""
    from app.core.mcp_client import init_mcp_from_config
    from app.tools.mcp_tool import register_mcp_tools, unregister_mcp_tools

    unregister_mcp_tools()
    init_result = await init_mcp_from_config()
    registered = register_mcp_tools()
    return {"init": init_result, "registered": registered}


@router.get("/providers")
async def list_providers() -> dict:
    """可用模型 Provider 列表(带 key 与否,供 UI 下拉)。"""
    import os

    providers = [
        {"id": "deepseek", "name": "DeepSeek", "configured": bool(settings.deepseek_api_key)},
        {"id": "qwen", "name": "通义千问 Qwen", "configured": bool(settings.qwen_api_key)},
        {"id": "ollama", "name": "本地 Ollama", "configured": bool(settings.ollama_base_url)},
        {"id": "openai", "name": "OpenAI", "configured": bool(os.environ.get("OPENAI_API_KEY"))},
        {"id": "compat", "name": "OpenAI 兼容", "configured": bool(settings.openai_compat_base_url)},
    ]
    return {
        "providers": providers,
        "default": settings.agent_llm_provider,
        "active": settings.agent_llm_provider,
    }


class TodoCreate(BaseModel):
    title: str
    category: str = "默认"
    note: str = ""
    due_at: str = ""


class TodoToggle(BaseModel):
    id: int


class TodoDelete(BaseModel):
    id: int


async def _run_tool(name: str, **kwargs) -> dict:
    """执行待办工具并解析其 JSON 字符串结果。"""
    import json

    from app.tools.registry import get_registry

    t = get_registry().get(name)
    if t is None:
        return {"error": f"工具不存在: {name}"}
    res = await t.run(kwargs)
    return json.loads(res)


@router.get("/todos")
async def list_todos(only_pending: bool = Query(False)) -> dict:
    from app.tools.todo_tool import _connect, _row_to_dict

    conn = _connect()
    sql = "SELECT id,title,note,category,done,due_at,created_at FROM todos"
    if only_pending:
        sql += " WHERE done=0"
    sql += " ORDER BY done ASC, COALESCE(due_at, 1e18) ASC, created_at DESC"
    rows = conn.execute(sql).fetchall()
    return {"todos": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.post("/todos")
async def create_todo(req: TodoCreate) -> dict:
    return await _run_tool("create_todo", title=req.title, category=req.category, note=req.note, due_at=req.due_at)


@router.post("/todos/toggle")
async def toggle_todo(req: TodoToggle) -> dict:
    return await _run_tool("toggle_todo", todo_id=req.id)


@router.post("/todos/delete")
async def delete_todo(req: TodoDelete) -> dict:
    return await _run_tool("delete_todo", todo_id=req.id)


class ConfirmRespond(BaseModel):
    approved: bool
    result: str = ""


def _confirm_store():
    """获取确认请求存储(复用协作工具的持久化存储)。"""
    from app.tools.collaboration_tool import _get_store

    return _get_store()


@router.get("/confirmations/pending")
async def list_pending_confirmations() -> dict:
    """列出所有待确认的高风险操作(前端据此渲染批准卡片)。"""
    store = _confirm_store()
    pending = store.list_pending()
    return {"pending": pending, "total": len(pending)}


@router.post("/confirmations/{conf_id}/respond")
async def respond_confirmation(conf_id: str, req: ConfirmRespond) -> dict:
    """用户对某个确认请求做出批准/拒绝决定。"""
    store = _confirm_store()
    conf = store.respond(conf_id, approved=req.approved, result=req.result)
    if conf is None:
        return {"status": "error", "error": f"确认请求不存在或已处理: {conf_id}"}
    return {"status": "ok", "confirmation": conf}
