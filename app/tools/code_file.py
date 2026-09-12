"""code_file 工具:结构化代码编辑(补丁式修改)+语法检查。

对标 Claude Code 的 edit_file 能力:
- read_file: 读取文件内容
- edit_file: 基于精确字符串替换的补丁式编辑(需匹配原文,支持多段)
- apply_patch: 基于 unified diff 格式应用补丁
- check_syntax: 对支持的语言做语法检查(Python/JS/TS 等)
- count_lines: 统计文件行数(用于评估改动范围)

设计原则:
- 所有编辑必须通过原文匹配确认目标位置,防止意外修改
- 支持多段编辑(一次调用修改多处),每段独立验证
- 语法检查利用对应语言的编译器/linter 输出错误行号
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Optional

from pydantic import Field

from app.config import settings
from app.tools.project_context import resolve_cwd
from app.tools.registry import tool


def _resolve_path(path: str, base_dir: Optional[str] = None) -> str:
    """解析文件路径:优先 base_dir,其次工作目录,绝对路径直接返回。"""
    if os.path.isabs(path):
        return path
    base = base_dir or resolve_cwd()
    return os.path.abspath(os.path.join(base, path))


def _sanitize_path(path: str, allowed_dirs: Optional[list[str]] = None) -> bool:
    """检查路径是否在允许的写入目录下。"""
    real = os.path.realpath(path)
    bases = allowed_dirs or settings.allowed_write_dirs.split(",") if settings.allowed_write_dirs else []
    bases = [os.path.realpath(d) for d in bases if d.strip()]
    if not bases:
        # 默认允许 data 目录和工作目录
        bases = [settings.resolved_data_dir, resolve_cwd()]
        bases = [os.path.realpath(d) for d in bases]
    return any(real.startswith(b) for b in bases)


@tool("read_file", "读取文件内容并返回文本,支持大文件部分读取", timeout=10)
def read_file(
    path: str = Field(description="文件路径(相对于工作目录或绝对路径)"),
    offset: int = Field(0, description="起始行号(从 1 开始),默认 0 表示从头读"),
    limit: int = Field(0, description="读取行数,0=全量;大文件建议用 100~500"),
) -> dict:
    """读取文件内容,返回文本与元信息。"""
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        return {"error": f"路径解析失败: {e}"}
    if not os.path.exists(resolved):
        return {"error": f"文件不存在: {path}"}
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    start = max(0, offset - 1)
    end = start + limit if limit > 0 else len(all_lines)
    lines = all_lines[start:end]
    text = "".join(lines)
    return {
        "path": path,
        "absolute_path": resolved,
        "total_lines": len(all_lines),
        "offset": offset,
        "limit": limit or len(all_lines),
        "content": text,
        "length": len(text),
    }


@tool("edit_file", "基于原文精确匹配的结构化代码编辑,支持多段替换,返回 unified diff 和风险评级", timeout=30)
def edit_file(
    path: str = Field(description="文件路径"),
    old_string: str = Field(description="原文片段(必须完全匹配,包含缩进;用于定位目标位置)"),
    new_string: str = Field(description="替换后的新内容"),
    replace_all: bool = Field(False, description="是否替换所有匹配项(默认只替换第一次出现)"),
) -> dict:
    """补丁式编辑:用 old_string 精确定位,替换为 new_string。返回 unified diff 和风险评级。"""
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        return {"error": f"路径解析失败: {e}"}
    if not os.path.exists(resolved):
        return {"error": f"文件不存在: {path}"}
    if not _sanitize_path(resolved):
        return {"error": f"路径不在允许写入目录内: {path}"}
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"error": f"读取失败: {e}"}

    if old_string not in content:
        return {
            "error": "old_string 未在文件中找到",
            "hint": "请提供完整的原文片段(含缩进),或用 read_file 查看目标位置",
            "match_count": 0,
        }

    # 生成 unified diff(diff 行数限制以避免超大输出)
    import difflib

    changes = content.count(old_string)
    old_lines = content.splitlines(keepends=True)
    new_content = content.replace(old_string, new_string, 1) if not replace_all else content.replace(old_string, new_string)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, n=3))
    diff_text = "".join(diff_lines)[:8000]  # 截断避免超大 diff

    if not replace_all and changes > 1:
        # 提示用户多个匹配项
        return {
            "warning": f"文件中有 {changes} 处匹配 old_string,仅替换了第 1 处",
            "match_count": changes,
            "suggestion": "如需替换全部,设置 replace_all=true",
            "diff": diff_text,
            "risk": _assess_risk(path, diff_text),
        }

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return {"error": f"写入失败: {e}", "diff": diff_text}

    risk = _assess_risk(path, diff_text)
    return {
        "path": path,
        "status": "ok",
        "old_length": len(content),
        "new_length": len(new_content),
        "change_delta": len(new_content) - len(content),
        "matches_replaced": changes if replace_all else 1,
        "diff": diff_text,
        "risk": risk,
        "note": "编辑成功,建议用 check_syntax 验证语法",
    }


# 风险评级规则:根据 diff 特征判断改动风险等级
# 仅危险操作(执行代码/系统调用/文件写入)标记高,新增函数/类属于常规开发不标记高
_RISK_HIGH_PATTERNS = (
    "exec(", "eval(", "compile(", "subprocess",
    "os.system", "os.popen", "__import__",
    "# -*- coding:", "chmod", "chown", "remove(", "unlink(",
    "rmtree", "Popen", "call(", "check_output",
)
_RISK_MEDIUM_PATTERNS = (
    "return ", "if ", "for ", "while ", "with ",
    "print(", "logging.", "self.", "open(",
)


def _assess_risk(path: str, diff: str) -> dict:
    """根据 diff 内容评估修改风险等级。"""
    low_lines = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    high_hits = sum(1 for line in low_lines for p in _RISK_HIGH_PATTERNS if p in line)
    medium_hits = sum(1 for line in low_lines for p in _RISK_MEDIUM_PATTERNS if p in line)
    total_changes = len(low_lines)

    if total_changes == 0:
        level = "none"
    elif high_hits >= 3 or total_changes > 50:
        level = "high"
    elif high_hits >= 1 or total_changes > 20:
        level = "medium"
    else:
        level = "low"

    return {
        "level": level,
        "added_lines": total_changes,
        "high_risk_indicators": high_hits,
        "hint": (
            "高风险改动,建议仔细审查后再提交" if level == "high"
            else "中等风险改动,建议验证后提交" if level == "medium"
            else "低风险改动"
        ),
    }


@tool("apply_patch", "基于 unified diff 格式应用代码补丁(适合自动生成的大段改动)", timeout=30)
def apply_patch(
    path: str = Field(description="目标文件路径"),
    diff: str = Field(description="unified diff 格式的补丁字符串"),
    dry_run: bool = Field(False, description="仅预览不实际写入"),
) -> dict:
    """应用 unified diff 补丁。补丁格式示例:
    --- a/src/foo.py
    +++ b/src/foo.py
    @@ -10,3 +10,4 @@
     def foo():
    +    new_line = True
         return bar
    """
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        return {"error": f"路径解析失败: {e}"}
    if not _sanitize_path(resolved):
        return {"error": f"路径不在允许写入目录内: {path}"}

    # 尝试用系统 patch 命令,否则手动应用
    try:
        # 构造临时 .patch 文件
        with tempfile.NamedTemporaryFile(suffix=".patch", mode="w", encoding="utf-8", delete=False) as f:
            patch_path = f.name
            f.write(diff)
        args = ["patch", "--quiet", "--forward"]
        if dry_run:
            args.append("--dry-run")
        result = subprocess.run(args + [resolved, patch_path], capture_output=True, text=True, timeout=30)
        os.unlink(patch_path)
        if result.returncode != 0:
            return {"error": f"patch 命令失败: {result.stderr[:500]}", "stdout": result.stdout[:500]}
        return {"status": "ok", "dry_run": dry_run, "note": "补丁应用成功"}
    except FileNotFoundError:
        pass  # 无 patch 命令,使用 Python 手动解析
    except Exception as e:  # noqa: BLE001
        return {"error": f"补丁应用失败: {e}"}

    # Python 手动应用 unified diff
    return _manual_apply_patch(resolved, diff, dry_run)


def _manual_apply_patch(file_path: str, diff: str, dry_run: bool) -> dict:
    """Python 实现的 unified diff 应用。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            original = f.read()
    except Exception as e:
        return {"error": f"读取文件失败: {e}"}

    lines = diff.split("\n")
    hunks: list[dict] = []
    current_hunk: Optional[dict] = None

    for line in lines:
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            m = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                current_hunk = {
                    "old_start": int(m.group(1)),
                    "old_count": int(m.group(2)) if m.group(2) else 1,
                    "new_start": int(m.group(3)),
                    "new_count": int(m.group(4)) if m.group(4) else 1,
                    "lines": [],
                }
        elif current_hunk is not None:
            if line.startswith("+"):
                current_hunk["lines"].append(("add", line[1:]))
            elif line.startswith("-"):
                current_hunk["lines"].append(("del", line[1:]))
            elif line.startswith(" "):
                current_hunk["lines"].append(("keep", line[1:]))
            elif line.startswith("\\"):
                pass  # 忽略 \ No newline
            else:
                current_hunk["lines"].append(("keep", line))

    if current_hunk:
        hunks.append(current_hunk)

    if not hunks:
        return {"error": "未解析到有效的 hunk", "diff_preview": diff[:200]}

    # 逐 hunk 应用(带边界检查)
    original_lines = original.split("\n")
    result_lines = list(original_lines)
    max_line = len(result_lines)

    for hunk in hunks:
        old_start = max(0, hunk["old_start"] - 1)
        new_start = max(0, hunk["new_start"] - 1)
        old_count = min(hunk["old_count"], max_line - old_start)
        hunk_lines = hunk["lines"]
        applied = []
        i = old_start
        j = 0
        while j < len(hunk_lines):
            op, val = hunk_lines[j]
            if op == "keep":
                if i < len(result_lines) and result_lines[i] != val:
                    return {"error": f"上下文不匹配 at line {i+1}: 期望 {val!r},实际 {result_lines[i]!r}"}
                applied.append(result_lines[i] if i < len(result_lines) else val)
                i += 1
                j += 1
            elif op == "del":
                if i >= len(result_lines) or result_lines[i] != val:
                    return {"error": f"删除行不匹配 at line {i+1}"}
                i += 1
                j += 1
            elif op == "add":
                applied.append(val)
                j += 1
        # 替换该 hunk 区间(带边界保护)
        end_idx = min(old_start + old_count, len(result_lines))
        result_lines[old_start:end_idx] = applied

    if dry_run:
        return {
            "status": "dry_run",
            "original_lines": len(original_lines),
            "new_lines": len(result_lines),
            "preview": "\n".join(result_lines[:100]),
            "note": "dry_run 模式,未写入文件",
        }

    new_content = "\n".join(result_lines)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    return {
        "status": "ok",
        "original_lines": len(original_lines),
        "new_lines": len(result_lines),
        "hunks_applied": len(hunks),
        "note": "补丁应用成功",
    }


