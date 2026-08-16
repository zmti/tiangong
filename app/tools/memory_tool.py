"""长期记忆工具:保存/检索/删除记忆,供 Agent 在对话中自主管理记忆。

用法:
- 用户透露了偏好/事实 -> Agent 调 memory_save 记住
- 用户问及旧信息 -> Agent 调 memory_search 检索
- 记忆也会在每轮对话前自动检索注入上下文(见 agent.py)

会话上下文:Agent 执行工具时通过 `_current_session` contextvar 注入 session_id,
因此 memory_search 无需感知 session 也能同时检索 global 与当前会话的记忆。
"""
from __future__ import annotations

import contextvars

from pydantic import Field

from app.memory.ltm import get_memory_store
from app.tools.registry import tool

# Agent 在执行工具前设置,用于把当前 session_id 透传给记忆工具
_current_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "memory_current_session", default=""
)


@tool("memory_save", "记住一条长期信息(用户偏好、项目事实、重要结论),之后任何会话都可检索", timeout=10)
def memory_save(
    content: str = Field(description="要记住的内容,一句话陈述句"),
    keywords: str = Field("", description="逗号分隔的关键词,用于之后检索"),
    scope: str = Field("global", description="作用域:global=所有会话, session=仅当前会话"),
) -> dict:
    """保存长期记忆。"""
    store = get_memory_store()
    kw = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else []
    session_id = None
    if scope == "session":
        session_id = _current_session.get() or None
    return store.save(content, keywords=kw, scope=scope, session_id=session_id, source="manual")


@tool("memory_search", "检索之前保存的长期记忆,返回相关记忆", timeout=10)
def memory_search(
    query: str = Field(description="要查找的记忆内容描述"),
    top_k: int = Field(5, description="返回条数"),
) -> dict:
    """检索长期记忆:global 记忆 + 当前会话的 session 记忆。"""
    store = get_memory_store()
    results = store.search(query, top_k=top_k, count_hit=True)
    session_id = _current_session.get()
    if session_id:
        session_results = store.search(
            query, top_k=top_k, scope="session", session_id=session_id, count_hit=True
        )
        # 合并去重(global 与 session 可能命中同一条记忆)
        seen = {r["id"] for r in results}
        for r in session_results:
            if r["id"] not in seen:
                results.append(r)
                seen.add(r["id"])
    return {"query": query, "results": results}


@tool("memory_forget", "删除一条长期记忆", timeout=10)
def memory_forget(
    memory_id: str = Field(description="记忆 ID"),
) -> dict:
    """删除指定记忆。"""
    store = get_memory_store()
    ok = store.delete(memory_id)
    return {"deleted": ok, "memory_id": memory_id}


@tool("memory_update", "修改一条已保存的长期记忆的内容或关键词(不改变作用域)", timeout=10)
def memory_update(
    memory_id: str = Field(description="记忆 ID"),
    content: str = Field("", description="新的记忆内容(留空则保留原内容)"),
    keywords: str = Field("", description="新的关键词,逗号分隔(留空则保留原关键词)"),
) -> dict:
    """更新指定记忆。"""
    store = get_memory_store()
    kw = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None
    updated = store.update(memory_id, content=content or None, keywords=kw)
    if updated is None:
        return {"error": f"记忆不存在: {memory_id}"}
    return {"ok": True, "memory": updated}


@tool("memory_list", "列出已保存的长期记忆", timeout=10)
def memory_list(
    limit: int = Field(20, description="返回条数"),
) -> dict:
    """列出所有长期记忆。"""
    store = get_memory_store()
    return {"memories": store.list(limit=limit)}


@tool("memory_stats", "获取长期记忆统计", timeout=10)
def memory_stats() -> dict:
    """记忆库统计信息。"""
    store = get_memory_store()
    return store.stats()
