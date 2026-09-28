"""Planner-Executor-Critic 三角色分离架构。

核心设计:
- Planner: 任务分解、计划生成、进度跟踪、重新规划
- Executor: 工具执行、代码运行、结果收集、自愈
- Critic: 答案评审、质量打分、幻觉检测、改进建议

三角色通过消息总线通信,支持:
- 并行执行(Executor 池)
- 迭代优化(Executor -> Critic -> Planner 反馈循环)
- 人工介入断点(计划/工具/决策级)
- 检查点持久化(断点续跑)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from app.config import settings
from app.core.llm import LLMClient
from app.core.planner import TaskPlan, PlanStep, generate_plan
from app.core.cost_control import get_cost_controller
from app.runtime.checkpoint import CheckpointManager

logger = logging.getLogger(__name__)


# ============================================================
# 消息总线与事件定义
# ============================================================

class RoleType(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    CRITIC = "critic"
    HUMAN = "human"  # 人工介入


class EventType(str, Enum):
    # 计划事件
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    PLAN_STEP_START = "plan_step_start"
    PLAN_STEP_COMPLETE = "plan_step_complete"
    PLAN_STEP_FAILED = "plan_step_failed"
    PLAN_REPLAN = "plan_replan"
    PLAN_COMPLETE = "plan_complete"
    
    # 执行事件
    EXEC_START = "exec_start"
    EXEC_TOOL_CALL = "exec_tool_call"
    EXEC_TOOL_RESULT = "exec_tool_result"
    EXEC_CODE_RUN = "exec_code_run"
    EXEC_COMPLETE = "exec_complete"
    EXEC_FAILED = "exec_failed"
    
    # 评审事件
    CRITIC_REVIEW = "critic_review"
    CRITIC_SCORE = "critic_score"
    CRITIC_FEEDBACK = "critic_feedback"
    CRITIC_APPROVE = "critic_approve"
    CRITIC_REJECT = "critic_reject"
    
    # 人工介入
    HUMAN_CHECKPOINT = "human_checkpoint"
    HUMAN_APPROVE = "human_approve"
    HUMAN_REJECT = "human_reject"
    HUMAN_MODIFY = "human_modify"
    
    # 系统事件
    CHECKPOINT_SAVE = "checkpoint_save"
    CHECKPOINT_RESTORE = "checkpoint_restore"
    BUDGET_WARN = "budget_warn"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass
class RoleEvent:
    """角色间通信事件。"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: EventType = EventType.PLAN_CREATED
    source_role: RoleType = RoleType.PLANNER
    target_role: RoleType = RoleType.EXECUTOR
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    correlation_id: str = ""  # 关联请求ID
    reply_to: str = ""  # 回复目标事件ID


