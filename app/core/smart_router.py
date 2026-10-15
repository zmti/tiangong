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
import threading
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


# ── 通用词(停止词) ──────────────────────────────────────────────
# 这些词几乎出现在每个工具的描述里("根据描述生成…""分析当前…""读取文件内容…")。
# 一旦进入关键词索引,任何含这些词的日常问句都会被判为命中,进而把付费的图像/视频
# 生成工具塞进候选集 —— 实测"根据当前知识库,分析智能体"仅因"根据"二字命中
# generate_video 的描述就被路由进来,模型随即真的去生成视频/作画(大错容 + 真实消耗)。
# 这里在分词源头过滤掉它们;多字词(如"生成视频""读取文件")不受影响,精度反而更高。
_GENERIC_TOKENS = frozenset({
    "根据", "当前", "描述", "内容", "信息", "支持", "可以", "使用", "进行",
    "相关", "提供", "获取", "指定", "其他", "全部", "所有", "自动", "快速",
    "简单", "注意", "说明", "结果", "返回", "执行", "输入", "输出", "文本",
    "字符", "格式", "类型", "对象", "字段", "数据", "一个", "这个", "什么",
    "如何", "怎么", "需要", "能够", "用于", "通过", "以及", "或者", "并且",
    "时间", "状态", "名称", "参数", "功能", "工具", "系统", "默认", "例如",
    "生成", "分析", "处理", "查询", "显示", "添加", "删除", "修改", "设置",
    "创建", "读取", "写入", "编辑", "保存", "打开", "关闭", "运行", "启动",
})

# ── 付费/重资源媒体生成工具 ──────────────────────────────────────
# 这两类工具一旦被调用就要真金白银出图/出视频,且 security 层已不再强制确认
# (security.py: generate_image/generate_video 不再强制确认)。因此只有当请求里
# 出现明确的多模态生成意图时才允许进入候选集,否则一律剔除。
_MEDIA_TOOLS = frozenset({"generate_image", "generate_video"})

_MEDIA_INTENT_RE = re.compile(
    r"(图片|图像|图像|插图|配图|海报|封面|照片|画图|作画|绘图|画一|画张|画个|"
    r"视频|动画|短片|动图|gif|出图|生成图|"
    r"\bimage\b|\bpicture\b|\bphoto\b|\bdraw\b|\bpaint\b|\bposter\b|"
    r"\bvideo\b|\banimation\b|\bclip\b|\bmovie\b)",
    re.IGNORECASE,
)


def _has_media_intent(query: str) -> bool:
    """请求是否包含明确的多模态生成意图。"""
    return bool(query) and _MEDIA_INTENT_RE.search(query) is not None


def _filter_media_tools(query: str, names: list[str]) -> list[str]:
    """无明确图像/视频生成意图时,剔除付费媒体生成工具。"""
    if _has_media_intent(query):
        return names
    return [n for n in names if n not in _MEDIA_TOOLS]


def filter_paid_media_tools(query: str, names: list[str]) -> list[str]:
    """公开入口:无明确图像/视频生成意图时剔除付费媒体生成工具。

    供 agent 内核在"路由失败回退全量工具集"的场景下调用 —— 那种情况下
    _active_tools 为 None,模型会看到注册表里的全部工具,容易顺手调用
    generate_image/generate_video(真实扣费 + 长时间等待 + 答非所问)。
    """
    return _filter_media_tools(query, names)


# ── 关键词路由(第一级,最快) ─────────────────────────────────────
_KEYWORD_INDEX: dict[str, list[str]] = {}


async def _build_keyword_index(registry) -> None:
    """构建关键词→工具名索引(使用智能分词)。

    支持中英文混合:提取 ASCII 单词和中文词语(2-4字符)。
    """
    global _KEYWORD_INDEX
    _KEYWORD_INDEX = {}
    async for tool in registry.list():
        desc_lower = (tool.description or "").lower()
        name_lower = tool.name.lower()
        combined = desc_lower + " " + name_lower
        # 使用智能分词提取关键词
        keywords = _smart_tokenize(combined)
        for kw in keywords:
            _KEYWORD_INDEX.setdefault(kw, []).append(tool.name)


# ── 缓存:避免每次路由重建索引 ──────────────────────────────────
_keyword_index_lock = threading.Lock()
_keyword_index_key = None  # 上次构建时 registry 的工具数量


