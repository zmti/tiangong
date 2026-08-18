"""增强型自主规划与执行引擎:复杂任务自动分解、工具链编排、状态恢复。

对标 Claude Code / Devin / Manus 的自主规划能力:
1. 任务分解:LLM 自动将复杂任务拆分为可执行子步骤
2. 工具链编排:根据子步骤自动选择并编排工具调用顺序
3. 状态恢复:中断后从断点续跑,保留完整执行轨迹
4. 动态重规划:执行失败时自动重新规划
5. 并行扇出:独立子任务并行执行(map-reduce)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings
from app.core.llm import LLMClient


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class TaskType(str, Enum):
    SIMPLE = "simple"           # 单步问答
    SEQUENTIAL = "sequential"   # 有序多步
    PARALLEL = "parallel"       # 可并行子任务
    COMPLEX = "complex"         # 需要循环/条件


@dataclass
class PlanStep:
    """单个计划步骤。"""
    id: str = ""
    title: str = ""
    description: str = ""
    step_type: str = "normal"   # normal / tool_call / parallel_branch / condition
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    status: str = "pending"
    result: str = ""
    error: str = ""
    retry_count: int = 0
    max_retries: int = 2
    dependencies: list[str] = field(default_factory=list)  # 前置步骤 ID
    parallel_group: str = ""   # 并行组 ID(同组可并发执行)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "step_type": self.step_type,
            "tool_name": self.tool_name,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "dependencies": self.dependencies,
            "parallel_group": self.parallel_group,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlanStep":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            step_type=data.get("step_type", "normal"),
            tool_name=data.get("tool_name", ""),
            tool_args=data.get("tool_args", {}),
            status=data.get("status", "pending"),
            result=data.get("result", ""),
            error=data.get("error", ""),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 2),
            dependencies=data.get("dependencies", []),
            parallel_group=data.get("parallel_group", ""),
        )


@dataclass
class ExecutionPlan:
    """执行计划:步骤列表 + 执行状态。"""
    task_id: str = ""
    task_description: str = ""
    task_type: str = "sequential"
    steps: list[PlanStep] = field(default_factory=list)
    current_index: int = 0
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    status: str = "pending"  # pending / running / paused / completed / failed

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_description": self.task_description,
            "task_type": self.task_type,
            "steps": [s.to_dict() for s in self.steps],
            "current_index": self.current_index,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionPlan":
        return cls(
            task_id=data.get("task_id", ""),
            task_description=data.get("task_description", ""),
            task_type=data.get("task_type", "sequential"),
            steps=[PlanStep.from_dict(s) for s in data.get("steps", [])],
            current_index=data.get("current_index", 0),
            created_at=data.get("created_at", 0.0),
            started_at=data.get("started_at", 0.0),
            completed_at=data.get("completed_at", 0.0),
            status=data.get("status", "pending"),
        )

    def is_complete(self) -> bool:
        return self.status in ("completed", "failed")

    def is_running(self) -> bool:
        return self.status == "running"

    def summary(self) -> str:
        """返回计划步骤摘要(与 TaskPlan.summary 兼容)。"""
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


# ── 规划提示词 ─────────────────────────────────────────────────────
PLAN_PROMPT = """你是任务规划专家。分析用户任务并生成可执行的计划。

## 任务类型判断
- simple: 单步问答、简单计算、直接查询 → 返回 {{"task_type": "simple"}}
- sequential: 需要多个有序步骤,前后有依赖 → 返回有序步骤列表
- parallel: 多个独立子任务可并发执行 → 按 parallel_group 分组
- complex: 需要循环、条件分支、异常处理的复杂任务 → 返回详细步骤含条件逻辑

## 输出格式(JSON,不要其他文字)
{{
  "task_type": "simple|sequential|parallel|complex",
  "steps": [
    {{
      "title": "步骤短标题",
      "description": "具体做什么、预期结果",
      "step_type": "normal|tool_call|parallel_branch|condition",
      "tool_name": "工具名(仅 tool_call 类型)",
      "tool_args": {{}},
      "dependencies": [],
      "parallel_group": ""
    }}
  ]
}}

