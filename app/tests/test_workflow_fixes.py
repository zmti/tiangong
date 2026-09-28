"""工作流执行引擎修复后的回归测试（真实执行,不依赖 mock）。

覆盖三处 P0 修复:
1. 循环节点子图不再只跑一遍(force 重跑 + 顶层拓扑排除循环托管节点)
2. 条件分支/动态边路由在 run() 中真正门控下游执行
3. 层结果 zip 对齐(nodes_to_run 与 layer_results 一一对应)
"""
from __future__ import annotations

import asyncio

from app.core.workflow.executor import WorkflowExecutor
from app.core.workflow.models import (
    Workflow,
    WorkflowNode,
    WorkflowEdge,
    NodeType,
    NodeStatus,
)


def _run(wf: Workflow, inputs: dict) -> "object":
    return asyncio.run(WorkflowExecutor().run(wf, inputs))


# ---------------------------------------------------------------------------
# 1) 循环体每轮都真实执行(修复前只跑一次)
# ---------------------------------------------------------------------------

def _make_loop_wf() -> Workflow:
    wf = Workflow(id="wf_loop", name="loop")
    wf.add_node(WorkflowNode(id="start", type=NodeType.START, name="start"))
    wf.add_node(WorkflowNode(id="body", type=NodeType.SCRIPT, name="body",
                             script_code="v = 1"))
    wf.add_node(WorkflowNode(id="end", type=NodeType.END, name="end"))
    wf.add_node(WorkflowNode(
        id="loop", type=NodeType.LOOP, name="loop",
        loop_config={"type": "foreach", "iterable": [1, 2, 3], "subgraph_ids": ["body"]},
        subgraph_ids=["body"],
    ))
    wf.add_edge(WorkflowEdge(id="e1", source="start", target="loop"))
    wf.add_edge(WorkflowEdge(id="e2", source="loop", target="end"))
    return wf


def test_loop_body_runs_each_iteration():
    run = _run(_make_loop_wf(), {})
    assert run.status == "completed", run.output
    starts = [h for h in run.history if h["node_id"] == "body" and h["action"] == "start"]
    # 修复前:循环体只执行 1 次;修复后:foreach [1,2,3] 执行 3 次
    assert len(starts) == 3, f"body 执行了 {len(starts)} 次,应为 3"


def test_loop_excluded_from_topo_does_not_double_run():
    run = _run(_make_loop_wf(), {})
    # body 不应出现在顶层拓扑层里重复执行,总数严格等于迭代次数 3
    starts = [h for h in run.history if h["node_id"] == "body" and h["action"] == "start"]
    assert len(starts) == 3
    # 且 body 节点状态应为成功
    assert run.node_states["body"]["status"] == NodeStatus.SUCCESS.value


# ---------------------------------------------------------------------------
# 2) 条件分支门控(修复前两条分支都会无条件执行)
# ---------------------------------------------------------------------------

def _make_cond_wf() -> Workflow:
    wf = Workflow(id="wf_cond", name="cond")
    wf.add_node(WorkflowNode(id="start", type=NodeType.START, name="start"))
    # 条件节点表达式用裸 Python(由 safe_eval 直接求值,不支持 {{ }})
    wf.add_node(WorkflowNode(id="cond", type=NodeType.CONDITION, name="cond",
                             condition_expr="mode == 'a'"))
    wf.add_node(WorkflowNode(id="a", type=NodeType.SCRIPT, name="a", script_code="r = 'A'"))
    wf.add_node(WorkflowNode(id="b", type=NodeType.SCRIPT, name="b", script_code="r = 'B'"))
    wf.add_edge(WorkflowEdge(id="e1", source="start", target="cond"))
    # 边条件经过 DynamicEdgeEvaluator,支持 {{node.output}} 形式
    wf.add_edge(WorkflowEdge(id="e2", source="cond", target="a",
                             condition_expr="{{cond.output}}"))
    wf.add_edge(WorkflowEdge(id="e3", source="cond", target="b",
                             condition_expr="{{cond.output}} == False"))
    return wf


