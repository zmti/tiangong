"""代码执行工具:通过沙箱运行 Python 代码。"""
from __future__ import annotations

from pydantic import Field

from app.tools.code_sandbox import run_code_sandboxed
from app.tools.registry import tool


@tool("run_code", "在安全沙箱中执行 Python 代码并返回运行结果,可用于计算、数据分析、代码调试", timeout=60)
def run_code(
    code: str = Field(description="要执行的 Python 代码"),
    timeout: int = Field(30, description="超时秒数,最大 60"),
    use_docker: bool | None = Field(None, description="是否强制使用 Docker 沙箱,默认自动检测"),
) -> str:
    timeout = max(1, min(timeout, 60))
    result = run_code_sandboxed(code, timeout=timeout, use_docker=use_docker)
    return result.to_dict()
