"""Computer Use 增强版:截图→多模态决策→坐标操作闭环,含元素定位/状态恢复。

对标 Claude Computer Use / OpenAI SWE-agent 视觉驾驶能力:
- 元素级定位:通过 DOM 提取可交互元素及其边界框坐标
- 智能决策:多模态 LLM 观察截图 + 元素列表,决策下一步动作
- 状态恢复:每步截图落盘,支持从任意步骤恢复执行
- 安全边界:最大步数限制、操作类型白名单、元素越界校验
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app.tools.registry import tool
from pydantic import Field


@dataclass
class Element:
    """网页可交互元素。"""
    tag: str          # button, input, a, img, div...
    text: str         # 可见文本
    xpath: str        # 定位路径
    x: int            # 中心点 x
    y: int            # 中心点 y
    width: int        # 元素宽度
    height: int       # 元素高度
    role: str = ""    # aria-role
    name: str = ""    # aria-name / placeholder

    def to_dict(self) -> dict:
        return {
            "tag": self.tag, "text": self.text[:50],
            "xpath": self.xpath, "x": self.x, "y": self.y,
            "w": self.width, "h": self.height,
            "role": self.role, "name": self.name,
        }


class ComputerUseEngine:
    """Computer Use 引擎:截图→决策→操作闭环。"""

    def __init__(
        self,
        llm_client,
        screenshot_dir: str = "",
        max_steps: int = 15,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ):
        self._llm = llm_client
        self._screenshot_dir = screenshot_dir or os.path.join(
            os.path.expanduser("~"), ".agent-service", "computer_use_screenshots"
        )
        os.makedirs(self._screenshot_dir, exist_ok=True)
        self._max_steps = max_steps
        self._vp_w = viewport_width
        self._vp_h = viewport_height
        self._step_history: list[dict] = []
        self._state_file = os.path.join(self._screenshot_dir, "state.json")

    def _save_state(self) -> None:
        """保存当前执行状态(支持断点恢复)。"""
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "step_history": self._step_history,
                    "last_step": len(self._step_history),
                    "updated_at": time.time(),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_state(self) -> Optional[dict]:
        """加载上次执行状态。"""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    async def extract_elements(self, page) -> list[Element]:
        """从 DOM 提取可交互元素及其坐标。"""
        try:
            elements_data = await page.evaluate("""
            () => {
                const selectors = [
                    'a[href]', 'button', 'input', 'textarea', 'select',
                    '[role="button"]', '[role="link"]', '[role="textbox"]',
                    '.clickable', '[onclick]', '[tabindex="0"]'
                ];
                const els = document.querySelectorAll(selectors.join(','));
                return Array.from(els).slice(0, 50).map(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) return null;
                    return {
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
                        xpath: el.tagName.toLowerCase() + (el.id ? '#'+el.id : '') + (el.className ? '.'+el.className.split(' ')[0] : ''),
                        x: Math.round(r.left + r.width / 2),
                        y: Math.round(r.top + r.height / 2),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        role: el.getAttribute('role') || '',
                        name: el.getAttribute('aria-label') || el.placeholder || '',
                    };
                }).filter(Boolean);
            }
            """)
            return [Element(**e) for e in (elements_data or [])]
        except Exception:
            return []

    async def _vision_decision(
        self, goal: str, screenshot_b64: str, elements: list[Element], step: int
    ) -> dict:
        """多模态 LLM 决策下一步动作。"""
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "double_click", "type", "scroll", "right_click", "hotkey", "wait", "done"],
                },
                "x": {"type": "integer", "minimum": 0},
                "y": {"type": "integer", "minimum": 0},
                "text": {"type": "string"},
                "direction": {"type": "string", "enum": ["up", "down"]},
                "hotkey": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["action", "reason"],
        }

        elem_text = ""
        if elements:
            lines = []
            for i, e in enumerate(elements[:20]):
                label = e.name or e.text or e.tag
                lines.append(f"{i+1}. [{e.tag}] \"{label}\" at ({e.x},{e.y}) {e.w}x{e.h}")
            elem_text = "\n可交互元素:\n" + "\n".join(lines)

        messages = [
            {
                "role": "system",
                "content": (
                    f"你是 Computer Use 代理,目标: {goal}\n"
                    "你每次只能执行一个动作。根据截图和元素列表决策。\n"
                    "坐标为视口内像素坐标,原点在左上角。\n"
                    "完成目标或无法继续时 action=done。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"第 {step} 步。{elem_text}\n请决策下一步动作:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                ],
            },
        ]

        try:
            result = await self._llm.chat_json_schema(messages, schema, temperature=0.1)
            return result if isinstance(result, dict) else {"action": "done", "reason": "决策解析失败"}
        except Exception as e:
            return {"action": "done", "reason": f"决策失败: {e}"}

    async def run(
        self,
        page,
        goal: str,
        max_steps: int = 15,
        resume_from_step: int = 0,
    ) -> dict:
        """执行 Computer Use 闭环,返回轨迹。"""
        self._step_history = []
        steps = max_steps or self._max_steps
        last_screenshot = ""

        for step in range(1, steps + 1):
            if step <= resume_from_step:
                continue  # 跳过已完成的步骤

            # 1. 截图
            try:
                shot = await page.screenshot(full_page=False, type="png")
                b64 = base64.b64encode(shot).decode()
                last_screenshot = b64
                # 保存截图到文件
                shot_file = os.path.join(self._screenshot_dir, f"step_{step:03d}.png")
                try:
                    with open(shot_file, "wb") as f:
                        f.write(shot)
                except Exception:
                    pass
            except Exception as e:
                return {"status": "error", "error": f"截图失败: {e}", "steps": self._step_history}

            # 2. 提取元素
            try:
                elements = await self.extract_elements(page)
            except Exception:
                elements = []

            # 3. LLM 决策
            decision = await self._vision_decision(goal, b64, elements, step)
            action = decision.get("action", "done")
            reason = decision.get("reason", "")

            entry = {"step": step, "action": action, "reason": reason, "screenshot": b64[:100]}
            if elements:
                entry["elements_count"] = len(elements)
            self._step_history.append(entry)
            self._save_state()

            if action == "done":
                break

            # 4. 执行动作
            try:
                if action == "click":
                    x = min(max(int(decision.get("x", 0)), 0), self._vp_w - 1)
                    y = min(max(int(decision.get("y", 0)), 0), self._vp_h - 1)
                    await page.mouse.click(x, y)
                elif action == "double_click":
                    x = min(max(int(decision.get("x", 0)), 0), self._vp_w - 1)
                    y = min(max(int(decision.get("y", 0)), 0), self._vp_h - 1)
                    await page.mouse.dblclick(x, y)
                elif action == "right_click":
                    x = min(max(int(decision.get("x", 0)), 0), self._vp_w - 1)
                    y = min(max(int(decision.get("y", 0)), 0), self._vp_h - 1)
                    await page.mouse.click(x, y, button="right")
                elif action == "type":
                    text = decision.get("text", "")
                    await page.keyboard.type(text)
                elif action == "scroll":
                    direction = decision.get("direction", "down")
                    delta = -300 if direction == "up" else 300
                    await page.mouse.wheel(0, delta)
                elif action == "hotkey":
                    key = decision.get("hotkey", "")
                    await page.keyboard.press(key)
                elif action == "wait":
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(0.5)
            except Exception as e:
                entry["error"] = str(e)

        # 清理状态文件
        try:
            os.remove(self._state_file)
        except FileNotFoundError:
            pass

        return {
            "status": "ok",
            "goal": goal,
            "total_steps": len(self._step_history),
            "traces": [
                {
                    "step": t["step"],
                    "action": t["action"],
                    "reason": t["reason"],
                    "elements_found": t.get("elements_count", 0),
                    "error": t.get("error", ""),
                }
                for t in self._step_history
            ],
            "final_screenshot_base64": last_screenshot,
        }


# ── 工具注册 ──────────────────────────────────────────────────────
@tool(
    "computer_use",
    "Computer Use 视觉驾驶:截图→多模态 LLM 决策→坐标操作,完成网页任务。"
    "适合需要看着页面操作的复杂网页任务(填表单、点按钮、翻页、登录等)。",
    timeout=300,
)
async def computer_use(
    goal: str = Field(description="要完成的任务目标"),
    max_steps: int = Field(15, description="最大执行步数"),
    resume_step: int = Field(0, description="从第几步恢复(0=从头开始)"),
) -> dict:
    """Computer Use 入口工具。"""
    from app.tools.browser_tool import _get_session
    from app.core.llm import LLMClient

    try:
        session = await _get_session()
    except RuntimeError:
        return {"status": "error", "error": "浏览器未启动,请先调用 browser_open"}

    if not session.is_active:
        return {"status": "error", "error": "浏览器会话无效"}

    llm = LLMClient()
    engine = ComputerUseEngine(llm, max_steps=max_steps)
    page = session._page

    result = await engine.run(page, goal, max_steps=max_steps, resume_from_step=resume_step)
    return result


@tool(
    "computer_use_extract_elements",
    "提取当前页面的可交互元素列表(带坐标),供 computer_use 分析使用。",
    timeout=15,
)
async def computer_use_extract_elements() -> dict:
    """提取页面元素列表。"""
    from app.tools.browser_tool import _get_session
    from app.core.llm import LLMClient

    try:
        session = await _get_session()
        if not session.is_active:
            return {"error": "浏览器未启动"}
        engine = ComputerUseEngine(LLMClient())
        elements = await engine.extract_elements(session._page)
        return {
            "status": "ok",
            "elements": [e.to_dict() for e in elements],
            "count": len(elements),
        }
    except Exception as e:
        return {"error": f"提取元素失败: {e}"}
