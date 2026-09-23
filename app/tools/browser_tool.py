"""浏览器控制工具:自动化操作网页,支持截图、点击、输入等。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
from typing import Optional

from app.tools.registry import tool

logger = logging.getLogger(__name__)


# 浏览器 JS 执行沙箱:禁止访问危险全局对象/属性
_BROWSER_JS_DANGEROUS_PATTERNS = [
    "eval(", "Function(", "constructor",
    "process.", "require(", "import(",
    "window.location", "document.cookie",
    "fetch(", "XMLHttpRequest",
    "localStorage", "sessionStorage",
    "parent.", "top.", "frame",
    "crypto.", "indexedDB",
    "navigator.serviceWorker",
]


def _sanitize_js_expression(expression: str) -> str:
    """对 JS 表达式做基础安全检查,阻止常见危险模式。

    注意:这不能完全沙箱化 JS,只是减少高风险操作。
    在生产环境中应使用独立沙箱环境(如 jsdom sandbox)。
    """
    expr_lower = expression.lower()
    for pattern in _BROWSER_JS_DANGEROUS_PATTERNS:
        if pattern.lower() in expr_lower:
            raise ValueError(
                f"JS 表达式包含危险模式 {pattern!r},已拒绝执行。"
                "仅允许纯计算和页面元素查询,不允许网络请求/文件访问/本地存储操作。"
            )
    return expression


class BrowserSession:
    """浏览器会话管理。"""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None
        self._context = None
        # 测试用错误捕获(默认空,不影响既有交互工具)
        self._console_errors: list[dict] = []
        self._page_errors: list[str] = []
        self._failed_requests: list[dict] = []

    async def launch(self, headless: bool = True) -> bool:
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            # 测试用:挂错误捕获监听(对交互工具透明,仅 web_auto_test 读取)
            self._page.on("console", self._on_console)
            self._page.on("pageerror", lambda exc: self._page_errors.append(str(exc)))
            self._page.on("requestfailed", lambda req: self._failed_requests.append(
                {"url": req.url, "error": str(getattr(req.failure, "error_text", "") or "network error")}))
            self._page.on("response", self._on_response)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        finally:
            self._browser = None
            self._playwright = None
            self._page = None
            self._context = None

    @property
    def is_active(self) -> bool:
        return self._page is not None

    # ---- 测试错误捕获(web_auto_test 使用) ----
    def _on_console(self, msg) -> None:
        # 仅记录 error 级(警告不计入失败,避免噪声)
        if getattr(msg, "type", "") == "error":
            loc = getattr(msg, "location", None) or {}
            self._console_errors.append({
                "text": str(msg.text),
                "location": f"{loc.get('url', '')}:{loc.get('lineNumber', '')}" if loc else None,
            })

    def _on_response(self, resp) -> None:
        # HTTP 4xx/5xx 视为失败请求(favicon 误报忽略)
        try:
            status = resp.status
        except Exception:  # noqa: BLE001
            return
        if status >= 400 and not str(resp.url).rstrip("/").endswith("favicon.ico"):
            self._failed_requests.append({"url": str(resp.url), "status": status})

    def reset_capture(self) -> None:
        """清空已捕获的错误(每次测试前调用,避免跨页面污染)。"""
        self._console_errors = []
        self._page_errors = []
        self._failed_requests = []

    def get_capture(self) -> dict:
        """返回本次会话捕获的浏览器错误汇总。"""
        return {
            "console_errors": list(self._console_errors),
            "page_errors": list(self._page_errors),
            "failed_requests": list(self._failed_requests),
        }

    async def goto(self, url: str, wait_until: str = "load", timeout: int = 30000) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            response = await self._page.goto(url, wait_until=wait_until, timeout=timeout)
            title = await self._page.title()
            return {
                "status": "ok",
                "url": self._page.url,
                "title": title,
                "status_code": response.status if response else None,
            }
        except Exception as e:
            return {"error": f"导航失败: {e}"}

    async def screenshot(self, full_page: bool = False) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            await self._page.screenshot(path=tmp.name, full_page=full_page)
            with open(tmp.name, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            os.unlink(tmp.name)
            return {"status": "ok", "image_base64": img_b64, "format": "png"}
        except Exception as e:
            return {"error": f"截图失败: {e}"}

    async def get_content(self) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            content = await self._page.content()
            title = await self._page.title()
            url = self._page.url
            return {"status": "ok", "url": url, "title": title, "html": content[:50000]}
        except Exception as e:
            return {"error": f"获取内容失败: {e}"}

    async def get_text(self) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            text = await self._page.inner_text("body")
            title = await self._page.title()
            return {"status": "ok", "url": self._page.url, "title": title, "text": text[:20000]}
        except Exception as e:
            return {"error": f"获取文本失败: {e}"}

    async def click(self, selector: str) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            await self._page.click(selector, timeout=10000)
            return {"status": "ok", "message": f"已点击: {selector}"}
        except Exception as e:
            return {"error": f"点击失败: {e}"}

    async def click_coords(self, x: int, y: int) -> dict:
        """按屏幕坐标点击(computer-use 风格,配合截图视觉定位)。"""
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            await self._page.mouse.click(x, y)
            return {"status": "ok", "message": f"已点击坐标 ({x}, {y})"}
        except Exception as e:
            return {"error": f"坐标点击失败: {e}"}

    async def type_text(self, selector: str, text: str, clear: bool = True) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            if clear:
                await self._page.fill(selector, text, timeout=10000)
            else:
                await self._page.type(selector, text, timeout=10000)
            return {"status": "ok", "message": f"已输入到 {selector}"}
        except Exception as e:
            return {"error": f"输入失败: {e}"}

    async def evaluate(self, expression: str) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        # 安全检查:防止执行危险 JS
        try:
            sanitized = _sanitize_js_expression(expression)
        except ValueError as e:
            return {"error": str(e)}
        try:
            result = await self._page.evaluate(sanitized)
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"error": f"执行失败: {e}"}

    async def wait_for(self, selector: str, timeout: int = 10000) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            return {"status": "ok", "message": f"元素已出现: {selector}"}
        except Exception as e:
            return {"error": f"等待超时: {e}"}

    async def scroll(self, direction: str = "down", amount: int = 500) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            if direction == "down":
                await self._page.evaluate(f"window.scrollBy(0, {amount})")
            elif direction == "up":
                await self._page.evaluate(f"window.scrollBy(0, -{amount})")
            elif direction == "top":
                await self._page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return {"status": "ok", "message": f"已向{direction}滚动"}
        except Exception as e:
            return {"error": f"滚动失败: {e}"}

    async def back(self) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            await self._page.go_back()
            return {"status": "ok", "url": self._page.url}
        except Exception as e:
            return {"error": f"后退失败: {e}"}

    async def forward(self) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            await self._page.go_forward()
            return {"status": "ok", "url": self._page.url}
        except Exception as e:
            return {"error": f"前进失败: {e}"}

    async def get_links(self) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        try:
            links = await self._page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).slice(0, 50).map(a => ({
                    text: a.innerText.trim().slice(0, 100),
                    url: a.href
                })).filter(l => l.text)
            """)
            return {"status": "ok", "links": links, "count": len(links)}
        except Exception as e:
            return {"error": f"获取链接失败: {e}"}

    async def fill_form(self, fields: list[dict]) -> dict:
        if not self._page:
            return {"error": "浏览器未启动"}
        results = []
        for field in fields:
            selector = field.get("selector", "")
            value = field.get("value", "")
            action = field.get("action", "fill")
            try:
                if action == "fill":
                    await self._page.fill(selector, value, timeout=5000)
                elif action == "click":
                    await self._page.click(selector, timeout=5000)
                elif action == "select":
                    await self._page.select_option(selector, value, timeout=5000)
                results.append({"selector": selector, "status": "ok"})
            except Exception as e:
                results.append({"selector": selector, "status": "error", "error": str(e)})
        return {"status": "ok", "results": results}