@tool("check_syntax", "对代码文件做语法检查(支持 Python/JS/TS/Go/Rust 等)", timeout=15)
def check_syntax(
    path: str = Field(description="要检查的文件路径"),
    language: str = Field("", description="语言别名,留空自动检测(根据扩展名)"),
) -> dict:
    """运行对应语言的语法检查工具,返回错误列表。"""
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return {"error": f"文件不存在: {path}"}

    ext_map = {
        ".py": ("python", ["python", "-m", "py_compile", path]),
        ".js": ("javascript", ["node", "--check", path]),
        ".ts": ("typescript", ["tsc", "--noEmit", path]),
        ".go": ("go", ["go", "vet", path]),
        ".rs": ("rust", ["rustc", "--crate-type", "lib", "--emit=metadata", "-o", "/dev/null", path]),
    }

    lang = language or ext_map.get(os.path.splitext(resolved)[1], "").strip()
    if not lang:
        return {"error": "无法自动检测语言,请指定 language 参数"}

    cmd = ext_map.get(f".{lang}", (lang, [lang, "--syntax-only", path]))[1]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=os.path.dirname(resolved) or None)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        errors = []
        for line in output.split("\n"):
            line = line.strip()
            if line and ("error" in line.lower() or line.startswith(path) or ":", "Error" in line):
                errors.append(line)
        return {
            "path": path,
            "language": lang,
            "status": "ok" if result.returncode == 0 else "errors",
            "exit_code": result.returncode,
            "errors": errors[:20],
            "output": output[:2000],
        }
    except FileNotFoundError:
        return {"error": f"{lang} 编译器/linter 未安装(尝试: {' '.join(cmd[:3])})"}
    except subprocess.TimeoutExpired:
        return {"error": f"{lang} 语法检查超时(>30s)"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"语法检查失败: {e}"}


