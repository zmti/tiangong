"""计划工具:set_plan / update_plan,供 Agent 在 ReAct 循环中维护任务计划。

在 Agent 循环内,这两个工具的调用会被 Agent 拦截并更新其私有计划状态
(见 app/core/agent.py 的 _execute_plan_tool);
此处提供全局兜底实现,保证通过 /v1/tools 直接调用时也可用。
"""
from __future__ import annotations

import json

from app.core.planner import TaskPlan, apply_plan_tool
from app.tools.registry import tool

_ctx_plan: TaskPlan | None = None


@tool(
    "set_plan",
    "为当前任务创建或修订执行计划。steps: 步骤数组,每项为对象 {title: 步骤短标题, description: 步骤说明};最多 8 步。"
    "仅当任务需要多步骤执行时使用;简单任务不要调用。",
    timeout=10,
)
def set_plan(steps: list[dict], current: int = 0) -> dict:
    """创建/修订任务计划。实际状态由 Agent 维护,此函数为兜底实现。"""
    global _ctx_plan
    _ctx_plan, result = apply_plan_tool(
        _ctx_plan, "set_plan", json.dumps({"steps": steps, "current": current})
    )
    return result


@tool(
    "update_plan",
    "更新任务计划进度。done: 已完成步骤的索引列表(从 0 开始,例如 [0,1]);"
    "current: 当前进行中的步骤索引(从 0 开始,省略则自动推进到下一个待办步骤)。",
    timeout=10,
)
def update_plan(done: list[int] = [], current: int = -1) -> dict:
    """更新计划进度。实际状态由 Agent 维护,此函数为兜底实现。"""
    global _ctx_plan
    args: dict = {"done": done}
    if isinstance(current, int) and current >= 0:
        args["current"] = current
    _ctx_plan, result = apply_plan_tool(_ctx_plan, "update_plan", json.dumps(args))
    return result