_session: Optional[BrowserSession] = None
_session_lock = asyncio.Lock()


async def _get_session() -> BrowserSession:
    global _session
    async with _session_lock:
        if _session is None or not _session.is_active:
            _session = BrowserSession()
            ok = await _session.launch()
            if not ok:
                _session = None
                raise RuntimeError("无法启动浏览器,请安装 playwright: pip install playwright && playwright install chromium")
    return _session


@tool("browser_open", "打开网页", timeout=30)
async def browser_open(
    url: str,
    wait_until: str = "load",
) -> dict:
    session = await _get_session()
    return await session.goto(url, wait_until=wait_until)


@tool("browser_screenshot", "截取当前网页截图", timeout=15)
async def browser_screenshot(
    full_page: bool = False,
) -> dict:
    session = await _get_session()
    return await session.screenshot(full_page=full_page)


@tool("browser_get_text", "获取网页文本内容", timeout=15)
async def browser_get_text() -> dict:
    session = await _get_session()
    return await session.get_text()


@tool("browser_click", "点击网页元素", timeout=10)
async def browser_click(
    selector: str,
) -> dict:
    session = await _get_session()
    return await session.click(selector)


@tool("browser_type", "在输入框中输入文本", timeout=10)
async def browser_type(
    selector: str,
    text: str,
    clear: bool = True,
) -> dict:
    session = await _get_session()
    return await session.type_text(selector, text, clear=clear)