def test_condition_routing_true_branch():
    run = _run(_make_cond_wf(), {"mode": "a"})
    assert run.status == "completed", run.output
    assert run.node_states["a"]["status"] == NodeStatus.SUCCESS.value
    assert run.node_states["b"]["status"] == NodeStatus.SKIPPED.value


def test_condition_routing_false_branch():
    run = _run(_make_cond_wf(), {"mode": "b"})
    assert run.status == "completed", run.output
    assert run.node_states["b"]["status"] == NodeStatus.SUCCESS.value
    assert run.node_states["a"]["status"] == NodeStatus.SKIPPED.value


# ---------------------------------------------------------------------------
# 4) 默认分支:条件分支都不命中时,默认边兜底激活
# ---------------------------------------------------------------------------

def _make_default_wf() -> Workflow:
    wf = Workflow(id="wf_def", name="default")
    wf.add_node(WorkflowNode(id="start", type=NodeType.START, name="start"))
    wf.add_node(WorkflowNode(id="cond", type=NodeType.CONDITION, name="cond",
                             condition_expr="mode == 'a'"))
    wf.add_node(WorkflowNode(id="a", type=NodeType.SCRIPT, name="a", script_code="r='A'"))
    wf.add_node(WorkflowNode(id="dflt", type=NodeType.SCRIPT, name="dflt", script_code="r='D'"))
    wf.add_edge(WorkflowEdge(id="e1", source="start", target="cond"))
    wf.add_edge(WorkflowEdge(id="e2", source="cond", target="a",
                             condition_expr="{{cond.output}}"))
    # 默认边:自身条件恒为假,但标记 is_default,用于兜底
    wf.add_edge(WorkflowEdge(id="e3", source="cond", target="dflt",
                             condition_expr="{{cond.output}} == 'zzz'", is_default=True))
    return wf


def test_default_branch_activates_when_no_other():
    run = _run(_make_default_wf(), {"mode": "b"})  # cond 为 False
    assert run.status == "completed", run.output
    assert run.node_states["a"]["status"] == NodeStatus.SKIPPED.value
    assert run.node_states["dflt"]["status"] == NodeStatus.SUCCESS.value


# ---------------------------------------------------------------------------
# 3) 层结果对齐(zip 使用 nodes_to_run 而非整层)
# ---------------------------------------------------------------------------

def test_layer_results_aligned_with_nodes_to_run():
    """验证 run() 内部 zip 基于 nodes_to_run,失败检测不会错位到错误节点。"""
    wf = Workflow(id="wf_align", name="align")
    wf.add_node(WorkflowNode(id="start", type=NodeType.START, name="start"))
    wf.add_node(WorkflowNode(id="n1", type=NodeType.SCRIPT, name="n1", script_code="x=1"))
    wf.add_node(WorkflowNode(id="n2", type=NodeType.SCRIPT, name="n2", script_code="y=2"))
    wf.add_edge(WorkflowEdge(id="e1", source="start", target="n1"))
    wf.add_edge(WorkflowEdge(id="e2", source="start", target="n2"))
    run = _run(wf, {})
    assert run.status == "completed", run.output
    assert run.node_states["n1"]["status"] == NodeStatus.SUCCESS.value
    assert run.node_states["n2"]["status"] == NodeStatus.SUCCESS.value


# ---------------------------------------------------------------------------
# 5) 断点续跑:注入真实 CheckpointManager 后,已完成节点可被跳过
# ---------------------------------------------------------------------------

def _make_linear_wf() -> Workflow:
    wf = Workflow(id="wf_ckpt", name="ckpt")
    wf.add_node(WorkflowNode(id="start", type=NodeType.START, name="start"))
    wf.add_node(WorkflowNode(id="n1", type=NodeType.SCRIPT, name="n1", script_code="x=1"))
    wf.add_node(WorkflowNode(id="n2", type=NodeType.SCRIPT, name="n2", script_code="y=2"))
    wf.add_edge(WorkflowEdge(id="e1", source="start", target="n1"))
    wf.add_edge(WorkflowEdge(id="e2", source="n1", target="n2"))
    return wf


