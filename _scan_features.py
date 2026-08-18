"""自动化扫描:列出所有工具/模块/能力,用于差距分析。"""
import ast
import os
import importlib
import json

def scan_tools():
    """扫描所有注册的工具。"""
    from app.tools.registry import get_registry
    reg = get_registry()
    tools = reg.list(enabled_only=True)
    return [{"name": t.name, "desc": t.description[:80], "timeout": t.timeout} for t in tools]


def scan_agents():
    """扫描所有角色。"""
    from app.agents.roles import ensure_default_roles, get_role_registry
    ensure_default_roles()
    team = get_role_registry().team
    return [{"name": r.name, "desc": r.description, "tools": r.tools} for r in team.list()]


def scan_features():
    """扫描核心功能模块。"""
    features = {}
    core_dir = "app/core"
    api_dir = "app/api"
    for d in [core_dir, api_dir]:
        if not os.path.exists(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".py") and not f.startswith("_"):
                mod_name = f.replace(".py", "")
                features[f"{d}/{f}"] = mod_name
    return features


def scan_api_routes():
    """扫描所有 API 路由。"""
    routes = []
    for root, dirs, files in os.walk("app/api"):
        for f in files:
            if not f.endswith(".py") or f.startswith("_"):
                continue
            fp = os.path.join(root, f)
            with open(fp, encoding="utf-8-sig") as fh:
                src = fh.read()
            # 简单正则提取 @router.* 装饰器
            import re
            matches = re.findall(r'@router\.(get|post|put|delete)\(["\']([^"\']+)["\']', src)
            for method, path in matches:
                prefix = root.replace("app/api", "").lstrip("/")
                full_path = f"/{prefix}/{path}" if prefix else path
                routes.append({"method": method.upper(), "path": full_path, "file": f})
    return sorted(routes, key=lambda r: r["path"])


def scan_config():
    """扫描所有配置项。"""
    from app.config import settings
    items = {}
    for name in dir(settings):
        if name.startswith("_"):
            continue
        try:
            val = getattr(settings, name)
            if not callable(val):
                items[name] = str(val)[:100]
        except Exception:
            pass
    return items


if __name__ == "__main__":
    print(json.dumps({
        "tools": scan_tools(),
        "agents": scan_agents(),
        "features": scan_features(),
        "routes": scan_api_routes(),
        "config_count": len(scan_config()),
    }, ensure_ascii=False, indent=2, default=str))
