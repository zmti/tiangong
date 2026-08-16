"""代码库理解工具:内容搜索、符号大纲、项目结构、带行号上下文、语义检索。

对标 Claude Code 的代码库能力:
- code_search      —— grep 式全文搜索(带上下文行)
- code_symbols     —— 提取函数/类/方法定义(按语言正则)
- project_tree     —— 目录树 + 文件行数统计
- code_context     —— 读取指定文件的行号区间(带行号)
- code_semantic_search —— 基于嵌入的近似语义搜索(本地哈希,零依赖)
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from pydantic import Field

from app.tools.registry import tool

CODE_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    "dist", "build", "build_app", "build_pyinstaller", "site-packages",
    "bin", "obj", ".next", ".nuxt", ".cache", ".pytest_cache", ".mypy_cache",
    ".tox", ".gradle", "target", "out", "coverage",
}
CODE_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".pyc", ".pyo",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map",
    ".mp3", ".mp4", ".wav", ".ogg", ".mov", ".avi",
    ".lock", ".min.js",
}
CODE_TEXT_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".cc",
    ".hpp", ".cs", ".go", ".rs", ".php", ".rb", ".kt", ".kts", ".swift",
    ".sql", ".sh", ".bat", ".ps1", ".html", ".css", ".scss", ".less", ".vue",
    ".svelte", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".xml", ".dockerfile", "dockerfile", ".makefile", "makefile",
    ".gradle", ".properties", ".env", ".editorconfig", ".gitignore",
}
MAX_FILE_BYTES = 1_000_000


def _walk_code(path: str):
    """遍历代码文件,产出 (相对路径, 绝对路径)。"""
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in CODE_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in CODE_SKIP_EXT or fn.endswith(".min.js"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(full, path)
            yield rel, full


def _read_text(full: str) -> str | None:
    try:
        with open(full, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


@tool(
    "code_search",
    "在代码库中全文搜索关键词或正则(默认忽略大小写),返回 文件:行号 匹配及上下文行。"
    "用于定位函数/变量的定义与调用位置、查找报错字符串",
    timeout=40,
)
def code_search(
    query: str = Field(description="搜索关键词或正则表达式"),
    path: str = Field("", description="代码库根目录,留空用当前工作目录"),
    regex: bool = Field(False, description="True 时把 query 当正则表达式"),
    context_lines: int = Field(1, description="匹配行前后附带的上下文行数,0 为不带"),
    max_results: int = Field(50, description="最大匹配数"),
) -> str:
    root = os.path.abspath(os.path.expanduser(path)) if path else os.getcwd()
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    q = (query or "").strip()
    if not q:
        return {"path": root, "error": "查询不能为空"}
    try:
        pat = re.compile(q, re.IGNORECASE) if regex else re.compile(re.escape(q), re.IGNORECASE)
    except re.error as e:
        return {"path": root, "error": f"正则无效: {e}"}
    context_lines = max(0, min(context_lines, 5))
    results: list[dict] = []
    for rel, full in _walk_code(root):
        text = _read_text(full)
        if text is None:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if pat.search(line):
                ctx = []
                if context_lines:
                    for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                        if j == i:
                            continue
                        ctx.append(f"{j + 1}: {lines[j]}")
                results.append({"file": rel, "line": i + 1, "text": line[:300], "context": ctx})
                if len(results) >= max_results:
                    return {"path": root, "query": query, "results": results, "truncated": True}
    return {"path": root, "query": query, "results": results, "total": len(results)}


# 语言 -> (文件后缀列表, 正则列表(name_group, kind))
_SYMBOL_PATTERNS: dict[tuple, list[tuple[str, str]]] = {}
_PATTERNS: dict[str, list[tuple[str, str]]] = {
    ".py": [
        (r"^\s*(async\s+)?def\s+(\w+)", "function"),
        (r"^\s*class\s+(\w+)", "class"),
    ],
    ".js": [
        (r"^\s*(async\s+)?function\s*\*?\s*(\w+)\s*\(", "function"),
        (r"^\s*const\s+(\w+)\s*=\s*(async\s*)?\(", "function"),
        (r"^\s*export\s+(default\s+)?(async\s+)?function\s*\*?\s*(\w+)\s*\(", "function"),
        (r"^\s*class\s+(\w+)", "class"),
    ],
    ".ts": [
        (r"^\s*(async\s+)?function\s*\*?\s*(\w+)\s*\(", "function"),
        (r"^\s*const\s+(\w+)\s*=\s*(async\s*)?\(", "function"),
        (r"^\s*(public|private|protected)?\s*(static\s+)?(async\s+)?(\w+)\s*\([^)]*\)\s*:", "method"),
        (r"^\s*(export\s+)?(abstract\s+)?class\s+(\w+)", "class"),
        (r"^\s*(export\s+)?interface\s+(\w+)", "interface"),
        (r"^\s*(export\s+)?type\s+(\w+)\s*=", "type"),
    ],
    ".tsx": [
        (r"^\s*(async\s+)?function\s*\*?\s*(\w+)\s*\(", "function"),
        (r"^\s*const\s+(\w+)\s*=\s*(async\s*)?\(", "function"),
        (r"^\s*export\s+(default\s+)?function\s+(\w+)", "function"),
        (r"^\s*(export\s+)?(abstract\s+)?class\s+(\w+)", "class"),
        (r"^\s*(export\s+)?interface\s+(\w+)", "interface"),
    ],
    ".jsx": [
        (r"^\s*(async\s+)?function\s*\*?\s*(\w+)\s*\(", "function"),
        (r"^\s*const\s+(\w+)\s*=\s*\(\s*\)?\s*=>", "function"),
        (r"^\s*(export\s+)?(default\s+)?function\s+(\w+)", "function"),
        (r"^\s*class\s+(\w+)", "class"),
    ],
    ".java": [
        (r"^\s*(public|private|protected|static|final|abstract|synchronized|native)?\s*[\w<>\[\],\s?]+\s+(\w+)\s*\(", "method"),
        (r"^\s*(public|private|protected|static|final|abstract)?\s*class\s+(\w+)", "class"),
        (r"^\s*(public|private|protected)?\s*interface\s+(\w+)", "interface"),
    ],
    ".cs": [
        (r"^\s*(public|private|protected|internal|static|virtual|abstract|override|async|readonly)?\s*[\w<>\[\],\s?]+\s+(\w+)\s*\(", "method"),
        (r"^\s*(public|private|protected|internal)?\s*(partial\s+)?(class|interface|struct|enum)\s+(\w+)", "type"),
    ],
    ".go": [
        (r"^\s*func\s*(\([^)]*\)\s*)?(\w+)\s*\(", "function"),
        (r"^\s*type\s+(\w+)\s+(struct|interface)", "type"),
    ],
    ".rs": [
        (r"^\s*(pub\s+)?(async\s+)?fn\s+(\w+)", "function"),
        (r"^\s*(pub\s+)?(struct|enum|trait|impl|mod)\s+(\w+)", "type"),
    ],
    ".php": [
        (r"^\s*(public|private|protected|static|final|abstract)?\s*function\s+(\w+)", "function"),
        (r"^\s*(abstract\s+|final\s+)?class\s+(\w+)", "class"),
    ],
    ".rb": [
        (r"^\s*def\s+(\w+)", "function"),
        (r"^\s*(class|module)\s+(\w+)", "class"),
    ],
    ".kt": [
        (r"^\s*(override\s+|private\s+|public\s+|internal\s+)?fun\s+(\w+)", "function"),
        (r"^\s*(data\s+|sealed\s+|abstract\s+)?(class|interface|enum)\s+(\w+)", "class"),
    ],
    ".c": [
        (r"^\s*[\w\s\*]+\s+(\w+)\s*\([^)]*\)\s*\{", "function"),
        (r"^\s*(typedef\s+)?(struct|enum|union)\s+(\w+)", "type"),
    ],
    ".h": [
        (r"^\s*[\w\s\*]+\s+(\w+)\s*\([^)]*\);", "function"),
        (r"^\s*(typedef\s+)?(struct|enum|union)\s+(\w+)", "type"),
    ],
    ".cpp": [
        (r"^\s*[\w:<>,&\*\s]+\s+(\w+)\s*\([^)]*\)\s*(const\s*)?\{", "function"),
        (r"^\s*(class|struct)\s+(\w+)", "type"),
    ],
}


@tool(
    "code_symbols",
    "提取代码库中的函数/类/接口/方法定义大纲(按语言规则),返回 文件:行号:名称:类型。"
    "用于快速了解项目结构和 API 表面",
    timeout=40,
)
def code_symbols(
    path: str = Field("", description="代码库根目录,留空用当前工作目录"),
    max_symbols: int = Field(300, description="最大返回符号数"),
) -> str:
    root = os.path.abspath(os.path.expanduser(path)) if path else os.getcwd()
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    symbols: list[dict] = []
    for rel, full in _walk_code(root):
        ext = os.path.splitext(full)[1].lower()
        patterns = _PATTERNS.get(ext)
        if not patterns:
            continue
        text = _read_text(full)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat, kind in patterns:
                m = re.match(pat, line)
                if m:
                    name = m.group(m.lastindex or 1)
                    symbols.append({"file": rel, "line": i, "name": name, "kind": kind})
                    break
            if len(symbols) >= max_symbols:
                return {"path": root, "symbols": symbols, "total": len(symbols), "truncated": True}
    return {"path": root, "symbols": symbols, "total": len(symbols)}


@tool(
    "project_tree",
    "输出项目目录树(含每个文件的代码行数,跳过 node_modules/.git/.venv 等),用于了解整体代码结构",
    timeout=30,
)
def project_tree(
    path: str = Field("", description="项目根目录,留空用当前工作目录"),
    depth: int = Field(3, description="目录递归深度,最大 6"),
    max_entries: int = Field(200, description="最大返回条目数"),
) -> str:
    root = os.path.abspath(os.path.expanduser(path)) if path else os.getcwd()
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    depth = max(1, min(depth, 6))
    entries: list[dict] = []

    def _count_lines(full: str) -> int:
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _walk(cur: str, level: int, prefix: str) -> None:
        if len(entries) >= max_entries:
            return
        try:
            names = sorted(os.listdir(cur))
        except OSError:
            return
        dirs = [n for n in names if os.path.isdir(os.path.join(cur, n)) and n not in CODE_SKIP_DIRS and not n.startswith(".")]
        files = [n for n in names if os.path.isfile(os.path.join(cur, n)) and os.path.splitext(n)[1].lower() not in CODE_SKIP_EXT]
        for d in dirs:
            if len(entries) >= max_entries:
                return
            entries.append({"path": os.path.relpath(os.path.join(cur, d), root).replace(os.sep, "/"), "type": "dir", "lines": 0})
            if level < depth:
                _walk(os.path.join(cur, d), level + 1, prefix + "  ")
        for fn in files:
            if len(entries) >= max_entries:
                return
            full = os.path.join(cur, fn)
            entries.append({"path": os.path.relpath(full, root).replace(os.sep, "/"), "type": "file", "lines": _count_lines(full)})

    _walk(root, 1, "")
    total_lines = sum(e["lines"] for e in entries if e["type"] == "file")
    lines = []
    for e in entries:
        indent = "  " * (e["path"].count("/"))
        if e["type"] == "dir":
            lines.append(f"{indent}{e['path'].split('/')[-1]}/")
        else:
            lines.append(f"{indent}{e['path'].split('/')[-1]}  ({e['lines']} 行)")
    return {
        "path": root,
        "tree": "\n".join(lines[:max_entries]),
        "files": sum(1 for e in entries if e["type"] == "file"),
        "dirs": sum(1 for e in entries if e["type"] == "dir"),
        "total_lines": total_lines,
        "truncated": len(entries) >= max_entries,
    }


@tool(
    "code_context",
    "读取代码文件中指定行号附近的内容(带行号),用于查看函数实现、局部上下文。行号来自 code_search/code_symbols",
    timeout=20,
)
def code_context(
    path: str = Field(description="要读取的代码文件完整路径"),
    line: int = Field(1, description="起始行号(1 开始)"),
    lines: int = Field(40, description="向下读取的行数,最大 200"),
) -> str:
    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(full):
        return {"path": full, "error": f"文件不存在: {full}"}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return {"path": full, "error": f"读取失败: {e}"}
    all_lines = content.splitlines()
    total = len(all_lines)
    start = max(1, line)
    lines = max(1, min(lines, 200))
    end = min(total, start + lines - 1)
    if start > total:
        return {"path": full, "line": start, "total_lines": total, "error": "超出文件行数"}
    body = "\n".join(f"{i + 1:>6} | {all_lines[i]}" for i in range(start - 1, end))
    return {"path": full, "start_line": start, "end_line": end, "total_lines": total, "content": body}


# ---- 语义搜索(嵌入缓存) ----
_semantic_cache: dict[str, tuple[str, list[float]]] = {}


@tool(
    "code_semantic_search",
    "语义检索代码库:按含义而非字面关键词查找相关代码(如查'处理登录逻辑'能找到 auth/login 相关函数)。"
    "本地嵌入,首次索引目录较慢,之后按文件变更增量更新",
    timeout=120,
)
def code_semantic_search(
    query: str = Field(description="自然语言描述想找的代码功能"),
    path: str = Field("", description="代码库根目录,留空用当前工作目录"),
    top_k: int = Field(8, description="返回条数"),
) -> str:
    from app.rag.embeddings import HashEmbedding
    from app.rag.store import cosine_similarity

    root = os.path.abspath(os.path.expanduser(path)) if path else os.getcwd()
    if not os.path.isdir(root):
        return {"path": root, "error": f"目录不存在: {root}"}
    q = (query or "").strip()
    if not q:
        return {"path": root, "error": "查询不能为空"}

    embedder = HashEmbedding()
    q_vec = embedder.embed(q)
    scored: list[dict] = []
    total = 0
    for rel, full in _walk_code(root):
        ext = os.path.splitext(full)[1].lower()
        if ext not in CODE_TEXT_EXT and not os.path.basename(full).lower() in ("dockerfile", "makefile"):
            continue
        total += 1
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        key = full
        cached = _semantic_cache.get(key)
        if cached is None or cached[0] != f"{mtime}":
            text = _read_text(full)
            if text is None:
                continue
            # 每文件取前后各 4000 字符(签名/头尾最能代表语义)
            sample = text[:4000]
            if len(text) > 4000:
                sample += "\n" + text[-2000:]
            vec = embedder.embed(sample)
            _semantic_cache[key] = (f"{mtime}", vec)
        else:
            vec = cached[1]
        score = cosine_similarity(q_vec, vec)
        if score > 0:
            scored.append({"file": rel, "score": round(score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {
        "path": root,
        "query": query,
        "results": scored[:max(1, min(top_k, 20))],
        "indexed_files": total,
        "note": "本地哈希嵌入,仅近似语义;精确查找请用 code_search",
    }
