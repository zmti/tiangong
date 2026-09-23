"""实时搜索与联网能力:新闻、数据、实时信息获取。

集成多种搜索源:
1. 付费搜索:Serper/SerpAPI/Bing/Google(需 API Key)
2. 免费搜索:DuckDuckGo HTML 回退(无需 Key)
3. 实时新闻:RSS/新闻 API
4. 网页内容抓取:增强版 fetch_webpage + 结构化提取
"""
from __future__ import annotations

import asyncio
import base64
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
    "duckduckgo": {"base": None, "key_env": None},  # 免费回退(海外)
    "baidu": {"base": None, "key_env": None},       # 免费(国内)
    "sogou": {"base": None, "key_env": None},       # 免费(国内)
}


def get_search_config():
    """从环境变量获取搜索配置。"""
    from app.config import settings
    return {
        "api_key": settings.search_api_key or "",
        "provider": settings.search_provider or "serpapi",
    }


# ── 国内搜索引擎 ─────────────────────────────────────────────────
async def search_bing_cn(query: str, max_results: int = 10) -> list[dict]:
    """必应中文搜索(国内可用,无需 API Key)。

    Bing CN 返回的 URL 是重定向链接,需解码 u= 参数获取真实 URL。
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://cn.bing.com/search?q={encoded_query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:  # noqa: BLE001
        return [{"error": f"Bing 搜索失败: {e}"}]

    results = []
    # 提取 h2 结果块
    h2_blocks = re.findall(r'<h2[^>]*>(.*?)</h2>', html, re.DOTALL)
    # 提取 snippet(多种可能的 class)
    snippet_patterns = [
        r'class="b_lineclamp[^"]*">(.*?)</div>',
        r'class="b_lineclamp[^"]*">(.*?)</span>',
        r'class="linesnippet[^"]*">(.*?)</div>',
        r'class="b_caption[^"]*"[^>]*>(.*?)</p>',
    ]
    all_snippets = []
    for p in snippet_patterns:
        matches = re.findall(p, html, re.DOTALL)
        if matches:
            all_snippets.extend(matches)
            break
    snippets = all_snippets[:len(h2_blocks)]

    for i, block in enumerate(h2_blocks[:max_results]):
        # 提取标题(去 HTML 标签,处理 <strong> 等嵌套)
        # 先找 <a> 标签内容
        a_match = re.search(r'<a[^>]*>(.*?)</a>', block, re.DOTALL)
        if not a_match:
            continue
        raw_title = a_match.group(1)
        # 去除所有 HTML 标签获取纯文本标题
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        # 提取 URL(Bing 重定向格式,解码 u= 参数)
        url_match = re.search(r'href="([^"]+)"', block)
        real_url = ""
        if url_match:
            raw_url = url_match.group(1)
            # Bing 重定向: https://cn.bing.com/ck/a?...&u=base64url
            u_match = re.search(r'[?&]u=([A-Za-z0-9+/=]+)', raw_url)
            if u_match:
                try:
                    padded = u_match.group(1) + "=" * (4 - len(u_match.group(1)) % 4) if len(u_match.group(1)) % 4 else u_match.group(1)
                    decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
                    real_url = decoded
                except Exception:
                    real_url = raw_url
            else:
                real_url = raw_url
        snippet = snippets[i].strip() if i < len(snippets) else ""
        snippet = re.sub(r'<[^>]+>', '', snippet)[:300]
        if title and real_url:
            results.append({
                "title": title[:200],
                "url": real_url,
                "snippet": snippet,
            })

    return results or [{"error": "未找到搜索结果"}]


async def search_sogou(query: str, max_results: int = 10) -> list[dict]:
    """搜狗中文搜索(国内可用,无需 API Key)。"""
    encoded_query = urllib.parse.quote(query)
    url = f"https://www.sogou.com/web?query={encoded_query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:  # noqa: BLE001
        return [{"error": f"搜狗搜索失败: {e}"}]

    results = []
    # 搜狗结果在 .vr-wrap 容器中
    items = re.findall(r'class="vr-wrap[^"]*"[^>]*>(.*?)</div>\s*<div[^>]*class="f[zp]"', html, re.DOTALL)
    if not items:
        items = re.findall(r'class="main_result[^"]*"[^>]*>(.*?)</div>\s*<div[^>]*class="page"', html, re.DOTALL)
    for item in items[:max_results]:
        m_url = re.search(r'href="([^"]+)"', item)
        m_title = re.search(r'>([^<]{3,})</a>', item)
        m_desc = re.search(r'class="abstract[^"]*"[^>]*>([^<]*)', item)
        if m_url and m_title:
            results.append({
                "title": m_title.group(1).strip(),
                "url": m_url.group(1),
                "snippet": (m_desc.group(1) if m_desc else "")[:200],
            })
    return results or [{"error": "未找到搜索结果,搜狗可能限流"}]


# ── DuckDuckGo 免费搜索 ──────────────────────────────────────────
async def search_duckduckgo(query: str, max_results: int = 10) -> list[dict]:
    """DuckDuckGo HTML 搜索(无需 API Key)。

    注意:DDG 在中国大陆可能被墙,超时设置为 5s 以避免长时间等待。
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=5, headers=headers) as client:
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
    """实时搜索:付费 API > Bing CN(国内首选) > 搜狗(国内兜底) > DuckDuckGo(海外)。

    在中国大陆网络环境下,优先使用国内搜索引擎以避免 DDG 超时。
    """
    config = get_search_config()
    api_key = config["api_key"]
    provider = config["provider"]

    # 1. 付费 API(需配置 API Key)
    if use_paid and api_key:
        if provider == "serpapi":
            return await search_serpapi(query, api_key, max_results)
        if provider == "serper":
            return await search_serper(query, api_key, max_results)
        if provider == "bing":
            return await search_bing(query, api_key, max_results)

    # 2. Bing CN 搜索(国内首选,1-2秒返回)
    try:
        results = await search_bing_cn(query, max_results)
        if results and "error" not in results[0]:
            return results
    except Exception:  # noqa: BLE001
        pass

    # 3. 搜狗(国内兜底,2-3秒返回)
    try:
        results = await search_sogou(query, max_results)
        if results and "error" not in results[0]:
            return results
    except Exception:  # noqa: BLE001
        pass

    # 4. DuckDuckGo(海外,可能被墙,5秒超时)
    try:
        results = await search_duckduckgo(query, max_results)
        if results and "error" not in results[0]:
            return results
    except Exception:  # noqa: BLE001
        pass

    # 全部失败
    return [{"error": "所有搜索引擎均不可用,请检查网络或配置付费 API Key"}]