@tool("browser_scroll", "滚动页面", timeout=10)
async def browser_scroll(
    direction: str = "down",
    amount: int = 500,
) -> dict:
    session = await _get_session()
    return await session.scroll(direction, amount)


@tool("browser_back", "浏览器后退", timeout=10)
async def browser_back() -> dict:
    session = await _get_session()
    return await session.back()


@tool("browser_forward", "浏览器前进", timeout=10)
async def browser_forward() -> dict:
    session = await _get_session()
    return await session.forward()


@tool("browser_get_links", "获取页面所有链接", timeout=10)
async def browser_get_links() -> dict:
    session = await _get_session()
    return await session.get_links()


@tool("browser_fill_form", "填写表单", timeout=15)
async def browser_fill_form(
    fields: str,
) -> dict:
    session = await _get_session()
    try:
        field_list = json.loads(fields) if isinstance(fields, str) else fields
    except json.JSONDecodeError:
        return {"error": "fields 参数必须是 JSON 数组"}
    return await session.fill_form(field_list)


@tool("browser_evaluate", "执行 JavaScript 代码(受限沙箱)", timeout=15)
async def browser_evaluate(
    expression: str,
) -> dict:
    """执行浏览器 JS。注意:仅允许安全表达式,禁止网络/文件/存储操作。"""
    session = await _get_session()
    return await session.evaluate(expression)


@tool("browser_wait", "等待元素出现", timeout=30)
async def browser_wait(
    selector: str,
    timeout: int = 10000,
) -> dict:
    session = await _get_session()
    return await session.wait_for(selector, timeout)


@tool("browser_close", "关闭浏览器", timeout=10)
async def browser_close() -> dict:
    global _session
    with _session_lock:
        if _session:
            await _session.close()
            _session = None
    return {"status": "ok", "message": "浏览器已关闭"}


def _classify_mime(b64_head: bytes) -> str:
    if b64_head[:4] == b"\x89PNG":
        return "image/png"
    if b64_head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b64_head[:4] == b"RIFF":
        return "image/webp"
    return "image/png"


async def _vision_next_action(goal: str, history: list[dict], llm, viewport: tuple[int, int]) -> dict:
    """把当前截图交给多模态 LLM,决策下一步动作(computer-use 风格)。

    返回 {"action": "click|type|scroll|wait|done", ...} 的动作 JSON;
    任何失败返回 {"action": "done"} 由调用方结束循环(避免死循环)。
    """
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["click", "type", "scroll", "wait", "done"]},
            "x": {"type": "integer", "description": "click 时的目标 x 坐标"},
            "y": {"type": "integer", "description": "click 时的目标 y 坐标"},
            "text": {"type": "string", "description": "type 时输入的文字"},
            "direction": {"type": "string", "description": "scroll 方向 up/down"},
            "reason": {"type": "string", "description": "这一步的简短理由"},
        },
        "required": ["action", "reason"],
    }

    def _validator(data):
        if not isinstance(data, dict):
            return False, "不是对象"
        if data.get("action") not in ("click", "type", "scroll", "wait", "done"):
            return False, "action 非法"
        return True, None

    try:
        return await llm.chat_json_schema(history, schema, temperature=0.1, validator=_validator)
    except Exception:  # noqa: BLE001
        return {"action": "done", "reason": "vision 决策失败,结束"}