@tool("count_lines", "统计文件的行数(用于评估改动范围)", timeout=5)
def count_lines(
    path: str = Field(description="文件路径"),
    glob: str = Field("", description="可选,通配符路径(如 src/**/*.py),返回每个文件的行数"),
) -> dict:
    """统计行数。"""
    if glob:
        import glob as glob_mod

        files = glob_mod.glob(_resolve_path(glob))
        rows = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    lines = sum(1 for _ in fh)
                rows.append({"path": f, "lines": lines})
            except Exception:  # noqa: BLE001
                pass
        return {"files": rows, "total_lines": sum(r["lines"] for r in rows)}
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return {"error": f"文件不存在: {path}"}
    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
        lines = sum(1 for _ in f)
    return {"path": path, "absolute_path": resolved, "lines": lines}


@tool("find_functions", "扫描文件中定义的函数/方法列表", timeout=10)
def find_functions(
    path: str = Field(description="文件路径"),
    language: str = Field("", description="语言(留空自动检测)"),
) -> dict:
    """提取文件中的函数/方法定义。"""
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return {"error": f"文件不存在: {path}"}
    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return {"error": f"读取失败: {e}"}

    lang = language or (".py" if resolved.endswith(".py") else ".js")
    patterns = {
        ".py": [
            (r"^def\s+(\w+)\s*\(", "function"),
            (r"^class\s+(\w+)", "class"),
        ],
        ".js": [
            (r"(?:function|const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function|\(", "function"),
            (r"(\w+)\s*\(.*?\)\s*\{", "method"),
        ],
    }
    funcs = []
    for pattern, kind in patterns.get(lang, patterns[".py"]):
        for m in re.finditer(pattern, content, re.MULTILINE):
            funcs.append({"name": m.group(1), "kind": kind, "line": content[:m.start()].count("\n") + 1})
    return {
        "path": path,
        "total_lines": content.count("\n") + 1,
        "functions": funcs[:50],
        "count": len(funcs),
    }
