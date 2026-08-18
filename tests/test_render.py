"""渲染引擎测试。"""
import pytest

from app.core.render import (
    RenderEngine,
    detect_content_type,
    render_markdown_to_html,
    highlight_code,
    render_mermaid_diagram,
    _escape_inline,
    get_render_engine,
)


def test_detect_markdown():
    assert detect_content_type("# Hello\n\n- Item 1") == "markdown"
    assert detect_content_type("```python\nprint('hi')\n```") == "markdown"


def test_detect_html():
    assert detect_content_type("<div>Hello</div>") == "html"
    assert detect_content_type("<table><tr><td>X</td></tr></table>") == "html"
    assert detect_content_type("<!DOCTYPE html><html>") == "html"


def test_detect_mermaid():
    assert detect_content_type("graph TD\n    A-->B") == "mermaid"
    assert detect_content_type("```mermaid\nflowchart LR\n````") == "mermaid"
    assert detect_content_type("sequenceDiagram\n    A->>B") == "mermaid"
    assert detect_content_type("classDiagram\n    class A") == "mermaid"


def test_detect_fallback():
    assert detect_content_type("plain text") == "markdown"
    assert detect_content_type("") == "markdown"


def test_render_markdown_basic():
    result = render_markdown_to_html("# Hello\n\nWorld")
    assert "<h1>" in result
    assert "Hello" in result
    assert "World" in result


def test_render_markdown_code_block():
    result = render_markdown_to_html("```python\nprint('hello')\n```")
    assert "<pre" in result
    assert "print" in result


def test_render_markdown_list():
    result = render_markdown_to_html("- Item 1\n- Item 2")
    assert "<ul>" in result
    assert "<li>Item 1</li>" in result
    assert "<li>Item 2</li>" in result


def test_escape_inline_escapes_html():
    # _escape_inline 是安全转义，<b> 应被转义为 &lt;b&gt;
    result = _escape_inline("<b>bold</b>")
    assert "&lt;b&gt;" in result
    assert "bold" in result


def test_escape_inline_preserves_markdown():
    result = _escape_inline("**bold**")
    assert "<strong>bold</strong>" in result


def test_highlight_code_simple():
    result = highlight_code("x = 1", "python")
    assert "x" in result
    assert "=" in result


def test_render_mermaid_fallback():
    result = render_mermaid_diagram("graph TD\nA-->B")
    assert "mermaid" in result.lower()


def test_render_engine_caching():
    engine = RenderEngine()
    content = "# Hello World"

    r1 = engine.render(content)
    r2 = engine.render(content)

    assert r1.cache_key == r2.cache_key
    assert r1.content == r2.content


def test_render_engine_clear_cache():
    engine = RenderEngine()
    engine.render("# Test")
    count = engine.clear_cache()
    assert count >= 1


def test_render_engine_stats():
    engine = RenderEngine()
    stats = engine.get_cache_stats()
    assert "size" in stats
    assert "max_size" in stats
    assert "utilization_pct" in stats


def test_render_html_sanitize():
    engine = RenderEngine()
    # 包含 script 标签的 HTML 应被清洗
    content = '<div>Hello</div><script>alert(1)</script>'
    result = engine.render(content, content_type="html")
    assert "script" not in result.content.lower() or result.content_type == "markdown"


def test_render_engine_content_types():
    engine = RenderEngine()

    md_result = engine.render("# Title")
    assert md_result.content_type == "markdown"

    # 明确指定 html 类型
    html_result = engine.render("<p>Test</p>", content_type="html")
    assert html_result.content_type == "html"

    mermaid_result = engine.render("graph TD\nA-->B")
    assert mermaid_result.content_type == "mermaid"


def test_singleton():
    e1 = get_render_engine()
    e2 = get_render_engine()
    assert e1 is e2
