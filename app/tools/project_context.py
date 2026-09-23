"""项目路径上下文:通过 ContextVar 让工具在运行时感知当前项目目录。"""
from __future__ import annotations

import contextvars
import os

_project_path: contextvars.ContextVar[str] = contextvars.ContextVar("project_path", default="")


def set_project_path(path: str) -> None:
    _project_path.set(path)


def get_project_path() -> str:
    return _project_path.get()


def resolve_cwd() -> str:
    """获取当前工作目录:项目路径 > settings.terminal_cwd > os.getcwd()。

    返回前必须 os.path.abspath 规范化 —— 前端(JS)传来的 project_path 常带正斜杠
    (如 "E:/dev/TianGong/MES-System")或尾斜杠,而文件工具的白名单是用
    os.path.abspath 后的反斜杠路径做 **字符串前缀** 比较(_resolve_write_path)。
    若这里原样返回未规范化的路径,相对路径写入会被拼成
    "E:/dev/TianGong/MES-System\\frontend/index.html",与白名单
    "E:\\dev\\TianGong\\MES-System" 前缀不匹配 → 误报"路径不在允许写入目录内"。
    """
    p = get_project_path()
    if p and os.path.isdir(p):
        import logging as _log
        _log.getLogger("project").debug("resolve_cwd -> %s", p)
        return os.path.abspath(p)
    return os.getcwd()


# ---- 项目类型识别(供 web_auto_test / 自动触发使用) ----
_WEB_FRAMEWORK_HINTS = {
    "vite": ["vite"],
    "vue": ["vue", "@vue/cli-service", "@vue/compiler-sfc"],
    "react": ["react", "react-dom", "@vitejs/plugin-react"],
    "next": ["next"],
    "nuxt": ["nuxt", "nuxt3"],
    "angular": ["@angular/core", "@angular/cli"],
    "svelte": ["svelte", "@sveltejs/kit"],
    "uni-app": ["@dcloudio/uni-app", "@dcloudio/uni-h5", "uni-app", "uni-app-x"],
}


def detect_project_type(project_path: str | None = None) -> dict:
    """识别项目类型,返回 {"is_web", "framework", "is_static", "package_json", "path"}。

    - H5/网页:含 package.json 且依赖中出现前端框架,或根目录存在 index.html(视为静态站点)。
    - 后端/其他:无上述标记。
    容错:任何异常都返回 is_web=False,不影响主流程。
    """
    path = os.path.abspath(project_path) if project_path else resolve_cwd()
    result = {"is_web": False, "framework": None, "is_static": False,
              "package_json": False, "path": path}
    try:
        pkg_path = os.path.join(path, "package.json")
        has_index = os.path.isfile(os.path.join(path, "index.html"))
        if not os.path.isfile(pkg_path) and not has_index:
            return result
        # 静态站点:有 index.html 但无 package.json
        if not os.path.isfile(pkg_path) and has_index:
            result["is_web"] = True
            result["is_static"] = True
            result["framework"] = "static"
            return result
        import json
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
        result["package_json"] = True
        deps = {}
        deps.update(pkg.get("dependencies") or {})
        deps.update(pkg.get("devDependencies") or {})
        dep_names = " ".join(deps.keys()).lower()
        for fw, markers in _WEB_FRAMEWORK_HINTS.items():
            if any(m.lower() in dep_names for m in markers):
                result["is_web"] = True
                result["framework"] = fw
                return result
        # 有构建/ dev 脚本但依赖里没明确框架(如用 webpack 自配):仍视为 web
        scripts = pkg.get("scripts") or {}
        if any(k in scripts for k in ("dev", "serve", "start")) and (
            "webpack" in dep_names or "html-webpack" in dep_names
            or has_index
        ):
            result["is_web"] = True
            result["framework"] = "web-generic"
            return result
    except Exception:  # noqa: BLE001
        pass
    return result