## 要求
- 步骤严格有序,后续依赖前面结果
- 描述具体可执行
- 不要把"总结回答"作为单独步骤
- parallel_group 相同 ID 的步骤可并发执行
- 每步最多 8 个

当前时间: {now}
"""


REPLAN_PROMPT = """任务执行中遇到失败,请重新规划。

原始任务: {task}
当前进度: {progress}
失败信息: {failures}

请生成修订后的执行计划(只返回 JSON,不要其他文字):
{{
  "task_type": "sequential|parallel|complex",
  "steps": [...]
}}
"""


# ── 规划器 ────────────────────────────────────────────────────────
async def generate_execution_plan(
    llm: LLMClient,
    user_input: str,
    max_steps: int = 10,
) -> Optional[ExecutionPlan]:
    """让 LLM 生成可执行的计划。简单任务返回 None。"""
    if getattr(llm, "provider", "") == "mock":
        return None

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prompt = [
        {"role": "system", "content": PLAN_PROMPT.format(now=now, max_steps=max_steps)},
        {"role": "user", "content": user_input},
    ]

    try:
        if hasattr(llm, "chat_json"):
            data = await llm.chat_json(prompt, temperature=0.2)
        else:
            resp = await llm.chat(prompt, temperature=0.2)
            data = _parse_json_object(resp)
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(data, dict):
        return None

    task_type = data.get("task_type", "sequential")
    raw_steps = data.get("steps", [])

    if not raw_steps:
        return ExecutionPlan(
            task_id="",
            task_description=user_input,
            task_type="simple",
        )

    steps = []
    for i, s in enumerate(raw_steps[:max_steps]):
        if not isinstance(s, dict):
            continue
        step = PlanStep(
            id=f"step_{i}",
            title=str(s.get("title", f"步骤{i+1}"))[:100],
            description=str(s.get("description", ""))[:500],
            step_type=s.get("step_type", "normal"),
            tool_name=s.get("tool_name", ""),
            tool_args=s.get("tool_args", {}),
            dependencies=s.get("dependencies", []),
            parallel_group=s.get("parallel_group", ""),
        )
        steps.append(step)

    if not steps:
        return None

    plan = ExecutionPlan(
        task_id="",
        task_description=user_input,
        task_type=task_type,
        steps=steps,
        created_at=time.time(),
    )
    return plan


async def replan_on_failure(
    llm: LLMClient,
    original_task: str,
    progress: str,
    failures: str,
) -> Optional[ExecutionPlan]:
    """基于失败原因重新规划。"""
    prompt = [
        {"role": "system", "content": REPLAN_PROMPT.format(
            task=original_task, progress=progress, failures=failures,
        )},
        {"role": "user", "content": "请生成新的执行计划。"},
    ]

    try:
        if hasattr(llm, "chat_json"):
            data = await llm.chat_json(prompt, temperature=0.3)
        else:
            resp = await llm.chat(prompt, temperature=0.3)
            data = _parse_json_object(resp)
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(data, dict) or not data.get("steps"):
        return None

    steps = []
    for i, s in enumerate(data["steps"][:8]):
        if not isinstance(s, dict):
            continue
        steps.append(PlanStep(
            id=f"step_{i}",
            title=str(s.get("title", f"步骤{i+1}"))[:100],
            description=str(s.get("description", ""))[:500],
            step_type=s.get("step_type", "normal"),
        ))

    if not steps:
        return None

    return ExecutionPlan(
        task_id="",
        task_description=original_task,
        task_type=data.get("task_type", "sequential"),
        steps=steps,
        created_at=time.time(),
    )


# ── 并行执行 ──────────────────────────────────────────────────────
async def execute_parallel_group(
    steps: list[PlanStep],
    tool_executor: Callable,
    max_concurrent: int = 5,
) -> list[tuple[PlanStep, str]]:
    """并行执行同一 parallel_group 的 steps。

    返回 [(step, result), ...] 按原顺序排列。
    """
    results: list[tuple[PlanStep, str]] = [("", "")] * len(steps)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _exec_one(idx: int, step: PlanStep) -> tuple[int, str]:
        async with semaphore:
            try:
                result = await tool_executor(step.tool_name, step.tool_args)
                return idx, result
            except Exception as e:
                return idx, f"[错误] {e}"

    tasks = [_exec_one(i, s) for i, s in enumerate(steps)]
    completed = await asyncio.gather(*tasks)

    for idx, result in completed:
        results[idx] = (steps[idx], result)

    return results


# ── 执行引擎 ──────────────────────────────────────────────────────
class ExecutionEngine:
    """自主执行引擎:按计划执行步骤,支持断点续跑。"""

    def __init__(
        self,
        llm: LLMClient,
        tool_executor: Callable,
        max_replan_times: int = 2,
    ):
        self.llm = llm
        self.tool_executor = tool_executor
        self.max_replan_times = max_replan_times
        self._replan_count = 0

    async def execute(
        self,
        plan: ExecutionPlan,
        user_input: str,
        checkpoint_fn: Optional[Callable] = None,
    ) -> ExecutionPlan:
        """执行计划,返回最终状态。"""
        if not plan.steps:
            plan.status = "completed"
            plan.completed_at = time.time()
            return plan

        plan.status = "running"
        plan.started_at = time.time()

        # 按依赖关系拓扑排序
        ordered = self._topological_sort(plan.steps)

        for i, step in enumerate(ordered):
            plan.current_index = i

            # 保存 checkpoint
            if checkpoint_fn:
                try:
                    checkpoint_fn(plan)
                except Exception:  # noqa: BLE001
                    pass

            # 执行步骤
            if step.step_type == "parallel_branch":
                # 并行组执行
                group_steps = [s for s in ordered if s.parallel_group == step.parallel_group]
                group_results = await execute_parallel_group(group_steps, self.tool_executor)
                for gs, result in group_results:
                    gs.status = StepStatus.DONE
                    gs.result = result
                step.status = StepStatus.DONE
                continue

            if step.step_type == "tool_call" and step.tool_name:
                success = False
                for attempt in range(step.max_retries + 1):
                    step.status = StepStatus.IN_PROGRESS
                    step.retry_count = attempt
                    try:
                        result = await self.tool_executor(step.tool_name, step.tool_args)
                        step.status = StepStatus.DONE
                        step.result = result
                        success = True
                        break
                    except Exception as e:
                        step.error = str(e)
                        if attempt < step.max_retries:
                            step.status = StepStatus.RETRYING
                            await asyncio.sleep(1 * (2 ** attempt))
                        else:
                            step.status = StepStatus.FAILED
                if not success:
                    # 尝试重新规划
                    if self._replan_count < self.max_replan_times:
                        self._replan_count += 1
                        new_plan = await replan_on_failure(
                            self.llm, user_input,
                            self._get_progress(plan),
                            step.error,
                        )
                        if new_plan:
                            return await self.execute(new_plan, user_input, checkpoint_fn)
                    plan.status = "failed"
                    plan.completed_at = time.time()
                    return plan
            else:
                # 普通步骤(由外层 agent 处理)
                step.status = StepStatus.DONE

        plan.status = "completed"
        plan.completed_at = time.time()
        return plan

    def _topological_sort(self, steps: list[PlanStep]) -> list[PlanStep]:
        """按依赖关系拓扑排序步骤。"""
        step_map = {s.id: s for s in steps}
        visited = set()
        ordered = []

        def _visit(sid: str):
            if sid in visited:
                return
            visited.add(sid)
            step = step_map.get(sid)
            if step:
                for dep in step.dependencies:
                    _visit(dep)
                ordered.append(step)

        for s in steps:
            _visit(s.id)

        return ordered

    def _get_progress(self, plan: ExecutionPlan) -> str:
        completed = [s for s in plan.steps if s.status == StepStatus.DONE]
        failed = [s for s in plan.steps if s.status == StepStatus.FAILED]
        lines = []
        for s in completed:
            lines.append(f"[完成] {s.title}: {s.result[:200]}")
        for s in failed:
            lines.append(f"[失败] {s.title}: {s.error}")
        return "\n".join(lines) if lines else "无进度"


# ── 工具 ──────────────────────────────────────────────────────────
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
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None
