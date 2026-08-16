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


def run_in_subprocess(code: str, timeout: int = DEFAULT_TIMEOUT) -> SandboxResult:
    """在本地 subprocess 中执行(弱隔离,演示用)。"""
    python = _resolve_python()
    if not python:
        return SandboxResult(
            success=False,
            output="[沙箱] 未找到可用的 Python 解释器,请配置 SANDBOX_PYTHON 指向 python.exe",
            returncode=-1,
            mode="subprocess",
            duration=0,
        )
    workdir = tempfile.mkdtemp(prefix="agent_sandbox_")
    try:
        # 写代码到临时文件(绝对路径)
        script = os.path.join(workdir, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(code))

        start = time.time()
        try:
            r = subprocess.run(
                [python, "-u", script],
                capture_output=True,
                text=True,
                timeout=timeout,
                # cwd 用当前工作目录(与 write_file 的相对路径基准一致),脚本本身用绝对路径定位
                cwd=os.getcwd(),
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": "",  # 禁止导入业务代码
                },
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            output = r.stdout
            if r.stderr.strip():
                output += "\n[stderr]\n" + r.stderr
            output = output[:6000]
            return SandboxResult(
                success=r.returncode == 0,
                output=output or "(无输出)",
                returncode=r.returncode,
                mode="subprocess",
                duration=time.time() - start,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False,
                output=f"[沙箱] 执行超时({timeout}s),已终止进程",
                returncode=-1,
                mode="subprocess",
                duration=time.time() - start,
            )
        except Exception as e:  # noqa: BLE001
            return SandboxResult(
                success=False,
                output=f"[沙箱] 执行异常: {e}",
                returncode=-1,
                mode="subprocess",
                duration=time.time() - start,
            )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_code_sandboxed(code: str, timeout: int = DEFAULT_TIMEOUT, use_docker: Optional[bool] = None) -> SandboxResult:
    """统一入口:优先 Docker,不可用时降级 subprocess。

    use_docker: True 强制 Docker / False 强制 subprocess / None 自动检测
    """
    if use_docker is None:
        use_docker = docker_available()

    if use_docker:
        return run_in_docker(code, timeout)
    return run_in_subprocess(code, timeout)
