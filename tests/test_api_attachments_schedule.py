"""附件 / 上下文用量 / 定时任务 HTTP 端点测试。"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.api.attachments import clear_attachments, delete_attachment, list_attachments, upload_attachment
from app.api.chat import chat_context
from app.api.schedule import create_schedule, delete_schedule, list_schedules, pause_schedule, resume_schedule
from app.memory.attachments import attachment_store


def run(coro):
    return asyncio.run(coro)


class FakeUpload:
    def __init__(self, name, content, content_type="text/plain"):
        self.filename = name
        self._content = content.encode("utf-8") if isinstance(content, str) else content
        self.content_type = content_type

    async def read(self):
        return self._content


@pytest.fixture
def isolated_attachments(tmp_path, monkeypatch):
    """让附件存储写到临时目录。"""
    from app.memory.attachments import AttachmentStore, _LOCK, _index

    store = AttachmentStore()
    with _LOCK:
        _index.clear()

    def fake_dir(self, session_id):
        d = tmp_path / session_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(AttachmentStore, "_dir", fake_dir)
    return store


def test_upload_and_list_attachment(isolated_attachments):
    res = run(upload_attachment(FakeUpload("notes.md", "# 项目笔记\n内容"), session_id="s1"))
    assert res["id"] and res["name"] == "notes.md"
    items = run(list_attachments(session_id="s1"))
    assert items["total"] == 1
    assert items["attachments"][0]["name"] == "notes.md"


def test_upload_oversize_rejected(isolated_attachments):
    from fastapi import HTTPException

    from app.memory.attachments import MAX_ATTACHMENT_BYTES

    big = b"x" * (MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(HTTPException):
        run(upload_attachment(FakeUpload("big.bin", big), session_id="s1"))


def test_delete_and_clear_attachment(isolated_attachments):
    res = run(upload_attachment(FakeUpload("a.txt", "hello"), session_id="s2"))
    ok = run(delete_attachment(res["id"], session_id="s2"))
    assert ok["deleted"] is True
    assert run(list_attachments(session_id="s2"))["total"] == 0
    run(upload_attachment(FakeUpload("b.txt", "x"), session_id="s2"))
    assert run(clear_attachments(session_id="s2"))["deleted"] == 1


def test_attachment_inject_text(isolated_attachments):
    res = run(upload_attachment(FakeUpload("ref.txt", "重要参考资料 ABC"), session_id="s3"))
    injected = attachment_store.inject_text("s3", [res["id"]])
    assert "重要参考资料 ABC" in injected
    assert "[附件: ref.txt]" in injected


def test_chat_context_usage(monkeypatch, tmp_path):
    from app.memory.session import SessionStore

    store = SessionStore(db_path=os.path.join(str(tmp_path), "sessions.db"))
    monkeypatch.setattr("app.api.chat.session_store", store)

    async def seed():
        await store.add_message("s1", "user", "你好,世界")
        await store.add_message("s1", "assistant", "你好,我是助手")

    run(seed())
    data = run(chat_context(session_id="s1"))
    assert data["messages"] == 2
    assert data["tokens"] > 0
    assert data["token_budget"] > 0
    assert 0 <= data["usage_pct"] <= 100


def test_schedule_crud(monkeypatch, tmp_path):
    import app.api.schedule as sched_mod
    from app.api.schedule import ScheduleCreate
    from app.core.scheduler import TaskScheduler

    sched = TaskScheduler(task_file=os.path.join(str(tmp_path), "tasks.json"))
    sched.register_function("get_current_time", lambda: {"time": "2026-01-01"})

    def fake_scheduler():
        return sched

    sched_mod._get_scheduler = fake_scheduler  # noqa: SLF001

    req = ScheduleCreate(name="每日问候", func_name="get_current_time", cron_expr="0 9 * * *")
    created = run(create_schedule(req))
    tid = created["task"]["task_id"]
    assert created["task"]["cron_expr"] == "0 9 * * *"
    assert run(list_schedules())["total"] == 1
    assert run(pause_schedule(tid))["status"] == "paused"
    assert run(resume_schedule(tid))["status"] == "resumed"
    assert run(delete_schedule(tid))["deleted"] is True


def test_schedule_create_invalid_func(monkeypatch, tmp_path):
    import app.api.schedule as sched_mod
    from app.api.schedule import ScheduleCreate
    from app.core.scheduler import TaskScheduler

    sched = TaskScheduler(task_file=os.path.join(str(tmp_path), "tasks2.json"))
    sched.register_function("get_current_time", lambda: {"time": "x"})

    def fake_scheduler():
        return sched

    sched_mod._get_scheduler = fake_scheduler  # noqa: SLF001
    with pytest.raises(Exception):
        run(create_schedule(ScheduleCreate(name="t", func_name="no_such_func", cron_expr="0 9 * * *")))
