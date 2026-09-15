"""Token 级上下文预算与渐进摘要。

背景:旧的上下文管理用 `len(str)`(字符数)近似预算,对中英混合不准确。
这里提供基于真实 tokenizer(优先 tiktoken)的精确估算,不可用时自动降级到
启发式(CJK 每字≈1 token,ASCII≈每 4 字符 1 token),
把"系统提示 + 工具描述 + 记忆 + 历史"按真实 token 预算裁剪,并在超预算时
对最早的消息做渐进式 LLM 摘要(保留最近消息原样,压缩更早的内容)。

合并了 long_context.py 的重要性评分和 LongContextEngine。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 优先使用 tiktoken(精确),不可用时降级到启发式
_tiktoken_encoding = None
try:
    import tiktoken  # noqa: F401

    _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    pass


# ── 重要性评分(来自 long_context.py) ─────────────────────────────
_ROLE_IMPORTANCE = {"system": 1.0, "user": 0.9, "assistant": 0.7, "tool": 0.3}
_KEYWORD_IMPORTANCE = {
    "decision": ["决定", "选择", "采用", "使用", "配置为", "设置为"],
    "preference": ["偏好", "喜欢", "讨厌", "习惯", "风格"],
    "fact": ["结果是", "发现", "确认", "验证", "证明", "计算得"],
    "task": ["待办", "未完成", "继续", "下一步", "TODO"],
    "code": ["def ", "function ", "class ", "import ", "# ", "```"],
}


def score_message_importance(msg: dict) -> float:
    """评估单条消息的重要性分数(0~1),用于智能裁剪决策。"""
    role = msg.get("role", "")
    content = str(msg.get("content", ""))
    base = _ROLE_IMPORTANCE.get(role, 0.5)
    keyword_bonus = 0.0
    for category, keywords in _KEYWORD_IMPORTANCE.items():
        for kw in keywords:
            if kw in content:
                keyword_bonus += 0.05
                break
    return min(1.0, base + keyword_bonus)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF   # CJK 统一汉字
        or 0x3400 <= o <= 0x4DBF  # 扩展 A
        or 0x20000 <= o <= 0x2A6DF  # 扩展 B
        or 0x3040 <= o <= 0x30FF  # 平假名/片假名
        or 0xAC00 <= o <= 0xD7AF  # 谚文
    )


def estimate_tokens(text: str) -> int:
    """精确 token 估算:优先 tiktoken(cl100k_base),不可用时降级到启发式。

    cl100k_base 覆盖 GPT-4/GPT-3.5/DeepSeek/Qwen 等主流模型。
    """
    if not text:
        return 0
    if _tiktoken_encoding is not None:
        try:
            return len(_tiktoken_encoding.encode(text))
        except Exception:  # noqa: BLE001
            pass
    # 启发式降级
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _msg_len(m: dict) -> int:
    content = m.get("content", "")
    return estimate_tokens(str(content)) if isinstance(content, str) else 0


def budget_messages(
    messages: list[dict],
    max_tokens: int,
    head_tokens: int = 0,
    min_keep: int = 4,
) -> list[dict]:
    """按 token 预算挑选 messages(从后往前保留,保留最近消息)。

    head_tokens: system 提示等必须保留的前缀 token 数(预算先扣掉)。
    min_keep: 至少保留多少条最近消息(即使超出预算也保留,由调用方决定是否截断单条)。
    返回:按原顺序、预算内能容纳的 messages 子集。
    """
    if max_tokens <= 0:
        return messages
    budget = max_tokens - head_tokens
    if budget <= 0:
        return []
    keep: list[dict] = []
    total = 0
    # 从最后往前累加,直到超出预算
    for m in reversed(messages):
        ln = _msg_len(m)
        if total + ln <= budget:
            keep.insert(0, m)
            total += ln
        else:
            if len(keep) < min_keep:
                keep.insert(0, m)  # 保证至少保留最近几条(内容过长的单条由调用方截断)
            break
    return keep


def split_history(
    history: list[dict],
    max_tokens: int,
    head_tokens: int = 0,
    min_keep: int = 4,
) -> tuple[list[dict], list[dict]]:
    """把历史拆成 (需保留的最近消息, 更早的可压缩消息)。"""
    keep = budget_messages(history, max_tokens, head_tokens, min_keep)
    old = history[: len(history) - len(keep)]
    return keep, old


async def progressive_summarize(
    llm,
    history: list[dict],
    max_tokens: int,
    head_tokens: int = 0,
    min_keep: int = 6,
) -> list[dict]:
    """渐进式压缩:最早的消息合并成摘要注入,保留最近的完整消息。

    返回处理后的 history(最前一条可能是 system 摘要,其余为保留的最近消息)。
    摘要失败时回退为"截断"(只保留最近消息)。
    """
    if not history:
        return history
    keep, old = split_history(history, max_tokens, head_tokens, min_keep)
    if not old:
        return history
    from app.memory.summarize import summarize_messages_async

    summary = await summarize_messages_async(llm, old, max_chars=settings.agent_summary_chars)
    if summary:
        return [{
            "role": "system",
            "content": f"以下是本会话早期内容的摘要(请据此理解前文背景):\n{summary}",
        }] + keep
    return keep or history


def estimate_system_tokens(system_prompt: str, memory_note: str, plan_summary: str) -> int:
    """估算固定前缀(system + 记忆 + 计划)的 token 数,供预算扣除。"""
    return estimate_tokens((system_prompt or "") + "\n" + (memory_note or "") + "\n" + (plan_summary or ""))


async def extract_facts(llm, messages: list[dict], max_facts: int = 8) -> list[str]:
    """用 LLM 从历史消息中抽取结构化关键事实(人名/数字/决策/约定/未完成任务)。

    抽取失败(无 chat_json / 网络错误)返回空列表,由调用方回退为普通摘要。
    """
    if not messages:
        return []
    if getattr(llm, "provider", "") == "mock":
        return []
    try:
        payload = json.dumps(messages, ensure_ascii=False)[:6000]
        if hasattr(llm, "chat_json"):
            data = await llm.chat_json(
                [
                    {"role": "system", "content": (
                        "从对话中抽取关键事实,用于压缩上下文后仍能支撑后续回答。"
                        "优先抽取:用户偏好/个人信息、具体数字与结果、决策与约定、"
                        "尚未完成的任务与下一步。每条一句话,去掉客套话。"
                        '只输出 JSON: {"facts": ["...", "..."]}'
                    )},
                    {"role": "user", "content": payload},
                ],
                temperature=0.2,
            )
        else:
            resp = await llm.chat(
                [{"role": "system", "content": (
                    "从对话中抽取关键事实,每条一句话,输出 JSON: {\"facts\": [...]}"
                )},
                 {"role": "user", "content": payload}],
                temperature=0.2,
            )
            data = _extract_json_obj(resp)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return []
    return [str(f).strip() for f in data["facts"] if str(f).strip()][:max_facts]


def _extract_json_obj(text: str):
    if not text:
        return None
    import re

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


async def layered_compress(
    llm,
    history: list[dict],
    max_tokens: int,
    head_tokens: int = 0,
    min_keep: int = 6,
) -> tuple[list[dict], dict]:
    """三层上下文压缩:最近原文保留 / 中期抽结构化事实 / 早期合并摘要。

    比单一摘要更接近主流 agent 的上下文工程:
    - 最近消息完整保留(决策依赖细节)
    - 中期消息压缩成"关键事实"块(支撑后续回答,检索友好)
    - 最早消息合并成一段摘要(只留背景)
    返回 (处理后的 history, 压缩统计)。任一层失败都自动降级,不影响主流程。
    """
    if not history:
        return history, {"strategy": "none"}
    keep, old = split_history(history, max_tokens, head_tokens, min_keep)
    if not old:
        return history, {"strategy": "none"}

    # 三分:最早 40% → 摘要;中段 60% → 抽事实
    n = len(old)
    # 修复: 摘要/抽事实 LLM 只认 OpenAI 合法角色, 先降级 error/plan/孤立tool
    from app.memory.session import sanitize_history_for_llm
    very_old, mid = old[: int(n * 0.4)], old[int(n * 0.4):]
    very_old = sanitize_history_for_llm(very_old)
    mid = sanitize_history_for_llm(mid)

    stats: dict = {"strategy": "layered", "fact_count": 0, "summary": False}

    from app.memory.summarize import summarize_messages_async

    blocks: list[dict] = []
    if very_old:
        summary = await summarize_messages_async(llm, very_old, max_chars=settings.agent_summary_chars)
        if summary:
            blocks.append({
                "role": "system",
                "content": f"以下是本会话早期内容的背景摘要(了解即可,不必引用):\n{summary}",
            })
            stats["summary"] = True
    if mid:
        facts = await extract_facts(llm, mid)
        if facts:
            blocks.append({
                "role": "system",
                "content": "以下是本会话中段内容的关键事实(回答时优先参考):\n- " + "\n- ".join(facts),
            })
            stats["fact_count"] = len(facts)

    if not blocks:
        # 全失败:回退为普通截断(只留最近消息),保证预算内
        return keep or history, {"strategy": "fallback_keep"}
    return blocks + keep, stats


# ── 智能裁剪(来自 long_context.py) ───────────────────────────────
@dataclass
class CompressStats:
    """压缩统计信息。"""
    strategy: str = "none"
    kept_messages: int = 0
    compressed_chunks: int = 0
    dropped_messages: int = 0
    original_tokens: int = 0
    final_tokens: int = 0


def smart_trim_by_importance(
    messages: list[dict],
    max_tokens: int,
    min_keep: int = 6,
) -> tuple[list[dict], CompressStats]:
    """按重要性智能裁剪:保留高重要性消息,优先丢弃低重要性工具结果。

    返回 (裁剪后消息列表, 统计信息)。
    """
    if max_tokens <= 0:
        return messages, CompressStats(strategy="none")

    original_tokens = sum(_msg_len(m) for m in messages)
    if original_tokens <= max_tokens:
        return messages, CompressStats(
            strategy="none", kept_messages=len(messages),
            original_tokens=original_tokens, final_tokens=original_tokens,
        )

    # 评分并排序
    scored = [(score_message_importance(m), i, m) for i, m in enumerate(messages)]
    scored.sort(key=lambda x: x[0], reverse=True)

    # 贪心保留:从高重要性开始,直到超出预算
    kept: list[dict] = []
    kept_tokens = 0
    for score, idx, msg in scored:
        msg_tokens = _msg_len(msg)
        if kept_tokens + msg_tokens <= max_tokens or len(kept) < min_keep:
            kept.insert(0, msg)
            kept_tokens += msg_tokens
        else:
            break

    stats = CompressStats(
        strategy="importance_trim",
        kept_messages=len(kept),
        dropped_messages=len(messages) - len(kept),
        original_tokens=original_tokens,
        final_tokens=kept_tokens,
    )
    return kept, stats


# ── 长上下文引擎(来自 long_context.py) ───────────────────────────
class LongContextEngine:
    """超级长上下文引擎,支持 200K+ token 对话历史管理。

    核心能力:
    1. 动态 token 预算分配(系统提示 / 记忆 / 历史按比例)
    2. 三层渐进压缩(最近原文 > 中段事实 > 早期摘要)
    3. 语义重要性感知裁剪(保留关键决策和事实)
    """

    DEFAULT_MAX_TOKENS = 200000

    def __init__(self, max_tokens: int = 0, llm=None):
        self.max_tokens = max_tokens or settings.agent_token_budget or self.DEFAULT_MAX_TOKENS
        self.llm = llm

    def compute_budget(
        self,
        system_prompt: str,
        memory_note: str,
        profile_prompt: str = "",
    ) -> tuple[int, int, dict]:
        """计算系统/记忆/历史的 token 预算分配。

        返回 (head_tokens, history_max_tokens, stats_dict)。
        """
        head_tokens = estimate_tokens(system_prompt or "")
        head_tokens += estimate_tokens(memory_note or "")
        head_tokens += estimate_tokens(profile_prompt or "")
        history_budget = max(1000, self.max_tokens - head_tokens - 2000)
        return head_tokens, history_budget, {
            "total": self.max_tokens,
            "head_tokens": head_tokens,
            "history_budget": history_budget,
        }

    async def compress(
        self,
        messages: list[dict],
        system_prompt: str = "",
        memory_note: str = "",
        profile_prompt: str = "",
    ) -> tuple[list[dict], CompressStats]:
        """对完整消息列表进行智能压缩。

        流程:
        1. 计算各部分 token 预算
        2. 按语义重要性裁剪历史
        3. 对超预算部分应用三层压缩
        """
        head_tokens, history_budget, _ = self.compute_budget(
            system_prompt, memory_note, profile_prompt,
        )

        # 第一阶段:按重要性裁剪
        trimmed, stats = smart_trim_by_importance(messages, history_budget)

        # 第二阶段:对剩余部分做三层压缩(如需)
        if stats.dropped_messages > 0 and self.llm is not None:
            try:
                compressed, stats2 = await layered_compress(
                    self.llm, trimmed, history_budget, head_tokens=head_tokens,
                )
                stats.compressed_chunks = stats2.get("compressed_chunks", 0)
                stats.strategy = stats2.get("strategy", stats.strategy)
            except Exception:
                pass

        return trimmed, stats

    def format_context_summary(self, stats: CompressStats) -> str:
        """格式化压缩统计为可读文本。"""
        return (
            f"上下文压缩统计:\n"
            f"  策略: {stats.strategy}\n"
            f"  原始 token: {stats.original_tokens:,}\n"
            f"  最终 token: {stats.final_tokens:,}\n"
            f"  保留消息: {stats.kept_messages}\n"
            f"  压缩块数: {stats.compressed_chunks}\n"
            f"  丢弃消息: {stats.dropped_messages}"
        )


def get_long_context_engine(llm=None) -> LongContextEngine:
    """获取长上下文引擎实例。"""
    return LongContextEngine(llm=llm)