# ── 工具注册 ──────────────────────────────────────────────────────
@tool("web_search", "实时网络搜索,获取最新资讯和数据(支持百度/搜狗/DDG 多源回退)", timeout=30)
async def web_search(
    query: str = Field(description="搜索关键词"),
    max_results: int = Field(10, ge=1, le=20, description="最多返回结果数"),
    use_paid: bool = Field(False, description="优先使用付费搜索(需配置 API Key)"),
) -> dict:
    """执行实时网络搜索,自动在国内/国外搜索引擎间回退。"""
    try:
        results = await search_realtime(query, max_results, use_paid)
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
async def news_search(
    topic: str = Field(description="新闻主题/关键词"),
    max_results: int = Field(5, ge=1, le=15, description="最多返回结果数"),
) -> dict:
    """新闻搜索(复用通用搜索,附加新闻筛选)。"""
    query = f"{topic} 新闻"
    try:
        results = await search_realtime(query, max_results)
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
    # fetch_webpage 是 @tool 装饰后的 Tool 实例;Tool 现已实现 __call__(转发到
    # self.func),因此可以直接调用。历史上这里拿 Tool 当函数用,报
    # "'Tool' object is not callable",本工具 100% 失败。
    from app.tools.web_tool import fetch_webpage
    return fetch_webpage(url=url, max_chars=max_chars, extract_links=extract_links)


@tool("read_url", "读取网页文本内容(不抓链接)", timeout=15)
def read_url(
    url: str = Field(description="目标网页 URL"),
    max_chars: int = Field(5000, description="最大返回字符数"),
) -> dict:
    """轻量版网页读取。"""
    from app.tools.web_tool import fetch_webpage
    return fetch_webpage(url=url, max_chars=max_chars, extract_links=False)
