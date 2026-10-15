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

# 终端命令在用户环境执行:继承完整用户环境变量(用户凭据需可用),
# 但剥离 API 密钥类变量, 防止 LLM 生成命令通过 env 读取密钥外传(密钥泄露防护)。
_SENSITIVE_ENV_PREFIXES = ("AGENT_API_KEY", "API_KEY", "APIKEY", "SECRET", "PASSWORD", "PASSWD", "TOKEN")
_SENSITIVE_ENV_EXACT = {
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "QWEN_API_KEY", "QWEN_DASHSCOPE_API_KEY",
    "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITLAB_TOKEN", "HUGGINGFACE_TOKEN",
    "DATABASE_PASSWORD", "DB_PASSWORD", "MYSQL_PASSWORD", "PGPASSWORD",
}

def _terminal_safe_env() -> dict:
    """用户全量环境 + 剥离明文凭据变量(密钥名),保留 PATH 等运行必需变量。"""
    env = {k: v for k, v in os.environ.items()
           if k not in _SENSITIVE_ENV_EXACT and not any(
               k.startswith(p) or p in k for p in _SENSITIVE_ENV_PREFIXES)}
    env["PYTHONPATH"] = ""  # 防 subprocess 误导入业务代码
    return env

_TERMINAL_SAFE_ENV = _terminal_safe_env()

# 从 security.py 导入统一的命令分类列表
from app.core.security import BLOCKED_COMMAND_PATTERNS, _classify_command

# 保留兼容:旧的 DANGEROUS_PATTERNS 映射到 BLOCKED_COMMAND_PATTERNS
DANGEROUS_PATTERNS = [(p, p) for p in BLOCKED_COMMAND_PATTERNS]


def _blocked_reason(command: str) -> str:
    """检查命令是否被硬拦截。返回拦截原因或空字符串。

    分层拦截:
    1. 硬拦截黑名单(危险操作: 删除根目录/格式化/关机/注册表等)
    2. shell 元字符组合注入拦截(多分隔符 + 重定向到系统文件)
    3. 解释器嵌套执行拦截(python -c/node -e/base64 解码等, 可绕过 blacklist)
    """
    low = command.strip().lower()
    # 1. 危险命令黑名单
    classification = _classify_command(command)
    if classification == "blocked":
        return f"命令包含危险操作已被拦截: {command.strip()[:100]}"
    # 2. shell 元字符组合注入拦截: 多分隔符视为注入; 单重定向目标落在系统路径时拦截
    dangerous_meta = [';', '&&', '||', '|', '`', '$(', '${', '>', '>>']
    _SYSTEM_FILE_DIRS = ("/etc/", "c:\\windows", "c:\\winnt", "/system32",
                         "/boot/", "/sbin/", "/usr/", "/bin/", "/lib", "/dev/", "registry", "regedit")
    for meta in dangerous_meta:
        if meta in low:
            parts = command.split(meta)
            if len(parts) > 2:  # 多个分隔符说明是注入尝试
                return f"命令包含危险字符 '{meta}' 被拦截"
            if meta in ('>', '>>'):  # 单重定向: 目标落在系统路径才拦截(允许正常日志重定向)
                target = parts[1].strip().lower()
                if any(d in target for d in _SYSTEM_FILE_DIRS):
                    return f"重定向目标为系统文件/目录, 已被拦截: {meta}{target[:60]}"
    # 3. 解释器嵌套执行拦截(内部 payload 可能绕过 blacklist)
    _INTERPRETER_EXEC = (
        "python -c", "python3 -c", "node -e", "node --eval", "perl -e", "ruby -e",
        "php -r", "bash -c", "sh -c", "/bin/sh -c", "cmd /c", "powershell -command",
        "powershell -enc", "powershell -encodedcommand",
        "base64 -d", "base64 --decode", "certutil -decode",
    )
    if any(marker in low for marker in _INTERPRETER_EXEC):
        return f"命令通过解释器/解码器执行, 可能绕过安全检查, 已被拦截: {command.strip()[:100]}"
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
    from app.tools.project_context import resolve_cwd
    return resolve_cwd()


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
            env=_TERMINAL_SAFE_ENV,
            creationflags=creationflags,
            start_new_session=sys.platform != "win32",
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            # 杀进程树后管道写端关闭,正常会很快 EOF;这里带 10s 兜底,
            # 取回"已产出的部分 stdout/stderr",避免孙进程残留持有写端句柄时
            # 第二次 communicate() 无保护地永久阻塞(历史 bug:超时命令把整个
            # Agent 卡死)。仍超时则放弃剩余数据,绝不无限等。
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
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
        partial = "已带回超时前产出的部分输出(见 stdout/stderr)" if (out or err) else "超时前无输出"
        result["error"] = f"命令执行超时({timeout}s),已终止进程树,{partial}"
    return result


@tool(
    "run_terminal",
    "在本地终端执行 shell 命令(Windows 默认 cmd),用于运行构建/测试、安装依赖(npm/pip)、执行 git、查询系统信息。"
    "命令在当前用户环境运行,破坏性操作(格式化/删除注册表/关机等)会被拦截。返回 stdout/stderr/退出码",
    # 同 run_code 的道理:内部钳位上限是 300s,registry 上限必须更大。
    # 否则请求 timeout=300 时 registry 在 180s 就硬杀 asyncio.to_thread 的 await,
    # 而底层 Popen 子进程仍在后台跑 → 命令没停、工具却报「执行超时(180s)」。
    # 330 = 300(内部上限) + 30(_kill_tree 收尾与 communicate 的余量)
    timeout=330,
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
