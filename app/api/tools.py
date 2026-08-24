"""工具管理接口:查看已注册工具。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
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

    from app.core.provider_registry import list_custom

    providers = [
        {"id": "deepseek", "name": "DeepSeek", "configured": bool(settings.deepseek_api_key)},
        {"id": "qwen", "name": "通义千问 Qwen", "configured": bool(settings.qwen_api_key)},
        {"id": "ollama", "name": "本地 Ollama", "configured": bool(settings.ollama_base_url)},
        {"id": "openai", "name": "OpenAI", "configured": bool(os.environ.get("OPENAI_API_KEY"))},
        {"id": "compat", "name": "OpenAI 兼容", "configured": bool(settings.openai_compat_base_url)},
    ]
    providers += [
        {"id": c["id"], "name": c["name"], "configured": True}
        for c in list_custom()
    ]
    return {
        "providers": providers,
        "default": settings.agent_llm_provider,
        "active": settings.agent_llm_provider,
    }


class ProviderUpsert(BaseModel):
    id: str | None = None
    name: str
    base_url: str
    api_key: str = ""
    model: str = ""
    models: list[dict] | None = None  # 该厂商下的完整模型列表(探测或手动维护)


class ProviderTest(BaseModel):
    base_url: str
    api_key: str = ""
    id: str | None = None  # 编辑时 Key 留空表示沿用旧值


class ModelUpsert(BaseModel):
    id: str
    name: str = ""
    context: int | None = None


@router.get("/providers/custom")
async def list_custom_providers() -> dict:
    """自定义提供商列表(Key 掩码,不回传明文)。"""
    from app.core.provider_registry import list_custom, mask_key, list_models as registry_list_models

    items = []
    for c in list_custom():
        items.append({
            "id": c.get("id", ""),
            "name": c.get("name", ""),
            "base_url": c.get("base_url", ""),
            "model": c.get("model", ""),
            "api_key_masked": mask_key(c.get("api_key", "")),
            # 使用 list_models 兜底:models 为空但配置了默认 model 时,仍返回该模型,
            # 避免编辑表单/厂商列表把已有模型误判为"0 个模型"
            "models": registry_list_models(c["id"]),
        })
    return {"providers": items}


@router.post("/providers/custom")
async def save_custom_provider(req: ProviderUpsert) -> dict:
    from app.core.provider_registry import upsert_custom

    try:
        entry = upsert_custom(
            pid=req.id,
            name=req.name,
            base_url=req.base_url,
            api_key=req.api_key,
            model=req.model,
            models=req.models,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": entry["id"]}


@router.delete("/providers/custom/{pid}")
async def delete_custom_provider(pid: str) -> dict:
    from app.core.provider_registry import delete_custom

    return {"ok": delete_custom(pid)}


@router.get("/providers/custom/{pid}/models")
async def list_custom_models(pid: str) -> dict:
    from app.core.provider_registry import get_custom, list_models

    if not get_custom(pid):
        raise HTTPException(status_code=404, detail=f"自定义提供商不存在: {pid}")
    return {"provider": pid, "models": list_models(pid)}


@router.post("/providers/custom/{pid}/models")
async def add_custom_model(pid: str, req: ModelUpsert) -> dict:
    from app.core.provider_registry import add_model

    try:
        entry = add_model(pid, req.id, req.name, req.context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "entry": entry}


@router.delete("/providers/custom/{pid}/models/{mid}")
async def remove_custom_model(pid: str, mid: str) -> dict:
    from app.core.provider_registry import remove_model

    try:
        entry = remove_model(pid, mid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "entry": entry}


@router.post("/providers/custom/{pid}/models/{mid}/default")
async def set_custom_default_model(pid: str, mid: str) -> dict:
    from app.core.provider_registry import set_default_model

    try:
        entry = set_default_model(pid, mid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "entry": entry}


@router.post("/providers/custom/test")
async def test_custom_provider(req: ProviderTest) -> dict:
    """测试连通性并尝试拉取模型列表(GET {base_url}/models)。"""
    import httpx

    from app.core.provider_registry import get_custom, normalize_base_url

    key = req.api_key.strip()
    if not key and req.id:
        old = get_custom(req.id)
        key = (old or {}).get("api_key", "")
    base = normalize_base_url(req.base_url)
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "error": f"Base URL 无效: {base}"}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(f"{base}/models", headers=headers)
        if resp.status_code == 200:
            data = resp.json().get("data", []) or []
            models = [
                {"id": m.get("id", ""), "name": m.get("id", "")}
                for m in data if isinstance(m, dict) and m.get("id")
            ]
            return {"ok": True, "models": models}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        msg = str(exc) or repr(exc) or type(exc).__name__
        return {"ok": False, "error": msg[:300]}


@router.post("/providers/builtin/{pid}/models")
async def add_builtin_ext_model(pid: str, req: ModelUpsert) -> dict:
    from app.core.provider_registry import add_builtin_model, BUILTIN_PROVIDERS

    if pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {pid}")
    try:
        result = add_builtin_model(pid, req.id, req.name, req.context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "result": result}


@router.delete("/providers/builtin/{pid}/models/{mid}")
async def remove_builtin_ext_model(pid: str, mid: str) -> dict:
    from app.core.provider_registry import remove_builtin_model, BUILTIN_PROVIDERS

    if pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {pid}")
    return {"ok": remove_builtin_model(pid, mid)}


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
