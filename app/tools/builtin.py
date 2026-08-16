"""内置工具集:时间、日期计算、数学计算、联网搜索。"""
from __future__ import annotations

import datetime
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
    """搜索是否需要配置 key。SerpAPI 必须 key;Bing 也需 key。无 key 禁用。"""
    return bool(settings.search_api_key)


@tool("web_search", "联网搜索网络信息,返回搜索结果列表(需在 .env 配置 SEARCH_API_KEY)", timeout=20, enabled=_search_enabled())
def web_search(query: str = Field(description="搜索关键词"), num: int = Field(5, description="返回结果数")) -> str:
    """使用 SerpAPI(需 key);生产环境可替换为官方搜索 API。"""
    api_key = settings.search_api_key
    try:
        if settings.search_provider == "bing":
            import json

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
            import json

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
        return {"error": f"搜索失败: {e}"}
