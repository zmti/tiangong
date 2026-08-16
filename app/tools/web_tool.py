"""网页抓取工具:抓取网页内容、提取正文、解析结构化数据。"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request

from pydantic import Field

from app.tools.registry import tool


def _clean_text(text: str) -> str:
    """清理文本:去除多余空白、脚本/样式内容。"""
    # 移除 script/style 标签内容
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 移除 HTML 注释
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 移除多余空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _html_to_text(html: str) -> str:
    """简单 HTML 转文本(不依赖外部库)。"""
    text = _clean_text(html)
    # 移除所有 HTML 标签
    text = re.sub(r"<[^>]+>", "\n", text)
    # 解码常见 HTML 实体
    entities = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&apos;": "'", "&nbsp;": " ",
        "&#39;": "'", "&mdash;": "—", "&ndash;": "–",
        "&hellip;": "…", "&copy;": "©", "&reg;": "®",
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    # 清理多余空行
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text.strip()


def _extract_meta(html: str) -> dict:
    """提取 HTML meta 信息。"""
    meta = {}
    # title
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        meta["title"] = m.group(1).strip()
    # description
    m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
    if not m:
        m = re.search(r'<meta[^>]*content=["\'](.*?)["\'][^>]*name=["\']description["\']', html, re.IGNORECASE)
    if m:
        meta["description"] = m.group(1).strip()
    # keywords
    m = re.search(r'<meta[^>]*name=["\']keywords["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
    if m:
        meta["keywords"] = m.group(1).strip()
    return meta


def _extract_links(html: str, base_url: str) -> list[dict]:
    """提取页面中的链接。"""
    links = []
    seen = set()
    for m in re.finditer(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE):
        href, text = m.group(1), m.group(2)
        # 清理链接文本
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text or len(text) > 200:
            continue
        # 处理相对路径
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            parsed = urllib.parse.urlparse(base_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        elif not href.startswith(("http://", "https://", "mailto:", "tel:", "javascript:")):
            continue
        # 跳过锚点和不可用链接
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        href = href.split("#")[0]  # 移除锚点
        if href not in seen:
            seen.add(href)
            links.append({"text": text[:100], "url": href})
    return links[:50]  # 限制数量


@tool("fetch_webpage", "抓取网页内容,返回标题、正文摘要、链接列表", timeout=20)
def fetch_webpage(
    url: str,
    max_chars: int = 5000,
    extract_links: bool = True,
) -> dict:
    """抓取网页并提取关键信息。"""
    # URL 校验
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": f"不支持的协议: {parsed.scheme},仅支持 http/https"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            # 获取实际编码
            content_type = resp.headers.get("Content-Type", "")
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].strip()
            html = resp.read().decode(charset, errors="ignore")
    except Exception as e:
        return {"error": f"抓取失败: {e}"}

    # 提取信息
    meta = _extract_meta(html)
    body_text = _html_to_text(html)

    result = {
        "url": url,
        "status": "ok",
        "length": len(html),
    }

    if meta.get("title"):
        result["title"] = meta["title"]
    if meta.get("description"):
        result["description"] = meta["description"]

    # 截取正文
    if len(body_text) > max_chars:
        body_text = body_text[:max_chars] + "\n...[内容过长已截断]"
    result["content"] = body_text

    # 提取链接
    if extract_links:
        links = _extract_links(html, url)
        result["links"] = links
        result["link_count"] = len(links)

    return result


@tool("extract_text", "从 HTML 中提取纯文本", timeout=10)
def extract_text(
    html: str,
    max_chars: int = 5000,
) -> dict:
    """将 HTML 转换为纯文本。"""
    text = _html_to_text(html)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[内容过长已截断]"
    return {"text": text, "length": len(text)}
