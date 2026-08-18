"""图式编排引擎:节点 + 边 + 条件路由 + 并行分支 + 循环(带步数预算)。

对标 LangGraph 的核心抽象,零第三方依赖:

- `add_node(name, fn)`: 注册一个节点。fn 签名为
      async def fn(state: AgentState, graph: Graph) -> Any
  节点通过修改 state 完成工作;返回值作为"节点输出"(供条件路由/并行合并使用)。
- `add_edge(a, b)`: 静态边,a 完成后进入 b。
- `add_conditional_edge(a, fn)`: 条件边,fn(state) 返回下一个节点名(返回 None/END 停止)。
- `add_parallel(branches, merge)`: 并行节点,branches 为若干条线性节点管线,
  用 asyncio.gather 并发执行,全部完成后 merge(state, branch_states) 合并回主状态。
- 循环:条件边/静态边可以指回前序节点(如 llm -> tools -> llm),由 max_iters 兜底防死循环。
- checkpoint:每执行完一个节点,把 state(含"下一个节点"指针)持久化;
  进程重启后从 checkpoint 恢复即可断点续跑。

状态机:state.node 表示"最近一次成功执行的节点的下一个节点"(即恢复起点)。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Awaitable, Callable, Optional

from app.runtime.state import AgentState

logger = logging.getLogger(__name__)

END = "__end__"

# 节点函数签名: async def fn(state, graph) -> Any
NodeFunc = Callable[[AgentState, "Graph"], Awaitable[Any]]
# 条件路由函数签名: (state) -> node_name | None
RouterFunc = Callable[[AgentState], Optional[str]]
RouterFuncAsync = Callable[[AgentState], Awaitable[Optional[str]]]
# 并行合并函数签名: async def merge(state, branch_states) -> None
MergeFunc = Callable[[AgentState, list[AgentState]], Awaitable[None]]


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


class GraphInterrupt(Exception):
    """节点内主动暂停图执行,等待外部输入后恢复(LangGraph interrupt 语义)。

    节点在需要人机交互时调用 `graph.interrupt(state, value)` 抛出此异常;
    引擎捕获后把当前运行现场落盘并返回控制权,由外部决定何时 resume。
    """

    def __init__(self, value):
        super().__init__("graph_interrupt")
        self.value = value


class Send:
    """动态扇出指令(LangGraph Send 语义):把一条分支作为独立子任务并行执行。

    节点返回 `[Send(branch, payload), Send(branch, payload), ...]` 时,
    引擎为每个 Send 复制当前状态、按 payload 改写后并发运行该分支,
    全部完成后把各分支的 steps/events 合并回主状态,再继续执行主图的后续节点。

    Args:
        branch: 子分支线性节点管线(按顺序执行,不允许嵌套并行)。
        payload: dict,可覆盖子分支状态的任意字段(如 user_input / metadata)。
    """

    def __init__(self, branch: list[str], payload: Optional[dict] = None):
        self.branch = branch
        self.payload = payload or {}


class Node:
    """单个图节点。"""

    def __init__(self, name: str, func: NodeFunc):
        self.name = name
        self.func = func

    async def __call__(self, state: AgentState, graph: "Graph") -> Any:
        result = self.func(state, graph)
        if asyncio.iscoroutine(result):
            return await result
        return result


class ParallelNode(Node):
    """并行节点:把若干条线性节点管线并发执行,再合并回主状态。

    branches: list[list[str]] —— 每条子列表是一条线性节点管线(按顺序执行)。
    merge: async def merge(state, branch_states) -> None —— 把各分支结果合并回主状态;
           默认实现把各分支的 steps/events 追加到主状态,丢弃其余。
    """

    def __init__(self, name: str, branches: list[list[str]],
                 merge: Optional[MergeFunc] = None):
        super().__init__(name, self._run)
        self.branches = branches
        self._merge = merge

    async def _run(self, state: AgentState, graph: "Graph") -> Any:
        tasks = [
            graph._run_branch(branch, state) for branch in self.branches
        ]
        branch_states: list[AgentState] = list(await asyncio.gather(*tasks))
        if self._merge is not None:
            await self._merge(state, branch_states)
        else:
            for bs in branch_states:
                state.steps.extend(bs.steps)
                state.events.extend(bs.events)
        return branch_states


class Graph:
    def __init__(
        self,
        name: str = "graph",
        checkpoint: Optional[Any] = None,
        max_iters: int = 200,
        max_node_iters: int = 0,
        checkpoint_every_node: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        self.name = name
        self.checkpoint = checkpoint  # CheckpointManager 或 None
        self.max_iters = max_iters
        self.max_node_iters = max_node_iters  # 绝对节点数上限(0=关闭),兜底防死循环
        self.checkpoint_every_node = checkpoint_every_node
        self._cancel_check = cancel_check  # 可选: 取消检查回调 (返回 True 表示已取消)

        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, str] = {}  # a -> b(静态边)
        self._conditional: dict[str, RouterFunc] = {}  # a -> fn(state) -> next
        self._entry: Optional[str] = None
        # 流式运行时的实时事件转发器(arun_stream 挂载,节点内 emit 立即调用)
        self._stream_hook: Optional[Callable[[dict], None]] = None

    # ---- 构建 ----

    def add_node(self, node: Node) -> Node:
        self._nodes[node.name] = node
        return node

    def node(self, name: str):
        """装饰器用法: @g.node('name') async def fn(state, graph): ..."""
        def _decorate(func):
            self.add_node(Node(name, func))
            return func
        return _decorate

    def set_entry(self, name: str) -> None:
        self._entry = name

    def add_edge(self, a: str, b: str) -> None:
        self._edges[a] = b

    def add_conditional_edge(self, a: str, fn: RouterFunc) -> None:
        self._conditional[a] = fn

    # ---- 运行 ----

    def _checkpoint(self, state: AgentState) -> None:
        if self.checkpoint is not None:
            try:
                self.checkpoint.save(state)
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkpoint save failed for run %s: %s", state.run_id, exc)

    def interrupt(self, state: AgentState, value) -> None:
        """节点内主动暂停图执行(LangGraph interrupt 语义)。

        抛出 GraphInterrupt 让引擎捕获,把当前现场落盘并返回控制权;
        恢复时重入当前节点,此时 state.interrupt 携带外部回填的 resume 值。
        """
        state.interrupt = {"value": value, "resume": None}
        raise GraphInterrupt(value)

    async def _resolve_next(self, state: AgentState, node_name: str, ret: Any) -> Optional[str]:
        """决定节点 node_name 执行完后的下一个节点。

        优先级:节点返回值(str,END 表示停止) > 条件边 > 静态边 > 停止。
        """
        if isinstance(ret, str):
            if ret == END:
                return None
            if ret in self._nodes:
                return ret
        cond = self._conditional.get(node_name)
        if cond is not None:
            nxt = await _maybe_await(cond(state))
            return None if nxt in (None, END) else nxt
        edge = self._edges.get(node_name)
        return None if edge in (None, END) else edge

    async def astep(self, state: AgentState) -> tuple[list[dict], bool]:
        """执行恰好一个节点。返回 (events, finished)。

        finished=True 表示本轮运行终止:可能是正常到达 END、
        节点异常失败,或节点主动 interrupt(暂停等待外部输入)。
        """
        # 节点间 cancel 检查:防止长时间任务无法中断
        if self._cancel_check and self._cancel_check():
            state.status = "cancelled"
            state.node = END
            self._checkpoint(state)
            return [], True
        node_name = state.node or self._entry
        if node_name in (None, END):
            return [], True
        node = self._nodes.get(node_name)
        if node is None:
            raise RuntimeError(f"图 {self.name} 中没有节点 {node_name}")

        state.status = "running"
        state.node = node_name
        state._step_index += 1
        # 流式模式下让节点内 emit 实时转发,节点结束后恢复原状(支持嵌套/复用)
        prev_hook = state._on_event
        if self._stream_hook is not None:
            state._on_event = self._stream_hook
        try:
            ret = await node(state, self)
        except GraphInterrupt:
            # 节点主动暂停:保留 node 指针指向当前节点,resume 时重入
            state._on_event = prev_hook
            state.status = "interrupted"
            state.node = node_name
            self._checkpoint(state)
            events = list(state.events)
            state.events = []
            return events, True
        except Exception as e:  # noqa: BLE001  # 节点异常按失败终止,便于 checkpoint 观察
            state._on_event = prev_hook
            state.status = "failed"
            state.error = str(e)[:500]
            state.node = END
            self._checkpoint(state)
            events = list(state.events)
            state.events = []
            return events, True
        state._on_event = prev_hook

        nxt = await self._resolve_next(state, node_name, ret)
        # 动态扇出:节点返回 Send 列表时,先并行执行各分支再继续
        if isinstance(ret, list) and any(isinstance(r, Send) for r in ret):
            state.node = node_name
            self._checkpoint(state)
            await self._apply_send(state, ret)
        state.node = nxt if nxt not in (None, END) else END

        # 逐节点 checkpoint(记录"下一个节点"作为恢复起点)
        if self.checkpoint_every_node or nxt in (None, END):
            self._checkpoint(state)

        events = list(state.events)
        state.events = []
        finished = nxt in (None, END)
        if finished:
            state.status = "done"
            self._checkpoint(state)
        return events, finished

    async def _apply_send(self, state: AgentState, ret: Any) -> None:
        """处理动态扇出(Send 语义)。

        节点返回 [Send(branch, payload), ...] 时,为每个 Send 复制当前状态并
        并发执行其分支管线,完成后把各分支的 steps/events 合并回主状态。
        下一步节点已由 _resolve_next 沿当前节点的静态边/条件边确定。
        """
        sends = [s for s in ret if isinstance(s, Send)]
        if not sends:
            return

        branch_states = await asyncio.gather(
            *[self._run_branch(s.branch, state, s.payload) for s in sends]
        )
        for bs in branch_states:
            state.steps.extend(bs.steps)
            state.events.extend(bs.events)

    async def _run_branch(self, branch: list[str], base_state: AgentState, payload: Optional[dict] = None) -> AgentState:
        """执行一条并行分支管线(线性,不允许嵌套并行)。

        payload 提供分支差异字段(默认保留主状态,仅覆盖 payload 键)。
        """
        st = copy.deepcopy(base_state)
        st.events = []
        if payload:
            for key, value in payload.items():
                if key in ("user_input", "session_id", "run_id"):
                    setattr(st, key, value)
                elif key in ("metadata", "messages", "steps", "plan", "active_tools", "approve_tools"):
                    setattr(st, key, copy.deepcopy(value))
                else:
                    st.metadata[key] = value
        for node_name in branch:
            node = self._nodes.get(node_name)
            if node is None:
                raise RuntimeError(f"图 {self.name} 中没有节点 {node_name}")
            await node(st, self)
            # 记录节点事件,合并回主状态时一并使用
            st.events = list(st.events)  # 保留本节点产出(供 _apply_send 合并)
        st.status = "done"
        return st

    async def arun(self, state: AgentState, stop_at: Optional[str] = None):
        """运行到 END / 中断 / 指定的哨兵节点。返回 (state, events)。"""
        all_events: list[dict] = []
        guard = 0
        while True:
            # arun 循环级 cancel 检查
            if self._cancel_check and self._cancel_check():
                state.status = "cancelled"
                state.node = END
                self._checkpoint(state)
                return state, all_events
            events, finished = await self.astep(state)
            all_events.extend(events)
            if finished:
                return state, all_events
            if stop_at and state.node == stop_at:
                return state, all_events
            guard += 1
            if guard > self.max_iters or (self.max_node_iters and guard > self.max_node_iters):
                state.status = "done"
                state.node = END
                state.error = f"图节点执行超过上限(max_iters={self.max_iters}, max_node_iters={self.max_node_iters})"
                self._checkpoint(state)
                return state, all_events

    async def arun_stream(self, state: AgentState):
        """流式运行:事件实时逐条产出(不等节点结束),结束由调用方通过 finished 判断。

        实现:节点在后台任务中顺序执行,并通过 state._on_event 把内部 emit 的每个事件
        立即送入队列,本生成器逐条 yield——token/thinking/tool/plan 在生成过程中即可
        送达调用方(如 SSE),不再等到整轮 finalize 结束才一股脑返回。
        """
        q: asyncio.Queue = asyncio.Queue()

        def _forward(event: dict) -> None:
            try:
                q.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

        async def _pump():
            guard = 0
            try:
                while True:
                    # 流式循环也需检查 cancel,与 arun 保持一致
                    if self._cancel_check and self._cancel_check():
                        state.status = "cancelled"
                        state.node = END
                        self._checkpoint(state)
                        return
                    _events, finished = await self.astep(state)
                    if finished:
                        return
                    guard += 1
                    if guard > self.max_iters or (self.max_node_iters and guard > self.max_node_iters):
                        state.status = "done"
                        state.node = END
                        state.error = f"图节点执行超过上限(max_iters={self.max_iters}, max_node_iters={self.max_node_iters})"
                        self._checkpoint(state)
                        return
            finally:
                await q.put(None)  # 哨兵:通知消费方流入结束

        pump_task = asyncio.create_task(_pump())
        self._stream_hook = _forward
        try:
            while True:
                evt = await q.get()
                if evt is None:
                    break
                # yield 后检查 cancel,确保流式场景也能快速响应取消
                if self._cancel_check and self._cancel_check():
                    state.status = "cancelled"
                    state.node = END
                    self._checkpoint(state)
                    break
                yield evt
        finally:
            self._stream_hook = None
            if not pump_task.done():
                pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass  # 消费方主动中断或泵被取消,属预期清理路径
            except Exception:
                # 后台泵的异常保持向外抛出,与旧实现的失败语义一致(调用方可感知)
                raise

    async def aresume(self, state: AgentState, value=None, new_input: Optional[str] = None):
        """从中断处恢复运行(LangGraph Command(resume=...) 语义)。

        中断时 state.node 停留在原节点;这里把外部值回填到 state.interrupt["resume"],
        然后继续 arun —— 重入原节点,节点读取 resume 值继续执行。
        """
        if state.status != "interrupted":
            raise RuntimeError(f"状态为 {state.status},并非可恢复的中断状态")
        if state.interrupt is None:
            raise RuntimeError("状态没有中断点信息")
        if new_input is not None:
            state.user_input = new_input
        state.interrupt["resume"] = value
        state.status = "running"
        return await self.arun(state)

    # ---- 描述 ----

    def describe(self) -> dict:
        return {
            "name": self.name,
            "entry": self._entry,
            "nodes": sorted(self._nodes.keys()),
            "edges": [{f"{a} -> {b}"} for a, b in self._edges.items()],
            "conditional_nodes": sorted(self._conditional.keys()),
        }
