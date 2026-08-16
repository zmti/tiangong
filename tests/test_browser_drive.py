"""browser_drive 视觉驾驶循环测试(全部 Mock,不依赖真实浏览器/网络)。"""
import asyncio

from app.tools.browser_tool import _classify_mime, browser_drive


class _FakePage:
    def __init__(self):
        self.viewport_size = {"width": 800, "height": 600}
        self.keyboard = _FakeKeyboard()


async def _fake_get_session(session):
    return session


class _FakeKeyboard:
    async def type(self, text):
        return None


class _FakeSession:
    def __init__(self):
        self.page = _FakePage()
        self.clicks = []
        self.types = []
        self.scrolls = []

    @property
    def is_active(self):
        return True

    @property
    def _page(self):
        return self.page

    async def screenshot(self, full_page=False):
        return {"status": "ok", "image_base64": "iVBORw0KGgoAAAANSUhEUg=="}

    async def click_coords(self, x, y):
        self.clicks.append((x, y))
        return {"status": "ok"}

    async def scroll(self, direction):
        self.scrolls.append(direction)
        return {"status": "ok"}


class _VisionLLM:
    """返回预先编排的决策序列。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = 0

    async def chat_json_schema(self, messages, schema, temperature=0.1, validator=None):
        self.calls += 1
        data = self._decisions[min(self.calls - 1, len(self._decisions) - 1)]
        if validator is not None:
            ok, err = validator(data)
            assert ok, f"动作 schema 校验失败: {err}"
        return data


def test_drive_loop_click_then_done(monkeypatch):
    """驾驶循环:截图->点击->截图->done。"""
    session = _FakeSession()
    llm = _VisionLLM([
        {"action": "click", "x": 120, "y": 340, "reason": "点击目标按钮"},
        {"action": "done", "reason": "目标完成"},
    ])

    import app.core.llm as llm_mod
    import app.tools.browser_tool as bt

    monkeypatch.setattr(bt, "_get_session", lambda: _fake_get_session(session))
    monkeypatch.setattr(llm_mod, "LLMClient", lambda: llm)

    out = asyncio.run(browser_drive.func(goal="测试目标", max_steps=5))
    assert out["status"] == "ok"
    assert session.clicks == [(120, 340)]
    steps = [s["action"] for s in out["steps"]]
    assert steps == ["click", "done"]
    assert llm.calls == 2
    assert out["final_screenshot"]


def test_drive_loop_type_and_scroll(monkeypatch):
    session = _FakeSession()
    llm = _VisionLLM([
        {"action": "type", "text": "hello", "reason": "输入关键词"},
        {"action": "scroll", "direction": "down", "reason": "看下一页"},
        {"action": "done", "reason": "完成"},
    ])

    import app.core.llm as llm_mod
    import app.tools.browser_tool as bt

    monkeypatch.setattr(bt, "_get_session", lambda: _fake_get_session(session))
    monkeypatch.setattr(llm_mod, "LLMClient", lambda: llm)

    out = asyncio.run(browser_drive.func(goal="测试", max_steps=5))
    assert session.scrolls == ["down"]
    assert [s["action"] for s in out["steps"]] == ["type", "scroll", "done"]


def test_drive_loop_respects_max_steps(monkeypatch):
    """从不返回 done 时,循环在 max_steps 后停止。"""
    session = _FakeSession()
    llm = _VisionLLM([{"action": "wait", "reason": "等待"}] * 10)

    import app.core.llm as llm_mod
    import app.tools.browser_tool as bt

    monkeypatch.setattr(bt, "_get_session", lambda: _fake_get_session(session))
    monkeypatch.setattr(llm_mod, "LLMClient", lambda: llm)

    out = asyncio.run(browser_drive.func(goal="测试", max_steps=3))
    assert len(out["steps"]) == 3
    assert out["steps"][-1]["action"] == "wait"


def test_drive_loop_vision_failure_stops(monkeypatch):
    """视觉决策抛异常时返回 done,循环安全结束。"""
    session = _FakeSession()

    class _BrokenVision:
        async def chat_json_schema(self, messages, schema, temperature=0.1, validator=None):
            raise RuntimeError("vision model down")

    import app.core.llm as llm_mod
    import app.tools.browser_tool as bt

    monkeypatch.setattr(bt, "_get_session", lambda: _fake_get_session(session))
    monkeypatch.setattr(llm_mod, "LLMClient", lambda: _BrokenVision())

    out = asyncio.run(browser_drive.func(goal="测试", max_steps=5))
    assert out["status"] == "ok"
    assert out["steps"][-1]["action"] == "done"


def test_classify_mime_png():
    assert _classify_mime(b"\x89PNG\r\n") == "image/png"
    assert _classify_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert _classify_mime(b"RIFFxxxx") == "image/webp"
    assert _classify_mime(b"other") == "image/png"
