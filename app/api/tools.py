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
    for t in registry.list_sync():
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


class ProviderUpsert(BaseModel):
    id: str | None = None
    name: str
    base_url: str
    api_key: str = ""
    model: str = ""
    models: list | None = None  # 该厂商下的完整模型列表(探测或手动维护),支持字符串或对象


class ProviderTest(BaseModel):
    base_url: str
    pid: str = ""
    api_key: str = ""
    id: str | None = None  # 编辑时 Key 留空表示沿用旧值


class ModelUpsert(BaseModel):
    id: str
    name: str = ""
    context: int | None = None


class ModelBatchUpsert(BaseModel):
    models: list[ModelUpsert]


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


def _is_dns_error(exc: Exception) -> bool:
    """判断是否为 DNS 解析失败类错误。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("getaddrinfo", "name or service not known", "nodename nor servname", "dns", "gaierror"))


def _build_proxy_info() -> dict:
    """收集系统代理环境变量,供前端提示使用。"""
    import os
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"]
    return {k: os.environ.get(k, "") for k in keys if os.environ.get(k)}


@router.post("/providers/custom/test")
async def test_custom_provider(req: ProviderTest) -> dict:
    """测试连通性并尝试拉取模型列表(GET {base_url}/models)。"""
    import os
    import httpx

    from app.core.provider_registry import get_custom, normalize_base_url

    key = req.api_key.strip()
    if not key and req.id:
        old = get_custom(req.id)
        key = (old or {}).get("api_key", "")
    base = normalize_base_url(req.base_url)
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "error": f"Base URL 无效: {base}"}

    # 检测目标主机名
    from urllib.parse import urlparse
    parsed = urlparse(base)
    hostname = parsed.hostname or base

    # 构建代理配置(若系统设置了 HTTP(S)_PROXY,httpx 会自动读取)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    from app.config import get_httpx_proxy

    last_error = None
    for attempt in range(2):  # 最多重试 1 次
        try:
            proxy = get_httpx_proxy()
            client_kwargs = {
                "timeout": httpx.Timeout(15.0, connect=10.0),
                "follow_redirects": True,
            }
            if proxy:
                client_kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(f"{base}/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", []) or []
                models = [
                    {"id": m.get("id", ""), "name": m.get("id", "")}
                    for m in data if isinstance(m, dict) and m.get("id")
                ]
                return {"ok": True, "models": models}
            # 非 200 状态码直接返回,不重试
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            last_error = exc
            if not _is_dns_error(exc):
                # 非 DNS 错误不重试
                break
            # DNS 错误可能是临时网络波动,重试一次
            continue

    # 处理最终错误
    if last_error:
        msg = str(last_error) or repr(last_error) or type(last_error).__name__
        if _is_dns_error(last_error):
            proxy_info = _build_proxy_info()
            hint_parts = [f"DNS解析失败:无法访问 {hostname}"]
            if proxy_info:
                hint_parts.append("检测到系统代理设置,如仍无法访问请确认代理是否可用")
            else:
                hint_parts.append("如 DNS 仍无法解析,请检查网络或配置 HTTP_PROXY/HTTPS_PROXY 代理")
            return {
                "ok": False,
                "error": "; ".join(hint_parts),
                "detail": msg[:200],
                "proxy_configured": bool(proxy_info),
            }
        return {"ok": False, "error": msg[:300]}
    return {"ok": False, "error": "未知错误"}


@router.get("/providers/builtin/{pid}/models")
async def list_builtin_models(pid: str) -> dict:
    """获取内置厂商的全部模型列表(含用户追加的扩展模型)。"""
    from app.core.provider_registry import BUILTIN_PROVIDERS, list_builtin_ext_models

    if pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {pid}")
    from app.api.chat import _PROVIDER_DEFAULT_MODELS

    models = list(_PROVIDER_DEFAULT_MODELS.get(pid, []))
    for ext in list_builtin_ext_models(pid):
        models.append({
            "id": ext.get("id"),
            "name": ext.get("name") or ext.get("id"),
            "speed": ext.get("speed", ""),
            "quality": ext.get("quality", ""),
            "context": ext.get("context"),
        })
    return {"provider": pid, "models": models}


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


@router.post("/providers/builtin/{pid}/models/batch")
async def add_builtin_ext_models_batch(pid: str, req: ModelBatchUpsert) -> dict:
    """批量添加内置厂商扩展模型。"""
    from app.core.provider_registry import add_builtin_model, BUILTIN_PROVIDERS

    if pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {pid}")
    added, errors = [], []
    for m in req.models:
        try:
            add_builtin_model(pid, m.id, m.name, m.context)
            added.append(m.id)
        except ValueError as exc:
            errors.append({"id": m.id, "error": str(exc)})
    return {"ok": True, "added": added, "errors": errors}


@router.delete("/providers/builtin/{pid}/models/{mid}")
async def remove_builtin_ext_model(pid: str, mid: str) -> dict:
    from app.core.provider_registry import remove_builtin_model, BUILTIN_PROVIDERS

    if pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {pid}")
    return {"ok": remove_builtin_model(pid, mid)}


@router.post("/providers/fetch_models")
async def fetch_models(req: ProviderTest) -> dict:
    """拉取内置厂商模型列表并追加到扩展模型。"""
    import httpx
    from app.core.provider_registry import BUILTIN_PROVIDERS, add_builtin_model, list_builtin_ext_models
    from app.config import get_httpx_proxy
    from urllib.parse import urlparse

    if req.pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {req.pid}")

    base = req.base_url.strip()
    key = req.api_key.strip()

    # 内置厂商未传 api_key 时从配置中获取
    if not key:
        from app.config import settings as _cfg
        if req.pid == "deepseek":
            key = _cfg.deepseek_api_key or ""
        elif req.pid == "qwen":
            key = _cfg.qwen_api_key or ""
        elif req.pid == "openai":
            import os
            key = os.environ.get("OPENAI_API_KEY", "")
        elif req.pid == "compat":
            key = _cfg.openai_compat_api_key or ""
        elif req.pid == "ollama":
            key = "ollama"

    if not base:
        return {"ok": False, "error": "Base URL 不能为空"}
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "error": "Base URL 无效"}
    if not key:
        return {"ok": False, "error": f"{req.pid} 的 API Key 未配置"}

    parsed = urlparse(base)
    hostname = parsed.hostname or base

    headers = {"Authorization": f"Bearer {key}"}
    last_error = None

    for attempt in range(2):
        try:
            proxy = get_httpx_proxy()
            client_kwargs = {
                "timeout": httpx.Timeout(15.0, connect=10.0),
                "follow_redirects": True,
            }
            if proxy:
                client_kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(f"{base}/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", []) or []
                added = 0
                existing_ids = {m.get("id") for m in list_builtin_ext_models(req.pid)}
                for m in data:
                    if not isinstance(m, dict) or not m.get("id"):
                        continue
                    mid = m["id"]
                    if mid in existing_ids:
                        continue
                    add_builtin_model(req.pid, mid, m.get("id", mid))
                    added += 1
                return {"ok": True, "added": added, "total": len(data)}
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            last_error = exc
            if not _is_dns_error(exc):
                break
            continue

    if last_error:
        msg = str(last_error) or repr(last_error)
        if _is_dns_error(last_error):
            return {
                "ok": False,
                "error": f"DNS解析失败:无法访问 {hostname}; 如 DNS 仍无法解析,请检查网络或配置 HTTP_PROXY/HTTPS_PROXY 代理",
                "detail": msg[:200],
            }
        return {"ok": False, "error": msg[:300]}
    return {"ok": False, "error": "未知错误"}


@router.post("/providers/builtin/{pid}/models/{mid}/default")
async def set_builtin_default_model(pid: str, mid: str) -> dict:
    """设置内置厂商的默认模型。"""
    from app.core.provider_registry import BUILTIN_PROVIDERS, set_builtin_default_model

    if pid not in BUILTIN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"未知内置厂商: {pid}")
    try:
        result = set_builtin_default_model(pid, mid)
        return {"ok": True, "result": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
