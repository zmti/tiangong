"""会话摘要压缩:历史超预算时,把较早对话压成摘要再注入上下文。

思路(对齐主流智能体的上下文管理):
1. 保留最近 N 条完整消息
2. 更早的消息合并 -> 调用 LLM 生成结构化摘要 {summary, key_facts}
3. 摘要以 system 消息注入,替代被裁剪的原始消息
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from app.config import settings


def summarize_messages(llm, messages: list[dict], max_chars: Optional[int] = None) -> str:
    """同步包装(供独立脚本/测试):把一批历史消息压缩为摘要文本。"""
    return asyncio.run(summarize_messages_async(llm, messages, max_chars))


async def summarize_messages_async(llm, messages: list[dict], max_chars: Optional[int] = None) -> str:
    """异步摘要:把一批历史消息压缩为摘要文本。

    llm: 具备 chat / chat_json 的客户端(Mock 也兼容)。
    返回摘要字符串;失败时返回空串(调用方回退到截断策略)。
    """
    if not messages:
        return ""
    max_chars = max_chars or settings.agent_summary_chars

    clipped = []
    for m in messages:
        content = str(m.get("content", ""))
        if m.get("role") in ("tool", "error"):
            continue
        clipped.append({"role": m.get("role", "user"), "content": content[: settings.message_char_cap]})
    if not clipped:
        return ""

    payload = json.dumps(clipped, ensure_ascii=False)[:max_chars]
    prompt = [
        {
            "role": "system",
            "content": (
                "你是对话压缩助手。请把以下对话压缩成简洁摘要,保留:用户的关键需求与偏好、"
                "重要事实与数据、已得出的结论、尚未完成的事项。只输出 JSON,格式为 "
                '{"summary": "摘要内容"}'
            ),
        },
        {"role": "user", "content": payload},
    ]

    try:
        return await _summarize_async(llm, prompt)
    except Exception:  # noqa: BLE001
        return ""


async def _summarize_async(llm, prompt: list[dict]) -> str:
    """异步执行摘要生成。"""
    if hasattr(llm, "chat_json"):
        try:
            data = await llm.chat_json(prompt, temperature=0.2)
            if isinstance(data, dict) and data.get("summary"):
                return str(data["summary"])[: settings.message_char_cap]
        except Exception:  # noqa: BLE001
            pass

    resp = await llm.chat(prompt, temperature=0.2)

    # 尝试解析 JSON,失败则回退为原文
    text = resp if isinstance(resp, str) else str(resp)
    text = text.strip()
    parsed = _extract_summary_json(text)
    if parsed:
        return parsed[: settings.message_char_cap]
    return text[: settings.message_char_cap]


def _extract_summary_json(text: str) -> Optional[str]:
    """从模型输出中提取 {"summary": ...} 中的摘要文本。"""
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("summary"):
            return str(data["summary"])
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict) and data.get("summary"):
                return str(data["summary"])
        except json.JSONDecodeError:
            return None
    return None
