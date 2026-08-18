"""插件市场 API:/v1/plugins/*

功能:
- GET /v1/plugins — 列出所有已注册插件
- GET /v1/plugins/search?q=keyword — 搜索插件工具
- POST /v1/plugins/install — 安装新插件(local/github/registry)
- DELETE /v1/plugins/{name} — 卸载插件
- GET /v1/plugins/{name} — 查看插件详情
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


@router.get("")
async def list_plugins() -> dict:
    """列出所有已注册插件。"""
    from app.core.plugin_market import get_plugin_registry

    registry = get_plugin_registry()
    plugins = [spec.to_dict() for spec in registry.list_all()]
    return {"plugins": plugins, "total": len(plugins)}


@router.get("/search")
async def search_plugins(q: str = "") -> dict:
    """搜索插件工具。"""
    from app.core.plugin_market import get_plugin_registry

    registry = get_plugin_registry()
    results = registry.find_tools(q or "", limit=20)
    return {
        "query": q,
        "results": [{"plugin": name, "tool": tool} for name, tool in results],
    }


class InstallRequest(BaseModel):
    source: str = "local"  # local / github
    path: str = ""         # 本地路径或 GitHub repo(user/repo)
    version: str = ""      # 指定版本





@router.post("/install")
async def install_plugin(req: InstallRequest) -> dict:
    """安装新插件。"""
    from app.core.plugin_market import PluginInstaller, get_plugin_registry

    registry = get_plugin_registry()
    installer = PluginInstaller(registry)
    try:
        if req.source == "local":
            if not req.path:
                raise HTTPException(status_code=400, detail="请提供插件路径")
            spec = installer.install_local(req.path)
            return {"installed": spec.name, "version": spec.version, "tools": len(spec.tools)}
        elif req.source == "github":
            if not req.path:
                raise HTTPException(status_code=400, detail="请提供 GitHub 仓库地址(格式: user/repo)")
            spec = installer.install_github(req.path, req.version)
            return {"installed": spec.name, "version": spec.version, "tools": len(spec.tools)}
        else:
            raise HTTPException(status_code=400, detail=f"不支持的安装源: {req.source}")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"安装失败: {e}")


@router.get("/{plugin_name}")
async def get_plugin(plugin_name: str) -> dict:
    """查看插件详情。"""
    from app.core.plugin_market import get_plugin_registry

    registry = get_plugin_registry()
    spec = registry.get(plugin_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"插件未找到: {plugin_name}")
    return {
        "name": spec.name,
        "version": spec.version,
        "description": spec.description,
        "tools_count": len(spec.tools),
        "tools": spec.tools,
    }


@router.delete("/{plugin_name}")
async def uninstall_plugin(plugin_name: str) -> dict:
    """卸载插件。"""
    from app.core.plugin_market import get_plugin_registry

    registry = get_plugin_registry()
    ok = registry.unregister(plugin_name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"插件未找到: {plugin_name}")
    return {"uninstalled": plugin_name, "ok": True}
