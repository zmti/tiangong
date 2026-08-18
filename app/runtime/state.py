"""Agent 可序列化状态:图运行时在节点间传递的唯一数据载体。

设计要点:
- 所有字段必须是 JSON 可序列化(plan/steps/messages 用 dict/list 表达),
  保证 CheckpointManager 能把任意时刻的运行现场完整落盘。
- llm/registry 等"运行时可执行对象"不进入 state,由 Agent 持有并在恢复时重建。
- node 记录最近一次成功执行的节点名,作为恢复的起点(恢复从下一个节点继续)。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Optional


class AgentState:
    """一次图运行的全部可变状态。"""

    def __init__(
        self,
        run_id: Optional[str] = None,
        session_id: str = "",
        user_input: str = "",
    ):
        self.run_id = run_id or uuid.uuid4().hex
        self.session_id = session_id
        self.user_input = user_input

        # 对话上下文(传给 LLM 的 messages,含 system 首条)
        self.messages: list[dict] = []
        # 执行轨迹(plan/tool/tool_route/answer 等步骤,供前端展示与 eval)
        self.steps: list[dict] = []
        # 任务计划(JSON dict,来自 planner.TaskPlan.to_dict)
        self.plan: Optional[dict] = None
        # 工具路由预选的工具子集(None=未路由/全量)
        self.active_tools: Optional[list[str]] = None
        # 本次运行的人工批准工具
        self.approve_tools: list[str] = []

        # 运行元数据(模型/provider/路由信息等,透传到 trace)
        self.metadata: dict[str, Any] = {}

        # 生命周期
        self.status: str = "pending"  # pending | running | done | failed | interrupted
        self.node: str = ""  # 最近成功执行的节点名(恢复起点)
        self.answer: str = ""
        self.error: str = ""
        # 中断暂停点(LangGraph interrupt 语义):
        #   {"value": <抛给外部查看的载荷>, "resume": <外部回填的恢复值>}
        self.interrupt: Optional[dict] = None

        # 节点内产生的 SSE 事件缓冲(引擎每步结束清空并返回给调用方)
        self.events: list[dict] = []

        # 节点内实时事件回调:流式场景(graph.arun_stream)挂载后,emit 立即可见;
        # 非流式恒为 None,buffer 语义不变。运行时可执行对象,不参与持久化。
        self._on_event: Optional[Callable[[dict], None]] = None

        # 内部字段: 执行步数、工具失败计数
        self._step_index: int = 0
        self._failures: dict[str, int] = {}
        self._created_at: float = time.time()
        self._updated_at: float = time.time()

    # ---- 事件 ----

    def emit(self, event: dict) -> None:
        """节点内产出 SSE 事件,引擎在节点结束时统一返回并清空。

        流式场景(arun_stream 挂载 _on_event)下,事件还会立即转发给回调,
        从而在节点(如 finalize 逐 token 生成)尚未结束时就能实时推送出去。
        """
        self.events.append(event)
        hook = self._on_event
        if hook is not None:
            try:
                hook(event)
            except Exception:  # noqa: BLE001
                pass

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "user_input": self.user_input,
            "messages": self.messages,
            "steps": self.steps,
            "plan": self.plan,
            "active_tools": self.active_tools,
            "approve_tools": self.approve_tools,
            "metadata": self.metadata,
            "status": self.status,
            "node": self.node,
            "answer": self.answer,
            "error": self.error,
            "interrupt": self.interrupt,
            "_step_index": self._step_index,
            "_failures": self._failures,
            "_created_at": self._created_at,
            "_updated_at": self._updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        st = cls(
            run_id=data.get("run_id"),
            session_id=data.get("session_id", ""),
            user_input=data.get("user_input", ""),
        )
        st.messages = data.get("messages", [])
        st.steps = data.get("steps", [])
        st.plan = data.get("plan")
        st.active_tools = data.get("active_tools")
        st.approve_tools = data.get("approve_tools", [])
        st.metadata = data.get("metadata", {})
        st.status = data.get("status", "pending")
        st.node = data.get("node", "")
        st.answer = data.get("answer", "")
        st.error = data.get("error", "")
        st.interrupt = data.get("interrupt")
        st._step_index = int(data.get("_step_index", 0) or 0)
        st._failures = data.get("_failures", {}) or {}
        st._created_at = float(data.get("_created_at", time.time()) or time.time())
        st._updated_at = float(data.get("_updated_at", time.time()) or time.time())
        return st

    @classmethod
    def from_json(cls, text: str) -> "AgentState":
        return cls.from_dict(json.loads(text))

    # ---- 摘要视图(供运行列表展示) ----

    def summary(self) -> dict:
        tool_steps = [s for s in self.steps if s.get("type") == "tool"]
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "input": self.user_input[:120],
            "status": self.status,
            "node": self.node,
            "steps": len(self.steps),
            "tools": len(tool_steps),
            "answer": self.answer[:200] if self.answer else "",
            "error": self.error[:300] if self.error else "",
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }
