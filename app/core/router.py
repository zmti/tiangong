"""模型路由:按任务复杂度在快模型(便宜)与强模型之间分流,节省成本。

策略:
- 复杂任务(分析/计算/工具调用/长文本/上传文件等)走强模型
- 简单问答(寒暄/短问题)走快模型
- 未配置路由时返回 None,由调用方保留默认 LLM

工具路由:见 select_tools() —— 对话前由 LLM 预选相关工具子集,
避免 100+ 工具全量注入导致调用精度下降(参考 BFCL 的 tiered 建议)。
"""
from __future__ import annotations

import json
from typing import Optional

from app.config import settings
from app.tools.categories import SYSTEM_TOOLS, get_category

# 命中即判为复杂任务
_COMPLEX_KEYWORDS = (
    "分析", "计算", "比较", "对比", "汇总", "整理", "生成", "编写", "写一段", "写一个",
    "代码", "脚本", "数据库", "查询", "报表", "报告", "图表", "预测", "规划",
    "总结一下", "翻译", "解释", "公式", "正则", "Excel", "CSV", "表格",
    "上传", "文件", "邮件", "网页", "搜索", "知识库", "下载",
)
# 命中即判为简单任务(需同时满足短文本)
_SIMPLE_KEYWORDS = (
    "你好", "您好", "hi", "hello", "谢谢", "你是谁", "在吗", "早安", "晚安",
    "再见", "测试", "test",
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
    """返回 'simple' 或 'complex'。"""
    msg = (message or "").strip()
    if not msg:
        return "complex"
    lower = msg.lower()
    if any(k.lower() in lower for k in _COMPLEX_KEYWORDS):
        return "complex"
    if any(k.lower() in lower for k in _SIMPLE_KEYWORDS):
        return "simple"
    # 短文本(≤30 字)且无复杂特征按简单处理,省钱
    if len(msg) <= 30:
        return "simple"
    return "complex"


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


def build_tool_router_index(registry) -> str:
    """把注册器里的工具压缩成路由器可读的一行索引(名称[分类]: 描述)。"""
    lines = []
    for t in registry.list():
        desc = (t.description or "").strip().replace("\n", " ")
        if len(desc) > 80:
            desc = desc[:80] + "…"
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
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
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
    """用 LLM 从全量工具中预选相关工具子集。

    返回选中的工具名(不含系统工具);失败或无效结果返回空列表,
    由调用方决定回退为全量工具。
    """
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
        return []
    except Exception:  # noqa: BLE001
        return []

    # 只保留真实存在的工具;系统工具不需要路由器选择
    known = {t.name for t in registry.list()}
    valid = []
    for name in selected:
        name = name.strip()
        if name in known and name not in SYSTEM_TOOLS and name not in valid:
            valid.append(name)
    return valid[:max_tools]
