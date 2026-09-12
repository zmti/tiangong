"""依赖管理工具:安装/卸载/更新 Python 和 Node.js 依赖包。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

from pydantic import Field

from app.tools.project_context import resolve_cwd
from app.tools.registry import tool


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _run_cmd(cmd: list[str], cwd: str = None, timeout: int = 120) -> dict:
    """执行命令并返回结果。"""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": (r.stdout or "")[:3000],
            "stderr": (r.stderr or "")[:1000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"超时({timeout}s)"}
    except FileNotFoundError:
        return {"ok": False, "error": f"命令未找到: {cmd[0]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def install_pip_packages(packages: list[str], upgrade: bool = False) -> dict:
    """安装 Python 包。"""
    pip = _which("pip") or _which("pip3")
    if not pip:
        return {"ok": False, "error": "未找到 pip"}

    cmd = [pip, "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.extend(packages)

    result = _run_cmd(cmd)
    result["command"] = " ".join(cmd)
    return result


def uninstall_pip_packages(packages: list[str]) -> dict:
    """卸载 Python 包。"""
    pip = _which("pip") or _which("pip3")
    if not pip:
        return {"ok": False, "error": "未找到 pip"}

    cmd = [pip, "uninstall", "-y"] + packages
    result = _run_cmd(cmd)
    result["command"] = " ".join(cmd)
    return result


def list_pip_packages() -> dict:
    """列出已安装的 Python 包。"""
    pip = _which("pip") or _which("pip3")
    if not pip:
        return {"ok": False, "error": "未找到 pip"}

    result = _run_cmd([pip, "list", "--format=freeze"])
    packages = {}
    for line in (result.get("stdout") or "").splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            packages[name.strip()] = version.strip()
    result["packages"] = packages
    return result


def install_npm_packages(packages: list[str], dev: bool = False) -> dict:
    """安装 Node.js 包。"""
    npm = _which("npm")
    if not npm:
        return {"ok": False, "error": "未找到 npm"}

    cmd = [npm, "install"]
    if dev:
        cmd.append("--save-dev")
    cmd.extend(packages)

    result = _run_cmd(cmd)
    result["command"] = " ".join(cmd)
    return result


def list_npm_packages(path: str = ".") -> dict:
    """列出项目的 npm 依赖。"""
    pkg_json = os.path.join(path, "package.json")
    if not os.path.exists(pkg_json):
        return {"ok": False, "error": "未找到 package.json"}

    import json
    try:
        with open(pkg_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "ok": True,
            "dependencies": data.get("dependencies", {}),
            "devDependencies": data.get("devDependencies", {}),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@tool(
    "install_package",
    "安装 Python 或 Node.js 包。根据当前目录的配置文件自动选择包管理器。",
    timeout=180,
)
def install_package(
    packages: str = Field(description="要安装的包名，逗号分隔，如 'requests,numpy'"),
    upgrade: bool = Field(False, description="是否升级到最新版本"),
    dev: bool = Field(False, description="是否作为开发依赖安装(npm)"),
    path: str = Field("", description="项目根目录，留空用当前目录"),
) -> dict:
    """通用包安装工具。"""
    pkg_list = [p.strip() for p in (packages or "").split(",") if p.strip()]
    if not pkg_list:
        return {"ok": False, "error": "包名不能为空"}

    root = os.path.abspath(os.path.expanduser(path or resolve_cwd()))

    # 检测包管理器
    files = set(os.listdir(root))
    has_pyproject = "pyproject.toml" in files or "requirements.txt" in files
    has_package_json = "package.json" in files

    if has_package_json and not has_pyproject:
        return install_npm_packages(pkg_list, dev=dev)
    else:
        return install_pip_packages(pkg_list, upgrade=upgrade)


@tool(
    "list_packages",
    "列出当前项目的已安装依赖包。",
    timeout=30,
)
def list_packages(
    path: str = Field("", description="项目根目录，留空用当前目录"),
    type: str = Field("all", description="列出类型: all/python/npm"),
) -> dict:
    """列出依赖包。"""
    root = os.path.abspath(os.path.expanduser(path or resolve_cwd()))
    files = set(os.listdir(root))

    if type in ("all", "python"):
        pip_result = list_pip_packages()
    else:
        pip_result = None

    if type in ("all", "npm"):
        npm_result = list_npm_packages(root)
    else:
        npm_result = None

    return {
        "path": root,
        "python_packages": pip_result.get("packages", {}) if pip_result else None,
        "npm_dependencies": npm_result.get("dependencies", {}) if npm_result else None,
        "npm_dev_dependencies": npm_result.get("devDependencies", {}) if npm_result else None,
    }
