"""Artifacts 实时渲染 API。

对标 Claude Artifacts:
- /v1/artifacts/render: 渲染 Markdown/HTML/代码
- /v1/artifacts/cache: 缓存管理
- SSE 支持: 流式渲染

安全:
- 自动清理危险 HTML(如 script 标签)
- 沙箱渲染模式
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.chat import _auth_required  # 复用认证

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])
logger = logging.getLogger(__name__)


@router.post("/render")
async def render_artifact(
    content: str = Query(..., description="待渲染内容"),
    content_type: str = Query("auto", description="类型: auto/markdown/html/code/mermaid"),
    use_cache: bool = Query(True, description="是否使用缓存"),
) -> dict:
    """渲染内容并返回 HTML。

    支持类型:
    - auto: 自动检测(Markdown/HTML/Mermaid/代码)
    - markdown: Markdown → HTML
    - html: 清洗后直接返回
    - code: 代码高亮
    - mermaid: Mermaid 图表
    """
    from app.core.render import render_content, detect_content_type

    # 自动检测类型
    actual_type = content_type
    if content_type == "auto":
        actual_type = detect_content_type(content)

    try:
        result = render_content(content, content_type=actual_type, use_cache=use_cache)
    except Exception as e:
        logger.error("Render error: %s", e)
        raise HTTPException(status_code=500, detail=f"渲染失败: {e}")

    return {
        "content": result.content,
        "content_type": result.content_type,
        "cache_key": result.cache_key,
        "render_time_ms": round(result.render_time_ms, 2),
    }


@router.post("/render/stream")
async def render_artifact_stream(
    content: str = Query(..., description="待渲染内容"),
    content_type: str = Query("auto", description="类型"),
):
    """流式渲染，支持 SSE。"""
    from app.core.render import render_content, detect_content_type

    actual_type = content_type
    if content_type == "auto":
        actual_type = detect_content_type(content)

    async def event_generator():
        start = time.time()
        try:
            result = render_content(content, content_type=actual_type)
            elapsed = (time.time() - start) * 1000
            yield f"data: {json.dumps({'type': 'done', 'content_type': result.content_type, 'render_time_ms': round(elapsed, 2)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/cache/stats")
async def cache_stats() -> dict:
    """渲染缓存统计。"""
    from app.core.render import get_render_engine
    engine = get_render_engine()
    return engine.get_cache_stats()


@router.post("/cache/clear")
async def cache_clear() -> dict:
    """清空渲染缓存。"""
    from app.core.render import get_render_engine
    engine = get_render_engine()
    cleared = engine.clear_cache()
    return {"cleared": cleared}


@router.get("/preview")
async def preview_artifact(
    content: str = Query(..., description="内容"),
    title: str = Query("", description="标题(可选)"),
    height: int = Query(400, description="预览高度(px)"),
):
    """生成完整 HTML 预览页面。"""
    from app.core.render import render_content, detect_content_type

    actual_type = detect_content_type(content)
    result = render_content(content, content_type=actual_type)

    theme_css = _get_theme_css("light")
    preview_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{_escape_html(title or "Artifact Preview")}</title>
<style>
{theme_css}
.artifact-container {{ max-width: 100%; overflow: auto; }}
</style>
</head>
<body>
<div class="artifact-container" style="height: {height}px;">
{result.content}
</div>
</body>
</html>"""

    return {
        "html": preview_html,
        "content_type": result.content_type,
        "height": height,
    }


def _get_theme_css(theme: str) -> str:
    """返回主题 CSS。"""
    themes = {
        "light": """
body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #fff; color: #333; }
pre { background: #f6f8fa; padding: 16px; overflow-x: auto; border-radius: 6px; }
code { font-family: 'SF Mono', Monaco, monospace; font-size: 14px; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; }
th { background: #f6f8fa; }
blockquote { border-left: 4px solid #0969da; margin: 0; padding: 8px 16px; background: #f6f8fa; }
img { max-width: 100%; height: auto; }
""",
        "dark": """
body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; }
pre { background: #161b22; padding: 16px; overflow-x: auto; border-radius: 6px; }
code { font-family: 'SF Mono', Monaco, monospace; font-size: 14px; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { border: 1px solid #30363d; padding: 8px 12px; text-align: left; }
th { background: #161b22; }
blockquote { border-left: 4px solid #1f6feb; margin: 0; padding: 8px 16px; background: #161b22; }
img { max-width: 100%; height: auto; }
""",
    }
    return themes.get(theme, themes["light"])


def _escape_html(text: str) -> str:
    """转义 HTML 特殊字符。"""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
