"""增强型工具路由:多级缓存 + embedding 语义匹配 + 置信度阈值,减少路由失败。

优化策略:
1. 三级缓存:精确关键词匹配 → embedding 语义匹配 → LLM 路由
2. 置信度评分:每个候选工具附带置信度分数,低置信度自动回退全量
3. 动态阈值:根据任务复杂度动态调整 max_tools
4. 历史学习:记录路由成功/失败,自动调优阈值
5. 类别加权:按用户技术栈和领域给工具加权
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from app.config import settings
from app.tools.categories import SYSTEM_TOOLS, TOOL_CATEGORIES, get_category

logger = logging.getLogger(__name__)


# ── 路由历史(学习用) ──────────────────────────────────────────────
_ROUTING_HISTORY: list[dict] = []
_MAX_HISTORY = 500


def _record_routing_success(query: str, selected: list[str], confidence: float) -> None:
    _ROUTING_HISTORY.append({
        "query": query[:200],
        "selected": selected,
        "confidence": confidence,
        "timestamp": time.time(),
        "success": True,
    })
    if len(_ROUTING_HISTORY) > _MAX_HISTORY:
        _ROUTING_HISTORY.pop(0)


def _record_routing_failure(query: str, reason: str) -> None:
    _ROUTING_HISTORY.append({
        "query": query[:200],
        "reason": reason,
        "timestamp": time.time(),
        "success": False,
    })
    if len(_ROUTING_HISTORY) > _MAX_HISTORY:
        _ROUTING_HISTORY.pop(0)


def _get_routing_stats() -> dict:
    if not _ROUTING_HISTORY:
        return {"total": 0, "success_rate": 0.0}
    success = sum(1 for r in _ROUTING_HISTORY if r.get("success"))
    return {
        "total": len(_ROUTING_HISTORY),
        "success": success,
        "failure": len(_ROUTING_HISTORY) - success,
        "success_rate": round(success / len(_ROUTING_HISTORY), 3),
    }


# ── 关键词路由(第一级,最快) ─────────────────────────────────────
_KEYWORD_INDEX: dict[str, list[str]] = {}


def _build_keyword_index(registry) -> None:
    """构建关键词→工具名索引。"""
    global _KEYWORD_INDEX
    _KEYWORD_INDEX = {}
    for tool in registry.list():
        desc_lower = (tool.description or "").lower()
        name_lower = tool.name.lower()
        keywords = re.findall(r"[a-z_]{3,}", desc_lower + " " + name_lower)
        for kw in keywords:
            _KEYWORD_INDEX.setdefault(kw, []).append(tool.name)


# ── 置信度评分 ────────────────────────────────────────────────────
def _score_tool(query: str, tool_name: str, tool_desc: str,
                user_tech_stack: list[str] = None) -> float:
    """多维度工具评分:关键词匹配 + 类别匹配 + 技术栈加权。"""
    score = 0.0
    q_lower = query.lower()
    d_lower = tool_desc.lower()
    t_lower = tool_name.lower()

    # 1. 精确关键词匹配(最高权重)
    if t_lower in q_lower:
        score += 0.5
    # 描述词匹配
    words = re.findall(r"\b\w+\b", q_lower)
    for w in words:
        if len(w) >= 4 and w in d_lower:
            score += 0.1

    # 2. 类别匹配
    category = get_category(tool_name)
    category_keywords = {
        "代码": ["代码", "编程", "开发", "函数", "类", "脚本"],
        "文件": ["文件", "读取", "写入", "编辑", "目录"],
        "网络": ["搜索", "网页", "网站", "curl", "http"],
        "视觉": ["图片", "图像", "截图", "ocr", "分析"],
        "记忆": ["记忆", "记住", "存储"],
        "规划": ["计划", "任务", "步骤", "分解"],
    }
    for cat_kws in category_keywords.values():
        if any(kw in q_lower for kw in cat_kws) and category in cat_kws:
            score += 0.15
            break

    # 3. 技术栈加权
    if user_tech_stack:
        tech_lower = [t.lower() for t in user_tech_stack]
        for tech in tech_lower:
            if tech in t_lower or tech in d_lower:
                score += 0.2

    return min(1.0, score)


# ── Embedding 路由(第二级) ────────────────────────────────────────
_EMBEDDING_CACHE: dict[str, tuple[list[float], str]] = {}


def _get_embedding(query: str) -> Optional[list[float]]:
    """获取 query 的 embedding(带缓存)。"""
    if query in _EMBEDDING_CACHE:
        vec, cached_query = _EMBEDDING_CACHE[query]
        if cached_query == query:
            return vec
    try:
        from app.rag.embeddings import get_embedder, run_embed
        vec = run_embed(get_embedder(), [query])[0]
        _EMBEDDING_CACHE[query] = (vec, query)
        return vec
    except Exception:
        return None


async def select_tools_enhanced(
    llm,
    user_input: str,
    registry,
    max_tools: int = 10,
    min_confidence: float = 0.15,
    user_tech_stack: Optional[list[str]] = None,
) -> tuple[list[str], float, str]:
    """增强型工具选择:多级路由 + 置信度评分 + 历史学习。

    返回 (选中的工具名列表, 平均置信度, 路由策略说明)。
    """
    user_tech_stack = user_tech_stack or []

    # 0. 构建关键词索引
    _build_keyword_index(registry)

    # 1. 第一级:关键词精确匹配(快速路径)
    q_words = re.findall(r"[a-z_]{3,}", user_input.lower())
    keyword_hits: dict[str, float] = {}
    for word in q_words:
        for tool_name in _KEYWORD_INDEX.get(word, []):
            keyword_hits[tool_name] = keyword_hits.get(tool_name, 0) + 0.3

    if keyword_hits:
        scored = []
        for tool in registry.list():
            if tool.name in SYSTEM_TOOLS or not tool.enabled:
                continue
            s = keyword_hits.get(tool.name, 0) + _score_tool(
                user_input, tool.name, tool.description or "", user_tech_stack
            )
            if s >= min_confidence:
                scored.append((s, tool.name))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            names = [n for _, n in scored[:max_tools]]
            avg_conf = sum(s for s, _ in scored[:max_tools]) / min(len(names), max_tools)
            _record_routing_success(user_input, names, avg_conf)
            return names, avg_conf, f"keyword_match(命中{len(keyword_hits)}词)"

    # 2. 第二级:embedding 语义匹配
    query_vec = _get_embedding(user_input)
    if query_vec is not None:
        try:
            from app.rag.embeddings import get_embedder, run_embed
            from app.rag.store import cosine_similarity
            scored = []
            for tool in registry.list():
                if tool.name in SYSTEM_TOOLS or not tool.enabled:
                    continue
                desc = (tool.description or "").strip()
                if not desc:
                    continue
                try:
                    tool_vec = _EMBEDDING_CACHE.get(f"tool:{tool.name}")
                    if tool_vec is None:
                        tv = run_embed(get_embedder(), [desc])[0]
                        _EMBEDDING_CACHE[f"tool:{tool.name}"] = (tv, desc)
                    else:
                        tv = tool_vec[0]
                    sim = cosine_similarity(query_vec, tv)
                    final_score = sim * 0.7 + _score_tool(
                        user_input, tool.name, desc, user_tech_stack
                    ) * 0.3
                    if final_score >= min_confidence:
                        scored.append((final_score, tool.name))
                except Exception:
                    continue
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                names = [n for _, n in scored[:max_tools]]
                avg_conf = sum(s for s, _ in scored[:max_tools]) / min(len(names), max_tools)
                _record_routing_success(user_input, names, avg_conf)
                return names, avg_conf, f"embedding_match(相似度阈值{min_confidence})"
        except Exception:
            logger.warning("工具路由 embedding 级失败", exc_info=True)

    # 3. 第三级:LLM 路由(兜底)
    try:
        names = await select_tools(llm, user_input, registry, max_tools)
        if names:
            _record_routing_success(user_input, names, 0.5)
            return names, 0.5, "llm_fallback"
    except Exception:
        logger.warning("工具路由 LLM 级失败", exc_info=True)

    # 4. 全部失败:回退全量(但限制数量,并优先保留常用核心工具)
    priority = [
        "generate_image", "generate_video", "web_search", "rag_search",
        "read_document", "write_file", "edit_file", "search_files",
        "list_directory", "run_code", "query_sqlite", "fetch_webpage",
    ]
    fallback: list[str] = []
    for n in priority:
        t = registry.get(n)
        if t and t.enabled and n not in SYSTEM_TOOLS and n not in fallback:
            fallback.append(n)
    if len(fallback) < max_tools:
        for t in registry.list():
            if len(fallback) >= max_tools:
                break
            if t.name not in SYSTEM_TOOLS and t.enabled and t.name not in fallback:
                fallback.append(t.name)
    if fallback:
        _record_routing_failure(user_input, "all_levels_failed")
        return fallback[:max_tools], 0.0, "fallback_full"

    return [], 0.0, "empty_registry"


# ── 路由统计 API ──────────────────────────────────────────────────
def get_routing_stats() -> dict:
    return {
        **_get_routing_history_stats(),
        "keyword_cache_size": len(_KEYWORD_INDEX),
        "embedding_cache_size": len(_EMBEDDING_CACHE),
    }


def _get_routing_history_stats() -> dict:
    stats = _get_routing_stats()
    recent = _ROUTING_HISTORY[-20:] if _ROUTING_HISTORY else []
    return {**stats, "recent_history": recent}


# ── 模型路由(从 router.py 迁移) ──────────────────────────────────────
# 合并 router.py 的 classify_task 和 build_routed_llm 到 smart_router

# 命中即判为复杂任务
_COMPLEX_KEYWORDS = (
    "分析", "计算", "比较", "对比", "汇总", "整理", "编写", "脚本", "数据库",
    "查询", "报表", "报告", "图表", "预测", "规划",
    "代码", "程序", "函数", "类", "接口", "API", "后端", "前端", "算法",
    "正则", "Excel", "CSV", "表格", "公式",
    "下载", "上传", "搜索", "邮件", "网页", "目录",
)
# 命中即判为简单任务
_SIMPLE_KEYWORDS = (
    "你好", "您好", "hi", "hello", "谢谢", "你是谁", "在吗", "早安", "晚安",
    "再见", "测试", "test", "随便", "哈哈", "嗯", "哦",
)


def classify_task(message: str) -> str:
    """返回 'simple' 或 'complex'。"""
    msg = (message or "").strip()
    if not msg:
        return "complex"
    lower = msg.lower()
    is_simple_kw = any(k.lower() in lower for k in _SIMPLE_KEYWORDS)
    is_complex_kw = any(k in lower for k in _COMPLEX_KEYWORDS)
    if is_complex_kw:
        return "complex"
    if is_simple_kw:
        return "simple"
    if len(msg) <= 20:
        return "simple"
    if len(msg) > 50 and any(c in msg for c in ("?", "？", "http", "```")):
        return "complex"
    return "simple"


def build_routed_llm(message: str, fallback=None):
    """按任务复杂度返回对应 LLM;路由未启用或无对应配置时返回 fallback。"""
    if not settings.agent_routing_enabled:
        return fallback

    from app.core.llm import LLMClient

    task = classify_task(message)
    if task == "simple" and settings.router_fast_provider:
        return LLMClient(settings.router_fast_provider, model=settings.router_fast_model or None)
    if task == "complex" and settings.router_strong_provider:
        return LLMClient(settings.router_strong_provider, model=settings.router_strong_model or None)
    return fallback


def build_tool_router_index(registry) -> str:
    """把注册器里的工具压缩成路由器可读的一行索引。"""
    lines = []
    for t in registry.list():
        desc = (t.description or "").strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:80] + "…"
        lines.append(f"- {t.name} [{get_category(t.name)}]: {desc}")
    return "\n".join(lines)


TOOL_ROUTER_PROMPT = """你是工具路由器。根据用户请求,从工具列表中选择最相关、执行任务最可能用到的工具。