# ── 中文智能分词 ──────────────────────────────────────────────────
def _smart_tokenize(text: str) -> list[str]:
    """智能分词:支持中英文混合,提取有意义的关键词。

    策略:
    1. ASCII单词(3字符以上)直接提取
    2. 中文字符按2-4字符切分,避免过长短语导致匹配失败
    3. 额外提取常见动词(写/读/编辑/创建等),确保关键动作不丢失
    4. 合并去重,保留最长匹配
    """
    tokens: list[str] = []
    # 1. 提取ASCII单词
    ascii_words = re.findall(r"[a-z_]{3,}", text.lower())
    tokens.extend(ascii_words)
    # 2. 提取中文词语(按2-4字符切分)
    cn_chars = re.findall(r"[一-龥]+", text)
    for chunk in cn_chars:
        if len(chunk) >= 2:
            # 滑动窗口切分:优先保留4字符,其次3字符,最后2字符
            for size in (4, 3, 2):
                for i in range(0, len(chunk) - size + 1):
                    word = chunk[i: i + size]
                    if len(word) >= 2 and word not in tokens:
                        tokens.append(word)
    # 3. 额外提取常见动词(解决关键动词丢失问题)
    common_verbs = [
        "写", "读", "编辑", "创建", "删除", "搜索", "运行", "执行",
        "分析", "生成", "下载", "上传", "查看", "修改", "保存",
    ]
    for verb in common_verbs:
        if verb in text and verb not in tokens:
            tokens.append(verb)
    # 4. 过滤通用词:它们在几乎所有工具描述里都出现,保留会造成大量误命中
    #    (多字词如"生成视频/读取文件"不受影响,只滤掉无区分度的裸词)
    return [t for t in tokens if t not in _GENERIC_TOKENS]


# ── 置信度评分 ────────────────────────────────────────────────────
def _score_tool(query: str, tool_name: str, tool_desc: str,
                user_tech_stack: list[str] = None) -> float:
    """多维度工具评分:智能分词匹配 + 类别匹配 + 技术栈加权。

    改进:使用 _smart_tokenize 替代原来的 word-boundary 模式,正确处理中英文混合查询。
    """
    score = 0.0
    q_lower = query.lower()
    d_lower = tool_desc.lower()
    t_lower = tool_name.lower()

    # 1. 精确关键词匹配(最高权重)
    if t_lower in q_lower:
        score += 0.5
    # 2. 智能分词匹配(替代原来的 word-boundary 模式,支持中文)
    query_tokens = _smart_tokenize(query)
    for token in query_tokens:
        # 允许单字符动词(写/读/搜/运行等)匹配,提升评分灵敏度
        if len(token) >= 1 and token in d_lower:
            score += 0.1

    # 3. 类别匹配
    category = get_category(tool_name)
    category_keywords = {
        "代码": ["代码", "编程", "开发", "函数", "类", "脚本", "后端", "前端", "接口"],
        "文件": ["文件", "读取", "写入", "编辑", "目录", "路径"],
        "网络": ["搜索", "网页", "网站", "curl", "http", "下载", "上传"],
        "视觉": ["图片", "图像", "截图", "ocr", "分析", "生成"],
        "记忆": ["记忆", "记住", "存储"],
        "规划": ["计划", "任务", "步骤", "分解"],
    }
    for cat_kws in category_keywords.values():
        if any(kw in q_lower for kw in cat_kws) and category in cat_kws:
            score += 0.15
            break

    # 4. 技术栈加权
    if user_tech_stack:
        tech_lower = [t.lower() for t in user_tech_stack]
        for tech in tech_lower:
            if tech in t_lower or tech in d_lower:
                score += 0.2

    return min(1.0, score)


# ── Embedding 路由(第二级) ────────────────────────────────────────
_EMBEDDING_CACHE: dict[str, tuple[list[float], str]] = {}
_MAX_EMBEDDING_CACHE = 500  # 防止内存无限增长