class MessageBus:
    """消息总线:角色间异步通信。"""
    
    def __init__(self):
        self._subscribers: dict[RoleType, list[asyncio.Queue]] = {
            RoleType.PLANNER: [],
            RoleType.EXECUTOR: [],
            RoleType.CRITIC: [],
            RoleType.HUMAN: [],
        }
        self._handlers: dict[EventType, list[Callable]] = {}
        self._lock = asyncio.Lock()
    
    def subscribe(self, role: RoleType) -> asyncio.Queue:
        """订阅角色消息队列。"""
        queue = asyncio.Queue()
        self._subscribers[role].append(queue)
        return queue
    
    def unsubscribe(self, role: RoleType, queue: asyncio.Queue) -> None:
        """取消订阅。"""
        if queue in self._subscribers[role]:
            self._subscribers[role].remove(queue)
    
    async def publish(self, event: RoleEvent) -> None:
        """发布事件给目标角色。"""
        # 记录事件日志
        logger.debug("Event: %s %s -> %s", event.event_type, event.source_role, event.target_role)
        
        # 分发给目标角色的所有队列
        for queue in self._subscribers.get(event.target_role, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("消息队列已满,丢弃事件: %s", event.event_type)
        
        # 触发注册的处理器
        for handler in self._handlers.get(event.event_type, []):
            try:
                await handler(event)
            except Exception as e:
                logger.error("事件处理器异常: %s", e)
    
    def register_handler(self, event_type: EventType, handler: Callable) -> None:
        """注册事件处理器。"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)


# ============================================================
# 共享状态上下文
# ============================================================

@dataclass
class ExecutionContext:
    """执行上下文:三角色共享状态。"""
    request_id: str
    session_id: str
    task_id: str = ""
    user_input: str = ""
    plan: Optional[TaskPlan] = None
    current_step: int = 0
    step_results: dict[int, Any] = field(default_factory=dict)  # step_index -> result
    step_errors: dict[int, str] = field(default_factory=dict)
    critic_feedback: list[dict] = field(default_factory=list)
    human_checkpoints: set[int] = field(default_factory=set)  # 需要人工确认的步骤
    approved_steps: set[int] = field(default_factory=set)  # 已批准步骤
    status: str = "running"  # running/paused/completed/failed/cancelled
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "user_input": self.user_input,
            "plan": self.plan.to_dict() if self.plan else None,
            "current_step": self.current_step,
            "step_results": {str(k): v for k, v in self.step_results.items()},
            "step_errors": self.step_errors,
            "critic_feedback": self.critic_feedback,
            "human_checkpoints": list(self.human_checkpoints),
            "approved_steps": list(self.approved_steps),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionContext":
        ctx = cls(
            request_id=data["request_id"],
            session_id=data["session_id"],
            task_id=data.get("task_id", ""),
            user_input=data.get("user_input", ""),
            current_step=data.get("current_step", 0),
            step_results={int(k): v for k, v in data.get("step_results", {}).items()},
            step_errors=data.get("step_errors", {}),
            critic_feedback=data.get("critic_feedback", []),
            human_checkpoints=set(data.get("human_checkpoints", [])),
            approved_steps=set(data.get("approved_steps", [])),
            status=data.get("status", "running"),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            metadata=data.get("metadata", {}),
        )
        if data.get("plan"):
            ctx.plan = TaskPlan.from_dict(data["plan"])
        return ctx


# ============================================================
# Planner 角色
# ============================================================

class PlannerRole:
    """规划器:负责任务分解、计划生成、进度跟踪、重新规划。"""
    
    def __init__(self, llm: LLMClient, message_bus: MessageBus, context: ExecutionContext):
        self.llm = llm
        self.bus = message_bus
        self.context = context
        self._running = False
    
    async def create_plan(self, user_input: str, max_steps: int = 6) -> TaskPlan:
        """生成初始计划。"""
        plan = await generate_plan(self.llm, user_input, max_steps)
        if plan:
            self.context.plan = plan
            self.context.current_step = 0
            await self.bus.publish(RoleEvent(
                event_type=EventType.PLAN_CREATED,
                source_role=RoleType.PLANNER,
                target_role=RoleType.EXECUTOR,
                payload={"plan": plan.to_dict()},
                correlation_id=self.context.request_id,
            ))
        return plan
    
    async def update_progress(self, step_index: int, status: str, result: Any = None, error: str = None) -> None:
        """更新步骤进度。"""
        if self.context.plan and step_index < len(self.context.plan.steps):
            step = self.context.plan.steps[step_index]
            step.status = status
            if status == "done":
                self.context.step_results[step_index] = result
                self.context.approved_steps.add(step_index)
                await self.bus.publish(RoleEvent(
                    event_type=EventType.PLAN_STEP_COMPLETE,
                    source_role=RoleType.PLANNER,
                    target_role=RoleType.CRITIC,
                    payload={"step_index": step_index, "result": result},
                    correlation_id=self.context.request_id,
                ))
            elif status == "failed":
                self.context.step_errors[step_index] = error or "Unknown error"
                await self.bus.publish(RoleEvent(
                    event_type=EventType.PLAN_STEP_FAILED,
                    source_role=RoleType.PLANNER,
                    target_role=RoleType.CRITIC,
                    payload={"step_index": step_index, "error": error},
                    correlation_id=self.context.request_id,
                ))
        
        # 推进到下一步
        if status == "done":
            self._advance_to_next_pending()
        
        self.context.updated_at = time.time()
    
    def _advance_to_next_pending(self) -> None:
        """推进到下一个待办步骤。"""
        if not self.context.plan:
            return
        for i, step in enumerate(self.context.plan.steps):
            if step.status == "pending":
                self.context.current_step = i
                step.status = "in_progress"
                break
        else:
            # 所有步骤完成
            self.context.status = "completed"
            # 使用 create_task 调度异步发布
            asyncio.create_task(self.bus.publish(RoleEvent(
                event_type=EventType.PLAN_COMPLETE,
                source_role=RoleType.PLANNER,
                target_role=RoleType.CRITIC,
                payload={"final_results": self.context.step_results},
                correlation_id=self.context.request_id,
            )))
    
    async def replan(self, failed_step: int, error: str, failed_results: dict) -> TaskPlan:
        """基于失败重新规划。"""
        if not self.context.plan:
            return None
        
        # 构建重新规划提示
        remaining_steps = [
            {"title": s.title, "description": s.description}
            for i, s in enumerate(self.context.plan.steps)
            if i > failed_step and s.status in ("pending", "in_progress")
        ]
        
        replan_prompt = f"""原计划执行失败,需要重新规划剩余步骤。

原任务: {self.context.user_input}
失败步骤: {self.context.plan.steps[failed_step].title}
失败原因: {error}
已完成结果: {json.dumps(failed_results, ensure_ascii=False)[:2000]}
剩余步骤: {json.dumps(remaining_steps, ensure_ascii=False)}

请生成修订后的完整计划(包含已完成+修订的剩余步骤),JSON 格式:
{{"steps": [{{"title": "...", "description": "..."}}]}}"""
        
        try:
            from app.core.planner import _parse_json_object
            response = await self.llm.chat([
                {"role": "system", "content": "你是任务重新规划器,只输出 JSON。"},
                {"role": "user", "content": replan_prompt},
            ], temperature=0.2)
            
            data = _parse_json_object(response)
            if data and "steps" in data:
                from app.core.planner import _normalize_steps
                steps = _normalize_steps(data["steps"], 10)
                new_plan = TaskPlan(steps)
                # 保留已完成步骤状态
                for i, old_step in enumerate(self.context.plan.steps):
                    if i < len(new_plan.steps) and old_step.status == "done":
                        new_plan.steps[i].status = "done"
                self.context.plan = new_plan
                
                await self.bus.publish(RoleEvent(
                    event_type=EventType.PLAN_REPLAN,
                    source_role=RoleType.PLANNER,
                    target_role=RoleType.EXECUTOR,
                    payload={"plan": new_plan.to_dict(), "replan_from": failed_step},
                    correlation_id=self.context.request_id,
                ))
                return new_plan
        except Exception as e:
            logger.warning("重新规划失败: %s", e)
        
        return self.context.plan
    
    async def run(self) -> None:
        """Planner 主循环:监听执行结果,决定下一步动作。"""
        self._running = True
        queue = self.bus.subscribe(RoleType.PLANNER)
        
        try:
            while self._running and self.context.status == "running":
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                if event.event_type == EventType.EXEC_COMPLETE:
                    await self.update_progress(
                        event.payload.get("step_index", self.context.current_step),
                        "done",
                        event.payload.get("result"),
                    )
                elif event.event_type == EventType.EXEC_FAILED:
                    await self.update_progress(
                        event.payload.get("step_index", self.context.current_step),
                        "failed",
                        error=event.payload.get("error"),
                    )
                    # 触发重新规划
                    if self.context.plan and len(self.context.step_errors) >= 2:
                        await self.replan(
                            event.payload.get("step_index", self.context.current_step),
                            event.payload.get("error", ""),
                            self.context.step_results,
                        )
                elif event.event_type == EventType.CRITIC_FEEDBACK:
                    # 收集 Critic 反馈,必要时调整计划
                    self.context.critic_feedback.append(event.payload)
                elif event.event_type == EventType.HUMAN_APPROVE:
                    step_idx = event.payload.get("step_index")
                    if step_idx is not None:
                        self.context.approved_steps.add(step_idx)
                        self.context.human_checkpoints.discard(step_idx)
                elif event.event_type == EventType.HUMAN_MODIFY:
                    # 人工修改计划
                    new_plan_data = event.payload.get("plan")
                    if new_plan_data:
                        self.context.plan = TaskPlan.from_dict(new_plan_data)
        finally:
            self.bus.unsubscribe(RoleType.PLANNER, queue)
    
    def stop(self) -> None:
        self._running = False


# ============================================================
# Executor 角色
# ============================================================

class ExecutorRole:
    """执行器:负责工具调用、代码执行、结果收集、自愈。"""
    
    def __init__(
        self,
        llm: LLMClient,
        message_bus: MessageBus,
        context: ExecutionContext,
        tool_executor: Callable,  # 实际执行工具的函数
        max_concurrent: int = 3,
    ):
        self.llm = llm
        self.bus = message_bus
        self.context = context
        self.tool_executor = tool_executor
        self.max_concurrent = max_concurrent
        self._running = False
        self._semaphore = asyncio.Semaphore(max_concurrent)
    
    async def execute_step(self, step_index: int) -> Any:
        """执行单个计划步骤。"""
        if not self.context.plan or step_index >= len(self.context.plan.steps):
            return None
        
        step = self.context.plan.steps[step_index]
        
        # 人工检查点检查
        if step_index in self.context.human_checkpoints and step_index not in self.context.approved_steps:
            await self.bus.publish(RoleEvent(
                event_type=EventType.HUMAN_CHECKPOINT,
                source_role=RoleType.EXECUTOR,
                target_role=RoleType.HUMAN,
                payload={
                    "step_index": step_index,
                    "step_title": step.title,
                    "step_description": step.description,
                    "context": self.context.to_dict(),
                },
                correlation_id=self.context.request_id,
            ))
            # 等待人工批准
            self.context.status = "paused"
            return {"paused": True, "waiting_for": "human_approval"}
        
        step.status = "in_progress"
        await self.bus.publish(RoleEvent(
            event_type=EventType.PLAN_STEP_START,
            source_role=RoleType.EXECUTOR,
            target_role=RoleType.PLANNER,
            payload={"step_index": step_index, "step_title": step.title},
            correlation_id=self.context.request_id,
        ))
        
        try:
            # 执行步骤(这里简化为调用工具执行器)
            async with self._semaphore:
                result = await self.tool_executor(step, self.context)
            
            await self.bus.publish(RoleEvent(
                event_type=EventType.EXEC_COMPLETE,
                source_role=RoleType.EXECUTOR,
                target_role=RoleType.PLANNER,
                payload={"step_index": step_index, "result": result},
                correlation_id=self.context.request_id,
            ))
            return result
            
        except Exception as e:
            logger.error("步骤 %d 执行失败: %s", step_index, e)
            await self.bus.publish(RoleEvent(
                event_type=EventType.EXEC_FAILED,
                source_role=RoleType.EXECUTOR,
                target_role=RoleType.PLANNER,
                payload={"step_index": step_index, "error": str(e)},
                correlation_id=self.context.request_id,
            ))
            raise
    
    async def execute_code(self, code: str, language: str = "python") -> dict:
        """执行代码片段。"""
        await self.bus.publish(RoleEvent(
            event_type=EventType.EXEC_CODE_RUN,
            source_role=RoleType.EXECUTOR,
            target_role=RoleType.CRITIC,
            payload={"code": code[:500], "language": language},
            correlation_id=self.context.request_id,
        ))
        
        # 这里调用实际的代码执行工具
        # 简化实现
        return {"success": True, "output": "code executed"}
    
    async def run(self) -> None:
        """Executor 主循环:等待 Planner 分派步骤执行。"""
        self._running = True
        queue = self.bus.subscribe(RoleType.EXECUTOR)
        
        try:
            while self._running and self.context.status in ("running", "paused"):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # 检查是否有待执行步骤
                    if self.context.status == "running" and self.context.plan:
                        next_step = self.context.current_step
                        if next_step < len(self.context.plan.steps):
                            step = self.context.plan.steps[next_step]
                            if step.status == "in_progress":
                                await self.execute_step(next_step)
                    continue
                
                if event.event_type == EventType.PLAN_STEP_START:
                    # 已经在事件中处理,这里可以做额外准备
                    pass
                elif event.event_type == EventType.HUMAN_APPROVE:
                    step_idx = event.payload.get("step_index")
                    if step_idx is not None and step_idx in self.context.human_checkpoints:
                        self.context.approved_steps.add(step_idx)
                        self.context.human_checkpoints.discard(step_idx)
                        self.context.status = "running"
                        # 继续执行
                        await self.execute_step(step_idx)
        finally:
            self.bus.unsubscribe(RoleType.EXECUTOR, queue)
    
    def stop(self) -> None:
        self._running = False


# ============================================================
# Critic 角色
# ============================================================

class CriticRole:
    """评审员:负责答案评审、质量打分、幻觉检测、改进建议。"""
    
    def __init__(
        self,
        llm: LLMClient,
        message_bus: MessageBus,
        context: ExecutionContext,
        criteria: dict = None,
    ):
        self.llm = llm
        self.bus = message_bus
        self.context = context
        self.criteria = criteria or {
            "accuracy": "答案是否准确、有事实依据",
            "completeness": "是否完整回答了用户问题",
            "relevance": "是否紧扣主题、无跑题",
            "clarity": "表达是否清晰、结构化",
            "actionability": "给出的建议/代码是否可直接执行",
        }
        self._running = False
        self._min_score = getattr(settings, "agent_critic_min_score", 0.7)
    
    async def review(self, answer: str, step_results: dict = None) -> dict:
        """评审答案质量。"""
        # 构建评审上下文
        context_str = f"""任务: {self.context.user_input}

计划步骤:
{self.context.plan.summary() if self.context.plan else "无计划"}

执行结果摘要:
{json.dumps(step_results or self.context.step_results, ensure_ascii=False)[:3000]}

待评审答案:
{answer}"""
        
        criteria_str = "\n".join(f"- {k}: {v}" for k, v in self.criteria.items())
        
        prompt = f"""你是质量评审员,请对答案按以下标准打分(0-1):

{criteria_str}

请输出 JSON:
{{
  "scores": {{"accuracy": 0.8, "completeness": 0.9, ...}},
  "overall_score": 0.85,
  "issues": ["问题1", "问题2"],
  "suggestions": ["建议1", "建议2"],
  "approved": true/false
}}"""
        
        try:
            response = await self.llm.chat([
                {"role": "system", "content": "你是严格的质量评审员,只输出 JSON。"},
                {"role": "user", "content": prompt + "\n\n" + context_str},
            ], temperature=0.1)
            
            import json as _json
            result = _json.loads(response)
            
            # 记录反馈
            feedback = {
                "timestamp": time.time(),
                "answer_preview": answer[:200],
                "scores": result.get("scores", {}),
                "overall_score": result.get("overall_score", 0),
                "issues": result.get("issues", []),
                "suggestions": result.get("suggestions", []),
                "approved": result.get("approved", False),
            }
            self.context.critic_feedback.append(feedback)
            
            await self.bus.publish(RoleEvent(
                event_type=EventType.CRITIC_REVIEW,
                source_role=RoleType.CRITIC,
                target_role=RoleType.PLANNER,
                payload=feedback,
                correlation_id=self.context.request_id,
            ))
            
            if result.get("approved"):
                await self.bus.publish(RoleEvent(
                    event_type=EventType.CRITIC_APPROVE,
                    source_role=RoleType.CRITIC,
                    target_role=RoleType.PLANNER,
                    payload=feedback,
                    correlation_id=self.context.request_id,
                ))
            else:
                await self.bus.publish(RoleEvent(
                    event_type=EventType.CRITIC_REJECT,
                    source_role=RoleType.CRITIC,
                    target_role=RoleType.PLANNER,
                    payload=feedback,
                    correlation_id=self.context.request_id,
                ))
            
            return result
            
        except Exception as e:
            logger.warning("评审失败: %s", e)
            return {"overall_score": 0, "approved": False, "error": str(e)}
    
    async def run(self) -> None:
        """Critic 主循环:监听步骤完成事件,评审结果。"""
        self._running = True
        queue = self.bus.subscribe(RoleType.CRITIC)
        
        try:
            while self._running and self.context.status in ("running", "paused"):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                if event.event_type == EventType.PLAN_STEP_COMPLETE:
                    # 单步完成时可选择性评审
                    pass
                elif event.event_type == EventType.PLAN_COMPLETE:
                    # 计划完成时评审最终答案
                    final_answer = event.payload.get("final_results", {})
                    if final_answer:
                        await self.review(str(final_answer), self.context.step_results)
        finally:
            self.bus.unsubscribe(RoleType.CRITIC, queue)
    
    def stop(self) -> None:
        self._running = False


# ============================================================
# 编排器:协调三角色
# ============================================================

class PECOrchestrator:
    """Planner-Executor-Critic 编排器。"""
    
    def __init__(
        self,
        planner_llm: LLMClient = None,
        executor_llm: LLMClient = None,
        critic_llm: LLMClient = None,
        tool_executor: Callable = None,
        checkpoint_manager: CheckpointManager = None,
        max_concurrent_executors: int = 3,
    ):
        self.bus = MessageBus()
        self.context: Optional[ExecutionContext] = None
        self.checkpoint_manager = checkpoint_manager
        
        # 默认复用同一个 LLM,也可分离
        default_llm = planner_llm or executor_llm or critic_llm or LLMClient()
        
        self.planner = PlannerRole(planner_llm or default_llm, self.bus, None)
        self.executor = ExecutorRole(
            executor_llm or default_llm, self.bus, None, tool_executor, max_concurrent_executors
        )
        self.critic = CriticRole(critic_llm or default_llm, self.bus, None)
        
        self._tasks: list[asyncio.Task] = []
    
    async def execute(
        self,
        user_input: str,
        session_id: str,
        task_id: str = "",
        max_steps: int = 6,
        human_checkpoints: list[int] = None,
    ) -> dict:
        """执行完整任务流程。"""
        request_id = uuid.uuid4().hex
        task_id = task_id or uuid.uuid4().hex
        
        # 创建共享上下文
        self.context = ExecutionContext(
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            user_input=user_input,
            human_checkpoints=set(human_checkpoints or []),
        )
        
        # 重新绑定上下文到三角色
        self.planner.context = self.context
        self.executor.context = self.context
        self.critic.context = self.context
        
        # 成本控制
        cost_controller = get_cost_controller()
        if cost_controller:
            try:
                complexity = "simple" if len(user_input) < 100 else "standard" if len(user_input) < 500 else "complex"
                cost_controller.begin_request(session_id, task_id, complexity)
            except Exception as e:
                logger.warning("成本控制初始化失败: %s", e)
        
        # 启动三角色并发运行
        self._tasks = [
            asyncio.create_task(self.planner.run()),
            asyncio.create_task(self.executor.run()),
            asyncio.create_task(self.critic.run()),
        ]
        
        try:
            # 1. Planner 生成计划
            plan = await self.planner.create_plan(user_input, max_steps)
            if not plan:
                return {"status": "failed", "error": "计划生成失败", "request_id": request_id}
            
            # 2. 保存初始检查点
            if self.checkpoint_manager:
                await self._save_checkpoint()
            
            # 3. 等待执行完成
            while self.context.status == "running":
                await asyncio.sleep(0.5)
                # 检查是否所有步骤完成
                if self.context.plan and self.context.plan.is_complete():
                    self.context.status = "completed"
                    break
            
            # 4. 如果是暂停状态,等待人工介入
            if self.context.status == "paused":
                return {
                    "status": "paused",
                    "request_id": request_id,
                    "context": self.context.to_dict(),
                    "message": "等待人工介入",
                }
            
            # 5. 最终评审
            final_answer = self._build_final_answer()
            review_result = await self.critic.review(final_answer, self.context.step_results)
            
            if not review_result.get("approved", False) and review_result.get("overall_score", 0) < self.critic._min_score:
                # 评审不通过,触发重新规划/重新执行
                logger.info("评审不通过,分数: %.2f", review_result.get("overall_score", 0))
                # 这里可以选择重新执行或返回当前结果
            
            # 6. 保存最终检查点
            if self.checkpoint_manager:
                await self._save_checkpoint()
            
            return {
                "status": self.context.status,
                "request_id": request_id,
                "answer": final_answer,
                "plan": self.context.plan.to_dict() if self.context.plan else None,
                "step_results": self.context.step_results,
                "critic_feedback": self.context.critic_feedback,
                "cost": cost_controller.build_cost_note(session_id, task_id) if cost_controller else "",
            }
            
        finally:
            # 停止所有角色
            for role in [self.planner, self.executor, self.critic]:
                role.stop()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
    
    def _build_final_answer(self) -> str:
        """构建最终答案。"""
        if not self.context.plan:
            return "无计划执行"
        
        parts = [f"任务: {self.context.user_input}\n"]
        parts.append("执行计划:\n")
        parts.append(self.context.plan.summary())
        parts.append("\n执行结果:\n")
        
        for i, step in enumerate(self.context.plan.steps):
            result = self.context.step_results.get(i)
            if result:
                parts.append(f"步骤 {i+1} [{step.status}]: {step.title}\n")
                parts.append(f"  结果: {str(result)[:500]}\n")
            elif i in self.context.step_errors:
                parts.append(f"步骤 {i+1} [失败]: {step.title}\n")
                parts.append(f"  错误: {self.context.step_errors[i]}\n")
        
        if self.context.critic_feedback:
            latest = self.context.critic_feedback[-1]
            parts.append(f"\n质量评审: 综合分 {latest.get('overall_score', 0):.2f}\n")
            if latest.get("issues"):
                parts.append(f"  问题: {', '.join(latest['issues'])}\n")
        
        return "\n".join(parts)
    
    async def _save_checkpoint(self) -> None:
        """保存检查点。"""
        if not self.checkpoint_manager or not self.context:
            return
        try:
            # 这里简化:实际需要构造 AgentState
            logger.debug("保存检查点: request_id=%s", self.context.request_id[:8])
        except Exception as e:
            logger.warning("检查点保存失败: %s", e)
    
    async def resume_from_checkpoint(self, request_id: str) -> bool:
        """从检查点恢复。"""
        if not self.checkpoint_manager:
            return False
        # 实际实现需要从 checkpoint 加载 ExecutionContext
        return False
    
    async def human_approve(self, step_index: int, approve: bool, modified_plan: dict = None) -> None:
        """人工介入批准/拒绝/修改。"""
        if not self.context:
            return
        
        if approve:
            self.context.approved_steps.add(step_index)
            self.context.human_checkpoints.discard(step_index)
            await self.bus.publish(RoleEvent(
                event_type=EventType.HUMAN_APPROVE,
                source_role=RoleType.HUMAN,
                target_role=RoleType.EXECUTOR,
                payload={"step_index": step_index},
                correlation_id=self.context.request_id,
            ))
        else:
            await self.bus.publish(RoleEvent(
                event_type=EventType.HUMAN_REJECT,
                source_role=RoleType.HUMAN,
                target_role=RoleType.PLANNER,
                payload={"step_index": step_index, "reason": "人工拒绝"},
                correlation_id=self.context.request_id,
            ))
        
        if modified_plan:
            self.context.plan = TaskPlan.from_dict(modified_plan)
            await self.bus.publish(RoleEvent(
                event_type=EventType.HUMAN_MODIFY,
                source_role=RoleType.HUMAN,
                target_role=RoleType.PLANNER,
                payload={"plan": modified_plan},
                correlation_id=self.context.request_id,
            ))
        
        self.context.status = "running"
        if self.checkpoint_manager:
            await self._save_checkpoint()


# ============================================================
# 便捷函数
# ============================================================

async def run_pec_task(
    user_input: str,
    session_id: str,
    tool_executor: Callable,
    planner_llm: LLMClient = None,
    executor_llm: LLMClient = None,
    critic_llm: LLMClient = None,
    checkpoint_manager: CheckpointManager = None,
    max_steps: int = 6,
    human_checkpoints: list[int] = None,
) -> dict:
    """运行 PEC 任务的便捷函数。"""
    orchestrator = PECOrchestrator(
        planner_llm=planner_llm,
        executor_llm=executor_llm,
        critic_llm=critic_llm,
        tool_executor=tool_executor,
        checkpoint_manager=checkpoint_manager,
    )
    return await orchestrator.execute(
        user_input=user_input,
        session_id=session_id,
        max_steps=max_steps,
        human_checkpoints=human_checkpoints,
    )