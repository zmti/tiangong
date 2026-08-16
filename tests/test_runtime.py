"""Graph 运行时 + Checkpoint 单元测试。"""
import asyncio
import json
import os

import pytest

from app.runtime.checkpoint import CheckpointManager
from app.runtime.graph import END, Graph, Node, ParallelNode, Send
from app.runtime.state import AgentState


def run(coro):
    return asyncio.run(coro)


# ---- 线性图 ----

def test_linear_graph_runs_all_nodes():
    order = []

    async def n1(state, graph):
        order.append("n1")
        state.metadata["x"] = 1

    async def n2(state, graph):
        order.append("n2")
        state.answer = "done"
        state.steps.append({"type": "answer", "content": "done"})

    g = Graph(name="linear")
    g.add_node(Node("n1", n1))
    g.add_node(Node("n2", n2))
    g.set_entry("n1")
    g.add_edge("n1", "n2")

    state = AgentState(user_input="hi")
    state, events = run(g.arun(state))
    assert order == ["n1", "n2"]
    assert state.answer == "done"
    assert state.status == "done"
    assert state.node == END


def test_node_return_routing():
    async def n1(state, graph):
        return "n3"  # 覆盖静态边

    async def n2(state, graph):
        state.metadata["hit"] = "n2"

    async def n3(state, graph):
        state.metadata["hit"] = "n3"

    g = Graph()
    g.add_node(Node("n1", n1))
    g.add_node(Node("n2", n2))
    g.add_node(Node("n3", n3))
    g.set_entry("n1")
    g.add_edge("n1", "n2")  # 静态边指向 n2,但节点返回值优先

    state = AgentState()
    run(g.arun(state))
    assert state.metadata["hit"] == "n3"


def test_conditional_edge():
    async def decide(state):
        return state.metadata.get("go_a") and "n_a" or "n_b"

    async def n_a(state, graph):
        state.metadata["path"] = "a"

    async def n_b(state, graph):
        state.metadata["path"] = "b"

    g = Graph()
    g.add_node(Node("decide", lambda s, g: None))
    g.add_node(Node("n_a", n_a))
    g.add_node(Node("n_b", n_b))
    g.set_entry("decide")
    g.add_conditional_edge("decide", decide)

    s1 = AgentState()
    s1.metadata["go_a"] = True
    run(g.arun(s1))
    assert s1.metadata["path"] == "a"

    s2 = AgentState()
    s2.metadata["go_a"] = False
    run(g.arun(s2))
    assert s2.metadata["path"] == "b"


def test_conditional_edge_none_stops():
    async def decide(state):
        return None  # 停止

    g = Graph()
    g.add_node(Node("decide", lambda s, g: None))
    g.set_entry("decide")
    g.add_conditional_edge("decide", decide)
    state = AgentState()
    state, _ = run(g.arun(state))
    assert state.status == "done"
    assert state.node == END


def test_loop_with_budget():
    counter = {"n": 0}

    async def inc(state, graph):
        counter["n"] += 1
        return "llm" if counter["n"] < 5 else END  # 显式 END 停止(避免落到静态边死循环)

    g = Graph(max_iters=100)
    g.add_node(Node("llm", inc))
    g.set_entry("llm")
    g.add_edge("llm", "llm")  # 自环

    state = AgentState()
    run(g.arun(state))
    assert counter["n"] == 5


# ---- 并行分支 ----

