"""Graph 驱动的 Agent 核心。

架构:Agent 不再是手写 ReAct 循环,而是把一次运行建模为一张图:

    prepare(路由/工具路由/规划/构造消息) -> llm(调用模型)
        -> tools(并行执行工具/反思) -> llm  (循环,带步数预算)
        -> finalize(最终回答/自动记忆)     -> END
        -> giveup(达到步数上限)            -> END

关键能力:
- 逐节点 checkpoint:每个节点执行完把完整状态(消息/计划/步骤/路由)落盘
  (data/checkpoints.db),进程重启后可按 run_id 断点续跑。
- 与旧版行为完全兼容:原生 function calling + 并行工具 + 文本兜底、
  工具失败反思/自愈、任务规划、长期记忆注入/自动沉淀、工具路由、
  危险工具确认流、模型路由、会话摘要压缩、trace 记录。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
import uuid
from typing import Optional

from app.config import settings
from app.core.constants import TEST_FAIL_MARKERS, TRANSIENT_PATTERNS
from app.runtime.graph import END, Graph, Node
from app.runtime.state import AgentState


def _clip(text: str, cap: int) -> str:
    """按字符上限截断文本,防止超大工具结果/图片 base64 撑爆上下文。"""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n...[结果过长已截断]"


def _clip_history_for_json(history: list[dict]) -> str:
    """安全截断对话历史为 JSON:先逐条截断 content,再整体限制字节数。

    避免直接对 json.dumps 结果做字符截断导致 JSON 结构损坏。
    """
    clipped = []
    total_bytes = 0
    for m in history:
        raw = m.get("content", "") or m.get("kind", "")
        if not str(raw).strip():
            continue
        s = str(raw)
        # 按字节截断(UTF-8 下中文字符 3 字节)
        if len(s.encode("utf-8")) > 500:
            s = s.encode("utf-8")[:497].decode("utf-8", errors="ignore")
            s = s + "…"
        clipped.append({**m, "content": s})
        total_bytes += len(json.dumps({"role": m.get("role"), "content": s}, ensure_ascii=False).encode("utf-8"))
        if total_bytes > 5500:
            break
    return json.dumps(clipped, ensure_ascii=False)


def _truncate_to_token_budget(text: str, max_tokens: int, llm=None) -> tuple[str, bool]:
    """按 token 配额截断输出文本,返回 (文本, 是否被截断)。

    优先用 `llm.last_usage` 里的真实 completion_tokens 估算该模型的 token 密度;
    无用量信息时用启发式 estimate_tokens 估计。用于请求级 max_output_tokens 硬控制。
    """
    if max_tokens <= 0 or not text:
        return text, False
    from app.memory.context import estimate_tokens

    tokens = estimate_tokens(text)
    if tokens <= max_tokens:
        return text, False
    # 按比例算出可保留的字符数后截断
    keep_chars = max(1, int(len(text) * max_tokens / max(1, tokens)))
    cut = text[:keep_chars].rstrip()
    return cut, True
from app.core.llm import LLMClient, ToolCall
from app.core.planner import TaskPlan, _normalize_steps, apply_plan_tool, generate_plan
from app.core.prompts import TOOL_CALL_PATTERN, build_system_prompt, build_tools_description
from app.core.telemetry import _current_run_id, tracer
from app.tools.categories import SYSTEM_TOOLS
from app.tools.registry import ToolRegistry, get_registry
import logging  # noqa: F401  # 保留供后续代码使用

logger = logging.getLogger(__name__)

# 计划工具:在 ReAct 循环内被 Agent 拦截,维护私有计划状态
PLAN_TOOLS = ("set_plan", "update_plan")

# 不参与瞬态错误自动重试的工具(超时的终端命令不应被盲目重跑)
NO_RETRY_TOOLS = ("run_terminal",)

# 多智能体编排模式(Orchestration 层),与执行模式(single/auto/review/ensemble)正交
ORCHESTRATION_MODES = ("sequential", "hierarchical", "consensual")

# 工具路由快速通道阈值: 简短问候直接跳过 LLM 路由
_TOOL_ROUTE_SHORT_INPUT_THRESHOLD = 15
_TOOL_ROUTE_SHORT_INPUT_KEYWORDS = ("代码", "文件", "搜索", "网页", "执行", "运行", "分析", "报告", "计算")

# 任务规划快速通道阈值: 简短消息跳过规划
_PLAN_SHORT_INPUT_THRESHOLD = 20
_PLAN_SHORT_INPUT_KEYWORDS = ("代码", "文件", "搜索", "网页", "执行", "运行", "分析", "报告", "计算", "脚本", "程序")

# 确认流轮询间隔(秒)
_CONFIRM_POLL_INTERVAL = 0.8

# 错误分析最近消息数
_ERROR_RECENT_MESSAGE_COUNT = 6

# 历史摘要保留最近消息数
_MEMORY_RECENT_HISTORY_COUNT = 6

# 自动记忆沉淀单条事实最大字符数
_AUTO_MEMORY_FACT_MAX_CHARS = 30


class AgentResult:
    def __init__(self, answer: str, steps: list[dict], model: str, interrupt: Optional[dict] = None):
        self.answer = answer
        self.steps = steps
        self.model = model
        # 运行被 interrupt 暂停时携带暂停载荷(含待确认项);否则为 None
        self.interrupt = interrupt

    @property
    def interrupted(self) -> bool:
        return self.interrupt is not None


class Agent:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        max_steps: Optional[int] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        route: Optional[bool] = None,
        session_id: Optional[str] = None,
        approve_tools: Optional[list[str]] = None,
        mode: Optional[str] = None,
        critic_provider: str = "",
        critic_model: str = "",
        ensemble_models: Optional[list[str]] = None,
        max_output_tokens: int = 0,
        orch_subtasks: int = 0,
        orch_candidates: Optional[list[str]] = None,
        task_config: Optional[dict] = None,
    ):
        self.llm = llm or LLMClient()
        # 自动缩放配置:根据任务复杂度动态调整参数
        self._task_config = task_config
        if task_config:
            # task_config 优先，如果没有则使用参数或默认配置
            self.max_steps = task_config.get("max_steps") or max_steps or settings.agent_max_steps
            self.context_budget = task_config.get("context_budget") or settings.context_char_budget
            self.mode = task_config.get("mode") or (mode or settings.agent_mode or "auto").lower()
        else:
            self.max_steps = max_steps or settings.agent_max_steps
            self.context_budget = settings.context_char_budget
            self.mode = (mode or settings.agent_mode or "auto").lower()
        # 输出 token 配额硬控制:0 = 不限制;>0 时答案超过即截断并附提示
        self.max_output_tokens = max_output_tokens or int(settings.agent_default_max_output_tokens or 0)
        self.ensemble_models = list(ensemble_models or [])
        # 编排模式可调参数(UI 配置项):子任务数量提示 / 候选执行体清单。
        # 仅对 orchestration 类模式生效;执行模式(single/auto/review/ensemble)忽略。
        self.orch_subtasks = int(orch_subtasks or 0)
        _valid_modes = ("single", "auto", "review", "ensemble")
        self.orch_candidates = [c for c in (orch_candidates or []) if c in _valid_modes]
        # critic 独立评审模型(双角色反思);未配置时复用执行模型 self.llm。
        # 请求级 critic_provider/critic_model 优先,其次全局配置。
        self.critic_llm = None
        critic_provider = critic_provider or settings.agent_critic_provider
        critic_model = critic_model or settings.agent_critic_model
        if critic_provider:
            try:
                from app.core.llm import LLMClient as _L

                self.critic_llm = _L(critic_provider, critic_model or None)
            except Exception as e:
                logger.warning("critic_llm 初始化失败,降级为单模型模式: %s", e)
                self.critic_llm = None
        # 实例级评审/校验开关:single 关闭,review 强制开启,auto 跟随全局
        self._verify_enabled = settings.agent_verify
        self._reflect_enabled = settings.agent_reflect
        if self.mode == "single":
            self._verify_enabled = False
            self._reflect_enabled = False
        elif self.mode == "review":
            self._verify_enabled = True
            self._reflect_enabled = True
        self.registry = registry or get_registry()
        self.system_prompt = system_prompt or settings.agent_system_prompt
        self.temperature = temperature if temperature is not None else settings.agent_temperature
        self.route = route if route is not None else settings.agent_routing_enabled
        # 上下文预算:实例级覆盖(子智能体可给更小预算以强制聚焦)。
        # 默认继承全局;delegate/handoff 创建子 Agent 时显式收紧。
        # 注意: task_config 中的 context_budget 已在上方赋值，此处仅用于无 task_config 时初始化
        if not hasattr(self, 'context_budget') or self.context_budget is None:
            self.context_budget = settings.context_char_budget
        self.token_budget = settings.agent_token_budget
        # 当前会话 ID:用于检索/写入 session 级长期记忆,并透传给记忆工具
        self.session_id = session_id or ""
        # 人机协作确认通过的危险工具(本次运行允许执行);空=未开启确认流或未批准
        self.approve_tools = list(approve_tools or [])
        # 多模态内容块(OpenAI content parts):图片 image_url 等,随用户消息一起送 LLM。
        # 由 chat 接口解析 attachments 生成;模型不支持视觉时降级为文本提示。
        self.multimodal_parts: list[dict] = []
        # 分层记忆注入的提示块(由 _node_prepare 异步检索后缓存)
        self._memory_note: str = ""
        self._dangerous_tools = {s.strip() for s in settings.agent_dangerous_tools.split(",") if s.strip()}
        # 危险工具频率限制(共享实现,与 ToolExecutor 行为一致)
        from app.core.tool_safety import DangerousToolRateLimiter

        self._dangerous_rate_limiter = DangerousToolRateLimiter()
        # interrupt 式确认流:本次运行已由用户批准的工具名(runtime 运行时集合)
        self._runtime_approved: set[str] = set()
        # 工具路由:预选后只把相关工具子集暴露给模型;None=未路由(全量)
        self._active_tools: Optional[list[str]] = None
        self._always_tools = list(SYSTEM_TOOLS)
        # 原生工具调用开关:初始化时探测 LLM 是否支持
        self._native_enabled = settings.agent_native_tools and hasattr(self.llm, "chat_with_tools")
        self._schemas_cache = None
        # 任务计划状态(Plan-then-Execute)
        self._plan: Optional[TaskPlan] = None
        self._plan_dirty = False
        # 工具结果去重缓存:同一运行内 (tool, args) -> result(仅成功结果)
        # 使用 OrderedDict 实现 LRU 淘汰(而非 FIFO)
        try:
            from collections import OrderedDict
            self._tool_cache: dict[tuple[str, str], str] = OrderedDict()
        except Exception as e:  # noqa: BLE001
            logger.warning("工具缓存初始化失败,降级为无缓存模式: %s", e)
            self._tool_cache = {}
        # 工具缓存锁:保护 OrderedDict 操作，防止并发访问导致损坏
        self._tool_cache_lock = asyncio.Lock()
        # 仅对安全(纯函数)工具启用缓存
        safe_tools_str = settings.agent_tool_cache_safe_tools or ""
        self._tool_cache_safe: set[str] = {
            t.strip() for t in safe_tools_str.split(",") if t.strip()
        }
        # 图运行时
        self._graph: Optional[Graph] = None
        self._checkpoint = None
        if settings.agent_checkpoint_enabled:
            from app.runtime.checkpoint import CheckpointManager

            try:
                self._checkpoint = CheckpointManager()
            except Exception as e:  # noqa: BLE001  # checkpoint 不可用不影响主流程
                logger.warning("checkpoint 加载失败,降级为无持久化模式: %s", e)
                self._checkpoint = None

    # ---- 对外接口 ----

    async def run(self, user_input: str, history: list[dict] | None = None) -> AgentResult:
        """执行 Agent,返回结果和完整执行步骤。带 trace 记录。"""
        if self.mode == "ensemble":
            return await self._run_ensemble(user_input, history or [])
        if self.mode in ORCHESTRATION_MODES:
            return await self._run_orchestrated(user_input, history or [])
        state = AgentState(session_id=self.session_id, user_input=user_input)
        return await self._run_state(state, history or [], stream=False)

    async def resume(self, run_id: str, new_input: str | None = None,
                     resume_value: Optional[dict] = None,
                     stream: bool = False) -> AgentResult:
        """从 checkpoint 恢复并继续执行。

        Args:
            run_id: checkpoint run_id。
            new_input: 可选,恢复时替换的用户输入(很少用)。
            resume_value: interrupt 式确认流的用户决定。支持:
                - [工具名,...]:已批准的工具白名单
                - {confirmation_id: {"approved": bool}}:按确认请求逐个决定
            若运行处于中断状态则从中断节点继续,否则从头节点继续。
            stream: 是否流式返回事件(默认为 False,兼容旧调用方)。
        """
        if self._checkpoint is None:
            raise RuntimeError("checkpoint 未启用,无法恢复运行")
        state = self._checkpoint.load(run_id)
        if state is None:
            raise RuntimeError(f"未找到运行记录: {run_id}")
        if new_input is not None:
            state.user_input = new_input
        if state.status == "interrupted" and state.interrupt is not None:
            state.interrupt["resume"] = resume_value
            state.status = "running"
        self._sync_from_state(state)
        return await self._run_state(state, state.metadata.get("history", []) or [], stream=stream)

    def checkpoints(self, limit: int = 50, status: Optional[str] = None) -> list[dict]:
        """列出已持久化的运行记录(供后台任务/断点续跑管理)。"""
        if self._checkpoint is None:
            return []
        return self._checkpoint.list(limit=limit, status=status)

    def versions(self, run_id: str) -> list[dict]:
        """列出某次运行的全部历史版本(时间旅行)。"""
        if self._checkpoint is None:
            return []
        return self._checkpoint.versions(run_id)

    async def rollback(self, run_id: str, version: int,
                       resume_value: Optional[dict] = None) -> AgentResult:
        """时间旅行:回滚到历史版本,从该节点重新分叉执行。

        加载 run_id 的指定历史版本,重置最新指针后继续执行。
        """
        if self._checkpoint is None:
            raise RuntimeError("checkpoint 未启用,无法回滚")
        state = self._checkpoint.rollback(run_id, version)
        if state is None:
            raise RuntimeError(f"未找到运行记录 {run_id} 的版本 {version}")
        if state.status == "interrupted" and state.interrupt is not None:
            state.interrupt["resume"] = resume_value
            state.status = "running"
        self._sync_from_state(state)
        return await self._run_state(state, state.metadata.get("history", []) or [], stream=False)

    async def run_stream(self, user_input: str, history: list[dict] | None = None):
        """流式执行:先产出思考/工具事件,最后流式产出答案增量。

        yield 事件 dict:
          {"type": "thinking", "content": "..."}   LLM 推理/工具调用原文
          {"type": "tool", "name": ..., "args": ..., "result": ...}
          {"type": "token", "content": "..."}
          {"type": "done", "model": ..., "steps": [...]}
        """
        if self.mode == "ensemble":
            result = await self._run_ensemble(user_input, history or [])
            for ch in result.answer:
                yield {"type": "token", "content": ch}
            yield {"type": "done", "model": result.model, "steps": result.steps}
            return
        if self.mode in ORCHESTRATION_MODES:
            async for evt in self._run_orchestrated_stream(user_input, history or []):
                yield evt
            return
        state = AgentState(session_id=self.session_id, user_input=user_input)
        run_id = state.run_id
        token = _current_run_id.set(run_id)
        tracer.record({
            "type": "run_start",
            "run_id": run_id,
            "session_id": self.session_id,
            "input": (user_input or "")[:200],
            "max_steps": self.max_steps,
            "tool_confirm": bool(settings.agent_tool_confirm),
        })
        t0 = time.time()
        graph = self._build_graph()
        state.metadata["history"] = history or []
        state.metadata["stream"] = True
        try:
            async for evt in graph.arun_stream(state):
                yield evt
            model = state.metadata.get("model") or self.llm.info["model"]
            interrupt = None
            if state.status == "interrupted" and state.interrupt:
                interrupt = state.interrupt.get("value")
            done = {"type": "done", "model": model, "steps": state.steps}
            if interrupt is not None:
                done["interrupt"] = interrupt
            yield done
        except Exception as e:  # noqa: BLE001
            logger.warning("run() 运行异常，记录 trace 后终止: %s", e)
            tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                           "error": str(state.error or "")[:300],
                           "duration": round(time.time() - t0, 3)})
            _current_run_id.reset(token)
            raise
        tracer.record({"type": "run_end", "run_id": run_id, "ok": True,
                       "steps": len(state.steps), "answer_len": len(state.answer or ""),
                       "duration": round(time.time() - t0, 3)})
        _current_run_id.reset(token)

    # ---- 图运行 ----

    async def _run_state(self, state: AgentState, history: list[dict], stream: bool = False) -> AgentResult:
        run_id = state.run_id
        token = _current_run_id.set(run_id)
        tracer.record({
            "type": "run_start",
            "run_id": run_id,
            "session_id": self.session_id,
            "input": (state.user_input or "")[:200],
            "max_steps": self.max_steps,
            "tool_confirm": bool(settings.agent_tool_confirm),
        })
        t0 = time.time()
        graph = self._build_graph()
        state.metadata["history"] = history
        state.metadata["stream"] = stream
        try:
            state, _events = await graph.arun(state)
        except Exception as e:  # noqa: BLE001
            tracer.record({"type": "run_end", "run_id": run_id, "ok": False,
                           "error": str(e)[:300], "duration": round(time.time() - t0, 3)})
            _current_run_id.reset(token)
            raise
        tracer.record({"type": "run_end", "run_id": run_id, "ok": True,
                       "steps": len(state.steps), "answer_len": len(state.answer or ""),
                       "duration": round(time.time() - t0, 3)})
        _current_run_id.reset(token)
        model = state.metadata.get("model") or self.llm.info["model"]
        interrupt = None
        if state.status == "interrupted" and state.interrupt:
            interrupt = state.interrupt.get("value")
        return AgentResult(answer=state.answer, steps=state.steps, model=model, interrupt=interrupt)

    def _sync_from_state(self, state: AgentState) -> None:
        """从 checkpoint 恢复时,把状态同步回 Agent 的运行时字段。"""
        self._active_tools = state.active_tools
        self._schemas_cache = None
        if state.plan is not None:
            self._plan = TaskPlan.from_dict(state.plan)
            self._plan_dirty = False
        else:
            self._plan = None

    def _build_graph(self) -> Graph:
        # 每次调用创建新图,避免多次 run 复用同一图实例导致 state 残留
        # 动态计算初始步数:基于 task_config 中的 max_steps
        # 注意: 实际执行时会通过 _get_dynamic_max_steps() 动态调整
        base_max = self.max_steps
        # Graph.max_iters 需要预留足够余量，实际检查在 _node_llm 中进行
        g = Graph(name="agent", checkpoint=self._checkpoint,
                  max_iters=base_max + 100)  # 增加余量到100步
        g.add_node(Node("prepare", self._node_prepare))
        g.add_node(Node("llm", self._node_llm))
        g.add_node(Node("tools", self._node_tools))
        g.add_node(Node("finalize", self._node_finalize))
        g.add_node(Node("giveup", self._node_giveup))
        g.add_node(Node("reflect", self._node_reflect))
        g.add_node(Node("verify", self._node_verify))
        g.set_entry("prepare")
        g.add_edge("prepare", "llm")
        g.add_edge("tools", "llm")
        g.add_conditional_edge("llm", self._route_llm)
        g.add_conditional_edge("finalize", self._route_finalize)
        g.add_conditional_edge("reflect", self._route_reflect)
        g.add_conditional_edge("verify", self._route_verify)
        g.set_entry("prepare")
        return g

    async def _route_llm(self, state: AgentState) -> Optional[str]:
        """llm 节点之后的路由:给上限→giveup;有工具调用→tools;否则→finalize。"""
        if state.metadata.get("giveup"):
            return "giveup"
        if state.metadata.get("tool_calls"):
            return "tools"
        return "finalize"

    def _get_dynamic_max_steps(self, state: AgentState) -> int:
        """根据计划进度动态计算最大步数。

        规则:
        1. 如果有计划且未完成,根据剩余步骤数动态调整上限
        2. 每完成一步，剩余预算减少
        3. 动态上限 = base_max + remaining * steps_per_step
        """
        base_max = self.max_steps
        if not self._plan:
            return base_max

        completed = sum(1 for s in self._plan.steps if s.status == "done")
        total = len(self._plan.steps)
        remaining = total - completed

        if remaining <= 0:
            return base_max  # 计划已完成,使用基础步数

        # 正确公式: 基础步数 + 剩余步骤 × 每步预算
        # 随着任务完成，动态上限逐渐降低到 base_max
        steps_per_plan_step = 8  # 每个计划步骤分配 8 步 (思考 + 执行 + 验证)
        dynamic_max = base_max + remaining * steps_per_plan_step
        return int(dynamic_max)

    async def _route_finalize(self, state: AgentState) -> Optional[str]:
        """finalize 之后:简单回答跳过反思/校验;否则按预算进入反思→校验。"""
        answer = state.answer or ""
        has_tool_steps = any(s.get("type") == "tool" for s in state.steps)
        # 空答案:跟踪连续空答案次数，超过阈值则触发 giveup
        if not answer or not answer.strip():
            consecutive_empty = int(state.metadata.get("consecutive_empty_answers", 0)) + 1
            state.metadata["consecutive_empty_answers"] = consecutive_empty
            # 改进：提高阈值到 5 次，避免复杂任务过早放弃
            if consecutive_empty >= 5:
                state.metadata["giveup"] = True
                return "giveup"
            return "llm"
        # 有答案时重置计数器
        state.metadata["consecutive_empty_answers"] = 0
        # 简单回答(<200字且无工具调用):直接停止,跳过反思和校验
        # 提高阈值避免任务过早结束(原 100 字过低,容易导致 LLM 用简短回答逃避复杂任务)
        if len(answer.strip()) < 200 and not has_tool_steps:
            return END
        if not self._reflect_enabled:
            return END
        rounds = int(state.metadata.get("reflect_rounds", 0))
        if rounds >= settings.agent_reflect_max_rounds + 1:
            return END
        return "reflect"

    async def _route_reflect(self, state: AgentState) -> Optional[str]:
        """reflect 之后:空答案直接停止;通过→verify;不合格且仍有评审预算→回 llm 重写。"""
        answer = state.answer or ""
        if len(answer.strip()) < 50:
            return END
        if state.metadata.get("reflect_rewrite"):
            rounds = int(state.metadata.get("reflect_rounds", 0))
            if rounds >= settings.agent_reflect_max_rounds + 1:
                return "verify"
            return "llm"
        return "verify"

    async def _route_verify(self, state: AgentState) -> Optional[str]:
        """verify 之后:校验内已内联重写(最多一次),随后停止。"""
        return END

    # ---- 图节点 ----

    async def _node_prepare(self, state: AgentState, graph: Graph) -> None:
        """路由/工具路由/规划/构造消息:只在入口执行一次。"""
        user_input = state.user_input
        history = state.metadata.get("history") or []

        # 输入护栏
        from app.core.guardrails import INPUT_BLOCK_MESSAGE, run_input_guardrails
        guard = run_input_guardrails(user_input)
        if guard.blocked:
            state.answer = INPUT_BLOCK_MESSAGE
            state.steps.append({"step": 0, "type": "guardrail", "content": guard.reasons()})
            state.metadata["guardrail_violations"] = [v.__dict__ for v in guard.violations]
            return END

        # 准备阶段超时保护
        prepare_timeout = max(30, int(getattr(settings, "agent_llm_timeout", 60) or 60))
        try:
            await asyncio.wait_for(
                self._run_prepare(state, user_input, history, state.steps, 0),
                timeout=prepare_timeout
            )
        except asyncio.TimeoutError:
            state.emit({"type": "thinking", "content": f"[准备阶段超时({prepare_timeout}s),使用基础配置继续]"})
            if not state.messages:
                state.messages = [{"role": "user", "content": user_input}]
            # 超时后使用最小上下文继续,不中断主流程
            return

        state.active_tools = self._active_tools

    async def _run_prepare(self, state, user_input, history, steps, step):
        """prepare 主体逻辑,可被超时包裹。"""
        await self._maybe_route(user_input)
        state.metadata["model"] = self.llm.info["model"]
        history = await self._prepare_history(history)
        state.metadata["history"] = history

        route_evt = await self._maybe_select_tools(user_input, steps, step)
        if route_evt:
            state.emit(route_evt)

        if await self._plan_task(user_input, steps, step):
            state.emit({"type": "plan", "action": "create",
                        "plan": self._plan.to_dict() if self._plan else None})
        if self._plan is not None:
            state.plan = self._plan.to_dict()

        self._memory_note = await self._build_memory_note_async(user_input, history)
        state.messages = self._build_messages(user_input, history)
    async def _node_llm(self, state: AgentState, graph: Graph) -> None:
        """调用 LLM;把结果(tool_call_mode/content/tool_calls)暂存到 metadata 供路由与 tools 节点使用。"""
        rounds = int(state.metadata.get("llm_rounds", 0))
        # 动态步数计算:基于计划进度自动扩展
        dynamic_max = self._get_dynamic_max_steps(state)
        
        total_steps = len(state.steps)
        if rounds >= dynamic_max and total_steps >= dynamic_max:
            state.metadata["giveup"] = True
            logger.warning("步骤耗尽: rounds=%d, total_steps=%d, dynamic_max=%d",
                          rounds, total_steps, dynamic_max, effective_max)
            return
        
        state.metadata["llm_rounds"] = rounds + 1

        is_stream = state.metadata.get("stream", False)
        if is_stream:
            tool_call_mode, resp, tool_calls = await self._call_llm_stream(state.messages, state)
        else:
            tool_call_mode, resp, tool_calls = await self._call_llm(state.messages)
        state.metadata["tool_call_mode"] = tool_call_mode
        state.metadata["resp"] = resp or ""
        state.metadata["tool_calls"] = [tc.to_dict() for tc in tool_calls]

        # 推理模型:先把思维链透出为 thinking 事件(与正文分离)
        reasoning = getattr(self.llm, "_reasoning", "") or ""
        if reasoning:
            state.metadata["reasoning"] = reasoning
            state.emit({"type": "thinking", "content": reasoning, "reasoning": True})

        if tool_call_mode == "native" and tool_calls:
            state.messages.append({
                "role": "assistant",
                "content": resp or "",
                "tool_calls": [tc.to_openai() for tc in tool_calls],
            })
            if resp:
                state.emit({"type": "thinking", "content": resp})
        elif tool_calls:
            state.messages.append({"role": "assistant", "content": resp or ""})
            state.emit({"type": "thinking", "content": resp or ""})
        # 无工具调用:最终回答由 finalize 节点处理

    async def _node_tools(self, state: AgentState, graph: Graph) -> Optional[str]:
        """执行一批工具调用(并行)、回喂结果、更新计划、注入反思。终端工具成功则结束。

        interrupt 式确认流开启时:先预扫描工具调用,凡需要人工确认且未批准的,
        创建确认请求并 graph.interrupt(暂停落盘返回控制权),不执行任何工具;
        用户批准后 resume 重入本节点,已批准的工具放行执行。
        """
        tool_call_mode = state.metadata.get("tool_call_mode", "text")
        tool_calls = [
            ToolCall(id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments", "{}"))
            for tc in state.metadata.get("tool_calls", []) or []
        ]
        step = int(state.metadata.get("llm_rounds", 1)) - 1

        # ---- interrupt 式确认:预扫描 ----
        # 由 _scan_pending_confirmations 依据各机制(agent_tool_confirm / decide)决定是否需确认
        if settings.agent_confirm_interrupt and tool_calls:
            pending = self._scan_pending_confirmations(tool_calls)
            if pending:
                resuming = bool(state.interrupt and state.interrupt.get("resume") is not None)
                if not resuming:
                    # 首次遇到需确认的工具:创建请求 -> 发事件 -> 中断暂停
                    requests = self._create_confirm_requests(pending)
                    state.emit({"type": "confirmation", "requests": requests})
                    graph.interrupt(state, {
                        "type": "confirmation",
                        "requests": requests,
                        "run_id": state.run_id,
                    })
                    return None
                # 恢复:按用户决定更新运行时批准集合,未批准的将被硬拦截
                self._runtime_approved = set()
                for tc, _decision in pending:
                    if self._is_tool_approved_by_resume(tc, state):
                        self._runtime_approved.add(tc.name)

        # 工具开始执行:先逐工具发 tool_call_start,再批量执行
        state.emit({
            "type": "tool_call_start",
            "tools": [
                {"name": tc.name, "arguments": tc.arguments}
                for tc in tool_calls
            ],
        })
        # 也发送逐工具的 tool_start 事件(供旧的 ExecTrace 兼容)
        for tc in tool_calls:
            state.emit({"type": "tool_start", "name": tc.name, "arguments": tc.arguments})
        batch = await self._execute_batch(tool_calls, state.steps, step)
        # 代码级自愈:对失败的代码类工具自动执行"分析-修复-重跑"闭环
        batch = await self._maybe_self_heal(state, batch, step)
        tool_seq = int(state.metadata.get("tool_seq", 0))
        for i, (tc, result_str, _is_err) in enumerate(batch):
            step_id = f"{state.run_id}_t{tool_seq + i}"
            if tool_call_mode == "native":
                state.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _clip(result_str, settings.message_char_cap),
                })
            else:
                state.messages.append({
                    "role": "user",
                    "content": f"工具 {tc.name} 执行结果如下,请基于结果继续:\n{_clip(result_str, settings.message_char_cap)}",
                })
            # 发送工具执行完成事件,携带 step_id 供前端去重(全局单调递增,跨批次唯一)
            state.emit({
                "type": "tool",
                "name": tc.name,
                "arguments": tc.arguments,
                "result": result_str,
                "step_id": step_id,
            })
        state.metadata["tool_seq"] = tool_seq + len(batch)

        evt = self._flush_plan(state.steps, step)
        if evt:
            state.emit(evt)
        state.plan = self._plan.to_dict() if self._plan else None

        terminal = self._append_reflection(state.messages, batch, state._failures)
        if terminal:
            answer = f"已通过工具 {terminal} 完成任务,结果已展示。"
            state.answer = answer
            state.steps.append({"step": step + 1, "type": "answer", "content": answer})
            if state.metadata.get("stream"):
                for ch in answer:
                    state.emit({"type": "token", "content": ch})
            return END

        # 计划失败后重新规划:同工具连续失败达阈值时,基于失败原因生成修订版计划
        if await self._maybe_replan(state, batch):
            state.emit({"type": "plan", "action": "replan",
                        "plan": self._plan.to_dict() if self._plan else None})
        return None

    async def _node_finalize(self, state: AgentState, graph: Graph) -> None:
        """最终回答:更新计划、产出答案、自动沉淀记忆。流式模式逐 token 输出。"""
        step = int(state.metadata.get("llm_rounds", 1)) - 1
        evt = self._finalize_plan(state.steps, step)
        if evt:
            state.emit(evt)
        state.plan = self._plan.to_dict() if self._plan else None

        # 统一从 _node_llm 已收集的响应开始;流式已逐 token 发出,非流式尚未生成完整回答
        answer = state.metadata.get("resp", "") or ""

        # 空答案兜底:如果 resp 为空，尝试重新生成
        if not answer:
            # 先尝试 reasoning 作为兜底
            answer = state.metadata.get("reasoning", "") or ""
            if answer and state.metadata.get("stream"):
                for ch in answer:
                    state.emit({"type": "token", "content": ch})
            # 如果仍然为空，非流式模式下补一次 LLM 调用
            if not answer and not state.metadata.get("stream"):
                answer = await self.llm.chat(state.messages, temperature=self.temperature)

        # 输出 token 配额硬控制
        if self.max_output_tokens > 0 and answer:
            full, truncated = _truncate_to_token_budget(answer, self.max_output_tokens, self.llm)
            if truncated:
                answer = full + "\n...[已达输出配额上限,回答已截断]"
                if state.metadata.get("stream"):
                    state.emit({"type": "thinking",
                                "content": f"[已达输出配额上限 {self.max_output_tokens} token,回答已截断]"})

        state.answer = answer
        # 剥离 <thinking>...</thinking> 标签,确保存储的答案不含思考内容
        if state.answer:
            state.answer = re.sub(r'<thinking[^>]*>[\s\S]*?</thinking>', '', state.answer, flags=re.IGNORECASE).strip()
            state.answer = re.sub(r'[ \t]+', ' ', state.answer)
            state.answer = re.sub(r'\n{3,}', '\n\n', state.answer)
        state.steps.append({"step": step + 1, "type": "answer", "content": state.answer})

        # 输出护栏:敏感信息/提示词泄露检测(流式模式 token 已发出,仅记录;非流式可拦截)
        from app.core.guardrails import run_output_guardrails

        out_guard = run_output_guardrails(answer)
        if out_guard.blocked:
            state.steps.append({"step": step + 1, "type": "guardrail", "content": out_guard.reasons()})
            state.metadata["guardrail_violations"] = [v.__dict__ for v in out_guard.violations]
            if not state.metadata.get("stream"):
                sanitized = next((v.sanitized for v in out_guard.violations if v.sanitized), None)
                state.answer = sanitized or "回答包含敏感信息,已由安全护栏拦截。"
                state.steps[-1]["content"] = out_guard.reasons()

        await self._maybe_auto_memory(state.user_input, state.answer, state.metadata.get("history", []) or [])

    async def _node_giveup(self, state: AgentState, graph: Graph) -> None:
        """达到步数上限:给出兜底回答,附带失败细节帮助用户重新尝试。"""
        # 从对话历史中提取最后一条助手消息,了解最后一次尝试
        last_assistant = ""
        for msg in reversed(state.messages):
            if msg.get("role") == "assistant":
                last_assistant = (msg.get("content") or "")[:300]
                break

        # 从 _failures 提取失败工具摘要
        failed_tools = []
        for name, count in sorted(state._failures.items(), key=lambda x: -x[1])[:3]:
            failed_tools.append(f"{name}(失败{count}次)")
        failed_str = "、".join(failed_tools) if failed_tools else "无"

        # 从 messages 提取最近的错误/反思消息作为线索
        recent_errors = []
        for msg in reversed(state.messages[-_ERROR_RECENT_MESSAGE_COUNT:]):
            content = msg.get("content", "") or ""
            if msg.get("role") == "assistant" and ("失败" in content or "error" in content.lower()
                                                   or "无法" in content):
                recent_errors.append(content[:150])
            elif msg.get("role") == "tool" and "error" in content.lower():
                recent_errors.append(f"[{msg.get('name', '?')}] {content[:150]}")
            if len(recent_errors) >= 3:
                break

        detail_parts = []
        if failed_tools:
            detail_parts.append(f"失败工具: {failed_str}")
        if last_assistant:
            detail_parts.append(f"最后尝试: {last_assistant[:100]}")
        if recent_errors:
            detail_parts.append("错误线索: " + " | ".join(recent_errors))
        detail = "; ".join(detail_parts) if detail_parts else "无额外信息"

        answer = (
            f"无法完成任务：已尝试 {self.max_steps} 步，仍未能得到正确结果。\n\n"
            f"详情: {detail}\n\n"
            "建议：简化需求、提供更多背景信息，或分步骤描述任务。"
        )
        state.answer = answer
        state.steps.append({"step": self.max_steps, "type": "error",
                            "content": f"达到最大步数限制({self.max_steps}步)，失败工具: {failed_str}"})
        logger.info("giveup: input=%r steps=%d failures=%s last_assistant=%r",
                     state.user_input[:100], self.max_steps, failed_str, last_assistant[:100])
        if state.metadata.get("stream"):
            for ch in answer:
                state.emit({"type": "token", "content": ch})

    def _critic(self):
        """返回评审模型:配置了独立 critic 则用它,否则复用执行模型。"""
        return self.critic_llm or self.llm

    # ---- 多模型协作:ensemble(并行做 → 评审选优) ----

    def _ensemble_specs(self) -> list[tuple[str, str]]:
        """解析参与 ensemble 的模型规格列表 [(provider, model), ...]。

        来源优先级:请求级 ensemble_models > 全局 agent_ensemble_models。
        每项形如 "provider:model" 或 "provider"(用该 provider 默认模型)。
        无法解析时回退为当前执行模型。最多 agent_ensemble_max 个。
        """
        raw = list(self.ensemble_models)
        if not raw:
            raw = [s.strip() for s in settings.agent_ensemble_models.split(",") if s.strip()]
        specs: list[tuple[str, str]] = []
        for item in raw:
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                provider, model = item.split(":", 1)
            else:
                provider, model = item, ""
            specs.append((provider, model))
        if not specs:
            info = self.llm.info
            specs = [(info.get("provider", ""), info.get("model", ""))]
        return specs[: max(1, int(settings.agent_ensemble_max))]

    async def _run_ensemble(self, user_input: str, history: list[dict]) -> AgentResult:
        """多个模型并行做同一任务,再用评审模型对全部回答打分选优。

        每个模型用独立子 Agent(单模型模式)执行同一任务,结果合并后
        交给评审模型(critic 或主模型)按质量排序,返回最佳回答。
        """
        from app.core.llm import LLMClient as _L

        specs = self._ensemble_specs()
        agents: list[tuple[tuple[str, str], Agent]] = []
        for spec in specs:
            provider, model = spec
            try:
                llm = _L(provider, model or None)
            except Exception as e:  # noqa: BLE001
                logger.warning("子 Agent LLM 创建失败，跳过 provider=%s model=%s: %s", provider, model, e)
                continue
            sub = Agent(
                llm=llm,
                registry=self.registry,
                max_steps=self.max_steps,
                system_prompt=self.system_prompt,
                session_id=self.session_id,
                mode="single",  # 子模型只做不评,避免递归
                critic_provider=(self.critic_llm.provider if self.critic_llm else ""),
                critic_model=(self.critic_llm.model if self.critic_llm else ""),
            )
            sub._active_tools = self._active_tools
            sub._schemas_cache = None
            agents.append((spec, sub))

        if not agents:
            return AgentResult(answer="ensemble 模式没有可用模型。", steps=[], model=self.llm.info["model"])

        try:
            results = await asyncio.gather(*[a.run(user_input, history) for _, a in agents])
        except Exception as e:  # noqa: BLE001
            return AgentResult(answer=f"ensemble 执行失败: {e}", steps=[], model=self.llm.info["model"])

        # 各子 Agent 的轨迹合并为 steps,便于可观测
        # 用配对列表(spec, agent, result)而非 zip(specs, results),避免创建失败跳过导致的下标错位
        paired = [(spec, agent, res) for (spec, agent), res in zip(agents, results)]
        steps: list[dict] = []
        for i, (spec, agent, res) in enumerate(paired):
            steps.append({
                "step": i + 1,
                "type": "ensemble_member",
                "provider": spec[0],
                "model": spec[1] or res.model,
                "answer": res.answer,
                "member_steps": res.steps,
            })
        steps.append({"step": len(paired) + 1, "type": "ensemble_judge"})

        judge_results = [res for _spec, _agent, res in paired]
        best = await self._judge_ensemble(user_input, judge_results)
        if best is not None:
            return AgentResult(answer=best.answer, steps=steps, model=best.model)
        # 评审失败:退回第一个有效结果
        return AgentResult(answer=judge_results[0].answer, steps=steps, model=judge_results[0].model)

    async def _judge_ensemble(self, user_input: str, results: list[AgentResult]) -> Optional[AgentResult]:
        """对全部候选回答打分,返回最佳结果;失败返回 None(退回第一候选)。"""
        if not results:
            return None
        if len(results) == 1:
            return results[0]
        try:
            candidate_texts = "\n\n".join(
                f"[候选 {i}]({r.model}):\n{r.answer}" for i, r in enumerate(results)
            )
            schema = {
                "type": "object",
                "properties": {
                    "winner": {"type": "integer", "description": "最佳回答的候选索引"},
                    "scores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                                "score": {"type": "number", "description": "1-10 质量分"},
                            },
                            "required": ["index", "score"],
                        },
                    },
                    "reason": {"type": "string", "description": "选优理由"},
                },
                "required": ["winner", "scores", "reason"],
            }

            def _validator(data):
                if not isinstance(data, dict):
                    return False, "结果不是对象"
                if not isinstance(data.get("winner"), int):
                    return False, "winner 必须是整数"
                if not isinstance(data.get("scores"), list):
                    return False, "scores 必须是数组"
                return True, None

            prompt = (
                f"用户请求:\n{user_input}\n\n"
                f"下面是多个模型对同一请求的回答。请从准确性、完整性、清晰度三个维度打分(1-10),"
                f"并选出最佳的一个(返回它的索引 winner)。\n\n{candidate_texts}"
            )
            verdict = await self._critic().chat_json_schema(
                [
                    {"role": "system", "content": "你是严格的评审者,从多个候选回答中选出质量最佳的一个。"},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.2,
                validator=_validator,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("ensemble 评审失败，随机选取第一个结果: %s", e)
            return None
        winner = int(verdict.get("winner", 0))
        if not (0 <= winner < len(results)):
            winner = 0
        return results[winner]

    # ---- 多智能体编排(Orchestration 层):Sequential / Hierarchical / Consensual ----
    #
    # 与"执行模式"(single/auto/review/ensemble)正交:执行模式决定"单个智能体怎么干",
    # 编排模式决定"多个智能体怎么协作"。二者通过 _make_worker 桥接——
    # 每个子任务都由一个新的执行模式 Agent 完成,因此层级叶子也可以是
    # ensemble / review 等执行模式(天然递归)。

    def _make_worker(self, mode_name: str, active_tools: Optional[list[str]] = None) -> "Agent":
        """为某个执行模式创建一个子智能体(执行体)。"""
        sub = Agent(
            llm=self.llm,
            registry=self.registry,
            session_id=self.session_id,
            mode=mode_name,
            critic_provider=(self.critic_llm.provider if self.critic_llm else ""),
            critic_model=(self.critic_llm.model if self.critic_llm else ""),
            ensemble_models=self.ensemble_models,
            max_output_tokens=self.max_output_tokens,
        )
        # 子 Agent 继承父 Agent 的工具路由结果,避免调用无关工具
        sub._active_tools = active_tools or self._active_tools
        sub._schemas_cache = None
        return sub

    async def _register_orch_agents(self, orch, modes: list[str] | None = None) -> None:
        """把执行模式注册为编排器的"智能体"(幂等),供任务分解时按需分配。

        modes: 限定只注册这些执行模式(来自 UI 的"候选执行体清单");为 None 时注册全部。
        """
        from app.core.orchestrator import AgentCapability

        roster = {
            "auto": "通用执行:自动判断是否调用工具完成任务",
            "single": "单模型直接执行,不做额外评审,速度快",
            "review": "执行后由独立评审模型核查质量,适合校验与总结",
            "ensemble": "多模型并行生成再由评审模型择优,适合高可靠任务",
        }
        tool_names = [t.name for t in await self.registry.list()]
        names = modes if modes else list(roster.keys())
        for name in names:
            if name not in roster:
                continue
            if name not in orch._agents:
                orch.register_agent(name, AgentCapability(
                    name=name, description=roster[name],
                    tools=list(tool_names), expertise=[name],
                ))

    def _consensual_candidates(self) -> list[str]:
        """consensual 模式下的并行候选执行模式清单。优先用 UI 配置的 orch_candidates。"""
        if self.orch_candidates:
            return [c for c in self.orch_candidates if c in ("single", "auto", "review", "ensemble")]
        if self.ensemble_models:
            return ["single", "auto", "ensemble"]
        return ["single", "auto", "review"]

    async def _run_orchestrated(self, user_input: str, history: list[dict]) -> AgentResult:
        """非流式入口:驱动编排流并聚合为 AgentResult。"""
        answer: list[str] = []
        steps: list[dict] = []
        async for ev in self._run_orchestrated_stream(user_input, history or []):
            if ev["type"] == "token":
                answer.append(ev["content"])
            elif ev["type"] in ("tool", "thinking", "plan", "orchestration"):
                steps.append(ev)
        return AgentResult(answer="".join(answer), steps=steps, model=self.llm.info["model"])

    async def _run_orchestrated_stream(self, user_input: str, history: list[dict]):
        """流式编排主循环:按 flow 类型分发,逐 token 转发子智能体输出。

        yield 事件:
          {"type": "orchestration", "phase": "start"|"task_start"|"task_done"|...}
          {"type": "token"|"thinking"|"tool"|"plan"}  (来自子智能体,原样转发)
        """
        flow = self.mode
        yield {"type": "orchestration", "phase": "start", "flow": flow,
               "message": f"启动 {flow} 多智能体编排"}

        if flow == "consensual":
            async for ev in self._orch_consensual(user_input, history or []):
                yield ev
            yield {"type": "orchestration", "phase": "end", "flow": flow}
            return

        if flow == "sequential":
            # 顺序多阶段:默认 auto 探索 + review 核查;若 UI 配置了候选执行体清单,
            # 则按清单顺序串行执行(至少 2 个阶段才构成"顺序编排")。
            pipeline = self.orch_candidates if len(self.orch_candidates) >= 2 else ["auto", "review"]
            context = ""
            for i, worker_mode in enumerate(pipeline):
                yield {"type": "orchestration", "phase": "task_start",
                       "index": i, "total": len(pipeline), "agent": worker_mode,
                       "task": "整体任务(顺序两阶段)"}
                prompt = user_input
                if context:
                    prompt = (f"前序阶段已完成的工作如下,请在其基础上继续完善:\n{context}\n\n"
                              f"用户原始请求: {user_input}")
                sink: list[str] = []
                async for ev in self._orch_run_worker(worker_mode, prompt, history, i, _sink=sink):
                    yield ev
                context += f"[阶段 {i + 1} · {worker_mode} 产出]\n{''.join(sink)}\n"
            yield {"type": "orchestration", "phase": "end", "flow": flow}
            return

        # hierarchical: 任务分解 -> 拓扑排序 -> 依赖顺序执行 -> 合成
        from app.core.orchestrator import get_orchestrator, SubTask

        orch = get_orchestrator()
        # 先注册执行模式"智能体",decompose_task 才能据此分配子任务。
        # 若 UI 配置了候选执行体清单,则只注册这些模式(子任务只能分配给它们)。
        await self._register_orch_agents(orch, modes=self.orch_candidates or None)
        try:
            sub_tasks = await orch.decompose_task(
                user_input, user_input, self.llm,
                num_subtasks=self.orch_subtasks,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("子任务分解失败，降级为单任务执行: %s", e)
            sub_tasks = None
        if not sub_tasks:
            sub_tasks = [SubTask(description=user_input, assigned_agent="auto")]
        sub_tasks = orch._topological_sort(sub_tasks)

        context = ""
        for i, task in enumerate(sub_tasks):
            worker_mode = task.assigned_agent or "auto"
            if worker_mode not in ("single", "auto", "review", "ensemble"):
                worker_mode = "auto"
            yield {"type": "orchestration", "phase": "task_start",
                   "index": i, "total": len(sub_tasks),
                   "task": task.description, "agent": worker_mode}
            prompt = task.description
            if context:
                prompt = (f"以下是前序子任务已完成的工作,请在其基础上继续:\n{context}\n\n"
                          f"当前子任务: {task.description}")
            answer_parts: list[str] = []
            try:
                async for ev in self._orch_run_worker(worker_mode, prompt, history, i, _sink=answer_parts):
                    yield ev
            except Exception as e:  # noqa: BLE001
                yield {"type": "orchestration", "phase": "task_error",
                       "index": i, "error": str(e)[:200]}
                break
            context += f"\n[子任务 {i + 1} · {worker_mode}]\n{''.join(answer_parts)}\n"
            yield {"type": "orchestration", "phase": "task_done",
                   "index": i, "agent": worker_mode,
                   "answer_preview": "".join(answer_parts)[:400]}
        yield {"type": "orchestration", "phase": "end", "flow": flow}

    async def _orch_run_worker(self, mode_name: str, prompt: str, history, index: int, _sink: list[str] | None = None):
        """驱动单个执行体并转发其事件;token 同时写入 _sink 供上层汇总上下文。"""
        worker = self._make_worker(mode_name, active_tools=self._active_tools)
        async for ev in worker.run_stream(prompt, history=history):
            if ev["type"] == "token":
                if _sink is not None:
                    _sink.append(ev["content"])
                yield ev
            elif ev["type"] in ("thinking", "tool", "plan", "tool_start"):
                yield ev

    async def _orch_consensual(self, user_input: str, history: list[dict]):
        """共识投票:多个执行体并行作答,评审模型投票择优后流式输出最优答案。"""
        candidates = self._consensual_candidates()
        yield {"type": "orchestration", "phase": "parallel_start",
               "candidates": candidates,
               "message": f"并行启动 {len(candidates)} 个执行体,随后投票择优"}
        workers = [self._make_worker(m, active_tools=self._active_tools) for m in candidates]
        try:
            results = await asyncio.gather(*[w.run(user_input, history=history) for w in workers])
        except Exception as e:  # noqa: BLE001
            yield {"type": "orchestration", "phase": "task_error", "error": str(e)[:200]}
            return

        yield {"type": "orchestration", "phase": "voting",
               "message": "多执行体完成,评审模型投票择优中"}
        best = await self._judge_ensemble(user_input, list(results))
        if best is None and results:
            best = results[0]
        if best is None:
            yield {"type": "orchestration", "phase": "task_error", "error": "共识投票未产生结果"}
            return
        yield {"type": "orchestration", "phase": "vote_result",
               "winner": best.model,
               "message": f"投票选出最佳执行体: {best.model}"}
        for ch in best.answer:
            yield {"type": "token", "content": ch}

    async def _node_reflect(self, state: AgentState, graph: Graph) -> None:
        """自评反思(Reflexion 式):用 LLM 按结构化 schema 评价回答质量。

        不合格时把评审意见写回 messages,设置 reflect_rewrite 标志,
        由 _route_reflect 送回 llm 节点重写;通过则保持现状,路由停止。
        评审调用失败/超时一律视为通过,不影响主流程。
        """
        state.metadata["reflect_rounds"] = int(state.metadata.get("reflect_rounds", 0)) + 1
        state.metadata["reflect_rewrite"] = False
        answer = state.answer or ""
        if not answer.strip():
            return

        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "description": "质量评分 1-10"},
                "issues": {"type": "array", "items": {"type": "string"},
                           "description": "需要修正的具体问题,没有则为空数组"},
                "verdict": {"type": "string", "enum": ["pass", "rewrite"]},
            },
            "required": ["score", "issues", "verdict"],
        }

        def _validator(data):
            if not isinstance(data, dict):
                return False, "结果不是对象"
            if not isinstance(data.get("score"), int) or not 1 <= data["score"] <= 10:
                return False, "score 必须是 1-10 的整数"
            if data.get("verdict") not in ("pass", "rewrite"):
                return False, "verdict 必须是 pass 或 rewrite"
            if not isinstance(data.get("issues"), list):
                return False, "issues 必须是数组"
            return True, None

        review_prompt = (
            f"你是评审者。评价下面的回答是否准确、完整地满足了用户请求。\n"
            f"用户请求: {state.user_input}\n\n"
            f"回答:\n{answer}\n\n"
            f"判断标准:\n"
            f"- 是否直接回答了用户的问题\n"
            f"- 是否引用了工具结果(如有)\n"
            f"- 是否有多余/错误/幻觉内容\n"
            f"打分 1-10;\n"
            f"- 分数>=7 且无事实错误时,verdict 返回 pass\n"
            f"- 只有存在事实错误、幻觉或严重不完整时,verdict 才返回 rewrite\n"
            f"- 格式、语气、详细程度等主观问题不作为 rewrite 理由\n"
        )
        try:
            review = await self._critic().chat_json_schema(
                [
                    {"role": "system", "content": "你是严格但公正的评审者。"},
                    {"role": "user", "content": review_prompt},
                ],
                schema,
                temperature=0.2,
                validator=_validator,
            )
        except Exception as e:  # noqa: BLE001  # 评审失败视为通过
            logger.warning("反思评审失败，降级为直接通过: %s", e)
            return

        verdict = review.get("verdict", "pass")
        issues = review.get("issues") or []
        state.steps.append({
            "step": sum(1 for s in state.steps if s.get("type") == "reflect") + 1,
            "type": "reflect",
            "score": review.get("score"),
            "verdict": verdict,
            "issues": issues,
        })
        if verdict == "rewrite" and issues:
            # 只在实际问题需要修正时才重写，避免对正确答案的过度修改
            critical_issues = [i for i in issues if any(kw in str(i) for kw in
                ["错误", "幻觉", "不实", "不正确", "矛盾", "缺失关键"])]
            if not critical_issues:
                # 只有主观建议时不重写，保持原答案
                state.metadata["reflect_rewrite"] = False
                return
            state.metadata["reflect_rewrite"] = True
            state.messages.append({"role": "user", "content":
                "请根据以下反馈修正回答中的事实错误：\n- "
                + "\n- ".join(str(i) for i in critical_issues)
                + "\n\n请直接给出修正后的完整回答，不要道歉或重复之前的错误。"})

    async def _node_verify(self, state: AgentState, graph: Graph) -> None:
        """最终答案事实校验(LLM-as-judge):核对答案断言是否被工具结果支撑。

        用评审模型检查最终答案与工具结果的一致性:发现未支撑断言/幻觉时,
        直接把工具证据注入并让模型重写一次答案(内联,不走 llm 节点)。
        校验失败/无工具结果/非必要一律通过,不影响主流程。
        """
        if not self._verify_enabled or not state.answer or not state.answer.strip():
            return
        tool_results = [
            s.get("result", "") for s in state.steps
            if s.get("type") == "tool" and s.get("result")
        ]
        if not tool_results:
            return  # 没有工具结果可供交叉核对,跳过校验
        if state.metadata.get("verify_rewritten"):
            return  # 已重写过一次,不再二次重写

        evidence = "\n\n".join(tool_results)[:4000]
        schema = {
            "type": "object",
            "properties": {
                "supported": {"type": "boolean", "description": "答案是否全部被工具结果支撑"},
                "unsupported": {"type": "array", "items": {"type": "string"},
                                "description": "答案中未得到工具结果支撑的断言,没有则为空数组"},
            },
            "required": ["supported", "unsupported"],
        }

        def _validator(data):
            if not isinstance(data, dict):
                return False, "结果不是对象"
            if not isinstance(data.get("supported"), bool):
                return False, "supported 必须是布尔值"
            if not isinstance(data.get("unsupported"), list):
                return False, "unsupported 必须是数组"
            return True, None

        prompt = (
            f"你是事实核查员。下面是模型对用户请求的回答,以及回答所依据的工具结果。\n\n"
            f"用户请求: {state.user_input}\n\n"
            f"模型回答:\n{state.answer}\n\n"
            f"工具结果(证据):\n{evidence}\n\n"
            f"请判断回答中的每一个断言是否都有工具结果支撑。"
            f"不要把工具结果中不存在的信息当作支撑;含糊表述也算未支撑。"
            f"supported=true 表示全部支撑;否则 supported=false 并列出未支撑的断言。"
        )
        try:
            check = await self._critic().chat_json_schema(
                [
                    {"role": "system", "content": "你是严格但公平的事实核查员。"},
                    {"role": "user", "content": prompt},
                ],
                schema,
                temperature=0.1,
                validator=_validator,
            )
        except Exception as e:  # noqa: BLE001  # 校验失败视为通过
            logger.warning("事实校验失败，降级为直接通过: %s", e)
            return

        unsupported = check.get("unsupported") or []
        state.steps.append({
            "step": sum(1 for s in state.steps if s.get("type") == "verify") + 1,
            "type": "verify",
            "supported": bool(check.get("supported")),
            "unsupported": unsupported,
        })
        if not check.get("supported") and unsupported:
            state.metadata["verify_rewritten"] = True
            rewrite_prompt = (
                f"请根据以下工具结果修正回答中的事实错误:\n"
                f"未支撑断言:\n- " + "\n- ".join(str(u) for u in unsupported) + "\n\n"
                f"可用证据:\n{evidence}\n\n"
                f"重写要求:删除或修正所有无法由证据支撑的表述,不引入证据之外的新信息。"
                f"直接给出修正后的完整回答，不要道歉或重复之前的错误。"
                f"用户请求: {state.user_input}"
            )
            try:
                new_answer = await self.llm.chat(
                    [{"role": "user", "content": rewrite_prompt}],
                    temperature=self.temperature,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("校验后 LLM 重写回答失败: %s", e)
                return
            state.answer = new_answer
            state.steps.append({
                "step": sum(1 for s in state.steps if s.get("type") == "verify_rewrite") + 1,
                "type": "verify_rewrite",
                "content": new_answer,
            })
            state.emit({"type": "answer", "content": new_answer, "source": "verify"})

    # ---- 工具调用 ----

    @property
    def _schemas(self) -> list[dict]:
        if self._schemas_cache is None:
            if self._active_tools is None:
                tools = self.registry.list_sync()
            else:
                active = set(self._active_tools) | set(self._always_tools)
                tools = [t for t in self.registry.list_sync() if t.name in active]
            self._schemas_cache = [t.to_openai_schema() for t in tools]
        return self._schemas_cache

    async def _maybe_select_tools(self, user_input: str, steps: list[dict], step: int) -> Optional[dict]:
        """对话前用 LLM 预选相关工具子集(工具路由)。返回 SSE 事件;未路由/失败返回 None。

        优先使用增强型路由(smart_router 三级缓存+置信度),失败返回 None。
        """
        if not settings.agent_tool_router_enabled:
            return None
        if getattr(self.llm, "provider", "") == "mock":
            return None

        # 简短问候快速通道:跳过LLM路由
        cleaned = user_input.strip()
        if len(cleaned) <= _TOOL_ROUTE_SHORT_INPUT_THRESHOLD and not any(kw in cleaned for kw in _TOOL_ROUTE_SHORT_INPUT_KEYWORDS):
            return None
        # 改进：增加默认预选工具数量，从 10 提高到 20
        max_tools = min(int(settings.agent_max_tools or 20), len(await self.registry.list()))
        # 改进：提高置信度阈值，从 0.10 提高到 0.30
        min_confidence = 0.30

        # 尝试从缓存获取路由结果
        from app.core.tool_route_cache import get_tool_route_cache
        cache = get_tool_route_cache()
        cached = await cache.get(user_input)
        if cached is not None:
            logger.debug("使用缓存的工具路由结果: %d 个工具", len(cached.tools))
            self._active_tools = cached.tools
            self._schemas_cache = None
            steps.append({"step": step, "type": "tool_route", "tools": cached.tools, "reason": cached.reason, "cached": True})
            return {"type": "tool_route", "tools": cached.tools, "reason": cached.reason, "cached": True}

        # 使用智能路由(关键词+embedding+LLM 三级缓存 + 置信度评分)
        try:
            from app.core.smart_router import select_tools_enhanced
            selected, conf, reason = await select_tools_enhanced(
                self.llm, user_input, self.registry,
                max_tools=max_tools,
                min_confidence=min_confidence,
                user_tech_stack=getattr(self, "_tech_stack", []),
            )
            if selected and conf >= min_confidence:
                self._active_tools = selected
                self._schemas_cache = None
                # 写入缓存
                await cache.set(user_input, selected, conf, reason)
                steps.append({"step": step, "type": "tool_route", "tools": selected, "reason": reason})
                return {"type": "tool_route", "tools": selected, "reason": reason}
        except Exception as e:
            logger.warning("智能工具路由失败，回退到全量工具集: %s", e)

        return None

    async def _call_llm(self, messages: list[dict]) -> tuple[str, str, list[ToolCall]]:
        """调用 LLM。返回 (mode, content, tool_calls)。

        mode: "native"=原生 function calling / "text"=文本(手写 JSON)
        """
        if self._native_enabled:
            try:
                result = await self.llm.chat_with_tools(messages, tools=self._schemas, temperature=self.temperature)
                if result.tool_calls:
                    return "native", result.content, result.tool_calls
                # 原生模式无 tool_calls,但 content 可能是手写 JSON(部分 provider 忽略 tools)
                tool_calls = self._parse_tool_calls(result.content)
                if not tool_calls:
                    return "native", result.content, []
                return "text", result.content, tool_calls
            except Exception as e:  # noqa: BLE001  # 原生调用失败,自动降级
                logger.warning("native 模式调用失败，自动降级: %s", e)
                self._native_enabled = False

        resp = await self.llm.chat(messages, temperature=self.temperature)
        tool_calls = self._parse_tool_calls(resp)
        if not tool_calls:
            return "text", resp, []
        return "text", resp, tool_calls


    async def _call_llm_stream(
        self, messages: list[dict], state: AgentState
    ) -> tuple[str, str, list["ToolCall"]]:
        """流式调用 LLM:逐 token 发射事件,同时收集完整结果用于路由。"""
        if self._native_enabled:
            try:
                result_holder: dict = {"result": None}
                collected: list = []
                async for item in self.llm.chat_stream_with_tools(
                    messages, tools=self._schemas, temperature=self.temperature,
                    result_holder=result_holder,
                ):
                    if isinstance(item, dict):
                        state.emit(item)
                    else:
                        collected.append(item)
                        state.emit({"type": "token", "content": item})
                result = result_holder["result"]
                if result and result.tool_calls:
                    return "native", result.content or "", result.tool_calls
                full = "".join(collected)
                tool_calls = self._parse_tool_calls(full)
                if not tool_calls:
                    return "native", full, []
                return "text", full, tool_calls
            except Exception as e:  # noqa: BLE001
                logger.warning("native 模式调用失败，自动降级: %s", e)
                self._native_enabled = False

        # 文本模式流式
        try:
            collected: list = []
            async for tok in self.llm.chat_stream(messages, temperature=self.temperature):
                if isinstance(tok, dict):
                    state.emit(tok)
                    continue
                collected.append(tok)
                state.emit({"type": "token", "content": tok})
            full = "".join(collected)
            tool_calls = self._parse_tool_calls(full)
            if not tool_calls:
                return "text", full, []
            return "text", full, tool_calls
        except Exception as e:
            logger.error("文本模式流式调用失败: %s", e)
            raise
    async def _execute_tool(self, name: str, arguments: str) -> str:
        """执行单个工具(带瞬态错误重试/退避),统一返回 JSON 字符串结果。"""
        if name in PLAN_TOOLS:
            return await self._execute_plan_tool(name, arguments)
        if name in NO_RETRY_TOOLS:
            return await self._execute_tool_once(name, arguments)
        retries = max(0, int(settings.agent_tool_retries))
        for attempt in range(retries + 1):
            result = await self._execute_tool_once(name, arguments)
            if attempt < retries and self._is_transient_error(result):
                await asyncio.sleep(settings.agent_tool_retry_delay * (2 ** attempt))
                continue
            return result

    async def _execute_tool_once(self, name: str, arguments: str) -> str:
        # 危险工具频率限制(与 ToolExecutor 行为一致)
        if name in self._dangerous_tools:
            allowed, limit_msg = self._dangerous_rate_limiter.allow(name)
            if not allowed:
                return json.dumps({"error": limit_msg, "blocked": True}, ensure_ascii=False)
        # 工具路由裁剪:未选中的工具不允许执行,防止模型意外调用冷门工具
        if self._active_tools is not None and name not in self._active_tools and name not in self._always_tools:
            return json.dumps({"error": f"工具 {name} 未在当前任务启用(工具路由器未选中)"}, ensure_ascii=False)
        # 人工确认流:危险工具需在请求 approve_tools 中获批
        if settings.agent_tool_confirm and name in self._dangerous_tools and name not in self.approve_tools \
                and name not in self._runtime_approved:
            return json.dumps({
                "error": f"工具 {name} 需要用户确认后才能执行。请向用户说明将执行的操作,"
                         f"用户批准后再调用(批准方式:在请求 approve_tools 中带上 {name})"
            }, ensure_ascii=False)
        t0 = time.time()
        tool = self.registry.get(name)
        if tool is None:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)

        # 安全门禁:高风险操作必须等待用户人工批准(硬拦截,模型无法绕过)。
        # 已通过 approve_tools / interrupt 式运行时批准的工具直接放行。
        # 注意:此检查必须在工具缓存之前执行,防止缓存被用作绕过机制
        if settings.agent_confirm_enabled and name not in self.approve_tools \
                and name not in self._runtime_approved:
            from app.core.security import decide

            decision = decide(name, arguments)
            if decision.confirm:
                if settings.agent_confirm_interrupt:
                    # interrupt 模式下不应到达这里(预扫描已先中断);
                    # 兜底:返回拦截提示,避免绕过 pre-scan 的路径静默执行
                    return json.dumps({
                        "error": f"操作已被拦截: {decision.reason}(等待用户批准)",
                        "blocked": True,
                        "confirmation_required": True,
                    }, ensure_ascii=False)
                allowed, status = await self._await_confirmation(name, arguments, decision.reason)
                if not allowed:
                    return json.dumps({
                        "error": f"操作已被拦截: {decision.reason}(用户未批准)",
                        "blocked": True,
                        "confirmation_required": True,
                        "status": status,
                    }, ensure_ascii=False)

        try:
            result = await tool.run(arguments)
        except Exception as e:  # noqa: BLE001
            tracer.record({"type": "tool", "name": name, "ok": False,
                           "error": str(e)[:300], "duration": round(time.time() - t0, 3),
                           "args_preview": (arguments or "")[:120]})
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        tracer.record({"type": "tool", "name": name, "ok": not self._is_tool_error(result),
                       "result_len": len(result), "duration": round(time.time() - t0, 3),
                       "args_preview": (arguments or "")[:120]})
        # 缓存成功结果(去重缓存):仅安全工具缓存,失败/副作用工具不缓存
        if self._tool_cache is not None and not self._is_tool_error(result) and name in self._tool_cache_safe:
            key = (name, arguments)
            async with self._tool_cache_lock:
                if key not in self._tool_cache:
                    max_size = int(settings.agent_tool_cache_size or 0)
                    if max_size > 0 and len(self._tool_cache) >= max_size:
                        # LRU: 淘汰最久未使用的(字典第一个)
                        self._tool_cache.popitem(last=False)
                    self._tool_cache[key] = result
        return result

    def _scan_pending_confirmations(self, tool_calls: list[ToolCall]) -> list[tuple[ToolCall, object]]:
        """预扫描一批工具调用,返回需要人工确认且未批准的 (tool_call, decision)。

        判定规则统一由共享实现（app.core.tool_safety）处理两套机制:
        - agent_tool_confirm 机制的危险工具(未在 approve_tools)
        - agent_confirm_enabled 机制的 decide() 命中 confirm
        """
        from app.core.tool_safety import scan_pending_confirmations as _scan

        return _scan(tool_calls, self.approve_tools, self._runtime_approved, self._dangerous_tools)

    def _create_confirm_requests(self, pending: list[tuple[ToolCall, object]]) -> list[dict]:
        """为待确认的工具调用创建持久化确认请求,返回请求列表(供事件与 resume 使用)。"""
        from app.core.tool_safety import create_confirm_requests as _create

        return _create(pending)

    def _is_tool_approved_by_resume(self, tc: ToolCall, state: AgentState) -> bool:
        """恢复运行时,判断该工具是否已被用户批准。

        优先采用运行时批准集合;其次解析 resume 值(list/dict);
        最后回退查询持久化 confirmations.db,兼容用户已通过前端批准的情况。
        """
        from app.core.tool_safety import is_tool_approved_by_resume as _approved

        return _approved(tc, state, runtime_approved=self._runtime_approved)

    async def _await_confirmation(self, name: str, arguments: str, reason: str) -> tuple[bool, str]:
        """创建确认请求并轮询等待用户批准。返回 (是否放行, 状态)。

        请求会持久化到 confirmations.db,前端轮询 pending 接口展示批准卡片;
        用户在卡片上批准/拒绝后,此处轮询到结果并放行或拦截。
        注:interrupt 式确认流(agent_confirm_interrupt)不走此阻塞路径。
        """
        from app.core.security import describe
        from app.tools.collaboration_tool import _get_store

        try:
            params = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
        except (json.JSONDecodeError, TypeError):
            params = {}
        if not isinstance(params, dict):
            params = {"raw": str(params)}

        store = _get_store()
        rec = store.create(
            action=name,
            description=f"{reason}。\n详情: {describe(name, arguments)}",
            params=params,
        )
        deadline = time.time() + max(1, int(settings.agent_confirm_timeout))
        while time.time() < deadline:
            await asyncio.sleep(_CONFIRM_POLL_INTERVAL)
            conf = store.get(rec["id"])
            if conf:
                if conf["status"] == "approved":
                    return True, "approved"
                if conf["status"] == "rejected":
                    return False, "rejected"
        # 超时:返回标记以便前端展示明确提示
        return False, "timeout"

    _TRANSIENT_PATTERN = TRANSIENT_PATTERNS

    @classmethod
    def _is_transient_error(cls, result_str: str) -> bool:
        """判断工具结果是否为可重试的瞬态错误(网络/超时/限流/服务不可用)。"""
        try:
            data = json.loads(result_str)
            if isinstance(data, dict) and data.get("error"):
                text = str(data["error"]).lower()
            else:
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        return any(pat in text for pat in cls._TRANSIENT_PATTERN)

    async def _execute_plan_tool(self, name: str, arguments: str) -> str:
        """执行计划工具(set_plan/update_plan),更新 Agent 私有计划状态并返回 JSON。"""
        self._plan, result = apply_plan_tool(self._plan, name, arguments)
        self._plan_dirty = True
        return json.dumps(result, ensure_ascii=False)

    async def _execute_batch(
        self, tool_calls: list[ToolCall], steps: list[dict], step: int
    ) -> list[tuple[ToolCall, str, bool]]:
        """并行执行一批工具调用,记录执行步骤。"""
        from app.tools.memory_tool import _current_session

        # 透传当前 session_id 给记忆工具(memory_save/memory_search)
        token = _current_session.set(self.session_id)
        # 透传父 Agent 的模型/会话上下文给委派工具(delegate_task 据此创建子智能体)
        from app.tools.delegation_tool import set_delegate_context

        d_token = set_delegate_context(self.llm, self.session_id, self._active_tools)
        try:
            results = await asyncio.gather(
                *[self._execute_tool(tc.name, tc.arguments) for tc in tool_calls]
            )
        finally:
            _current_session.reset(token)
            from app.tools.delegation_tool import _delegate_parent

            _delegate_parent.reset(d_token)
        batch: list[tuple[ToolCall, str, bool]] = []
        for tc, result_str in zip(tool_calls, results):
            is_err = self._is_tool_error(result_str)
            steps.append({
                "step": step + 1,
                "type": "tool",
                "tool": tc.name,
                "arguments": tc.arguments,
                "result": result_str,
            })
            batch.append((tc, result_str, is_err))
        return batch

    # ---- 代码级自愈 ----

    async def _maybe_self_heal(
        self, state: AgentState,
        batch: list[tuple[ToolCall, str, bool]], step: int,
    ) -> list[tuple[ToolCall, str, bool]]:
        """对失败的代码类工具(run_code/run_terminal)尝试自动修复。

        调用 app.core.code_heal:LLM 分析失败输出→生成修复 patch→应用→重跑。
        自愈成功后用新结果替换原失败结果,并在 steps 记录 heal 步骤;
        自愈失败/未配置/非代码工具原样返回,交给后续反思逻辑。
        """
        if not settings.agent_code_self_heal:
            return batch
        if getattr(self.llm, "provider", "") == "mock":
            return batch
        out: list[tuple[ToolCall, str, bool]] = []
        heal_rounds = int(state.metadata.get("heal_rounds", 0))
        max_rounds = max(1, int(settings.agent_code_heal_rounds or 1))
        # 带"修复后仍失败"标记的结果不再重复自愈(收敛),原样交给后续反思
        for tc, result_str, is_err in batch:
            if not is_err or tc.name not in ("run_code", "run_terminal") \
                    or heal_rounds >= max_rounds or "heal_still_failing" in result_str:
                out.append((tc, result_str, is_err))
                continue
            try:
                from app.core.code_heal import heal_code_failure

                result = await heal_code_failure(
                    self.llm,
                    state.user_input,
                    tc.name,
                    tc.arguments,
                    result_str,
                    max_rounds=max_rounds - heal_rounds,
                )
            except Exception as e:  # noqa: BLE001  # 自愈失败不影响主循环
                logger.warning("代码自愈失败，保持原始错误结果: %s", e)
                result = {"healed": False}
            state.metadata["heal_rounds"] = heal_rounds + 1
            state.steps.append({
                "step": step + 1,
                "type": "heal",
                "tool": tc.name,
                "healed": bool(result.get("healed")),
                "attempts": result.get("rounds", 0),
                "details": result.get("steps", [])[-3:],
            })
            if result.get("healed"):
                out.append((tc, result.get("final", ""), False))
            else:
                out.append((tc, result_str, True))
        return out

    def _append_reflection(
        self, messages: list[dict], batch: list[tuple[ToolCall, str, bool]], failures: dict[str, int]
    ) -> Optional[str]:
        """对失败的工具体现反思提示;终结型工具成功则返回工具名。"""
        error_names = [tc.name for tc, _r, is_err in batch if is_err]
        terminal = None
        for tc, result_str, is_err in batch:
            tool = self.registry.get(tc.name)
            if tool is not None and tool.terminal and not is_err:
                terminal = tc.name
        for name in error_names:
            failures[name] = failures.get(name, 0) + 1
        if error_names:
            # 测试/构建失败:给出定向的"定位→修改→重跑"闭环提示
            name_to_result = {tc.name: result_str for tc, result_str, _e in batch}
            test_fail = [n for n in error_names if self._looks_like_test_failure(n, name_to_result.get(n, ""))]
            if test_fail:
                hint = (
                    "检测到测试/构建失败(" + "、".join(test_fail)
                    + ")。请根据失败输出定位原因:先用 code_context 查看相关源码,"
                    "再用 edit_file/write_file 修改代码,然后重新运行测试确认通过。不要重复运行相同命令。"
                )
                messages.append({"role": "user", "content": hint})
                return terminal
            stubborn = [n for n in set(error_names) if failures[n] >= 2]
            if stubborn:
                hint = "以下工具已连续失败,请不要再使用它们,改用其他工具或方案:" + "、".join(stubborn)
            else:
                hint = (
                    "以下工具执行失败:" + "、".join(error_names)
                    + "。请分析原因,修正参数或换用其他工具重试,不要重复相同的调用。"
                )
            messages.append({"role": "user", "content": hint})
        return terminal

    @staticmethod
    def _is_tool_error(result_str: str) -> bool:
        try:
            data = json.loads(result_str)
            if isinstance(data, dict):
                if data.get("error"):
                    return True
                # 终端命令失败/被拦截也视为失败,触发反思(否则测试失败无法进入修复循环)
                if data.get("success") is False or data.get("blocked") is True:
                    return True
        except (json.JSONDecodeError, TypeError):
            pass
        return False

    # ---- 测试/构建失败闭环 ----

    _TEST_FAIL_MARKERS = TEST_FAIL_MARKERS

    @classmethod
    def _looks_like_test_failure(cls, name: str, result_str: str) -> bool:
        """判断工具结果是否为测试/构建失败输出(用于给出定向修复提示)。"""
        if name != "run_terminal":
            return False
        try:
            data = json.loads(result_str)
            if not isinstance(data, dict):
                return False
        except (json.JSONDecodeError, TypeError):
            return False
        out = str(data.get("stdout", "")) + "\n" + str(data.get("stderr", ""))
        low = out.lower()
        return any(m in low for m in cls._TEST_FAIL_MARKERS)

    # ---- 任务规划(Plan-then-Execute) ----

    async def _plan_task(self, user_input: str, steps: list[dict], step: int) -> bool:
        """复杂任务先生成执行计划。返回是否创建了计划。"""
        self._plan = None
        self._plan_dirty = False
        if not settings.agent_planning:
            return False
        plan = await self._maybe_plan(user_input)
        if plan is None:
            return False
        self._plan = plan
        self._plan_dirty = False
        steps.append({"step": step, "type": "plan", "plan": plan.to_dict()})
        return True

    async def _maybe_plan(self, user_input: str) -> Optional[TaskPlan]:
        """让 LLM 判断复杂度并生成计划;失败/简单任务返回 None,不影响主循环。"""
        # 简短消息跳过规划
        cleaned = user_input.strip()
        if len(cleaned) <= _PLAN_SHORT_INPUT_THRESHOLD and not any(kw in cleaned for kw in _PLAN_SHORT_INPUT_KEYWORDS):
            return None
        try:
            plan = await generate_plan(self.llm, user_input, max_steps=settings.agent_plan_max_steps)
        except Exception as e:  # noqa: BLE001
            logger.warning("任务规划生成失败，放弃分步计划: %s", e)
            return None
        if plan is None or not plan.steps:
            return None
        plan.set_current(0)
        return plan

    async def _maybe_replan(
        self, state: AgentState, batch: list[tuple[ToolCall, str, bool]]
    ) -> bool:
        """工具连续失败时,基于失败原因生成修订版执行计划并替换旧计划。

        触发条件:存在计划、同工具连续失败 ≥ agent_replan_after_failures、
        未超过最大重规划次数、非 mock LLM。
        失败/无计划/超预算均静默返回 False,不影响主流程。
        """
        if not settings.agent_replan_enabled or self._plan is None:
            return False
        if getattr(self.llm, "provider", "") == "mock":
            return False
        replan_times = int(state.metadata.get("replan_times", 0))
        if replan_times >= settings.agent_replan_max_times:
            return False
        # 找出本轮失败的且已连续失败达阈值的工具
        errors = []
        for tc, result_str, is_err in batch:
            if is_err and state._failures.get(tc.name, 0) >= settings.agent_replan_after_failures:
                errors.append(f"- {tc.name}(参数: {tc.arguments}) → {result_str[:200]}")
        if not errors:
            return False

        schema = {
            "type": "object",
            "properties": {
                "needs_plan": {"type": "boolean"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["title", "description"],
                    },
                },
            },
            "required": ["needs_plan", "steps"],
        }
        prompt = (
            f"你正在执行一个任务,现有计划的部分步骤执行失败,需要你重新规划。\n\n"
            f"原始任务: {state.user_input}\n\n"
            f"现有计划:\n" + "\n".join(f"{i+1}. [{s.status}] {s.title} - {s.description}"
                                      for i, s in enumerate(self._plan.steps))
            + "\n\n"
            f"失败的执行:\n" + "\n".join(errors) + "\n\n"
            f"请输出修订后的完整计划(needs_plan=true 并给出 steps)。"
        )
        try:
            data = await self.llm.chat_json_schema(
                [{"role": "system", "content": "你是任务规划助手,输出 JSON。"},
                 {"role": "user", "content": prompt}],
                schema,
                temperature=0.2,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("任务规划 JSON 解析失败，降级为单步执行: %s", e)
            return False
        if not isinstance(data, dict) or not data.get("needs_plan"):
            return False
        steps = _normalize_steps(data.get("steps") or [], settings.agent_plan_max_steps)
        if not steps:
            return False
        state.metadata["replan_times"] = replan_times + 1
        self._plan = TaskPlan(steps)
        self._plan_dirty = True
        state.plan = self._plan.to_dict()
        state.steps.append({
            "step": len(state.steps) + 1,
            "type": "plan",
            "action": "replan",
            "reason": "工具连续失败,已重新规划",
            "plan": self._plan.to_dict(),
        })
        return True

    def _flush_plan(self, steps: list[dict], step: int) -> Optional[dict]:
        """计划状态变更后,记录到 steps 并返回 SSE 事件(无变更返回 None)。"""
        if not self._plan_dirty:
            return None
        self._plan_dirty = False
        plan_data = self._plan.to_dict() if self._plan else None
        steps.append({"step": step + 1, "type": "plan", "plan": plan_data})
        return {"type": "plan", "action": "update", "plan": plan_data}

    def _finalize_plan(self, steps: list[dict], step: int) -> Optional[dict]:
        """最终回答前,把剩余步骤标记完成并刷新计划状态。"""
        if self._plan is None:
            return None
        if not self._plan.is_complete():
            for s in self._plan.steps:
                if s.status in ("pending", "in_progress"):
                    s.status = "done"
            self._plan_dirty = True
        return self._flush_plan(steps, step)

    # ---- 路由 / 记忆 / 摘要 ----

    async def _maybe_route(self, user_input: str) -> None:
        """按任务复杂度切换模型(仅当显式启用路由)。"""
        if not self.route:
            return
        if getattr(self.llm, "provider", "") == "mock":
            return
        from app.core.smart_router import build_routed_llm

        routed = build_routed_llm(user_input)
        if routed is not None:
            self.llm = routed
            self._native_enabled = settings.agent_native_tools and hasattr(self.llm, "chat_with_tools")

    async def _prepare_history(self, history: list[dict]) -> list[dict]:
        """历史超预算时,用摘要压缩旧消息(替代粗暴截断)。

        使用 token 级预算(agent_token_budget>0 时),否则沿用字符预算。
        """
        if not history or not settings.agent_summarize:
            return history
        if self.token_budget and self.token_budget > 0:
            from app.memory.context import layered_compress

            processed, _stats = await layered_compress(
                self.llm,
                history,
                max_tokens=int(self.token_budget),
                head_tokens=0,
                min_keep=6,
            )
            return processed
        
        # 改进：使用 token 估算替代字符计数，更准确
        from app.memory.context import estimate_tokens
        total_tokens = sum(
            estimate_tokens(str(m.get("content", ""))) for m in history if m.get("role") in ("user", "assistant")
        )
        
        # 字符预算转换为 token 预算（约 4 字符/token）
        token_budget = int(self.context_budget / 4)
        summary_threshold = int(token_budget * settings.agent_summary_threshold_ratio)
        
        if total_tokens <= summary_threshold:
            return history
        
        # 改进：提高保留比例，从 50% 提高到 60%
        keep: list[dict] = []
        acc = 0
        keep_budget = int(token_budget * 0.6)  # 从 0.5 提高到 0.6
        for m in reversed(history):
            c = estimate_tokens(str(m.get("content", "")))
            if acc + c <= keep_budget:
                keep.insert(0, m)
                acc += c
            else:
                break
        old = history[: len(history) - len(keep)]
        if len(old) < 2:
            return history
        from app.memory.summarize import summarize_messages_async

        summary = await summarize_messages_async(self.llm, old)
        if summary:
            return [{
                "role": "system",
                "content": f"以下是本会话早期内容的摘要(请据此理解前文背景):\n{summary}",
            }] + keep
        return history

    async def _build_memory_note_async(self, user_input: str, history: list[dict]) -> str:
        """异步检索分层记忆(working/semantic/episodic),构造注入提示块。

        分层记忆开启时按三层注入;失败回退旧的单一 flat 检索。
        """
        if not settings.agent_memory_enabled:
            return ""
        try:
            from app.memory.layers import build_layered_memory_note, enable_layered

            if enable_layered():
                note = await build_layered_memory_note(
                    user_input, history, self.session_id, self.llm,
                )
                if note:
                    return note
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆沉淀主流程失败: %s", e)  # 分层失败回退旧逻辑
        try:
            from app.memory.ltm import get_memory_store

            store = get_memory_store()
            mems = store.search(user_input, top_k=4, count_hit=True)
            if self.session_id:
                seen = {m["id"] for m in mems}
                for m in store.search(
                    user_input, top_k=4, scope="session", session_id=self.session_id, count_hit=True
                ):
                    if m["id"] not in seen:
                        mems.append(m)
                        seen.add(m["id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("长期记忆检索失败，不回退历史上下文: %s", e)
            return ""
        if not mems:
            return ""
        lines = [f"- {m['content']}" for m in mems]
        return "根据长期记忆,以下信息可能与当前对话相关:\n" + "\n".join(lines)

    async def _maybe_auto_memory(self, user_input: str, answer: str, history: list[dict]) -> None:
        """对话结束后自动抽取事实和经验存入长期记忆(默认关闭)。

        不仅抽取事实性信息,还从工具执行步骤中提取成功经验/失败模式。
        """
        if not settings.agent_auto_memory:
            return
        try:
            recent = (history or [])[-_MEMORY_RECENT_HISTORY_COUNT:]
            prompt = [
                {
                    "role": "system",
                    "content": (
                        "从以下对话中抽取值得长期记住的内容。包括:\n"
                        "1. 事实:用户偏好、个人信息、项目事实、约定(每条不超过30字)\n"
                        "2. 经验:成功的工具调用模式和失败教训(如'使用 search_files 找到目标最快')\n"
                        "若没有值得记住的,返回空数组。\n"
                        '只输出 JSON,格式为 {"facts": ["内容1", "内容2"]}'
                        " facts 数组的每个元素必须是字符串，不能是对象。"
                    ),
                },
                {"role": "user", "content": _clip_history_for_json(recent)},
            ]
            if hasattr(self.llm, "chat_json"):
                data = await self.llm.chat_json(prompt, temperature=0.2)
            else:
                resp = await self.llm.chat(prompt, temperature=0.2)
                data = self._try_parse_json(resp)
            facts = data.get("facts", []) if isinstance(data, dict) else []
            # 规范化 facts: 可能是字符串列表，也可能是对象列表
            normalized_facts = []
            for f in facts:
                if isinstance(f, str) and f.strip():
                    normalized_facts.append(f.strip())
                elif isinstance(f, dict):
                    # 对象格式: 合并所有字段为字符串
                    parts = []
                    for k, v in f.items():
                        if v and str(v).strip():
                            parts.append(f"{k}: {v}")
                    if parts:
                        normalized_facts.append("、".join(parts))
            if normalized_facts:
                from app.memory.ltm import get_memory_store

                store = get_memory_store()
                max_facts = int(settings.agent_auto_memory_max_facts or 5)
                for f in normalized_facts[:max_facts]:
                    if isinstance(f, str) and f.strip():
                        store.save(f.strip(), source="auto")
                # 分层记忆写侧:同一事实同时写入 episodic(本会话情节作用域),
                # 与 layers._add_episodic 的检索口径对齐。
                if settings.agent_layered_memory and self.session_id:
                    max_episodic = int(settings.agent_auto_memory_max_episodic or 3)
                    for f in normalized_facts[:max_episodic]:
                        if isinstance(f, str) and f.strip():
                            try:
                                store.save(
                                    f.strip(),
                                    scope="session",
                                    session_id=self.session_id,
                                    source="auto",
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.warning("会话记忆保存失败: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆沉淀主流程失败: %s", e)

    @staticmethod
    def _try_parse_json(text: str):
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    # ---- 消息构造 ----

    def with_multimodal(self, parts: list[dict]) -> "Agent":
        """注入多模态内容块(OpenAI content parts),随后随用户消息一起送 LLM。"""
        self.multimodal_parts = list(parts or [])
        return self

    def _build_user_content(self, user_input: str) -> str | list[dict]:
        """构造最终用户消息 content:纯文本或 content parts 数组。

        视觉可用时返回 [text, image_url...];模型不支持视觉时把图片降级为
        文本提示,不让模型静默丢失图片信息。
        """
        if not self.multimodal_parts:
            return user_input
        model = self.llm.info.get("model", "")
        images = [p for p in self.multimodal_parts if p.get("type") == "image_url"]
        from app.core.multimodal import model_supports_vision

        if not model_supports_vision(model) or not settings.multimodal_vision_enabled:
            n = len(images)
            from app.core.multimodal import vision_notice

            notice = vision_notice(model, n) if n else ""
            text_parts = [p.get("text", "") for p in self.multimodal_parts if p.get("type") == "text"]
            text = "\n".join(t for t in text_parts if t)
            combined = (user_input.strip() + "\n" if user_input.strip() else "") + notice
            if text:
                combined = combined + "\n" + text
            return combined or user_input
        parts: list[dict] = [{"type": "text", "text": user_input}]
        for p in self.multimodal_parts:
            if p.get("type") in ("text", "image_url", "input_audio"):
                parts.append(p)
        return parts

    def _build_messages(self, user_input: str, history: list[dict]) -> list[dict]:
        schemas = self._schemas
        tools_desc = build_tools_description(schemas)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system = build_system_prompt(tools_desc, now_str, custom=self.system_prompt)

        memory_note = self._memory_note
        if memory_note:
            system += "\n\n" + memory_note

        if self._plan is not None:
            system += (
                "\n\n当前任务执行计划(请严格按步骤推进:每完成一步用 update_plan 标记进度,"
                "发现计划不合适应立即用 set_plan 修订):\n" + self._plan.summary()
            )

        messages: list[dict] = [{"role": "system", "content": system}]
        if settings.agent_token_budget and settings.agent_token_budget > 0:
            from app.memory.context import estimate_tokens

            head_tokens = estimate_tokens(system)
            budget = int(settings.agent_token_budget)
            from app.memory.context import budget_messages

            # 兼容新旧格式:assistant 消息内容可能在 kind 字段
            clipped = []
            for msg in history:
                if msg.get("role") in ("tool", "plan"):
                    continue
                # 获取实际内容:优先 content,其次 kind
                raw_content = msg.get("content", "") or msg.get("kind", "")
                if not str(raw_content).strip():
                    continue
                content = str(raw_content)
                if len(content) > settings.message_char_cap:
                    content = content[: settings.message_char_cap] + "\n...[内容过长已截断]"
                clipped.append({**msg, "content": content})
            messages += budget_messages(clipped, max_tokens=budget, head_tokens=head_tokens, min_keep=4)
        else:
            total_chars = len(system)
            budget = settings.context_char_budget
            for msg in history:
                if msg.get("role") in ("tool", "plan"):
                    continue  # 工具/计划事件仅用于前端展示,不进入模型上下文
                # 获取实际内容:优先 content,其次 kind
                content = msg.get("content", "") or msg.get("kind", "")
                if not str(content).strip():
                    continue  # 跳过空内容消息,避免 LLM 返回 400
                if isinstance(content, str) and len(content) > settings.message_char_cap:
                    content = content[: settings.message_char_cap] + "\n...[内容过长已截断]"
                line = f"{msg.get('role', '')}:{content}"
                if total_chars + len(line) > budget:
                    break
                messages.append({**msg, "content": content})
                total_chars += len(line)
        messages.append({"role": "user", "content": self._build_user_content(user_input)})
        return messages

    @staticmethod
    def _parse_tool_call(text: str) -> Optional[tuple[str, str]]:
        """从 LLM 回复中解析工具调用 JSON(单条)。

        注意:此方法仅解析第一条工具调用。如果 LLM 返回多条调用,
        应使用 _parse_tool_calls 解析全部。
        """
        if not text or not text.strip():
            return None
        # 尝试匹配 JSON 数组格式 [...],提取第一条
        stripped = text.strip()
        if stripped.startswith('['):
            try:
                data = json.loads(stripped)
                if isinstance(data, list) and data:
                    first = data[0]
                    if isinstance(first, dict) and "tool" in first:
                        return first["tool"], json.dumps(first.get("arguments", {}), ensure_ascii=False)
            except json.JSONDecodeError:
                pass
            # 数组解析失败,继续尝试单条匹配
        # 尝试匹配单条 JSON 对象格式 {...}
        m = re.search(TOOL_CALL_PATTERN, text, re.DOTALL)
        if m:
            return m.group(1), m.group(2)
        # 尝试宽松解析: 独立 JSON 对象含 tool 字段
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "tool" in data:
                args = data.get("arguments", {})
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                return data["tool"], args
        except json.JSONDecodeError:
            pass
        return None

    @staticmethod
    def _parse_tool_calls(text: str) -> list[ToolCall]:
        """从 LLM 回复中解析所有工具调用 JSON(支持单条和数组格式)。"""
        if not text or not text.strip():
            return []
        stripped = text.strip()
        calls: list[ToolCall] = []
        # 尝试解析 JSON 数组
        if stripped.startswith('['):
            try:
                data = json.loads(stripped)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "tool" in item:
                            args = item.get("arguments", {})
                            if isinstance(args, dict):
                                args = json.dumps(args, ensure_ascii=False)
                            calls.append(ToolCall(id="", name=item["tool"], arguments=args))
                    if calls:
                        return calls
            except json.JSONDecodeError:
                pass
        # 尝试正则匹配多条 JSON 对象
        for m in re.finditer(TOOL_CALL_PATTERN, text, re.DOTALL):
            calls.append(ToolCall(id="", name=m.group(1), arguments=m.group(2)))
        # 尝试宽松解析单条
        if not calls:
            parsed = Agent._parse_tool_call(text)
            if parsed:
                calls.append(ToolCall(id="", name=parsed[0], arguments=parsed[1]))
        return calls
