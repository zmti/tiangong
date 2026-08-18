"""代码执行工具:通过沙箱运行多语言代码。

支持的 20+ 种语言(需对应运行时已安装):
Python / JS / TS / Go / Rust / Java / C / C++ / Swift / Kotlin / C# / Zig / V / Haskell / Ruby / PHP / Perl / Lua / Dart / Julia / R / Octave / PowerShell / Shell
"""
from __future__ import annotations

from pydantic import Field

from app.tools.code_sandbox import run_code_sandboxed
from app.tools.registry import tool

SUPPORTED_LANGUAGES = [
    "python", "py",
    "javascript", "js", "typescript", "ts",
    "go", "rust", "java",
    "c", "cpp", "c++",
    "swift", "kotlin",
    "csharp", "c#", "cs",
    "zig", "v", "vlang",
    "haskell", "hs",
    "ruby", "rb",
    "php",
    "perl", "pl",
    "lua",
    "dart",
    "julia", "jl",
    "r",
    "octave", "matlab",
    "powershell", "ps1",
    "shell", "bash", "zsh", "fish",
]


@tool(
    "run_code",
    f"在安全沙箱中执行代码并返回运行结果。支持 {len(SUPPORTED_LANGUAGES)} 种语言: "
    + ", ".join(SUPPORTED_LANGUAGES[:10])
    + f"... 可用于计算、数据分析、算法验证、代码调试。",
    timeout=60,
)
def run_code(
    code: str = Field(description="要执行的代码"),
    language: str = Field("python", description=f"编程语言。支持: {', '.join(SUPPORTED_LANGUAGES)}"),
    timeout: int = Field(30, description="超时秒数,最大 60"),
    use_docker: bool | None = Field(None, description="是否强制使用 Docker 沙箱,默认自动检测"),
) -> str:
    timeout = max(1, min(timeout, 60))
    result = run_code_sandboxed(code, timeout=timeout, use_docker=use_docker, language=language)
    return result.to_dict()