@tool("browser_drive", "computer-use 视觉驾驶:循环截图-分析-操作,直到完成目标", timeout=600)
async def browser_drive(
    goal: str,
    max_steps: int = 6,
) -> dict:
    """视觉驾驶浏览器完成目标:截图→多模态 LLM 决策→点击/输入/滚动→再截图。

    适合:需要"看着页面操作"的网页任务(点按钮、填表单、翻页、做操作)。
    循环至多执行 max_steps 步;每一步产出事件(经 agent 流透出),最终返回
    动作轨迹与最后截图。需配置支持视觉的多模态模型(provider 缺省用主配置)。
    """
    session = await _get_session()
    if not session.is_active:
        return {"error": "浏览器未启动"}
    try:
        from app.core.llm import LLMClient
    except Exception as e:  # noqa: BLE001
        return {"error": f"无法加载 LLM 客户端: {e}"}

    llm = LLMClient()

    history: list[dict] = [
        {
            "role": "system",
            "content": (
                f"你是 computer-use 浏览器驾驶代理。目标: {goal}\n"
                "你通过观察截图决定下一步动作,一次只做一步。"
                "动作: click(x,y) / type(text 到当前聚焦输入框) / scroll(up/down) / wait / done。"
                "点击优先点按钮/链接的视觉中心坐标;输入前如未聚焦请先 click 目标输入框。"
                "目标完成或无法推进时返回 done。"
            ),
        }
    ]

    trace: list[dict] = []
    last_screenshot: Optional[str] = None
    for step in range(1, max_steps + 1):
        shot = await session.screenshot(full_page=False)
        if "error" in shot:
            return {"error": shot["error"], "steps": trace}
        last_screenshot = shot.get("image_base64", "")
        img_b64 = last_screenshot[:100]
        mime = _classify_mime(base64.b64decode(img_b64) if img_b64 else b"")
        try:
            vp = await session._page.viewport_size
        except Exception:  # noqa: BLE001
            vp = {"width": 1920, "height": 1080}
        vw, vh = int(vp.get("width", 1920)), int(vp.get("height", 1080))

        msg: dict = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"当前截图(第{step}步)。视口 {vw}x{vh}。请决策下一步动作。",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{last_screenshot}"},
                },
            ],
        }
        if history and history[-1]["role"] == "user":
            history.append(msg)
        else:
            history.append(msg)

        decision = await _vision_next_action(goal, history, llm, (vw, vh))
        action = decision.get("action", "done")
        reason = decision.get("reason", "")
        trace.append({"step": step, "action": action, "reason": reason})

        if action == "done":
            break
        if action == "click":
            x = int(decision.get("x", 0))
            y = int(decision.get("y", 0))
            history.append({"role": "assistant", "content": f"点击坐标 ({x},{y}): {reason}"})
            res = await session.click_coords(x, y)
            if "error" in res:
                return {"error": res["error"], "steps": trace}
        elif action == "type":
            text = decision.get("text", "")
            history.append({"role": "assistant", "content": f"输入: {text}"})
            await session._page.keyboard.type(text)
        elif action == "scroll":
            direction = decision.get("direction", "down")
            history.append({"role": "assistant", "content": f"滚动 {direction}: {reason}"})
            await session.scroll(direction)
        elif action == "wait":
            history.append({"role": "assistant", "content": "等待页面稳定"})
            await asyncio.sleep(1.0)

    return {
        "status": "ok",
        "goal": goal,
        "steps": trace,
        "final_screenshot": last_screenshot or "",
    }
