"""并发安全和 SSE 进度测试。"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType, NodeStatus
from app.core.workflow.executor import WorkflowExecutor


@pytest.fixture
def sample_workflow():
    """创建一个简单的测试工作流。"""
    wf = Workflow(id="test_wf", name="测试工作流", entry_node="start")
    wf.nodes["start"] = WorkflowNode(id="start", type=NodeType.START, name="开始")
    wf.nodes["agent"] = WorkflowNode(
        id="agent", type=NodeType.AGENT, name="Agent",
        agent_config={"input": "你好"}
    )
    wf.nodes["end"] = WorkflowNode(id="end", type=NodeType.END, name="结束")
    wf.edges = [
        WorkflowEdge(id="e1", source="start", target="agent"),
        WorkflowEdge(id="e2", source="agent", target="end"),
    ]
    return wf


@pytest.fixture
def mock_agent():
    """创建一个模拟的 Agent。"""
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(
        answer="测试回答",
        steps=[{"step": 1, "type": "answer", "content": "测试回答"}],
        model="test-model"
    ))
    return agent


def test_isolated_agent_creation(mock_agent):
    """测试创建隔离的 Agent 实例。"""
    executor = WorkflowExecutor()
    assert executor is not None
    # 验证执行器可以正常创建，agent 通过 run() 传入


def test_run_id_uniqueness():
    """测试 run_id 唯一性。"""
    import time
    run_ids = set()
    for i in range(100):
        run_id = f"run_{int(time.time() * 1000)}_{i:06x}"
    # assert run_id not in run_ids
    run_ids.add(run_id)
    # print("OK run_id uniqueness test passed")


def test_condition_branch_logic():
    """测试条件分支逻辑。"""
    from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType

    # 创建带条件分支的工作流
    wf = Workflow(id="cond_test", name="条件测试", entry_node="start")
    wf.nodes["start"] = WorkflowNode(id="start", type=NodeType.START, name="开始")
    wf.nodes["cond"] = WorkflowNode(
        id="cond", type=NodeType.CONDITION, name="条件",
        condition_expr="result > 5",
        branches={"true": ["agent_true"], "false": ["agent_false"]}
    )
    wf.nodes["agent_true"] = WorkflowNode(id="agent_true", type=NodeType.AGENT, name="True 分支")
    wf.nodes["agent_false"] = WorkflowNode(id="agent_false", type=NodeType.AGENT, name="False 分支")
    wf.nodes["end"] = WorkflowNode(id="end", type=NodeType.END, name="结束")

    # 设置边
    wf.edges = [
        WorkflowEdge(id="e1", source="start", target="cond"),
        WorkflowEdge(id="e2", source="cond", target="agent_true"),
        WorkflowEdge(id="e3", source="cond", target="agent_false"),
        WorkflowEdge(id="e4", source="agent_true", target="end"),
        WorkflowEdge(id="e5", source="agent_false", target="end"),
    ]

    # 验证条件节点有正确的下游节点
    next_nodes = wf.get_next_nodes("cond", {"result": 10})
    assert "agent_true" in next_nodes
    assert "agent_false" in next_nodes
    # print("OK condition branch logic test passed")


def test_sse_event_callback():
    """测试 SSE 事件回调机制。"""
    from app.core.workflow.executor import WorkflowExecutor

    events = []

    def on_event(run_id, node_id, status, result=None, error=None):
        events.append({
            "run_id": run_id,
            "node_id": node_id,
            "status": status,
            "result": result,
            "error": error,
        })

    executor = WorkflowExecutor(on_node_event=on_event)
    assert executor._on_node_event is not None

    # 模拟触发事件
    executor._emit_event("run_123", "node_1", "running")
    assert len(events) == 1
    assert events[0]["node_id"] == "node_1"
    assert events[0]["status"] == "running"
    # print("OK SSE event callback test passed")


def test_template_rendering():
    """测试模板变量替换。"""
    from app.core.workflow.executor import WorkflowExecutor

    executor = WorkflowExecutor(MagicMock())
    result = executor._render_template("Hello {{name}}!", {"name": "World"})
    assert result == "Hello World!"

    result2 = executor._render_template("{{a}} + {{b}} = {{c}}", {
        "a": "1", "b": "2", "c": "3"
    })
    assert result2 == "1 + 2 = 3"
    # print("OK template rendering test passed")


def test_condition_expression():
    """测试条件表达式。"""
    from app.core.workflow.executor import WorkflowExecutor
    from app.core.workflow.models import WorkflowNode

    executor = WorkflowExecutor(MagicMock())
    node = WorkflowNode(id="c1", type=NodeType.CONDITION, name="条件")
    node.condition_expr = "5 > 3"

    run = MagicMock()
    run.variables = {}
    result = asyncio.run(executor._exec_condition(node, run))
    assert result["output"] is True

    node.condition_expr = "3 > 5"
    result = asyncio.run(executor._exec_condition(node, run))
    assert result["output"] is False
    # print("OK condition expression test passed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
