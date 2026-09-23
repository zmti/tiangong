"""代码执行工具:通过沙箱运行多语言代码。

支持的 20+ 种语言(需对应运行时已安装):
Python / JS / TS / Go / Rust / Java / C / C++ / Swift / Kotlin / C# / Zig / V / Haskell / Ruby / PHP / Perl / Lua / Dart / Julia / R / Octave / PowerShell / Shell
"""
from __future__ import annotations

import ast
import re

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

# 「代码被输出上限截断」的 SyntaxError 特征。
# 典型现场(2026-09-23):模型在 run_code 里写一段调 API 的 Python,
# 输出 token 触顶后流被切断,网关把 JSON 补齐成"合法"参数,于是残缺代码
# 一路送进沙箱 → `SyntaxError: unterminated string literal (detected at line 4)`。
# 这种错误模型修不好(它看到的代码就是残的),只会原地重试到 3 连败 giveup。
# 这里在**执行前**识别它,直接回一条可操作的指引。
_TRUNCATION_MARKS = (
    "unterminated string literal",
    "unterminated triple-quoted",
    "unterminated f-string",
    "was never closed",
    "unexpected eof",
    "unexpected end of",
    "eof in multi-line",
    "incomplete input",
)

_TRUNCATION_HINT = (
    "代码看起来没有传完整(大概率是生成到输出长度上限被截断),本次**未执行**。\n"
    "请改用下面任一方式,不要原样重试:\n"
    "1. 用 write_file 把代码写成 .py 文件(可分多次追加:先写前半段,再 append 后半段),"
    "然后用 run_terminal 执行 `python 文件.py`;\n"
    "2. 把代码拆成几个短片段分别 run_code;\n"
    "3. 精简代码:去掉长注释/长字符串常量/不必要的 import,控制单次代码在 100 行以内。"
)


def _python_truncation(code: str) -> tuple[bool, str]:
    """用 ast 预解析 Python 代码,返回 (是否疑似被截断, 证据文本)。"""
    try:
        ast.parse(code)
    except SyntaxError as e:
        msg = (e.msg or "").lower()
        if any(m in msg for m in _TRUNCATION_MARKS):
            where = f"第 {e.lineno} 行" if e.lineno else "文件末尾"
            return True, f"{where}: {e.msg}"
        return False, ""
    except ValueError as e:
        # 源码内含空字节等 → 同样是传输出了问题
        return True, f"代码含非法字符: {e}"
    return False, ""


def _bracket_truncation(code: str) -> tuple[bool, str]:
    """非 Python 语言的轻量检查:括号/引号明显不闭合时判定为被截断。

    只做保守判断——成对计数不平衡且不是写在字符串注释里的常规写法。
    宁可漏报(交给沙箱报真错),也不要误杀能跑的代码。
    """
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    quote: str = ""
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return True, f"第 {code.count(chr(10), 0, i) + 1} 行出现不匹配的 '{ch}'"
            stack.pop()
        i += 1
    if quote:
        return True, f"{quote} 引号未闭合"
    if stack:
        return True, f"{len(stack)} 个括号未闭合('{stack[-1]}' 缺少收尾)"
    return False, ""


def _error_summary(output: str, cap: int = 800) -> str:
    """从沙箱合并输出里摘出「异常摘要」(traceback 最后一行 + 少量上文)。

    run_code 原本只回 success/output/returncode,失败时没有 error 字段:
    - 模型要在一大段 output 里自己找 traceback,容易看漏;
    - 下游(自愈/反思)只能靠 output 兜底。
    这里把 `TypeError: 'NoneType' object is not subscriptable` 这类
    决定性的一行单独拎出来放进 error,让失败原因一眼可见。
    """
    if not output:
        return ""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if not lines:
        return ""
    # 优先取最后一条 "...Error: ..." 行(traceback 的结论行)
    tail = ""
    for ln in reversed(lines):
        s = ln.strip()
        if re.search(r"^\w*(Error|Exception|Exit)\b", s) or ": " in s and "Error" in s:
            tail = s
            break
    # 再补上 traceback 里最靠近异常的 2 行源码上下文
    ctx: list[str] = []
    for ln in lines:
        if ln.strip().startswith("File ") or ln.strip().startswith("    "):
            ctx.append(ln.rstrip())
    body = "\n".join(ctx[-2:])
    return ((body + "\n" if body else "") + (tail or lines[-1]))[-cap:]


def _check_intact(code: str, language: str) -> tuple[bool, str]:
    """执行前检查代码是否完整。返回 (是否被截断, 证据)。"""
    lang = (language or "python").strip().lower()
    if not code or not code.strip():
        return False, ""
    if lang in ("python", "py", "python3"):
        return _python_truncation(code)
    if lang in ("javascript", "js", "typescript", "ts", "java", "go", "c", "cpp", "c++",
                "csharp", "c#", "cs", "php", "rust", "kotlin", "swift", "dart"):
        return _bracket_truncation(code)
    return False, ""


@tool(
    "run_code",
    f"在安全沙箱中执行代码并返回运行结果。支持 {len(SUPPORTED_LANGUAGES)} 种语言: "
    + ", ".join(SUPPORTED_LANGUAGES[:10])
    + f"... 可用于计算、数据分析、算法验证、代码调试。"
    "注意:单次代码请控制在 100 行以内;更长的代码请改用 write_file 写成文件(可分段追加),"
    "再用 run_terminal 执行,否则容易在生成时被输出长度上限截断。",
    # registry 超时必须【严格大于】下面的内部钳位上限(60s),否则两者同时到点,
    # registry 的 asyncio.wait_for 会抢先判死 → 模型只看到「工具执行超时(60s)」,
    # 而沙箱自己那句更有用的「[沙箱] 执行超时(60s)」永远来不及返回。
    # 75 = 60(内部上限) + 15(解释器启动/临时目录 IO 的余量)
    timeout=75,
)
def run_code(
    code: str = Field(description="要执行的代码"),
    language: str = Field("python", description=f"编程语言。支持: {', '.join(SUPPORTED_LANGUAGES)}"),
    timeout: int = Field(30, description="超时秒数,最大 60"),
    use_docker: bool | None = Field(None, description="是否强制使用 Docker 沙箱,默认自动检测"),
) -> str:
    timeout = max(1, min(timeout, 60))

    # 完整性预检:代码被截断时不执行,直接回可操作的指引。
    # 否则沙箱只会抛 SyntaxError: unterminated string literal,模型看不出
    # "是自己的输出被截断",只会拿同一段残代码反复重试。
    truncated, evidence = _check_intact(code, language)
    if truncated:
        return {
            "success": False,
            "output": (
                f"⚠️ 代码疑似不完整,已跳过执行 —— {evidence}\n\n{_TRUNCATION_HINT}"
            ),
            "returncode": -1,
            "mode": "precheck",
            "duration_s": 0.0,
            "truncated": True,
            "error": f"代码被截断: {evidence}",
        }

    result = run_code_sandboxed(code, timeout=timeout, use_docker=use_docker, language=language)
    data = result.to_dict()
    # 失败时补一个 error 摘要字段:让模型/自愈一眼看到决定性异常行,
    # 而不是在一整段 output 里自己找 traceback。
    if not data.get("success"):
        data["error"] = _error_summary(data.get("output", ""))
    return data
