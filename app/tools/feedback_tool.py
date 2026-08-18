"""用户反馈工具:把对 Agent 回答的赞/踩沉淀为长期记忆,驱动 Agent 越用越准。

设计:
- 前端对每条助手消息调用 like_feedback / dislike_feedback
- 反馈内容与上下文(问题+回答)存入长期记忆,scope=session
- 下次同会话检索时自动注入,作为"用户偏好/纠正"的参考
"""
from __future__ import annotations

from pydantic import Field

from app.tools.registry import tool


_FEEDBACK_PREFIX_GOOD = "[用户反馈-认可] "
_FEEDBACK_PREFIX_BAD = "[用户反馈-建议改进] "


@tool(
    "like_feedback",
    "记录用户对某条回答的认可(点赞)。反馈会存入长期记忆,帮助 Agent 后续提供更符合用户期望的回答。"
    "适用场景:用户对回答满意,希望记住这类回答风格或内容偏好。",
    timeout=5,
)
def like_feedback(
    context: str = Field(description="相关上下文(用户问题 + Agent 回答的摘要),用于记忆检索"),
    note: str = Field("", description="补充说明(如'喜欢简洁风格'、'这个角度很好')"),
) -> dict:
    content = f"{_FEEDBACK_PREFIX_GOOD}{context}"
    if note:
        content += f"\n备注: {note}"
    from app.memory.ltm import get_memory_store
    store = get_memory_store()
    return store.save(content, keywords=["feedback", "liked", "preferred"], scope="session", source="feedback")


@tool(
    "dislike_feedback",
    "记录用户对某条回答的不认可(踩点)。反馈会存入长期记忆,提示 Agent 避免类似问题。"
    "适用场景:回答有错误、风格不受欢迎、缺少关键信息等。",
    timeout=5,
)
def dislike_feedback(
    context: str = Field(description="相关上下文(用户问题 + Agent 回答的摘要),用于记忆检索"),
    reason: str = Field("", description="不喜欢的原因(如'答案错误'、'太啰嗦'、'缺少代码示例')"),
) -> dict:
    content = f"{_FEEDBACK_PREFIX_BAD}{context}"
    if reason:
        content += f"\n原因: {reason}"
    from app.memory.ltm import get_memory_store
    store = get_memory_store()
    return store.save(
        content,
        keywords=["feedback", "disliked", "improve"],
        scope="session",
        source="feedback",
    )
