"""Token 级上下文预算与渐进摘要。

背景:旧的上下文管理用 `len(str)`(字符数)近似预算,对中英混合不准确。
这里提供基于启发式的 token 估算(CJK 每字≈1 token,ASCII≈每 4 字符 1 token),
把"系统提示 + 工具描述 + 记忆 + 历史"按真实 token 预算裁剪,并在超预算时
对最早的消息做渐进式 LLM 摘要(保留最近消息原样,压缩更早的内容)。
"""
from __future__ import annotations

import json
from typing import Optional

from app.config import settings


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
    """粗略 token 估算(确定性、无外部依赖)。

    精确值取决于 tokenizer,这里给出足够做预算判断的近似:
    CJK 每字约 1 token;ASCII/其他每 4 字符约 1 token。
    """
    if not text:
        return 0
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
    very_old, mid = old[: int(n * 0.4)], old[int(n * 0.4):]

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
