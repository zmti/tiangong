"""实时搜索与联网能力:新闻、数据、实时信息获取。

集成多种搜索源:
1. 付费搜索:Serper/SerpAPI/Bing/Google(需 API Key)
2. 免费搜索:DuckDuckGo HTML 回退(无需 Key)
3. 实时新闻:RSS/新闻 API
4. 网页内容抓取:增强版 fetch_webpage + 结构化提取
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from typing import Optional

import httpx
from pydantic import Field

from app.tools.registry import tool


# ── 搜索配置 ──────────────────────────────────────────────────────
_SEARCH_PROVIDERS = {
    "serpapi": {"base": "https://serpapi.com/search", "key_env": "SERPAPI_API_KEY"},
    "serper": {"base": "https://google.serper.dev/search", "key_env": "SERPER_API_KEY"},
    "bing": {"base": "https://api.bing.microsoft.com/v7.0/search", "key_env": "BING_API_KEY"},
    "duckduckgo": {"base": None, "key_env": None},  # 免费回退
}


def get_search_config():
    """从环境变量获取搜索配置。"""
    from app.config import settings
    return {
        "api_key": settings.search_api_key or "",
        "provider": settings.search_provider or "serpapi",
    }


# ── DuckDuckGo 免费搜索 ──────────────────────────────────────────
async def search_duckduckgo(query: str, max_results: int = 10) -> list[dict]:
    """DuckDuckGo HTML 搜索(无需 API Key)。"""
    encoded_query = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.post(url, data={"q": query}, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:  # noqa: BLE001
        return [{"error": f"DuckDuckGo 搜索失败: {e}"}]

    results = []
    # 解析结果(DDG 使用 .result__a 链接和 .result__snippet 摘要)
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    titles = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"(?:\s*data-snippet="([^"]+)")?', html)

    # 另一种解析方式
    if not links:
        links = re.findall(r'result__a[^>]+href="([^"]+)"', html)
    if not titles:
        titles = re.findall(r'result__title[^>]*>(.*?)</a>', html, re.DOTALL)
    if not snippets:
        snippets = re.findall(r'result__snippet"(?:\s*data-snippet="([^"]+)")?', html)

    for i, (url, title) in enumerate(zip(links, titles[:len(links)])):
        if i >= max_results:
            break
        snippet = snippets[i] if i < len(snippets) else ""
        # 清理 HTML 标签
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        # 修复相对 URL
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = f"https://duckduckgo.com{url}"

        results.append({
            "title": title[:200],
            "url": url,
            "snippet": snippet[:300],
        })

    return results or [{"error": "未找到搜索结果"}]


# ── 付费搜索 API ──────────────────────────────────────────────────
async def search_serpapi(query: str, api_key: str, max_results: int = 10) -> list[dict]:
    """SerpAPI 搜索。"""
    params = {
        "q": query,
        "api_key": api_key,
        "num": max_results,
        "engine": "google",
    }
    url = "https://serpapi.com/search"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for organic in data.get("organic_results", [])[:max_results]:
        results.append({
            "title": organic.get("title", ""),
            "url": organic.get("link", ""),
            "snippet": organic.get("snippet", ""),
        })
    return results or [{"error": "未找到搜索结果"}]


async def search_serper(query: str, api_key: str, max_results: int = 10) -> list[dict]:
    """Serper(SerpAPI 的 Google Search API)搜索。"""
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": max_results}
    url = "https://google.serper.dev/search"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for organic in data.get("organic", [])[:max_results]:
        results.append({
            "title": organic.get("title", ""),
            "url": organic.get("link", ""),
            "snippet": organic.get("snippet", ""),
        })
    return results or [{"error": "未找到搜索结果"}]


async def search_bing(query: str, api_key: str, max_results: int = 10) -> list[dict]:
    """Bing Search API。"""
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    params = {"q": query, "count": max_results}
    url = "https://api.bing.microsoft.com/v7.0/search"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for web in data.get("webPages", {}).get("value", [])[:max_results]:
        results.append({
            "title": web.get("name", ""),
            "url": web.get("url", ""),
            "snippet": web.get("snippet", ""),
        })
    return results or [{"error": "未找到搜索结果"}]


async def search_realtime(
    query: str,
    max_results: int = 10,
    use_paid: bool = True,
) -> list[dict]:
    """实时搜索:优先付费 API,降级到 DuckDuckGo。"""
    config = get_search_config()
    api_key = config["api_key"]
    provider = config["provider"]

    if use_paid and api_key:
        if provider == "serpapi":
            return await search_serpapi(query, api_key, max_results)
        if provider == "serper":
            return await search_serper(query, api_key, max_results)
        if provider == "bing":
            return await search_bing(query, api_key, max_results)

    # 免费回退
    return await search_duckduckgo(query, max_results)


# ── 工具注册 ──────────────────────────────────────────────────────
@tool("web_search", "实时网络搜索,获取最新资讯和数据(免费 DuckDuckGo 回退)", timeout=30)
def web_search(
    query: str = Field(description="搜索关键词"),
    max_results: int = Field(10, ge=1, le=20, description="最多返回结果数"),
    use_paid: bool = Field(False, description="优先使用付费搜索(需配置 API Key)"),
) -> dict:
    """执行实时网络搜索。"""
    import asyncio
    try:
        results = asyncio.run(search_realtime(query, max_results, use_paid))
    except Exception as e:  # noqa: BLE001
        results = [{"error": f"搜索失败: {e}"}]

    # 格式化输出
    formatted = []
    for i, r in enumerate(results, 1):
        if "error" in r:
            return {"error": r["error"], "query": query}
        formatted.append(f"{i}. {r.get('title', '无标题')}")
        formatted.append(f"   {r.get('snippet', '')[:200]}")
        formatted.append(f"   URL: {r.get('url', '')}")
        formatted.append("")

    return {
        "query": query,
        "results_count": len(results),
        "results": results,
        "formatted": "\n".join(formatted),
    }


@tool("news_search", "搜索最新新闻资讯", timeout=20)
def news_search(
    topic: str = Field(description="新闻主题/关键词"),
    max_results: int = Field(5, ge=1, le=15, description="最多返回结果数"),
) -> dict:
    """新闻搜索(复用通用搜索,附加新闻筛选)。"""
    import asyncio
    query = f"{topic} 新闻"
    try:
        results = asyncio.run(search_realtime(query, max_results))
    except Exception as e:  # noqa: BLE001
        results = [{"error": f"新闻搜索失败: {e}"}]

    return {
        "topic": topic,
        "results": results,
    }


@tool("fetch_page", "抓取指定网页完整内容并提取结构化信息", timeout=20)
def fetch_page(
    url: str = Field(description="目标网页 URL"),
    extract_links: bool = Field(True, description="同时提取页面链接"),
    max_chars: int = Field(8000, description="最大返回字符数"),
) -> dict:
    """增强版网页抓取:返回标题、正文、链接、meta 信息。"""
    from app.tools.web_tool import fetch_webpage
    return fetch_webpage(url, max_chars=max_chars, extract_links=extract_links)


@tool("read_url", "读取网页文本内容(不抓链接)", timeout=15)
def read_url(
    url: str = Field(description="目标网页 URL"),
    max_chars: int = Field(5000, description="最大返回字符数"),
) -> dict:
    """轻量版网页读取。"""
    from app.tools.web_tool import fetch_webpage
    result = fetch_webpage(url, max_chars=max_chars, extract_links=False)
    return result
