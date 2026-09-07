"""分层记忆注入:working(即时)/ semantic(语义长期记忆)/ episodic(会话情节)。

主流 agent 把上下文记忆分为:
- working: 正在进行的会话即时上下文(最近消息)
- semantic: 跨会话的通用事实/偏好(语义检索命中)
- episodic: 某次会话发生过的事件/情节(会话级记忆 + 摘要)

这里把三层分别检索,以带强度标签的结构化提示注入,替代旧的单一 flat 注入;
任一层失败自动降级,不影响主流程。由 agent._build_messages 调用。
"""
from __future__ import annotations


async def build_layered_memory_note(
    user_input: str,
    history: list[dict],
    session_id: str,
    llm=None,
) -> str:
    """组装分层记忆提示块。返回注入文本;任何失败返回空串。

    history 为当前会话历史(working 层取最近几条原文);
    semantic 层走 LTM global 检索;episodic 层走 LTM session 检索。
    """
    blocks: list[str] = []
    # 融合对话上下文构造检索 query
    query = _build_query(user_input, history)
    try:
        _add_working(blocks, history)
        _add_semantic(blocks, query)
        _add_episodic(blocks, query, session_id)
    except Exception:  # noqa: BLE001  # 分层检索失败不影响主流程
        return ""
    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _build_query(user_input: str, history: list[dict]) -> str:
    """融合用户输入和最近对话构造检索 query。"""
    parts = [user_input or ""]
    # 取最近 3 条有效对话
    for m in reversed(history[-6:]):
        role = m.get("role")
        if role in ("user", "assistant"):
            content = str(m.get("content", ""))[:100]
            if content.strip():
                parts.append(content)
        if len(parts) >= 4:
            break
    return " ".join(p for p in parts if p.strip())


def _add_working(blocks: list[str], history: list[dict]) -> None:
    """working 层:最近 8 条有效对话原文(不注入会超预算的长工具结果)。"""
    recent: list[str] = []
    for m in reversed(history or []):
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(m.get("content", ""))[:600].strip()
        if not content:
            continue
        recent.insert(0, f"{'用户' if role == 'user' else '助手'}: {content}")
        if len(recent) >= 8:
            break
    if recent:
        blocks.append("【即时记忆(本会话最近对话)】\n" + "\n".join(recent))


def _add_semantic(blocks: list[str], query: str) -> None:
    """semantic 层:跨会话语义长期记忆(global)。"""
    from app.memory.ltm import get_memory_store

    mems = get_memory_store().search(query, top_k=4, count_hit=True)
    lines = [f"- {m['content']}" for m in mems if m.get("content")]
    if lines:
        blocks.append("【长期记忆(跨会话语义检索)】\n" + "\n".join(lines))


def _add_episodic(blocks: list[str], query: str, session_id: str) -> None:
    """episodic 层:本会话积累的情节记忆(session 作用域)。"""
    if not session_id:
        return
    from app.memory.ltm import get_memory_store

    mems = get_memory_store().search(
        query, top_k=4, scope="session", session_id=session_id, count_hit=True
    )
    lines = [f"- {m['content']}" for m in mems if m.get("content")]
    if lines:
        blocks.append("【情节记忆(本会话过往事件)】\n" + "\n".join(lines))


def enable_layered() -> bool:
    """分层记忆是否启用(配置开关)。"""
    from app.config import settings

    return bool(settings.agent_layered_memory)