def test_parallel_node_runs_branches_and_merges():
    order = []

    async def s1(state, graph):
        order.append("s1")
        state.steps.append({"branch": 1})

    async def s2(state, graph):
        order.append("s2")
        state.steps.append({"branch": 2})

    async def s3(state, graph):
        order.append("s3")
        state.steps.append({"branch": 3})

    async def join(state, graph):
        state.answer = "joined"

    async def merge(state, branch_states):
        for bs in branch_states:
            state.steps.extend(bs.steps)

    g = Graph()
    g.add_node(Node("s1", s1))
    g.add_node(Node("s2", s2))
    g.add_node(Node("s3", s3))
    g.add_node(ParallelNode("parallel", branches=[["s1", "s2"], ["s3"]], merge=merge))
    g.add_node(Node("join", join))
    g.set_entry("parallel")
    g.add_edge("parallel", "join")

    state = AgentState()
    state, _ = run(g.arun(state))
    assert state.answer == "joined"
    branches = [s["branch"] for s in state.steps]
    assert sorted(branches) == [1, 2, 3]
    assert set(order) == {"s1", "s2", "s3"}


def test_parallel_default_merge():
    async def s1(state, graph):
        state.steps.append({"branch": 1})

    g = Graph()
    g.add_node(Node("s1", s1))
    g.add_node(ParallelNode("parallel", branches=[["s1"]]))
    g.set_entry("parallel")

    state = AgentState()
    state, _ = run(g.arun(state))
    assert [s["branch"] for s in state.steps] == [1]


# ---- checkpoint ----

def test_checkpoint_save_load_resume(tmp_path):
    db = os.path.join(tmp_path, "ckpt.db")
    cm = CheckpointManager(db_path=db)

    state = AgentState(session_id="s1", user_input="你好")
    state.steps.append({"step": 1})
    state.node = "n2"
    cm.save(state)

    assert cm.exists(state.run_id)
    assert cm.count() == 1

    loaded = cm.load(state.run_id)
    assert loaded is not None
    assert loaded.session_id == "s1"
    assert loaded.user_input == "你好"
    assert len(loaded.steps) == 1
    assert loaded.node == "n2"

    # 列表视图
    items = cm.list()
    assert len(items) == 1
    assert items[0]["run_id"] == state.run_id
    assert items[0]["summary"]["input"] == "你好"

    cm.delete(state.run_id)
    assert not cm.exists(state.run_id)


def test_checkpoint_resume_continues_from_saved_node(tmp_path):
    """恢复后从 state.node(保存的下一节点)继续,不重跑已完成的节点。"""
    order = []

    async def n1(state, graph):
        order.append("n1")

    async def n2(state, graph):
        order.append("n2")
        state.answer = "finished"

    cm = CheckpointManager(db_path=os.path.join(tmp_path, "ckpt2.db"))
    g = Graph(checkpoint=cm)
    g.add_node(Node("n1", n1))
    g.add_node(Node("n2", n2))
    g.set_entry("n1")
    g.add_edge("n1", "n2")

    state = AgentState()
    # 跑一步:n1 执行完,checkpoint 保存,node=n2
    events, finished = run(g.astep(state))
    assert not finished
    assert state.node == "n2"

    # 模拟进程重启:从 checkpoint 恢复
    restored = cm.load(state.run_id)
    assert restored.node == "n2"

    # 继续执行
    run(g.arun(restored))
    assert restored.answer == "finished"
    assert order == ["n1", "n2"]
    assert restored.node == END


def test_astep_emits_and_clears_events():
    async def n1(state, graph):
        state.emit({"type": "thinking", "content": "x"})
        state.emit({"type": "tool", "name": "t"})

    g = Graph()
    g.add_node(Node("n1", n1))
    g.set_entry("n1")

    state = AgentState()
    events, finished = run(g.astep(state))
    assert [e["type"] for e in events] == ["thinking", "tool"]
    assert state.events == []  # 已清空


def test_graph_describe():
    g = Graph(name="g")
    g.add_node(Node("a", lambda s, g: None))
    g.set_entry("a")
    d = g.describe()
    assert d["name"] == "g"
    assert d["entry"] == "a"
    assert "a" in d["nodes"]


# ---- interrupt / resume(LangGraph interrupt 语义) ----

def test_interrupt_pauses_graph_and_returns_control():
    calls = []

    async def n_ask(state, graph):
        calls.append("ask")
        graph.interrupt(state, {"type": "confirm", "item": "delete_file"})
        calls.append("after_interrupt")

    async def n_done(state, graph):
        calls.append("done")
        state.answer = "finished"

    g = Graph()
    g.add_node(Node("ask", n_ask))
    g.add_node(Node("done", n_done))
    g.set_entry("ask")
    g.add_edge("ask", "done")

    state = AgentState()
    state, events = run(g.arun(state))
    assert state.status == "interrupted"
    assert state.node == "ask"  # 中断后 node 停留在原节点
    assert state.interrupt == {"value": {"type": "confirm", "item": "delete_file"}, "resume": None}
    assert calls == ["ask"]  # after_interrupt 未执行
    assert state.answer == ""


def test_resume_continues_from_interrupt_node():
    calls = []

    async def n_ask(state, graph):
        if state.interrupt and state.interrupt.get("resume") is not None:
            calls.append("ask_resumed")
            state.metadata["decision"] = state.interrupt["resume"]
            return None
        calls.append("ask_first")
        graph.interrupt(state, {"type": "confirm", "item": "delete_file"})

    async def n_done(state, graph):
        calls.append("done")
        state.answer = "finished"

    g = Graph()
    g.add_node(Node("ask", n_ask))
    g.add_node(Node("done", n_done))
    g.set_entry("ask")
    g.add_edge("ask", "done")

    state = AgentState()
    state, _ = run(g.arun(state))
    assert state.status == "interrupted"

    state, _ = run(g.aresume(state, value="approved"))
    assert state.status == "done"
    assert state.answer == "finished"
    assert state.metadata["decision"] == "approved"
    assert calls == ["ask_first", "ask_resumed", "done"]


def test_resume_requires_interrupted_state():
    g = Graph()
    g.add_node(Node("a", lambda s, g: None))
    g.set_entry("a")
    state = AgentState()
    run(g.arun(state))
    assert state.status == "done"
    try:
        run(g.aresume(state, value="x"))
        assert False, "应当拒绝在非中断状态上 resume"
    except RuntimeError as e:
        assert "并非可恢复的中断状态" in str(e)


def test_interrupt_survives_checkpoint_restart(tmp_path):
    """中断现场落盘后,进程重启可按 run_id 恢复并重入原节点。"""
    calls = []

    async def n_ask(state, graph):
        if state.interrupt and state.interrupt.get("resume") is not None:
            calls.append("resumed")
            state.answer = "after:" + str(state.interrupt["resume"])
            return None
        graph.interrupt(state, {"type": "confirm", "item": "x"})

    cm = CheckpointManager(db_path=os.path.join(tmp_path, "interrupt.db"))
    g = Graph(checkpoint=cm)
    g.add_node(Node("ask", n_ask))
    g.set_entry("ask")

    state = AgentState(session_id="s1", user_input="hi")
    state, _ = run(g.arun(state))
    assert state.status == "interrupted"
    assert cm.exists(state.run_id)

    # 模拟重启:从 checkpoint 恢复,再 resume
    restored = cm.load(state.run_id)
    assert restored.status == "interrupted"
    restored.interrupt["resume"] = "ok"
    run(g.arun(restored))
    assert restored.answer == "after:ok"
    assert calls == ["resumed"]


# ---- 动态扇出(Send 语义) ----

def test_send_dynamic_fan_out_parallel():
    """节点返回多个 Send,按 payload 并发执行分支并合并结果。"""
    order = []

    async def fan(state, graph):
        # 动态生成 3 个子任务,各带不同 user_input
        return [Send(["sub"], {"user_input": f"item-{i}"}) for i in range(3)]

    async def sub(state, graph):
        order.append(state.user_input)
        state.steps.append({"type": "work", "item": state.user_input})

    async def join(state, graph):
        state.answer = "all-done"

    g = Graph()
    g.add_node(Node("fan", fan))
    g.add_node(Node("sub", sub))
    g.add_node(Node("join", join))
    g.set_entry("fan")
    g.add_edge("fan", "join")

    state = AgentState(user_input="root")
    state, _ = run(g.arun(state))
    assert state.answer == "all-done"
    assert sorted(order) == ["item-0", "item-1", "item-2"]
    items = [s["item"] for s in state.steps if s["type"] == "work"]
    assert sorted(items) == ["item-0", "item-1", "item-2"]


