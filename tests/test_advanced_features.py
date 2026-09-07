"""多模态 / 后台任务 / 分层记忆 / 存储后端 / 代码自愈测试。"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.core.llm import LLMClient
from app.core.code_heal import _is_success, extract_error_text, heal_code_failure
from app.core.multimodal import (
    build_image_url_part,
    build_vision_content_part,
    image_to_data_url,
    is_image_mime,
    model_supports_vision,
    transcribe_audio,
)
from app.core.tasks import TaskManager
from app.memory.layers import _add_semantic, _add_working, build_layered_memory_note
from app.tools.registry import Tool, ToolRegistry, _build_params_model


def run(coro):
    return asyncio.run(coro)


def make_registry(tmp_path):
    """构造含 write_file/edit_file/run_terminal/run_code 的自愈测试工具集。"""
    reg = ToolRegistry()
    bad_file = os.path.join(str(tmp_path), "bad.py")

    def write_file(path, content):
        full = os.path.abspath(path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "ok", "path": full}

    def edit_file(path, old_text, new_text, dry_run=False):
        full = os.path.abspath(path)
        if not os.path.exists(full):
            return {"error": "文件不存在"}
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
        if old_text not in content:
            return {"error": "old_text 未命中"}
        if dry_run:
            return {"status": "preview", "applied": True}
        with open(full, "w", encoding="utf-8") as f:
            f.write(content.replace(old_text, new_text, 1))
        return {"status": "ok", "applied": True}

    def run_terminal(command: str, timeout: int = 120):
        # 修复成功(文件已写入正确答案)才算通过,否则复现失败
        if os.path.exists(bad_file):
            with open(bad_file, "r", encoding="utf-8") as f:
                if "return 42" in f.read():
                    return {"success": True, "stdout": "ok"}
        return {"success": False, "stderr": "Traceback: ValueError", "returncode": 1}

    for fn in (write_file, edit_file, run_terminal):
        model, schema = _build_params_model(fn)
        reg.register(Tool(name=fn.__name__, description=fn.__name__, func=fn,
                          params_model=model, json_schema=schema))
    return reg, bad_file


# ---- 多模态 ----

def test_model_supports_vision():
    assert model_supports_vision("qwen-vl-plus")
    assert model_supports_vision("gpt-4o")
    assert model_supports_vision("claude-3-opus")
    assert not model_supports_vision("deepseek-chat")
    assert not model_supports_vision("qwen-plus")


def test_image_to_data_url_and_mime():
    assert is_image_mime("image/png")
    assert is_image_mime("image/jpeg")
    assert not is_image_mime("text/plain")
    data_url = image_to_data_url(b"\x89PNGabc", "image/png")
    assert data_url.startswith("data:image/png;base64,")
    data_url2 = image_to_data_url(b"abc", "application/x")
    assert data_url2.startswith("data:image/png;base64,")


def test_build_image_url_part():
    part = build_image_url_part("data:image/png;base64,xx")
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image")

    part2 = build_vision_content_part("data:image/png;base64,xx", "看看图")
    assert part2["type"] == "text"
    assert "看看图" in part2["text"]


def test_transcribe_audio_unconfigured():
    """未配置 ASR 时返回引导提示而非抛异常(默认配置下必然未配置)。"""
    from app.config import settings

    text = run(transcribe_audio(b"\x00audio"))
    assert "尚未配置" in text or "ASR" in text or "语音" in text


# ---- 多层记忆 ----

def test_layered_working_block():
    history = [
        {"role": "user", "content": "我的项目叫 AgentService"},
        {"role": "assistant", "content": "好的,记下了。"},
    ]
    blocks: list[str] = []
    _add_working(blocks, history)
    assert any("即时记忆" in b for b in blocks)
    assert any("AgentService" in b for b in blocks)


def test_layered_semantic_block(monkeypatch, tmp_path):
    from app.memory.ltm import MemoryStore

    store = MemoryStore(db_path=os.path.join(str(tmp_path), "mem.db"))
    store.save("用户喜欢喝美式咖啡", source="auto")
    monkeypatch.setattr("app.memory.ltm._instance", store)
    blocks: list[str] = []
    _add_semantic(blocks, "咖啡偏好")
    assert any("美式咖啡" in b for b in blocks)


def test_layered_memory_note(tmp_path, monkeypatch):
    from app.memory.ltm import MemoryStore

    store = MemoryStore(db_path=os.path.join(str(tmp_path), "mem2.db"))
    store.save("用户是后端工程师", keywords=["后端", "工程师", "背景"], source="auto")
    monkeypatch.setattr("app.memory.ltm._instance", store)
    note = run(build_layered_memory_note(
        "我的背景", [{"role": "user", "content": "你好"}], "sess-1"))
    assert isinstance(note, str)
    assert "后端工程师" in note


# ---- 代码自愈 ----

class HealLLM(LLMClient):
    provider = "heal-mock"
    model = "heal-model"

    def __init__(self, plan):
        self._plan = list(plan)
        self._i = 0

    async def chat_json_schema(self, messages, schema, temperature=0.1, **kw):
        p = self._plan[min(self._i, len(self._plan) - 1)]
        self._i += 1
        return p


def test_extract_error_text():
    assert "Traceback" in extract_error_text(json.dumps(
        {"success": False, "stderr": "Traceback..."}))
    assert extract_error_text("plain") == "plain"


def test_is_success():
    assert _is_success(json.dumps({"success": True}))
    assert not _is_success(json.dumps({"success": False, "stderr": "x"}))
    assert not _is_success(json.dumps({"blocked": True}))
    assert not _is_success(json.dumps({"error": "boom"}))


def test_heal_code_failure_fix_file(tmp_path, monkeypatch):
    """自愈闭环:修复文件→重跑验证→healed。"""
    import app.tools.registry as reg_mod

    reg, bad_file = make_registry(tmp_path)
    with open(bad_file, "w", encoding="utf-8") as f:
        f.write("def foo():\n    return 'x'\n")

    monkeypatch.setattr(reg_mod, "get_registry", lambda: reg)
    llm = HealLLM([{
        "action": "fix_file",
        "file": bad_file,
        "old": "return 'x'",
        "new": "return 42",
        "explanation": "修复返回值",
    }])
    res = run(heal_code_failure(
        llm, "修 bug", "run_terminal",
        json.dumps({"command": "pytest tests/test_bad.py", "timeout": 30}),
        json.dumps({"success": False, "stderr": "Traceback: ValueError"}),
        max_rounds=2,
    ))
    assert res["healed"] is True
    assert res["final"] == '{"success": true, "stdout": "ok"}'
    assert any(s.get("action") == "rerun" for s in res["steps"])


def test_heal_code_failure_no_fix(tmp_path, monkeypatch):
    """LLM 判定无法修复→返回 no_fix_available,healed=False。"""
    import app.tools.registry as reg_mod

    reg, bad_file = make_registry(tmp_path)
    monkeypatch.setattr(reg_mod, "get_registry", lambda: reg)
    llm = HealLLM([{"action": "no_fix_available", "explanation": "无法定位到源码"}])
    res = run(heal_code_failure(
        llm, "修 bug", "run_terminal",
        json.dumps({"command": "pytest", "timeout": 30}),
        json.dumps({"success": False, "stderr": "ModuleNotFoundError"}),
        max_rounds=2,
    ))
    assert res["healed"] is False


# ---- 后台任务 ----

def test_task_manager_lifecycle(tmp_path):
    mgr = TaskManager(db_path=os.path.join(str(tmp_path), "tasks.db"))
    rec = mgr.create(session_id="s1", message="你好")
    assert rec.status == "queued"
    assert mgr.get(rec.task_id) is not None
    assert any(t["task_id"] == rec.task_id for t in mgr.list())
    assert mgr.cancel("nope") is False


def test_task_events_generator(tmp_path):
    mgr = TaskManager(db_path=os.path.join(str(tmp_path), "tasks2.db"))
    rec = mgr.create(session_id="s2", message="hi")
    rec.status = "done"
    mgr._queues[rec.task_id].put_nowait({"type": "task_done", "task_id": rec.task_id})

    async def collect():
        events = []
        async for evt in mgr.subscribe(rec.task_id):
            events.append(evt)
        return events

    events = run(collect())
    assert events and events[0]["type"] == "task_done"


# ---- 存储后端选择 ----

def test_storage_fallback_sqlite(tmp_path, monkeypatch):
    """无 Redis 时 get_session_backend 回退 SQLite。"""
    from app.core.storage import get_session_backend

    backend, store = get_session_backend()
    assert backend == "sqlite"
    assert hasattr(store, "add_message")


def test_session_search_method(tmp_path):
    """SQLite SessionStore 的 search 方法(替代原 /chat/search 直连 SQLite)。"""
    from app.memory.session import SessionStore
    from app.core.storage_backend import reset_storage_backend

    reset_storage_backend()
    store = SessionStore(db_path=os.path.join(str(tmp_path), "sessions.db"))
    run(store.add_message("s1", "user", kind="text", content="我喜欢蓝色"))
    results = run(store.search("蓝色"))
    assert results and results[0]["session_id"] == "s1"


def test_get_vector_backend_fallback(monkeypatch):
    """未配置 DSN/缺 psycopg 时向量后端回退 sqlite。"""
    from app.core.storage import get_vector_backend

    backend, dsn = get_vector_backend()
    assert backend == "sqlite"