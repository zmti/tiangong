"""任务规划器:复杂任务先分解步骤,再逐步执行(Plan-then-Execute)。

对标主流智能体(Claude Code / Devin / Manus)的规划能力:
1. 规划阶段:LLM 判断任务复杂度,复杂则产出有序步骤(<=6 步)
2. 执行阶段:ReAct 循环中通过 set_plan / update_plan 工具更新进度
3. 失败修订:允许 LLM 用 set_plan 重构剩余步骤
"""
from __future__ import annotations

import datetime
import json
from typing import Optional

PLAN_STEP_STATUS = ("pending", "in_progress", "done", "failed", "skipped")

PLAN_PROMPT = """你是任务规划器。判断用户任务是否需要多步骤规划。

- 如果任务简单(单步问答、无需工具或只需一个工具的简单请求),返回 {{"needs_plan": false}}
- 如果任务复杂(需要多个工具、多个阶段、文件操作、调查分析、代码编写等),返回 {{"needs_plan": true, "steps": [...]}}

steps 为 1-{max_steps} 个有序步骤,每步格式:
  {{"title": "步骤短标题", "description": "该步骤做什么、预期结果"}}

要求:
- 步骤严格有序,后续步骤依赖前面步骤的结果
- 描述具体可执行,便于检查是否完成
- 不要把"总结回答"作为单独步骤

只输出 JSON,不要任何其他文字。

当前时间: {now}
"""


class PlanStep:
    """单个计划步骤。"""

    def __init__(self, title: str, description: str = "", status: str = "pending"):
        self.title = title
        self.description = description
        self.status = status

    def to_dict(self) -> dict:
        return {"title": self.title, "description": self.description, "status": self.status}


class TaskPlan:
    """任务计划:步骤列表 + 当前步骤索引。"""

    def __init__(self, steps: list[PlanStep], current: int = 0):
        self.steps = steps
        self.current = min(current, len(steps) - 1) if steps else 0

    def set_current(self, index: int) -> None:
        """将指定步骤设为进行中;之前的未完成步骤自动标记完成。"""
        if not self.steps:
            return
        idx = max(0, min(int(index), len(self.steps) - 1))
        for i in range(idx):
            if self.steps[i].status in ("pending", "in_progress"):
                self.steps[i].status = "done"
        self.steps[idx].status = "in_progress"
        self.current = idx

    def mark_done(self, indices: list[int]) -> list[int]:
        """把指定步骤标记完成,并推进到下一个待办步骤。返回实际变更的索引。"""
        changed = []
        for i in indices:
            if 0 <= i < len(self.steps) and self.steps[i].status != "done":
                self.steps[i].status = "done"
                changed.append(i)
        if changed:
            self._advance()
        return changed

    def mark_failed(self, index: int) -> None:
        if 0 <= index < len(self.steps):
            self.steps[index].status = "failed"

    def is_complete(self) -> bool:
        return bool(self.steps) and all(s.status in ("done", "skipped", "failed") for s in self.steps)

    def _advance(self) -> None:
        for i, s in enumerate(self.steps):
            if s.status == "pending":
                s.status = "in_progress"
                self.current = i
                return
        self.current = len(self.steps) - 1 if self.steps else 0

    def summary(self) -> str:
        status_text = {
            "pending": "待办",
            "in_progress": "进行中",
            "done": "已完成",
            "failed": "失败",
            "skipped": "跳过",
        }
        lines = []
        for i, s in enumerate(self.steps):
            lines.append(f"{i + 1}. [{status_text.get(s.status, s.status)}] {s.title}: {s.description}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "current": self.current,
            "completed": self.is_complete(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskPlan":
        steps = [
            PlanStep(s.get("title", ""), s.get("description", ""), s.get("status", "pending"))
            for s in data.get("steps", [])
        ]
        return cls(steps, data.get("current", 0))


def _normalize_steps(raw: list, max_steps: int) -> list[PlanStep]:
    steps = []
    for s in raw[:max_steps]:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or s.get("name") or "").strip()[:100]
        desc = str(s.get("description") or "").strip()[:500]
        if not title and not desc:
            continue
        steps.append(PlanStep(title or desc, desc))
    return steps


def _parse_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


async def generate_plan(llm, user_input: str, max_steps: int = 6) -> Optional[TaskPlan]:
    """让 LLM 判断任务复杂度并生成计划。简单任务返回 None。"""
    from app.core.llm import LLMError

    if getattr(llm, "provider", "") == "mock":
        return None
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = [
        {"role": "system", "content": PLAN_PROMPT.format(max_steps=max_steps, now=now)},
        {"role": "user", "content": user_input},
    ]
    try:
        if hasattr(llm, "chat_json"):
            data = await llm.chat_json(prompt, temperature=0.2)
        else:
            data = _parse_json_object(await llm.chat(prompt, temperature=0.2))
    except LLMError:
        return None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not data.get("needs_plan"):
        return None
    steps = _normalize_steps(data.get("steps") or [], max_steps)
    if not steps:
        return None
    return TaskPlan(steps)


def apply_plan_tool(
    plan: Optional[TaskPlan], name: str, arguments: str | dict
) -> tuple[Optional[TaskPlan], dict]:
    """执行计划工具(set_plan/update_plan),返回 (新计划, 结果 dict)。"""
    if name == "set_plan":
        return _apply_set_plan(arguments, plan)
    if name == "update_plan":
        return _apply_update_plan(arguments, plan)
    return plan, {"error": f"未知计划工具: {name}"}


def _apply_set_plan(arguments: str | dict, plan: Optional[TaskPlan]) -> tuple[TaskPlan, dict]:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return plan, {"error": "参数解析失败"}  # type: ignore[return-value]
    if not isinstance(args, dict):
        return plan, {"error": "参数必须是 JSON 对象"}  # type: ignore[return-value]
    raw = args.get("steps", [])
    steps = _normalize_steps(raw, 8)
    if not steps:
        return plan, {"error": "计划步骤不能为空"}  # type: ignore[return-value]
    new_plan = TaskPlan(steps, args.get("current", 0) if isinstance(args.get("current", 0), int) else 0)
    new_plan.set_current(args.get("current", 0) if isinstance(args.get("current", 0), int) else 0)
    return new_plan, new_plan.to_dict()


def _apply_update_plan(
    arguments: str | dict, plan: Optional[TaskPlan]
) -> tuple[Optional[TaskPlan], dict]:
    if plan is None:
        return plan, {"error": "当前没有进行中的计划,请先用 set_plan 创建"}
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return plan, {"error": "参数解析失败"}
    if not isinstance(args, dict):
        return plan, {"error": "参数必须是 JSON 对象"}
    done = args.get("done") or []
    if not isinstance(done, list):
        done = [done]
    done = [int(i) for i in done if isinstance(i, (int, float))]
    changed = plan.mark_done(done)
    cur = args.get("current")
    if isinstance(cur, (int, float)) and int(cur) >= 0:
        plan.set_current(int(cur))
    result = plan.to_dict()
    result["changed"] = changed
    return plan, result
