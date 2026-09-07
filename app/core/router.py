"""模型路由:按任务复杂度在快模型(便宜)与强模型之间分流,节省成本。

策略:
- 复杂任务(分析/计算/工具调用/长文本/上传文件等)走强模型
- 简单问答(寒暄/短问题)走快模型
- 未配置路由时返回 None,由调用方保留默认 LLM

工具路由:见 select_tools() —— 对话前由 LLM 预选相关工具子集,
避免 100+ 工具全量注入导致调用精度下降(参考 BFCL 的 tiered 建议)。
同时提供 select_tools_by_embedding() 作为无 LLM 时的快速回退(基于 embedding 相似度)。
"""
from __future__ import annotations

import json
from typing import Optional

from app.config import settings
from app.tools.categories import SYSTEM_TOOLS, get_category

# 命中即判为复杂任务
_COMPLEX_KEYWORDS = (
    "分析", "计算", "比较", "对比", "汇总", "整理", "编写", "脚本", "数据库",
    "查询", "报表", "报告", "图表", "预测", "规划",
    "代码", "程序", "函数", "类", "接口", "API", "后端", "前端", "算法",
    "正则", "Excel", "CSV", "表格", "公式",
    "下载", "上传", "搜索", "邮件", "网页", "目录",
    # 代码相关词汇(短文本但复杂)
    "def ", "function ", "class ", "import ", "async ", "await ", "return ",
    "python", "javascript", "typescript", "go ", "rust", "java ", "c++",
)
# 命中即判为简单任务(需同时满足短文本)
_SIMPLE_KEYWORDS = (
    "你好", "您好", "hi", "hello", "谢谢", "你是谁", "在吗", "早安", "晚安",
    "再见", "测试", "test", "随便", "哈哈", "嗯", "哦",
)
# 代码/工具关键词:命中直接判复杂(即使文本很短)
_CODE_KEYWORDS = (
    "def ", "function ", "class ", "import ", "async ", "await ", "return ",
    "python", "javascript", "typescript", "golang", "rust", "java ", "c++",
    "http", "database", "sql", "query", "endpoint", "route", "controller",
    "module", "package", "dependency", "install", "build", "compile",
)

TOOL_ROUTER_PROMPT = """你是工具路由器。根据用户请求,从工具列表中选择最相关、执行任务最可能用到的工具。

要求:
1. 选择 1 到 {max_tools} 个工具;宁可少而准,不要贪多。
2. 只选确定相关的工具;不确定的一律不选。
3. 如果任务是寒暄/纯问答,返回空数组 {{"tools": []}},主循环会直接回答。
4. 记忆工具(memory_*)、规划工具(set_plan/update_plan)已常驻,不需要选择。

工具列表(名称 [分类]: 描述):
{index}

只输出 JSON,格式为: {{"tools": ["工具名", ...], "reason": "一句话说明"}}
"""


def classify_task(message: str) -> str:
    """返回 'simple' 或 'complex'。

    策略:先看关键词,再看文本长度+语义特征,最后默认走简单(省钱优先)。
    """
    msg = (message or "").strip()
    if not msg:
        return "complex"  # 空消息需要 LLM 处理
    lower = msg.lower()
    # 先检测代码/工具关键词(命中直接复杂,不论长度)
    if any(kw in lower for kw in _CODE_KEYWORDS):
        return "complex"
    # 再检测简单关键词
    is_simple_kw = any(k.lower() in lower for k in _SIMPLE_KEYWORDS)
    # 再检测复杂关键词
    is_complex_kw = any(k in lower for k in _COMPLEX_KEYWORDS)
    if is_complex_kw:
        return "complex"
    if is_simple_kw:
        return "simple"
    # 短文本(≤10字)且无复杂特征 → 简单,省钱
    if len(msg) <= 10:
        return "simple"
    # 长度中等且有特定结构(问号、链接、代码标记) → 复杂
    if len(msg) > 50 and any(c in msg for c in ("?", "？", "http", "```")):
        return "complex"
    # 默认:简单(省钱优先,强模型兜底)
    return "simple"


def build_routed_llm(message: str, fallback: Optional[object] = None):
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
    """把注册器里的工具压缩成路由器可读的一行索引(名称[分类]: 描述)。"""
    lines = []
    async for t in registry.list():
        desc = (t.description or "").strip().replace("\n", " ")
        # 从 80 增加到 200,保留更多关键参数信息
        if len(desc) > 200:
            desc = desc[:200] + "…"
        lines.append(f"- {t.name} [{get_category(t.name)}]: {desc}")
    return "\n".join(lines)


def _parse_tool_list(text: str) -> Optional[list[str]]:
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
    # 括号匹配提取,避免 rfind("}") 匹配到错误位置
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
    max_tools: Optional[int] = None,
) -> list[str]:
    """【已弃用】用 LLM 从全量工具中预选相关工具子集。

    已被 smart_router.select_tools_enhanced() 取代,保留仅为向后兼容。
    实际调用请改用 smart_router。

    返回选中的工具名(不含系统工具);失败或无效结果返回空列表,
    由调用方决定回退为全量工具。
    """
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
    except Exception:  # noqa: BLE001
        selected = []

    # LLM 路由失败时,尝试 embedding 相似度回退
    if not selected:
        selected = select_tools_by_embedding(user_input, registry, max_tools)

    # 只保留真实存在的工具;系统工具不需要路由器选择
    known = {t.name for t in await registry.list()}
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
    """基于 embedding 相似度的工具预选(无 LLM 时的快速回退)。

    将用户输入和每个工具的描述都嵌入为向量,计算余弦相似度,
    返回得分最高的工具名列表。不依赖外部 LLM 调用,速度快。
    """
    try:
        from app.rag.embeddings import get_embedder, run_embed
        from app.rag.store import cosine_similarity
    except Exception:  # noqa: BLE001
        return []

    try:
        query_vec = run_embed(get_embedder(), [user_input])[0]
    except Exception:  # noqa: BLE001
        return []

    scored: list[tuple[float, str]] = []
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
        except Exception:  # noqa: BLE001
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in scored[:max_tools]]
