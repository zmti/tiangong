"""运行记录 / 记忆 CRUD HTTP 端点测试(新增面板对应接口)。"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.api.chat import create_memory, delete_memory
from app.api.runs import delete_run, list_runs
from app.runtime.checkpoint import CheckpointManager
from app.runtime.state import AgentState


def run(coro):
    return asyncio.run(coro)


def test_list_runs_with_checkpoints(tmp_path):
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "ck.db"))
    s = AgentState(session_id="s1", user_input="你好")
    s.node = "llm"
    s.status = "interrupted"
    cm.save(s)

    # 让 list_runs 使用同一 checkpoint 文件
    import app.api.runs as runs_mod

    def fake_manager():
        return CheckpointManager(db_path=os.path.join(tmp_path, "ck.db"))

    runs_mod._manager = fake_manager  # noqa: SLF001
    data = run(list_runs(limit=50, status=""))
    assert data["total"] >= 1
    row = next(r for r in data["runs"] if r["run_id"] == s.run_id)
    assert row["status"] == "interrupted"
    assert row["summary"]["input"] == "你好"


def test_delete_run(tmp_path):
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "ck2.db"))
    s = AgentState(session_id="s1", user_input="x")
    cm.save(s)

    import app.api.runs as runs_mod

    def fake_manager():
        return CheckpointManager(db_path=os.path.join(tmp_path, "ck2.db"))

    runs_mod._manager = fake_manager  # noqa: SLF001
    res = run(delete_run(s.run_id))
    assert res["deleted"] is True
    assert not cm.exists(s.run_id)


def test_create_memory(monkeypatch):
    from app.api.chat import MemoryCreate
    from app.memory.ltm import MemoryStore

    store = MemoryStore(db_path=":memory:")
    monkeypatch.setattr("app.memory.ltm.get_memory_store", lambda: store)
    req = MemoryCreate(content="记住:项目名是 mj_asset", keywords=["项目", "mj_asset"])
    res = run(create_memory(req))
    assert res["id"]
    assert res["content"] == "记住:项目名是 mj_asset"
    assert store.stats()["total"] == 1


def test_create_memory_requires_content(monkeypatch):
    from app.api.chat import MemoryCreate
    from app.memory.ltm import MemoryStore

    store = MemoryStore(db_path=":memory:")
    monkeypatch.setattr("app.memory.ltm.get_memory_store", lambda: store)
    req = MemoryCreate(content="   ")
    with pytest.raises(Exception):
        run(create_memory(req))


def test_delete_memory(monkeypatch):
    from app.memory.ltm import MemoryStore

    store = MemoryStore(db_path=":memory:")
    saved = store.save("临时记忆")
    monkeypatch.setattr("app.memory.ltm.get_memory_store", lambda: store)
    res = run(delete_memory(saved["id"]))
    assert res["deleted"] is True
    assert store.stats()["total"] == 0
