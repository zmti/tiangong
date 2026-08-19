"""构建感知工具:检测项目配置文件,自动执行对应的构建/安装命令。

检测并支持的构建系统:
- Python: requirements.txt / pyproject.toml / setup.py / setup.cfg → pip install
- Node.js: package.json → npm install / yarn install / pnpm install
- Java: pom.xml → mvn compile
- Go: go.mod → go build
- Make: Makefile → make
- Rust: Cargo.toml → cargo build
- Docker: Dockerfile → docker build

配置项: AGENT_AUTO_BUILD=true 开启自动构建。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

from pydantic import Field

from app.tools.registry import tool


BUILD_DETECTORS: list[tuple[str, list[str], list[str]]] = [
    # (build_system, trigger_files, commands)
    ("pip", ["requirements.txt"], ["pip", "install", "-r", "requirements.txt"]),
    ("pip-poetry", ["pyproject.toml", "poetry.lock"], ["poetry", "install"]),
    ("pip-setup", ["setup.py", "setup.cfg"], ["python", "setup.py", "develop"]),
    ("npm", ["package.json"], ["npm", "install"]),
    ("yarn", ["package.json", "yarn.lock"], ["yarn", "install"]),
    ("pnpm", ["package.json", "pnpm-lock.yaml"], ["pnpm", "install"]),
    ("maven", ["pom.xml"], ["mvn", "compile", "-q"]),
    ("go", ["go.mod"], ["go", "build", "./..."]),
    ("cargo", ["Cargo.toml"], ["cargo", "build", "--release"]),
    ("make", ["Makefile", "makefile"], ["make", "-q"] or ["make"]),
]


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _detect_build_system(root: str) -> Optional[dict]:
    """检测项目根目录的构建系统,返回 {system, cmd, args}。"""
    files = set(os.listdir(root))
    for system, trigger_files, commands in BUILD_DETECTORS:
        if any(f in files for f in trigger_files):
            # 检查命令是否可用
            cmd = commands[0]
            if _which(cmd):
                return {"system": system, "cmd": cmd, "args": commands[1:], "root": root}
    return None


def _run_build(build_info: dict, timeout: int = 120) -> dict:
    """执行构建命令,返回结果。"""
    cmd = build_info["cmd"]
    args = build_info["args"]
    try:
        r = subprocess.run(
            [cmd, *args],
            cwd=build_info["root"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return {
            "system": build_info["system"],
            "command": f"{cmd} {' '.join(args)}",
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": (r.stdout or "")[:3000],
            "stderr": (r.stderr or "")[:1000],
        }
    except subprocess.TimeoutExpired:
        return {"system": build_info["system"], "error": f"构建超时({timeout}s)"}
    except FileNotFoundError:
        return {"system": build_info["system"], "error": f"命令未找到: {cmd}"}
    except Exception as e:
        return {"system": build_info["system"], "error": str(e)}


@tool(
    "detect_build_system",
    "检测当前目录(或指定路径)的构建系统,返回支持的构建命令列表。"
    "用于让 Agent 了解项目结构并自动选择正确的构建方式。",
    timeout=10,
)
def detect_build_system(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
) -> dict:
    root = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    info = _detect_build_system(root)
    if info is None:
        return {"path": root, "detected": None, "message": "未检测到已知的构建系统"}
    return {
        "path": root,
        "detected": info["system"],
        "command": f"{info['cmd']} {' '.join(info['args'])}",
        "message": f"检测到 {info['system']} 项目,运行 `{info['cmd']}` 可构建",
    }


@tool(
    "run_build",
    "检测并执行项目构建。自动识别构建系统(npm/pip/maven/go/cargo等)并运行对应命令。"
    "适合:修改代码后验证构建是否通过。",
    timeout=180,
)
def run_build(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
    timeout: int = Field(180, description="构建超时(秒)"),
) -> dict:
    root = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    info = _detect_build_system(root)
    if info is None:
        return {"path": root, "error": "未检测到支持的构建系统"}
    return _run_build(info, timeout=timeout)


@tool(
    "run_test",
    "检测并运行项目测试。根据构建系统自动选择测试命令(pytest/npm test/mvn test等)。",
    timeout=180,
)
def run_test(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
    extra_args: str = Field("", description="额外传递给测试命令的参数"),
) -> dict:
    root = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    files = set(os.listdir(root))

    test_cmds: list[tuple[str, list[str]]] = []
    if "pytest.ini" in files or "pyproject.toml" in files or "setup.cfg" in files:
        if _which("pytest"):
            test_cmds.append(("pytest", ["pytest", "-q"]))
    if "package.json" in files:
        # 尝试从 package.json 读取 scripts.test
        try:
            pkg = json.loads(open(os.path.join(root, "package.json"), encoding="utf-8").read())
            test_script = pkg.get("scripts", {}).get("test", "")
            if test_script:
                test_cmds.append(("npm-test", test_script.split()))
        except Exception:
            pass
        if not test_cmds and _which("npm"):
            test_cmds.append(("npm-test", ["npm", "test"]))
    if "pom.xml" in files and _which("mvn"):
        test_cmds.append(("maven", ["mvn", "test", "-q"]))
    if "go.mod" in files and _which("go"):
        test_cmds.append(("go-test", ["go", "test", "./..."]))
    if "Cargo.toml" in files and _which("cargo"):
        test_cmds.append(("cargo-test", ["cargo", "test"]))

    if not test_cmds:
        return {"path": root, "error": "未找到可用的测试命令"}

    # 按优先级执行第一个可用的
    for name, cmd in test_cmds:
        if not _which(cmd[0]):
            continue
        full_cmd = cmd + (extra_args.split() if extra_args else [])
        try:
            r = subprocess.run(
                full_cmd,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return {
                "path": root,
                "test_system": name,
                "command": " ".join(full_cmd),
                "ok": r.returncode == 0,
                "returncode": r.returncode,
                "stdout": (r.stdout or "")[:3000],
                "stderr": (r.stderr or "")[:1000],
            }
        except subprocess.TimeoutExpired:
            return {"path": root, "test_system": name, "error": "测试超时(180s)"}
        except Exception as e:
            return {"path": root, "test_system": name, "error": str(e)}

    return {"path": root, "error": "所有测试命令均不可用"}
