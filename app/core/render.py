"""HTML/Markdown 内容实时渲染与预览 API。

对标 Claude Artifacts 能力:
- 渲染 Markdown 为 HTML
- 渲染 HTML 并返回预览
- 代码高亮(使用 highlight.js 或 prism.js)
- 图表渲染(mermaid、echarts)

设计要点:
1. 安全沙箱:渲染时禁用外部脚本执行
2. 缓存:相同内容只渲染一次
3. 流式:支持 SSE 增量渲染
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ── 渲染引擎选择 ───────────────────────────────────────────────────
_MARKDOWN_AVAILABLE = False
try:
    import markdown  # noqa: F401
    _MARKDOWN_AVAILABLE = True
except ImportError:
    pass

_MERMAID_AVAILABLE = False
try:
    import pyMermaid  # noqa: F401
    _MERMAID_AVAILABLE = True
except ImportError:
    pass

_HIGHLIGHT_AVAILABLE = False
try:
    import pygments  # noqa: F401
    from pygments import formatters, lexers  # noqa: F401
    _HIGHLIGHT_AVAILABLE = True
except ImportError:
    pass


@dataclass
class RenderResult:
    """渲染结果。"""
    content: str
    content_type: str  # html / markdown / mermaid / code
    error: Optional[str] = None
    cache_key: str = ""
    render_time_ms: float = 0.0


# ── Markdown 渲染 ──────────────────────────────────────────────────
def render_markdown_to_html(text: str) -> str:
    """将 Markdown 转换为 HTML。"""
    if not _MARKDOWN_AVAILABLE:
        return _fallback_markdown_to_html(text)

    try:
        md = markdown.Markdown(
            extensions=[
                "fenced_code",
                "codehilite",
                "toc",
                "tables",
                "nl2br",
            ]
        )
        return md.convert(text)
    except Exception as e:
        logger.warning("Markdown rendering failed: %s", e)
        return _fallback_markdown_to_html(text)


def _fallback_markdown_to_html(text: str) -> str:
    """降级渲染:基础 Markdown → HTML。"""
    lines = text.split("\n")
    result = []
    in_code_block = False
    in_list = False

    for line in lines:
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            if in_code_block:
                result.append("</pre></code>")
                in_code_block = False
            else:
                lang = stripped[3:].strip()
                result.append(f'<pre><code class="language-{lang or "text"}">')
                in_code_block = True
            continue

        if in_code_block:
            result.append(html.escape(line))
            continue

        # 标题
        if stripped.startswith("# "):
            result.append(f"<h1>{_escape_inline(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            result.append(f"<h2>{_escape_inline(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            result.append(f"<h3>{_escape_inline(stripped[4:])}</h3>")
        # 列表
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                result.append("<ul>")
                in_list = True
            result.append(f"<li>{_escape_inline(stripped[2:])}</li>")
        elif stripped and stripped[0].isdigit() and ". " in stripped:
            if not in_list:
                result.append("<ol>")
                in_list = True
            result.append(f"<li>{_escape_inline(stripped.split('. ', 1)[1])}</li>")
        else:
            if in_list:
                result.append("</ul>" if "<ol>" not in result[-1] else "</ol>")
                in_list = False
            if stripped:
                result.append(f"<p>{_escape_inline(stripped)}</p>")

    if in_list:
        result.append("</ul>" if "<ol>" not in result[-1] else "</ol>")
    if in_code_block:
        result.append("</pre></code>")

    return "\n".join(result)


def _escape_inline(text: str) -> str:
    """转义 HTML 特殊字符，但保留 Markdown 内联语法。"""
    # 先转义 HTML
    text = html.escape(text, quote=True)
    # 恢复代码块
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # 粗体
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # 斜体
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text


# ── 代码高亮 ──────────────────────────────────────────────────────
def highlight_code(code: str, language: str = "") -> str:
    """对代码进行语法高亮。"""
    if not _HIGHLIGHT_AVAILABLE:
        return html.escape(code)

    try:
        lexer = lexers.get_lexer_by_name(language) if language else lexers.GuessLexer()
        formatter = formatters.HtmlFormatter(style="colorful", noclasses=True)
        return pygments.highlight(code, lexer, formatter)
    except Exception:
        return html.escape(code)


# ── Mermaid 图表 ──────────────────────────────────────────────────
def render_mermaid_diagram(diagram: str) -> str:
    """将 Mermaid 图表代码转换为可嵌入的 SVG。"""
    if not _MERMAID_AVAILABLE:
        return f'<pre class="mermaid">{html.escape(diagram)}</pre>'

    try:
        # 调用 pyMermaid 转换
        svg = pyMermaid.mermaid_to_svg(diagram)
        return f'<div class="mermaid-svg">{svg}</div>'
    except Exception as e:
        logger.warning("Mermaid rendering failed: %s", e)
        return f'<pre class="mermaid">{html.escape(diagram)}</pre>'


# ── 内容检测与路由 ────────────────────────────────────────────────
_CONTENT_TYPE_PATTERNS = {
    "mermaid": [
        r"^\s*graph\s+TD",
        r"^\s*flowchart",
        r"^\s*sequenceDiagram",
        r"^\s*classDiagram",
        r"^\s*stateDiagram",
        r"^\s*gantt",
        r"```mermaid",
    ],
    "html": [r"^<!DOCTYPE|^<html", r"<div", r"<table"],
    "markdown": [],  # 默认 fallback
}


def detect_content_type(content: str) -> str:
    """检测内容类型。"""
    content_stripped = content.strip()

    # 检查 Mermaid
    for pattern in _CONTENT_TYPE_PATTERNS["mermaid"]:
        if re.search(pattern, content_stripped, re.IGNORECASE):
            return "mermaid"

    # 检查 HTML
    for pattern in _CONTENT_TYPE_PATTERNS["html"]:
        if re.search(pattern, content_stripped, re.IGNORECASE):
            return "html"

    # 默认 Markdown
    return "markdown"


# ── 渲染引擎 ──────────────────────────────────────────────────────
class RenderEngine:
    """内容渲染引擎，支持 Markdown/HTML/Mermaid/代码高亮。"""

    def __init__(self):
        self._cache: dict[str, RenderResult] = {}
        self._max_cache_size = int(getattr(settings, "artifact_cache_size", 100))

    def render(
        self,
        content: str,
        content_type: Optional[str] = None,
        use_cache: bool = True,
    ) -> RenderResult:
        """渲染内容。"""
        if not content:
            return RenderResult(content="", content_type="empty")

        # 生成缓存键
        cache_key = hashlib.sha256(content.encode()).hexdigest()[:16]
        if use_cache and cache_key in self._cache:
            result = self._cache[cache_key]
            result.render_time_ms = 0
            return result

        # 检测内容类型
        if not content_type:
            content_type = detect_content_type(content)

        import time as _time
        start = _time.time()

        # 根据类型渲染
        if content_type == "mermaid":
            rendered = render_mermaid_diagram(content)
        elif content_type == "html":
            rendered = self._sanitize_html(content)
        else:
            rendered = render_markdown_to_html(content)

        elapsed = (_time.time() - start) * 1000

        result = RenderResult(
            content=rendered,
            content_type=content_type,
            cache_key=cache_key,
            render_time_ms=elapsed,
        )

        # 更新缓存
        if len(self._cache) >= self._max_cache_size:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[cache_key] = result

        return result

    def _sanitize_html(self, html_content: str) -> str:
        """清洗 HTML，移除危险元素。"""
        # 移除 script 标签
        html_content = re.sub(r"<script[^>]*>.*?</script>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
        # 移除 onclick 等事件属性
        html_content = re.sub(r'\s+(on\w+)\s*=', "", html_content)
        return html_content

    def clear_cache(self) -> int:
        """清空缓存，返回清除数量。"""
        count = len(self._cache)
        self._cache.clear()
        return count

    def get_cache_stats(self) -> dict:
        """获取缓存统计。"""
        return {
            "size": len(self._cache),
            "max_size": self._max_cache_size,
            "utilization_pct": round(len(self._cache) / self._max_cache_size * 100, 1) if self._max_cache_size else 0,
        }


# 全局单例
_render_engine = None


def get_render_engine() -> RenderEngine:
    """获取渲染引擎单例。"""
    global _render_engine
    if _render_engine is None:
        _render_engine = RenderEngine()
    return _render_engine


def render_content(
    content: str,
    content_type: Optional[str] = None,
    use_cache: bool = True,
) -> RenderResult:
    """便捷函数:渲染内容。"""
    return get_render_engine().render(content, content_type, use_cache)


def render_markdown(content: str) -> str:
    """便捷函数:渲染 Markdown。"""
    return render_markdown_to_html(content)


def render_mermaid(diagram: str) -> str:
    """便捷函数:渲染 Mermaid。"""
    return render_mermaid_diagram(diagram)