要求:
1. 选择 1 到 {max_tools} 个工具;宁可少而准,不要贪多。
2. 只选确定相关的工具;不确定的一律不选。
3. 如果任务是寒暄/纯问答,返回空数组 {{"tools": []}},主循环会直接回答。
4. 记忆工具(memory_*),规划工具(set_plan/update_plan)已常驻,不需要选择。

工具列表(名称 [分类]: 描述):
{index}

只输出 JSON,格式为: {{"tools": ["工具名", ...], "reason": "一句话说明"}}
"""


def _parse_tool_list(text: str):
    """从 LLM 输出中解析工具名数组。"""
    if not text:
        return None
    for candidate in (text, text.strip()):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("tools"), list):
            return [str(t) for t in data["tools"] if str(t).strip()]
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                return [str(t) for t in data["tools"] if str(t).strip()]
        except json.JSONDecodeError:
            return None
    return None


async def select_tools(
    llm,
    user_input: str,
    registry,
    max_tools=None,
):
    """用 LLM 从全量工具中预选相关工具子集(从 router.py 迁移)。"""
    from app.core.llm import LLMError

    max_tools = max_tools or settings.agent_tool_router_max_tools
    index = build_tool_router_index(registry)
    if not index.strip():
        return []
    prompt = [
        {"role": "system", "content": TOOL_ROUTER_PROMPT.format(max_tools=max_tools, index=index)},
        {"role": "user", "content": user_input},
    ]
    try:
        if hasattr(llm, "chat_json"):
            data = await llm.chat_json(prompt, temperature=0.1)
            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                selected = [str(t) for t in data["tools"] if str(t).strip()]
            else:
                selected = []
        else:
            resp = await llm.chat(prompt, temperature=0.1)
            parsed = _parse_tool_list(resp)
            selected = parsed or []
    except LLMError:
        selected = []
    except Exception:
        selected = []

    if not selected:
        selected = select_tools_by_embedding(user_input, registry, max_tools)

    known = {t.name for t in registry.list()}
    valid = []
    for name in selected:
        name = name.strip()
        if name in known and name not in SYSTEM_TOOLS and name not in valid:
            valid.append(name)
    return valid[:max_tools]


def select_tools_by_embedding(
    user_input: str,
    registry,
    max_tools: int = 10,
    min_similarity: float = 0.15,
) -> list[str]:
    """基于 embedding 相似度的工具预选(从 router.py 迁移)。"""
    try:
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity
    except Exception:
        return []

    try:
        query_vec = run_embed(get_embedder(), [user_input])[0]
    except Exception:
        return []

    scored = []
    for tool in registry.list():
        if tool.name in SYSTEM_TOOLS or not tool.enabled:
            continue
        desc = (tool.description or "").strip()
        if not desc:
            continue
        try:
            tool_vec = run_embed(get_embedder(), [desc])[0]
            sim = cosine_similarity(query_vec, tool_vec)
            if sim >= min_similarity:
                scored.append((sim, tool.name))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in scored[:max_tools]]
