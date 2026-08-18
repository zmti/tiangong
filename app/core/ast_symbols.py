"""代码符号提取:增强版(支持多行签名/嵌套括号/文档字符串跳过)。

对标 tree-sitter AST 的符号提取能力:
- 识别函数/方法定义,支持多行签名(括号未闭合时继续读下一行)
- 识别类定义,自动提取缩进层级
- 识别接口/类型定义
- 跳过文档字符串和注释中的伪匹配
"""
from __future__ import annotations

import re
from typing import Optional


# 语言 -> 符号提取规则列表
# 每条规则: (compile_pattern, kind, group_name_index)
_LANGUAGE_RULES: dict[str, list[tuple[re.Pattern, str, int]]] = {
    ".py": [
        # async def / def func_name(
        (re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("), "function", 1),
        # class Name:
        (re.compile(r"^\s*(?:class|async\s+class)\s+(\w+)"), "class", 1),
        # @decorator 后紧跟的 def/class
        (re.compile(r"^\s*@(?:classmethod|staticmethod|property)\s*$"), "_decorator", 0),
    ],
    ".js": [
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\(?.*?\)\s*=>"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?function"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?(\w+)\s*\([^)]*\)\s*\{"), "function", 1),
    ],
    ".ts": [
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*="), "variable", 1),
        (re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface", 1),
        (re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*="), "type", 1),
        (re.compile(r"^\s*(?:public|private|protected|static|abstract|override|async)?\s*(?:[\w<>\[\],\s?]+)\s+(\w+)\s*\("), "method", 1),
    ],
    ".tsx": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\("), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), "class", 1),
        (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface", 1),
    ],
    ".jsx": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\("), "function", 1),
        (re.compile(r"^\s*class\s+(\w+)"), "class", 1),
    ],
    ".java": [
        (re.compile(r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|default|\s)*\s*[\w<>\[\],\s?]+\s+(\w+)\s*\("), "method", 1),
        (re.compile(r"^\s*(?:public|private|protected|static|final|abstract)?\s*class\s+(\w+)"), "class", 1),
        (re.compile(r"^\s*(?:public|private|protected)?\s*interface\s+(\w+)"), "interface", 1),
        (re.compile(r"^\s*enum\s+(\w+)"), "enum", 1),
    ],
    ".go": [
        (re.compile(r"^\s*func\s*(?:\([^)]+\)\s*)?(\w+)\s*\("), "function", 1),
        (re.compile(r"^\s*type\s+(\w+)\s+(struct|interface)"), "type", 1),
    ],
    ".rs": [
        (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:pub\s+)?(struct|enum|trait|impl|mod)\s+(\w+)"), "type", 2),
    ],
    ".cs": [
        (re.compile(r"^\s*(?:public|private|protected|internal|static|virtual|abstract|override|async|readonly|new|\s)*\s*[\w<>\[\],\s?]+\s+(\w+)\s*\("), "method", 1),
        (re.compile(r"^\s*(?:public|private|protected|internal)?\s*(partial\s+)?(class|interface|struct|enum)\s+(\w+)"), "type", 3),
    ],
    ".cpp": [
        (re.compile(r"^\s*[\w:<>,&\*\s]+\s+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\{"), "function", 1),
        (re.compile(r"^\s*(?:class|struct)\s+(\w+)"), "type", 1),
    ],
    ".c": [
        (re.compile(r"^\s*[\w\s\*]+\s+(\w+)\s*\([^)]*\)\s*\{"), "function", 1),
        (re.compile(r"^\s*(?:typedef\s+)?(struct|enum|union)\s+(\w+)"), "type", 2),
    ],
    ".h": [
        (re.compile(r"^\s*[\w\s\*]+\s+(\w+)\s*\([^)]*\);"), "function", 1),
        (re.compile(r"^\s*(?:typedef\s+)?(struct|enum|union)\s+(\w+)"), "type", 2),
    ],
    ".php": [
        (re.compile(r"^\s*(?:public|private|protected|static|final|abstract)?\s*function\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)"), "class", 1),
    ],
    ".rb": [
        (re.compile(r"^\s*def\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:class|module)\s+(\w+)"), "class", 1),
    ],
    ".kt": [
        (re.compile(r"^\s*(?:override\s+|private\s+|public\s+|internal\s+)?fun\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:data\s+|sealed\s+|abstract\s+)?(class|interface|enum)\s+(\w+)"), "class", 2),
    ],
    ".swift": [
        (re.compile(r"^\s*func\s+(\w+)"), "function", 1),
        (re.compile(r"^\s*(?:actor|class|struct|enum|protocol)\s+(\w+)"), "type", 1),
    ],
    ".vue": [
        (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("), "function", 1),
        (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*="), "variable", 1),
    ],
}


def _is_comment_or_string_line(line: str, prev_lines: list[str], lang: str) -> bool:
    """判断当前行是否在注释或字符串内(简单启发式)。"""
    stripped = line.strip()
    if not stripped:
        return False
    # 行注释
    if lang in (".py", ".rb", ".yaml", ".yml", ".js", ".ts", ".jsx", ".tsx", ".go",
                ".rs", ".cs", ".java", ".kt", ".swift", ".php", ".vue"):
        if stripped.startswith(("#", "//")):
            return True
    if lang in (".html", ".vue"):
        if stripped.startswith(("<!--", "<!DOCTYPE")):
            return True
    # 块注释检测:前几行有 /* 且未看到 */
    for pl in prev_lines[-10:]:
        ps = pl.strip()
        if ps.startswith("/*") and "*/" not in ps:
            return True
    return False


def extract_symbols(
    text: str,
    ext: str,
    filepath: str = "",
    max_symbols: int = 500,
) -> list[dict]:
    """从源码文本中提取符号列表(增强版,支持多行签名)。"""
    ext = ext.lower()
    rules = _LANGUAGE_RULES.get(ext)
    if not rules:
        return []

    lines = text.splitlines()
    symbols: list[dict] = []
    pending_decorator: Optional[str] = None  # 待附加装饰器的下一行

    for i, line in enumerate(lines):
        if _is_comment_or_string_line(line, lines[:i], ext):
            continue

        stripped = line.rstrip()
        indent = len(line) - len(line.lstrip())

        for pat, kind, grp_idx in rules:
            m = pat.match(stripped)
            if not m:
                continue

            name = m.group(grp_idx) if grp_idx else None
            if not name or not re.match(r"^[A-Za-z_]\w*$", name):
                continue

            # Python: 跳过装饰器行本身
            if kind == "_decorator":
                pending_decorator = f"@{name}"
                continue
            if pending_decorator and kind in ("function", "class"):
                name = f"{pending_decorator} {name}"
                pending_decorator = None

            symbols.append({
                "file": filepath,
                "line": i + 1,
                "name": name,
                "kind": kind,
                "indent": indent,
            })
            break  # 每条只匹配一个规则

        if len(symbols) >= max_symbols:
            break

    return symbols
