"""终端命令工具:在本地执行 shell 命令,用于运行构建、测试、安装依赖、查询系统信息。

与 run_code 的区别:run_code 在沙箱里跑 Python 代码(隔离),run_terminal 在当前
用户环境执行任意命令(需要信任,含危险命令拦截)。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

from pydantic import Field

from app.config import settings
from app.tools.registry import tool

MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT = 120

# 破坏性/危险命令前缀拦截(命中即拒绝,引导用文件工具/安全方式)
DANGEROUS_PATTERNS = [
    ("format", "格式化磁盘"),
    ("diskpart", "磁盘分区操作"),
    ("deltree", "递归删除目录"),
    ("shutdown", "关机/重启"),
    ("reg delete", "删除注册表"),
    ("net user", "用户账户管理"),
    ("sc delete", "删除系统服务"),
    ("taskkill /f /im", "强制结束系统进程"),
    ("rm -rf /", "删除根目录"),
    (":(){", "fork 炸弹"),
    ("mkfs", "格式化文件系统"),
    ("rd /s", "递归删除目录"),
    ("rmdir /s", "递归删除目录"),
]


def _blocked_reason(command: str) -> str:
    low = command.strip().lower()
    for pattern, desc in DANGEROUS_PATTERNS:
        if pattern.lower() in low:
            return f"命令包含危险操作({desc})已被拦截: {pattern}"
    return ""


def _decode(data: bytes) -> str:
    if not data:
        return ""
    # cmd 输出编码不稳定(UTF-8/GBK 都可能),逐候选严格解码
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时后强制终止整个进程树(Windows 用 taskkill /T,类 Unix 用进程组)。"""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/pid", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _build_argv(command: str) -> list[str]:
    shell = (settings.terminal_shell or "auto").lower()
    if shell == "powershell":
        return ["powershell", "-NoProfile", "-Command", command]
    if shell == "bash":
        return ["bash", "-lc", command]
    if shell == "cmd":
        return ["cmd.exe", "/c", command]
    # auto: Windows 用 cmd,其他用 sh
    if sys.platform == "win32":
        return ["cmd.exe", "/c", command]
    return ["/bin/sh", "-lc", command]


def _resolve_cwd(cwd: str | None) -> str:
    if cwd:
        full = os.path.abspath(os.path.expanduser(cwd))
        if os.path.isdir(full):
            return full
        raise ValueError(f"工作目录不存在: {full}")
    cfg = (settings.terminal_cwd or "").strip()
    if cfg:
        full = os.path.abspath(os.path.expanduser(cfg))
        if os.path.isdir(full):
            return full
    return os.getcwd()


def _run_command(command: str, cwd: str, timeout: int) -> dict:
    start = time.time()
    argv = _build_argv(command)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=creationflags,
            start_new_session=sys.platform != "win32",
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            timed_out = True
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"启动命令失败: {e}", "duration_s": round(time.time() - start, 2)}

    stdout = _decode(out)[:MAX_OUTPUT_CHARS]
    stderr = _decode(err)[:MAX_OUTPUT_CHARS]
    truncated = len(out) > MAX_OUTPUT_CHARS or len(err) > MAX_OUTPUT_CHARS
    result = {
        "success": proc.returncode == 0 and not timed_out,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "cwd": cwd,
        "duration_s": round(time.time() - start, 2),
        "truncated": truncated,
    }
    if timed_out:
        result["error"] = f"命令执行超时({timeout}s),已终止进程树"
    return result


@tool(
    "run_terminal",
    "在本地终端执行 shell 命令(Windows 默认 cmd),用于运行构建/测试、安装依赖(npm/pip)、执行 git、查询系统信息。"
    "命令在当前用户环境运行,破坏性操作(格式化/删除注册表/关机等)会被拦截。返回 stdout/stderr/退出码",
    timeout=180,
)
def run_terminal(
    command: str = Field(description="要执行的命令,如 'npm test' 或 'python -m pytest'"),
    cwd: str = Field("", description="命令工作目录,留空用默认工作目录"),
    timeout: int = Field(120, description="超时秒数,最大 300"),
) -> str:
    command = (command or "").strip()
    if not command:
        return {"success": False, "error": "命令不能为空"}
    blocked = _blocked_reason(command)
    if blocked:
        return {"success": False, "error": blocked, "blocked": True}
    try:
        cwd_path = _resolve_cwd(cwd)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    timeout = max(1, min(timeout, 300))
    return _run_command(command, cwd_path, timeout)
