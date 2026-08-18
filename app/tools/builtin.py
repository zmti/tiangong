"""内置工具集:时间、日期计算、数学计算、联网搜索(含免费回退)。"""
from __future__ import annotations

import datetime
import json
import re
import urllib.parse
import urllib.request

from pydantic import Field

from app.config import settings
from app.tools.registry import tool


@tool("get_current_time", "获取当前日期和时间", timeout=5)
def get_current_time(tz: str = Field("local", description="时区,默认 local 本机时间")) -> str:
    if tz == "local":
        now = datetime.datetime.now()
    else:
        try:
            import zoneinfo

            now = datetime.datetime.now(zoneinfo.ZoneInfo(tz))
        except Exception:  # noqa: BLE001
            now = datetime.datetime.now()
    return {"datetime": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": now.strftime("%A")}


@tool("calculate", "执行数学表达式计算", timeout=5)
def calculate(expression: str = Field(description="数学表达式,如 (3+5)*2")) -> str:
    expr = expression.strip().replace(" ", "")
    if not re.fullmatch(r"[\d+\-*/().%\s]+", expr):
        return {"error": "表达式包含非法字符"}
    try:
        result = eval(expr, {"__builtins__": {}}, {})  # noqa: S307
        return {"expression": expression, "result": result}
    except Exception as e:  # noqa: BLE001
        return {"error": f"计算失败: {e}"}


@tool("days_between", "计算两个日期之间的天数差", timeout=5)
def days_between(start: str = Field(description="开始日期,格式 YYYY-MM-DD"),
                 end: str = Field(description="结束日期,格式 YYYY-MM-DD")) -> str:
    try:
        d1 = datetime.date.fromisoformat(start)
        d2 = datetime.date.fromisoformat(end)
        return {"days": (d2 - d1).days}
    except ValueError as e:
        return {"error": f"日期格式错误: {e}"}


def _search_enabled() -> bool:
    """搜索是否需要配置 key。SerpAPI 必须 key;Bing 也需 key。无 key 时降级到免费 DuckDuckGo 搜索。"""
    return True  # 始终可用(付费 API 优先,无 key 自动回退 DuckDuckGo)


def _duckduckgo_search(query: str, num: int = 5) -> list[dict]:
    """免费 DuckDuckGo HTML 搜索回退(无需 API key)。"""
    results = []
    try:
        url = (
            "https://html.duckduckgo.com/html/"
            f"?q={urllib.parse.quote(query)}&max_results={num}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # 解析结果:每段 .results_links 包含 a.rel 链接
        for m in re.finditer(
            r'<a[^>]*rel="[^"]*web[^"]*"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</span>',
            html, re.DOTALL | re.IGNORECASE
        ):
            url_found, title_raw, snippet_raw = m.group(1), m.group(2), m.group(3)
            title = re.sub(r"<[^>]+>", "", title_raw).strip()
            snippet = re.sub(r"<[^>]+>", "", snippet_raw).strip()
            if title and url_found:
                results.append({
                    "title": title[:200],
                    "url": url_found.split("?")[0],
                    "snippet": snippet[:300],
                })
                if len(results) >= num:
                    break

        # 备用解析:简单提取所有链接块
        if not results:
            for block in re.finditer(
                r'< class="result"[^>]*>(.*?)</div>\s*<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.DOTALL | re.IGNORECASE
            ):
                snippet_html = block.group(1)
                link = block.group(2)
                title = re.sub(r"<[^>]+>", "", block.group(3)).strip()
                snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()[:300]
                if title and link:
                    results.append({"title": title[:200], "url": link, "snippet": snippet})
                    if len(results) >= num:
                        break

    except Exception as e:  # noqa: BLE001
        return []
    return results


@tool("web_search", "联网搜索网络信息,返回搜索结果列表(付费 API 优先,无 key 自动回退免费搜索)", timeout=20)
def web_search(query: str = Field(description="搜索关键词"), num: int = Field(5, description="返回结果数")) -> str:
    """联网搜索:优先使用配置的付费 API(SerpAPI/Bing),无配置时自动回退 DuckDuckGo 免费搜索。"""
    api_key = settings.search_api_key

    # 有 API key:走付费搜索
    if api_key:
        try:
            if settings.search_provider == "bing":
                url = (
                    "https://api.bing.microsoft.com/v7.0/search"
                    f"?q={urllib.parse.quote(query)}&count={num}"
                )
                req = urllib.request.Request(url, headers={
                    "Ocp-Apim-Subscription-Key": api_key,
                    "User-Agent": "Mozilla/5.0",
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                results = []
                for item in data.get("webPages", {}).get("value", [])[:num]:
                    results.append({
                        "title": item.get("name", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("snippet", ""),
                    })
                if not results:
                    return {"query": query, "results": [], "note": "未解析到结果"}
                return {"query": query, "results": results}
            # SerpAPI
            url = (
                "https://serpapi.com/search.json"
                f"?q={urllib.parse.quote(query)}"
                f"&num={num}&engine=google&api_key={urllib.parse.quote(api_key)}"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            results = []
            for item in data.get("organic_results", [])[:num]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                })
            if not results:
                return {"query": query, "results": [], "note": "未解析到结果"}
            return {"query": query, "results": results}
        except Exception as e:  # noqa: BLE001
            # 付费搜索失败,回退免费搜索
            results = _duckduckgo_search(query, num)
            if not results:
                return {"error": f"付费搜索失败({e}),免费搜索也无结果"}
            return {"query": query, "results": results, "note": "使用免费搜索回退"}

    # 无 API key:直接用免费 DuckDuckGo 搜索
    results = _duckduckgo_search(query, num)
    if not results:
        return {"error": "DuckDuckGo 搜索失败,请检查网络连接或配置 SEARCH_API_KEY 使用付费搜索"}
    return {"query": query, "results": results, "note": "使用免费搜索(DuckDuckGo)"}
