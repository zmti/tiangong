"""长期记忆、会话摘要、模型路由、JSON 解析测试。"""
import json
from pathlib import Path

from app.core.llm import _extract_json
from app.core.router import classify_task
from app.core.storage_backend import reset_storage_backend
from app.memory.ltm import MemoryStore


def run(coro):
    import asyncio
    return asyncio.run(coro)


def _reset_ltm():
    import app.memory.ltm as ltm_mod
    ltm_mod._instance = None
    reset_storage_backend()


def test_memory_save_and_search():
    _reset_ltm()
    store = MemoryStore(db_path=":memory:")
    store.save("用户偏好:喜欢喝茶", keywords=["茶", "偏好"])
    store.save("项目将于下月上线", keywords=["项目", "上线"], source="auto")

    results = store.search("喜欢喝什么")
    assert len(results) >= 1
    assert any("茶" in r["content"] for r in results)

    results = store.search("项目进度")
    assert any("上线" in r["content"] for r in results)


def test_memory_delete_and_stats():
    _reset_ltm()
    store = MemoryStore(db_path=":memory:")
    rec = store.save("临时记忆内容", keywords=["临时"])
    assert run(store.stats())["total"] == 1
    assert run(store.delete(rec["id"])) is True
    assert run(store.stats())["total"] == 0


def test_memory_scope_filter():
    _reset_ltm()
    store = MemoryStore(db_path=":memory:")
    store.save("会话专属记忆", scope="session", session_id="s1")
    # global 作用域搜索不命中 session 记忆
    results = store.search("会话专属")
    assert all(r["scope"] != "session" for r in results)
    # 显式指定 session 作用域与 session_id 可命中
    results = store.search("会话专属", scope="session", session_id="s1")
    assert any(r["scope"] == "session" for r in results)


def test_memory_hit_count_increments_on_search():
    _reset_ltm()
    store = MemoryStore(db_path=":memory:")
    rec = store.save("用户叫张三", keywords=["张三"])
    assert run(store.stats())["hits"] == 0
    store.search("张三", count_hit=True)
    store.search("张三", count_hit=True)
    assert run(store.stats())["hits"] == 2
    # 不指定 count_hit 时不计数
    store.search("张三")
    assert run(store.stats())["hits"] == 2


def test_memory_tool_session_aware():
    """memory_search 工具能通过 contextvar 检索当前会话的 session 记忆。"""
    import app.memory.ltm as ltm_mod
    from app.tools import memory_tool
    from app.tools.memory_tool import _current_session

    store = MemoryStore(db_path=":memory:")
    store.save("会议定在周四", keywords=["会议"], scope="session", session_id="sx")
    store.save("用户喜欢咖啡", keywords=["咖啡"])
    old = ltm_mod._instance
    ltm_mod._instance = store
    try:
        token = _current_session.set("sx")
        try:
            res = memory_tool.memory_search.func(query="会议", top_k=5)
        finally:
            _current_session.reset(token)
    finally:
        ltm_mod._instance = old

    assert any(r["scope"] == "session" and r["session_id"] == "sx" for r in res["results"])
    # 无会话上下文时只返回 global
    token = _current_session.set("")
    try:
        res2 = memory_tool.memory_search.func(query="咖啡", top_k=5)
    finally:
        _current_session.reset(token)
    assert all(r["scope"] == "global" for r in res2["results"])


def test_memory_tool_save_session_uses_current_session():
    """scope=session 且未显式传 session 时,自动使用当前会话上下文。"""
    import app.memory.ltm as ltm_mod
    from app.tools import memory_tool
    from app.tools.memory_tool import _current_session

    store = MemoryStore(db_path=":memory:")
    old = ltm_mod._instance
    ltm_mod._instance = store
    try:
        token = _current_session.set("sx")
        try:
            memory_tool.memory_save.func(content="本会话的临时事项", keywords="", scope="session")
        finally:
            _current_session.reset(token)
    finally:
        ltm_mod._instance = old
    rows = run(store.list(scope="session"))
    assert rows and rows[0]["session_id"] == "sx"


def test_summarize_messages():
    from app.memory.summarize import summarize_messages

    class FakeLLM:
        async def chat(self, messages, temperature=0.7):
            return '{"summary": "测试摘要内容"}'

    summary = summarize_messages(FakeLLM(), [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好,有什么可以帮你?"},
    ])
    assert summary == "测试摘要内容"


def test_summarize_fallback_to_plain_text():
    from app.memory.summarize import summarize_messages

    class FakeLLM:
        async def chat(self, messages, temperature=0.7):
            return "非 JSON 的摘要文本"

    summary = summarize_messages(FakeLLM(), [
        {"role": "user", "content": "hi"},
    ])
    assert "摘要" in summary


def test_router_classify():
    assert classify_task("你好") == "simple"
    assert classify_task("谢谢") == "simple"
    assert classify_task("帮我分析这份报表的数据") == "complex"
    assert classify_task("写一段 Python 代码计算平均值") == "complex"
    assert classify_task("这是一个比较长的问题,需要认真思考给出详细准确的结果说明") == "complex"


def test_extract_json():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('前缀 {"summary": "x"} 后缀') == {"summary": "x"}
    assert _extract_json("no json here") is None
    assert _extract_json("") is None
