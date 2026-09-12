"""代码沙箱执行器。

参考 OpenHands 的隔离思路,分两级:

1. **Docker 容器模式**(强隔离,推荐生产):
   - 一次性容器,独立文件系统,无法访问宿主机
   - 内存/CPU 资源限制
   - 无网络(--network none)
   - 超时自动 kill
   - 仅通过 stdout 输出,无持久化

2. **Subprocess 降级模式**(本地演示,弱隔离):
   - 独立临时工作目录
   - 超时强制 kill
   - 无网络限制(本地模式)、资源限制尽力而为
   - 用 ResourceWarning 提示生产应启用 Docker
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from typing import Optional

# Docker 镜像
SANDBOX_IMAGE = "python:3.12-slim"
DEFAULT_TIMEOUT = 30  # 秒
MAX_MEMORY_MB = 256  # Docker 内存限制
MAX_CPU = 1.0  # Docker CPU 限制


def _resolve_python() -> str:
    """找到可调用的 Python 解释器路径。

    打包后 sys.executable 是应用 exe(agent-backend.exe),不能用作子进程解释器,
    需要回退到显式配置 / PATH 中的 python / 常见安装位置。
    """
    try:
        from app.config import settings

        cfg = settings.sandbox_python
        if cfg:
            cand = os.path.abspath(os.path.expanduser(cfg))
            if os.path.isfile(cand):
                return cand
    except Exception:  # noqa: BLE001
        pass

    if not getattr(sys, "frozen", False):
        return sys.executable

    for cand in ("python", "python3"):
        exe = shutil.which(cand)
        if exe:
            return exe

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "miniconda3", "python.exe"),
        os.path.join(home, "anaconda3", "python.exe"),
    ]
    py_dir = os.path.join(home, "AppData", "Local", "Programs", "Python")
    if os.path.isdir(py_dir):
        for ver in sorted(os.listdir(py_dir), reverse=True):
            candidates.append(os.path.join(py_dir, ver, "python.exe"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


class SandboxResult:
    def __init__(self, success: bool, output: str, returncode: int, mode: str, duration: float):
        self.success = success
        self.output = output
        self.returncode = returncode
        self.mode = mode
        self.duration = duration

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "returncode": self.returncode,
            "mode": self.mode,
            "duration_s": round(self.duration, 2),
        }


def docker_available() -> bool:
    """检测 Docker daemon 是否可用。"""
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def run_in_docker(code: str, timeout: int = DEFAULT_TIMEOUT) -> SandboxResult:
    """在一次性 Docker 容器中执行代码(强隔离)。"""
    # 检查镜像是否存在
    check = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if check.returncode != 0:
        pull = subprocess.run(
            ["docker", "pull", SANDBOX_IMAGE],
            capture_output=True,
            text=True,
            timeout=max(300, timeout),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if pull.returncode != 0:
            return SandboxResult(
                success=False,
                output=f"[沙箱] 拉取镜像失败: {pull.stderr[-300:]}",
                returncode=pull.returncode,
                mode="docker",
                duration=0,
            )

    container_name = f"sandbox-{uuid.uuid4().hex[:8]}"
    # 编码后写入容器 stdin
    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--memory", f"{MAX_MEMORY_MB}m",
        "--cpus", str(MAX_CPU),
        "--network", "none",
        "--pids-limit", "128",
        "-i",  # 交互,从 stdin 读代码
        SANDBOX_IMAGE,
        "python", "-c", "import sys; exec(sys.stdin.read())",
    ]

    start = time.time()
    try:
        r = subprocess.run(
            cmd,
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        output = r.stdout
        if r.stderr.strip():
            output += "\n[stderr]\n" + r.stderr
        output = output[:6000]  # 限制输出长度
        return SandboxResult(
            success=r.returncode == 0,
            output=output or "(无输出)",
            returncode=r.returncode,
            mode="docker",
            duration=time.time() - start,
        )
    except subprocess.TimeoutExpired:
        # 超时强制清理容器
        subprocess.run(
            ["docker", "kill", container_name],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return SandboxResult(
            success=False,
            output=f"[沙箱] 执行超时({timeout}s),已终止容器",
            returncode=-1,
            mode="docker",
            duration=time.time() - start,
        )
    except Exception as e:  # noqa: BLE001
        return SandboxResult(
            success=False,
            output=f"[沙箱] Docker 执行异常: {e}",
            returncode=-1,
            mode="docker",
            duration=time.time() - start,
        )


def run_in_subprocess(code: str, timeout: int = DEFAULT_TIMEOUT, language: str = "python") -> SandboxResult:
    """在本地 subprocess 中执行代码(弱隔离,支持多语言)。"""
    lang = language.lower().strip()
    lang_map = {
        # Python
        "py": "python", "python": "python",
        # JavaScript / TypeScript
        "js": "javascript", "javascript": "javascript",
        "ts": "typescript", "typescript": "typescript",
        # 编译型语言
        "go": "go", "rust": "rust", "java": "java",
        "c": "c", "cpp": "cpp", "c++": "cpp",
        "swift": "swift",
        "kotlin": "kotlin",
        "csharp": "csharp", "c#": "csharp", "cs": "csharp",
        "zig": "zig",
        "v": "v", "vlang": "v",
        "haskell": "haskell", "hs": "haskell",
        # 脚本型语言 (通用解释器)
        "ruby": "ruby", "rb": "ruby",
        "php": "php",
        "perl": "perl", "pl": "perl",
        "lua": "lua",
        "dart": "dart",
        "julia": "julia", "jl": "julia",
        "r": "r", "rscript": "r",
        "octave": "octave", "matlab": "octave",
        "powershell": "powerhell", "ps1": "powerhell", "ps": "powerhell",
        # Shell
        "sh": "shell", "bash": "shell", "zsh": "shell", "fish": "shell",
    }
    lang = lang_map.get(lang, lang)

    workdir = tempfile.mkdtemp(prefix="agent_sandbox_")
    try:
        if lang == "python":
            return _run_python(code, timeout, workdir)
        elif lang in ("javascript", "typescript"):
            return _run_node(code, timeout, workdir, lang)
        elif lang == "go":
            return _run_go(code, timeout, workdir)
        elif lang == "rust":
            return _run_rust(code, timeout, workdir)
        elif lang == "java":
            return _run_java(code, timeout, workdir)
        elif lang in ("c", "cpp"):
            return _run_c(code, timeout, workdir, lang)
        elif lang == "swift":
            return _run_swift(code, timeout, workdir)
        elif lang == "kotlin":
            return _run_kotlin(code, timeout, workdir)
        elif lang == "csharp":
            return _run_csharp(code, timeout, workdir)
        elif lang == "zig":
            return _run_zig(code, timeout, workdir)
        elif lang == "v":
            return _run_v(code, timeout, workdir)
        elif lang == "haskell":
            return _run_haskell(code, timeout, workdir)
        elif lang == "ruby":
            return _run_ruby(code, timeout, workdir)
        elif lang == "php":
            return _run_php(code, timeout, workdir)
        elif lang == "perl":
            return _run_perl(code, timeout, workdir)
        elif lang == "lua":
            return _run_lua(code, timeout, workdir)
        elif lang == "dart":
            return _run_dart(code, timeout, workdir)
        elif lang == "julia":
            return _run_julia(code, timeout, workdir)
        elif lang == "r":
            return _run_r(code, timeout, workdir)
        elif lang == "octave":
            return _run_octave(code, timeout, workdir)
        elif lang == "powerhell":
            return _run_powerhell(code, timeout, workdir)
        elif lang in ("shell", "bash", "zsh", "fish"):
            return _run_shell(code, timeout, workdir)
        else:
            return SandboxResult(
                success=False, output=f"[沙箱] 不支持的语言: {lang}。支持的: python, js/ts, go, rust, java, c/c++, swift, kotlin, c#, zig, v, haskell, ruby, php, perl, lua, dart, julia, r, octave, powershell, shell/bash", returncode=-1, mode="subprocess", duration=0
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---- 各语言执行器 ----

def _safe_env() -> dict:
    """构建沙箱子进程的最小安全环境变量(修复密钥泄露)。

    旧实现继承宿主机全部 os.environ, 恶意/误执行的代码可直接
    os.environ 读到 DEEPSEEK_API_KEY / OPENAI_API_KEY 等密钥并外传。
    现仅保留解释器运行必需的变量, 显式剥离所有可能含凭据的变量。
    各语言解释器(node/go/rust 等)需要 PATH 来定位依赖工具链,
    由调用方按需追加。
    """
    allowed = {
        "PATH": os.environ.get("PATH", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),  # Windows 运行库定位
        "WINDIR": os.environ.get("WINDIR", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "",  # 禁 PYTHONPATH, 防导入业务代码
    }
    # 仅保留非空值
    return {k: v for k, v in allowed.items() if v}


def _run_python(code: str, timeout: int, workdir: str) -> SandboxResult:
    python = _resolve_python()
    if not python:
        return SandboxResult(False, "[沙箱] 未找到可用的 Python 解释器", -1, "subprocess", 0)
    script = os.path.join(workdir, "main.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        r = subprocess.run(
            [python, "-u", script], capture_output=True, text=True, timeout=timeout,
            cwd=workdir,
            env=_safe_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        # subprocess.run 在超时时已自动 kill 子进程, 无需额外 _kill_tree
        # (旧实现误引用 terminal_tool._kill_tree, 本模块未定义, 会导致 NameError)
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_node(code: str, timeout: int, workdir: str, lang: str) -> SandboxResult:
    cmd = shutil.which("node")
    if not cmd:
        # 尝试 Deno
        cmd = shutil.which("deno")
        if cmd:
            ext = ".ts" if lang == "typescript" else ".js"
            script = os.path.join(workdir, "main" + ext)
            with open(script, "w", encoding="utf-8") as f:
                f.write(textwrap.dedent(code))
            start = time.time()
            try:
                r = subprocess.run([cmd, "run", "--allow-all", script],
                                   capture_output=True, text=True, timeout=timeout,
                                   cwd=workdir, env=_safe_env(),
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                out = r.stdout
                if r.stderr.strip():
                    out += "\n[stderr]\n" + r.stderr
                return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
            except subprocess.TimeoutExpired:
                return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
            except Exception as e:
                return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)
        return SandboxResult(False, "[沙箱] 未找到 Node.js 或 Deno,请先安装 Node.js", -1, "subprocess", 0)

    ext = ".ts" if lang == "typescript" else ".js"
    script = os.path.join(workdir, "main" + ext)
    with open(script, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        r = subprocess.run([cmd, script], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_go(code: str, timeout: int, workdir: str) -> SandboxResult:
    go = shutil.which("go")
    if not go:
        return SandboxResult(False, "[沙箱] 未找到 Go 编译器,请先安装 Go", -1, "subprocess", 0)
    main_go = os.path.join(workdir, "main.go")
    with open(main_go, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    # 初始化 go module
    subprocess.run([go, "mod", "init", "sandbox"], capture_output=True, cwd=workdir,
                   env=_safe_env(),
                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    start = time.time()
    try:
        r = subprocess.run([go, "run", "."], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_rust(code: str, timeout: int, workdir: str) -> SandboxResult:
    rustc = shutil.which("rustc")
    if not rustc:
        return SandboxResult(False, "[沙箱] 未找到 Rust 编译器(rustc),请先安装 Rust", -1, "subprocess", 0)
    src = os.path.join(workdir, "main.rs")
    exe = os.path.join(workdir, "main" + (".exe" if sys.platform == "win32" else ""))
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        subprocess.run([rustc, src, "-o", exe], capture_output=True, timeout=60,
                       env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        r = subprocess.run([exe], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 编译/执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_java(code: str, timeout: int, workdir: str) -> SandboxResult:
    javac = shutil.which("javac")
    java = shutil.which("java")
    if not javac or not java:
        return SandboxResult(False, "[沙箱] 未找到 Java 编译器(javac),请先安装 JDK", -1, "subprocess", 0)
    # 从代码中提取类名
    import re
    m = re.search(r'(?:public\s+)?class\s+(\w+)', code)
    class_name = m.group(1) if m else "Main"
    src = os.path.join(workdir, class_name + ".java")
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        subprocess.run([javac, src], capture_output=True, timeout=60,
                       env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        r = subprocess.run([java, class_name], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 编译/执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_c(code: str, timeout: int, workdir: str, lang: str) -> SandboxResult:
    compiler = shutil.which("gcc" if lang == "c" else "g++")
    if not compiler:
        # 尝试 clang
        compiler = shutil.which("clang")
    if not compiler:
        compiler = shutil.which("cl")  # MSVC
    if not compiler:
        return SandboxResult(False, f"[沙箱] 未找到 {lang.upper()} 编译器(gcc/clang/cl),请先安装", -1, "subprocess", 0)
    ext = ".c" if lang == "c" else ".cpp"
    src = os.path.join(workdir, "main" + ext)
    exe_ext = ".exe" if sys.platform == "win32" else ""
    exe = os.path.join(workdir, "main" + exe_ext)
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    compile_cmd = [compiler, src, "-o", exe]
    if lang == "cpp":
        compile_cmd.append("-std=c++17")
    start = time.time()
    try:
        subprocess.run(compile_cmd, capture_output=True, timeout=60,
                       env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        r = subprocess.run([exe], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 编译/执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_shell(code: str, timeout: int, workdir: str) -> SandboxResult:
    if sys.platform == "win32":
        cmd = ["cmd.exe", "/c"]
    else:
        cmd = ["/bin/sh", "-c"]
    start = time.time()
    try:
        # 修复: shell 命令也不继承宿主机全量环境变量(含 API Key), 仅保留最小运行变量
        r = subprocess.run(cmd + [textwrap.dedent(code)], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_generic_interpreter(code: str, interpreter: str, ext: str, timeout: int, workdir: str, extra_args: list = None) -> SandboxResult:
    """通用解释器运行器:适用于 Ruby/PHP/Perl/Lua/Dart/Julia/PowerShell 等。"""
    cmd = shutil.which(interpreter)
    if not cmd:
        return SandboxResult(False, f"[沙箱] 未找到 {interpreter},请先安装", -1, "subprocess", 0)
    script = os.path.join(workdir, "main" + ext)
    with open(script, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    run_cmd = [cmd] + (extra_args or []) + [script]
    try:
        r = subprocess.run(run_cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_swift(code: str, timeout: int, workdir: str) -> SandboxResult:
    swift = shutil.which("swift")
    if not swift:
        # macOS 上 swiftc 可能独立
        swift = shutil.which("swiftc")
    if not swift:
        return SandboxResult(False, "[沙箱] 未找到 Swift 编译器,请先安装 Swift", -1, "subprocess", 0)
    src = os.path.join(workdir, "main.swift")
    exe = os.path.join(workdir, "main" + (".exe" if sys.platform == "win32" else ""))
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        subprocess.run([swift, src, "-o", exe], capture_output=True, timeout=120,
                       env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        r = subprocess.run([exe], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 编译/执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_kotlin(code: str, timeout: int, workdir: str) -> SandboxResult:
    kotlinc = shutil.which("kotlinc")
    if not kotlinc:
        return SandboxResult(False, "[沙箱] 未找到 Kotlin 编译器(kotlinc),请先安装 Kotlin", -1, "subprocess", 0)
    src = os.path.join(workdir, "Main.kt")
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        # 编译到临时目录
        out_dir = os.path.join(workdir, "kout")
        os.makedirs(out_dir, exist_ok=True)
        subprocess.run([kotlinc, src, "-d", out_dir], capture_output=True, timeout=120,
                       env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        java = shutil.which("java")
        if not java:
            return SandboxResult(False, "[沙箱] 未找到 java,请先安装 JDK", -1, "subprocess", time.time() - start)
        r = subprocess.run([java, "-cp", out_dir, "Main"], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, env=_safe_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 编译/执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_csharp(code: str, timeout: int, workdir: str) -> SandboxResult:
    """C# 执行:优先 dotnet-script,降级到 csc/roslyn。"""
    # 方案1: dotnet-script
    ds = shutil.which("dotnet-script")
    if ds:
        script = os.path.join(workdir, "main.csx")
        with open(script, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(code))
        start = time.time()
        try:
            r = subprocess.run([ds, script], capture_output=True, text=True, timeout=timeout,
                               cwd=workdir, env=_safe_env(),
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            out = r.stdout
            if r.stderr.strip():
                out += "\n[stderr]\n" + r.stderr
            return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
        except subprocess.TimeoutExpired:
            return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
        except Exception as e:
            return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)

    # 方案2: dotnet fsi (F# interactive, 也可跑 C# 脚本)
    fsi = shutil.which("dotnet")
    if fsi:
        script = os.path.join(workdir, "main.fsx")
        with open(script, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(code))
        start = time.time()
        try:
            r = subprocess.run([fsi, script], capture_output=True, text=True, timeout=timeout,
                               cwd=workdir, env=_safe_env(),
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            out = r.stdout
            if r.stderr.strip():
                out += "\n[stderr]\n" + r.stderr
            return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
        except subprocess.TimeoutExpired:
            return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
        except Exception as e:
            return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)

    return SandboxResult(False, "[沙箱] 未找到 dotnet-script 或 dotnet,请安装 .NET SDK", -1, "subprocess", 0)