def _get_embedding(query: str) -> Optional[list[float]]:
    """获取 query 的 embedding(带缓存,LRU 淘汰)。"""
    if query in _EMBEDDING_CACHE:
        vec, cached_query = _EMBEDDING_CACHE[query]
        if cached_query == query:
            return vec
    try:
        from app.rag.embeddings import get_embedder, run_embed
        vec = run_embed(get_embedder(), [query])[0]
        _EMBEDDING_CACHE[query] = (vec, query)
        # LRU: 淘汰最旧的条目
        if len(_EMBEDDING_CACHE) > _MAX_EMBEDDING_CACHE:
            _EMBEDDING_CACHE.pop(next(iter(_EMBEDDING_CACHE)))
        return vec
    except Exception as e:
        logger.debug("embedding 缓存写入失败(非关键): %s", e)
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

    # 0. 构建关键词索引(缓存:仅在 registry 工具数量变化时重建)
    global _keyword_index_key
    tool_count = len([t async for t in registry.list()])
    if _keyword_index_key != tool_count:
        with _keyword_index_lock:
            if _keyword_index_key != tool_count:
                await _build_keyword_index(registry)
                _keyword_index_key = tool_count

    # 1. 第一级:关键词精确匹配(快速路径)
    # 改进: 使用 _smart_tokenize 提取关键词(支持中文), 替代原来的 ASCII-only 正则;
    # 同时保留 ASCII 长词兜底, 使"帮我查天气"与"  帮我查天气! "命中同一缓存键
    q_tokens = _smart_tokenize(user_input)
    # 过滤长度 < 2 的 token(单字符如"的"/"是"区分度低, 避免误命中)
    q_words = [t for t in q_tokens if len(t) >= 2]
    keyword_hits: dict[str, float] = {}
    for word in q_words:
        for tool_name in _KEYWORD_INDEX.get(word, []):
            keyword_hits[tool_name] = keyword_hits.get(tool_name, 0) + 0.3

    if keyword_hits:
        scored = []
        async for tool in registry.list():
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
            # 无明确多模态生成意图时剔除付费图像/视频工具(防止模型顺手就出图出视频)
            names = _filter_media_tools(user_input, names)
            if names:
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
            async for tool in registry.list():
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
                except Exception as e:
                    logger.debug("工具 embedding 计算失败(跳过): %s", e)
                    continue
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                names = [n for _, n in scored[:max_tools]]
                names = _filter_media_tools(user_input, names)
                if names:
                    avg_conf = sum(s for s, _ in scored[:max_tools]) / min(len(names), max_tools)
                    _record_routing_success(user_input, names, avg_conf)
                    return names, avg_conf, f"embedding_match(相似度阈值{min_confidence})"
        except Exception:
            logger.warning("工具路由 embedding 级失败", exc_info=True)

    # 3. 第三级:LLM 路由(兜底)
    try:
        names = await select_tools(llm, user_input, registry, max_tools)
        names = _filter_media_tools(user_input, names or [])
        if names:
            _record_routing_success(user_input, names, 0.5)
            return names, 0.5, "llm_fallback"
    except Exception:
        logger.warning("工具路由 LLM 级失败", exc_info=True)

    # 4. 全部失败:回退全量(但限制数量,并优先保留常用核心工具)
    # 注意:此处刻意**不**放 generate_image/generate_video —— 旧实现把它们排在前两位,
    # 于是任何路由失败的问句都会把付费媒体工具交给模型,模型又常常顺手调用一次。
    priority = [
        "web_search", "rag_search", "read_document", "write_file",
        "edit_file", "search_files", "list_directory", "run_code",
        "query_sqlite", "fetch_webpage",
    ]
    fallback: list[str] = []
    for n in priority:
        t = registry.get(n)
        if t and t.enabled and n not in SYSTEM_TOOLS and n not in fallback:
            fallback.append(n)
    if len(fallback) < max_tools:
        async for t in registry.list():
            if len(fallback) >= max_tools:
                break
            if t.name not in SYSTEM_TOOLS and t.enabled and t.name not in fallback:
                fallback.append(t.name)
    # 兜底路径同样剔除付费媒体工具:路由全挂时更不能让模型随手去出图出视频
    fallback = _filter_media_tools(user_input, fallback)
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


async def build_tool_router_index(registry) -> str:
    """把注册器里的工具压缩成路由器可读的一行索引。"""
    lines = []
    async for t in registry.list():
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
    index = await build_tool_router_index(registry)
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
        selected = await select_tools_by_embedding(user_input, registry, max_tools)

    known = {t.name for t in registry.list_sync()}
    valid = []
    for name in selected:
        name = name.strip()
        if name in known and name not in SYSTEM_TOOLS and name not in valid:
            valid.append(name)
    return valid[:max_tools]


async def select_tools_by_embedding(
    user_input: str,
    registry,
    max_tools: int = 10,
    min_similarity: float = 0.15,
) -> list[str]:
    """基于 embedding 相似度的工具预选(从 router.py 迁移)。"""
    try:
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity
    except Exception as e:
        logger.warning("embedding 模块不可用,降级为关键词路由: %s", e)
        return []

    try:
        query_vec = run_embed(get_embedder(), [user_input])[0]
    except Exception as e:
        logger.warning("查询 embedding 失败,降级: %s", e)
        return []

    scored = []
    async for tool in registry.list():
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
        except Exception as e:
            logger.debug("工具 embedding 计算失败(跳过): %s", e)
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in scored[:max_tools]]