def test_checkpoint_saved_and_resume_skips_completed_nodes(tmp_path):
    """注入真实 checkpoint_mgr 后:首次运行落盘,再次运行跳过已成功节点。

    修复前 API 一律传 WorkflowCheckpointManager("") 空路径 → save/load 全为
    no-op,断点续跑形同虚设。
    """
    from app.core.workflow.checkpoint import WorkflowCheckpointManager

    mgr = WorkflowCheckpointManager(str(tmp_path / "ckpt.db"))
    wf = _make_linear_wf()

    run1 = asyncio.run(WorkflowExecutor(checkpoint_mgr=mgr).run(wf, {}, run_id="run_ckpt_1"))
    assert run1.status == "completed", run1.output

    skippable = mgr.get_skippable_nodes("run_ckpt_1")
    assert {"start", "n1", "n2"} <= set(skippable), f"可跳过节点异常: {skippable}"

    # 第二次用同一 run_id 运行(模拟崩溃后续跑)
    run2 = asyncio.run(WorkflowExecutor(checkpoint_mgr=mgr).run(wf, {}, run_id="run_ckpt_1"))
    assert run2.node_states["n1"].get("skipped") is True, run2.node_states["n1"]
    assert run2.node_states["n2"].get("skipped") is True, run2.node_states["n2"]
    # 跳过的节点不应再产生一次真实的 start 事件
    starts = [h for h in run2.history if h.get("action") == "start"]
    assert starts == [], f"续跑时仍有节点被真实执行: {starts}"


# ---------------------------------------------------------------------------
# 6) SAGA 补偿:节点声明 compensation 且后续失败时,逆序触发补偿
# ---------------------------------------------------------------------------

def _make_compensable_wf() -> Workflow:
    wf = Workflow(id="wf_saga", name="saga")
    wf.add_node(WorkflowNode(id="start", type=NodeType.START, name="start"))
    wf.add_node(WorkflowNode(
        id="n1", type=NodeType.SCRIPT, name="n1", script_code="x=1",
        compensation={"type": "script", "code": "rolled_back = True"},
    ))
    wf.add_node(WorkflowNode(
        id="n2", type=NodeType.SCRIPT, name="n2", script_code="raise RuntimeError('boom')",
    ))
    wf.add_edge(WorkflowEdge(id="e1", source="start", target="n1"))
    wf.add_edge(WorkflowEdge(id="e2", source="n1", target="n2"))
    return wf


def test_saga_compensation_fires_on_failure():
    """n1 成功并登记补偿,n2 失败 → 逆序补偿 n1。

    修复前 get_executed_nodes() 恒为空(只在 saga.execute() 中追加,而执行器
    自己跑节点从不调用),导致 _compensate_failed_run 直接 return,补偿永不触发。
    """
    run = _run(_make_compensable_wf(), {})
    comps = [h for h in run.history if h.get("action") == "compensate"]
    assert comps, f"未触发任何补偿, history={[h.get('action') for h in run.history]}"
    assert comps[0]["node_id"] == "n1", comps


def test_saga_state_not_leaked_between_runs():
    """执行器被复用时,上一轮的补偿登记不得泄漏到下一轮。"""
    from app.core.workflow.saga import SagaOrchestrator

    saga = SagaOrchestrator()

    async def _noop(**kwargs):
        return {}

    saga.register_compensation("old_node", compensation=_noop)
    assert saga.get_executed_nodes() == ["old_node"]

    saga.reset()
    assert saga.get_executed_nodes() == [], "reset 后仍残留已执行节点"
    assert saga.to_dict()["registered_actions"] == [], "reset 后仍残留登记动作"