def test_send_branches_share_base_state():
    """Send 分支基于主状态深拷贝,保留未覆盖字段;分支改动不污染主状态。"""
    async def fan(state, graph):
        return [Send(["sub"], {"user_input": "child"})]

    async def sub(state, graph):
        state.metadata["branch_note"] = "changed"
        state.steps.append({"type": "work", "note": "x"})

    async def join(state, graph):
        state.answer = "joined"

    g = Graph()
    g.add_node(Node("fan", fan))
    g.add_node(Node("sub", sub))
    g.add_node(Node("join", join))
    g.set_entry("fan")
    g.add_edge("fan", "join")

    state = AgentState(user_input="parent")
    state.metadata["parent_flag"] = True
    state, _ = run(g.arun(state))
    # 主状态的 parent_flag 保留,分支的 branch_note 未泄漏回主状态
    assert state.metadata["parent_flag"] is True
    assert "branch_note" not in state.metadata
    assert state.answer == "joined"


# ---- 时间旅行(按版本加载 / 分支回滚) ----

def test_checkpoint_versions_accumulate(tmp_path):
    """每次 save 都追加版本快照,版本号递增。"""
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "tt.db"))
    st = AgentState(session_id="s1", user_input="hi")
    st.node = "a"
    st.status = "running"
    cm.save(st)
    st.node = "b"
    st.status = "running"
    cm.save(st)
    st.node = END
    st.status = "completed"
    st.answer = "done"
    cm.save(st)

    versions = cm.versions(st.run_id)
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert [v["node"] for v in versions] == ["a", "b", END]


def test_checkpoint_load_version_time_travel(tmp_path):
    """按版本号加载历史状态,可回到中间节点。"""
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "tt2.db"))
    st = AgentState(session_id="s1", user_input="hi")
    st.node = "a"
    st.metadata["step"] = 1
    cm.save(st)
    st.node = "b"
    st.metadata["step"] = 2
    cm.save(st)
    st.node = "c"
    st.metadata["step"] = 3
    cm.save(st)

    mid = cm.load_version(st.run_id, 2)
    assert mid is not None
    assert mid.node == "b"
    assert mid.metadata["step"] == 2


def test_rollback_resets_latest_pointer(tmp_path):
    """rollback 把最新指针重置到历史版本,后续执行从此分叉。"""
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "tt3.db"))
    st = AgentState(session_id="s1", user_input="hi")
    st.node = "a"
    cm.save(st)
    st.node = "b"
    cm.save(st)
    st.node = "c"
    cm.save(st)

    rolled = cm.rollback(st.run_id, 2)
    assert rolled is not None
    assert rolled.node == "b"
    # 最新指针已被重置为版本 2
    latest = cm.load(st.run_id)
    assert latest.node == "b"

    # 从回滚点继续执行会产生新版本(版本号继续递增,不覆盖历史)
    latest.node = "b2"
    cm.save(latest)
    versions = cm.versions(st.run_id)
    assert [v["version"] for v in versions] == [1, 2, 3, 4]
    assert versions[-1]["node"] == "b2"


def test_rollback_missing_version_returns_none(tmp_path):
    cm = CheckpointManager(db_path=os.path.join(tmp_path, "tt4.db"))
    st = AgentState(session_id="s1", user_input="hi")
    st.node = "a"
    cm.save(st)
    assert cm.load_version(st.run_id, 99) is None
    assert cm.rollback(st.run_id, 99) is None