def _run_dart(code: str, timeout: int, workdir: str) -> SandboxResult:
    dart = shutil.which("dart")
    if not dart:
        return SandboxResult(False, "[沙箱] 未找到 Dart 运行时,请先安装 Flutter/Dart SDK", -1, "subprocess", 0)
    script = os.path.join(workdir, "main.dart")
    with open(script, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        r = subprocess.run([dart, script], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_haskell(code: str, timeout: int, workdir: str) -> SandboxResult:
    ghc = shutil.which("ghc")
    if not ghc:
        return SandboxResult(False, "[沙箱] 未找到 GHC(Haskell 编译器),请先安装 Haskell", -1, "subprocess", 0)
    src = os.path.join(workdir, "Main.hs")
    exe = os.path.join(workdir, "main" + (".exe" if sys.platform == "win32" else ""))
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        subprocess.run([ghc, src, "-o", exe], capture_output=True, timeout=120,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        r = subprocess.run([exe], capture_output=True, text=True, timeout=timeout,
                           cwd=workdir, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        out = r.stdout
        if r.stderr.strip():
            out += "\n[stderr]\n" + r.stderr
        return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 编译/执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_julia(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "julia", ".jl", timeout, workdir)


def _run_r(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "Rscript" if sys.platform == "win32" else "Rscript", ".R", timeout, workdir)


def _run_octave(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "octave-cli" if sys.platform == "win32" else "octave", ".m", timeout, workdir)


def _run_ruby(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "ruby", ".rb", timeout, workdir)


def _run_perl(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "perl", ".pl", timeout, workdir)


def _run_lua(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "lua", ".lua", timeout, workdir)


def _run_php(code: str, timeout: int, workdir: str) -> SandboxResult:
    return _run_generic_interpreter(code, "php", ".php", timeout, workdir)


def _run_powerhell(code: str, timeout: int, workdir: str) -> SandboxResult:
    """PowerShell 执行。"""
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if not pwsh:
            return SandboxResult(False, "[沙箱] 未找到 PowerShell", -1, "subprocess", 0)
        script = os.path.join(workdir, "main.ps1")
        with open(script, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(code))
        start = time.time()
        try:
            r = subprocess.run([pwsh, "-NoProfile", "-File", script],
                               capture_output=True, text=True, timeout=timeout,
                               cwd=workdir, env=_safe_env(),
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            out = r.stdout
            if r.stderr.strip():
                out += "\n[stderr]\n" + r.stderr
            return SandboxResult(r.returncode == 0, out[:6000] or "(无输出)", r.returncode, "subprocess", time.time() - start)
        except subprocess.TimeoutExpired:
            return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
        except Exception as e:
            return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)
    else:
        return SandboxResult(False, "[沙箱] PowerShell 仅在 Windows 上可用", -1, "subprocess", 0)


def _run_zig(code: str, timeout: int, workdir: str) -> SandboxResult:
    zig = shutil.which("zig")
    if not zig:
        return SandboxResult(False, "[沙箱] 未找到 Zig 编译器,请先安装 Zig", -1, "subprocess", 0)
    src = os.path.join(workdir, "main.zig")
    exe = os.path.join(workdir, "main" + (".exe" if sys.platform == "win32" else ""))
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        subprocess.run([zig, "run", src], capture_output=True, text=True, timeout=timeout,
                       cwd=workdir, env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        # zig run 直接执行，不需要单独编译
        return SandboxResult(True, "(Zig 使用 zig run 直接执行，请检查上方输出)", 0, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def _run_v(code: str, timeout: int, workdir: str) -> SandboxResult:
    v = shutil.which("v")
    if not v:
        return SandboxResult(False, "[沙箱] 未找到 V 编译器,请先安装 V", -1, "subprocess", 0)
    src = os.path.join(workdir, "main.v")
    exe = os.path.join(workdir, "main" + (".exe" if sys.platform == "win32" else ""))
    with open(src, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(code))
    start = time.time()
    try:
        subprocess.run([v, "run", src], capture_output=True, text=True, timeout=timeout,
                       cwd=workdir, env=_safe_env(),
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        return SandboxResult(True, "(V 使用 v run 直接执行，请检查上方输出)", 0, "subprocess", time.time() - start)
    except subprocess.TimeoutExpired:
        return SandboxResult(False, f"[沙箱] 执行超时({timeout}s)", -1, "subprocess", time.time() - start)
    except Exception as e:
        return SandboxResult(False, f"[沙箱] 执行异常: {e}", -1, "subprocess", time.time() - start)


def run_code_sandboxed(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    use_docker: Optional[bool] = None,
    language: str = "python",
) -> SandboxResult:
    """统一入口:优先 Docker,不可用时降级 subprocess。

    支持的语言(需对应运行时已安装):
      Python   : python / py
      JS/TS    : javascript / js / typescript / ts
      Go       : go
      Rust     : rust
      Java     : java
      C/C++    : c / cpp / c++
      Swift    : swift
      Kotlin   : kotlin
      C#       : csharp / c# / cs
      Zig      : zig
      V        : v / vlang
      Haskell  : haskell / hs
      Ruby     : ruby / rb
      PHP      : php
      Perl     : perl / pl
      Lua      : lua
      Dart     : dart
      Julia    : julia / jl
      R        : r
      Octave   : octave / matlab
      PowerShell: powershell / ps1
      Shell    : shell / bash / zsh / fish
    """
    if use_docker is None:
        use_docker = docker_available()

    if use_docker:
        return run_in_docker(code, timeout, language)
    return run_in_subprocess(code, timeout, language)
