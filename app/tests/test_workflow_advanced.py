"""工作流模块单元测试。"""
import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


class TestDynamicEdgeEvaluator:
    """动态边路由测试。"""

    def test_evaluate_simple_condition(self):
        from app.core.workflow.dynamic_router import DynamicEdgeEvaluator
        evaluator = DynamicEdgeEvaluator(variables={"x": 10})
        assert evaluator.evaluate("x > 5") is True
        assert evaluator.evaluate("x < 5") is False

    def test_evaluate_template_variable(self):
        from app.core.workflow.dynamic_router import DynamicEdgeEvaluator
        evaluator = DynamicEdgeEvaluator(
            variables={},
            node_results={"classify": {"output": {"sentiment": "positive"}}}
        )
        assert evaluator.evaluate("{{classify.output.sentiment == 'positive'}}") is True
        assert evaluator.evaluate("{{classify.output.sentiment == 'negative'}}") is False

    def test_find_active_targets(self):
        from app.core.workflow.dynamic_router import DynamicEdgeEvaluator
        from app.core.workflow.models import WorkflowEdge

        evaluator = DynamicEdgeEvaluator(
            variables={},
            node_results={"result": {"output": {"score": 85}}}
        )

        edges = [
            WorkflowEdge(id="e1", source="start", target="pass", condition_expr="{{result.output.score >= 60}}"),
            WorkflowEdge(id="e2", source="start", target="fail", condition_expr="{{result.output.score < 60}}"),
        ]

        targets = evaluator.find_active_targets("start", edges)
        assert targets == ["pass"]


class TestLoopController:
    """循环控制器测试。"""

    @pytest.mark.asyncio
    async def test_foreach_loop(self):
        from app.core.workflow.loop_controller import LoopController, LoopControlType

        controller = LoopController(
            loop_config={
                "type": "foreach",
                "iterable": [1, 2, 3],
                "max_iterations": 10,
            },
            workflow_nodes={},
            variables={},
            node_results={},
            execute_node_func=lambda n: {"result": n},
        )

        from app.core.workflow.dynamic_router import DynamicEdgeEvaluator
        evaluator = DynamicEdgeEvaluator(variables={})
        result = await controller.execute(evaluator)

        assert result["iterations"] == 3
        assert result["loop_type"] == "foreach"

    @pytest.mark.asyncio
    async def test_while_loop(self):
        from app.core.workflow.loop_controller import LoopController

        call_count = 0

        async def mock_execute(node):
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        controller = LoopController(
            loop_config={
                "type": "while",
                "while_condition": "count < 3",
                "max_iterations": 10,
            },
            workflow_nodes={},
            variables={"count": 0},
            node_results={},
            execute_node_func=mock_execute,
        )

        from app.core.workflow.dynamic_router import DynamicEdgeEvaluator
        evaluator = DynamicEdgeEvaluator(variables={"count": 0})
        result = await controller.execute(evaluator)

        assert result["iterations"] == 3


class TestWorkflowCheckpointManager:
    """CheckPoint 管理器测试。"""

    def test_save_and_load(self, tmp_path):
        from app.core.workflow.checkpoint import WorkflowCheckpointManager

        db_path = tmp_path / "test_checkpoints.db"
        mgr = WorkflowCheckpointManager(str(db_path))

        mgr.save_checkpoint(
            run_id="run_1",
            node_id="node_1",
            status="success",
            variables={"key": "value"},
            result={"output": "hello"},
            duration=1.5,
        )

        cp = mgr.load_checkpoint("run_1", "node_1")
        assert cp is not None
        assert cp.status == "success"
        assert cp.result == {"output": "hello"}
        assert cp.duration == 1.5

    def test_get_skippable_nodes(self, tmp_path):
        from app.core.workflow.checkpoint import WorkflowCheckpointManager

        mgr = WorkflowCheckpointManager(str(tmp_path / "test.db"))
        mgr.save_checkpoint("run_1", "node_1", "success", {}, {"r": 1})
        mgr.save_checkpoint("run_1", "node_2", "failed", {}, error="err")
        mgr.save_checkpoint("run_1", "node_3", "success", {}, {"r": 3})

        skippable = mgr.get_skippable_nodes("run_1")
        assert skippable == {"node_1", "node_3"}


class TestSagaOrchestrator:
    """SAGA 补偿测试。"""

    @pytest.mark.asyncio
    async def test_execute_and_compensate(self):
        from app.core.workflow.saga import SagaOrchestrator

        saga = SagaOrchestrator()

        executed = []
        compensated = []

        async def action(**kwargs):
            executed.append(kwargs.get("node_id"))
            return {"status": "ok"}

        async def compensation(**kwargs):
            compensated.append(kwargs.get("node_id"))

        saga.register("node_1", action, compensation, description="创建")
        saga.register("node_2", action, compensation, description="处理")

        await saga.execute("node_1", node_id="node_1")
        await saga.execute("node_2", node_id="node_2")

        assert executed == ["node_1", "node_2"]

        compensated_nodes = await saga.compensate_up_to("")
        assert compensated_nodes == ["node_2", "node_1"]


class TestWorkflowTemplateRegistry:
    """模板注册表测试。"""

    def test_list_builtin_templates(self):
        from app.core.workflow.templates import get_template_registry

        registry = get_template_registry()
        templates = registry.list_all()
        assert len(templates) >= 3  # 至少有内置的三个模板

    def test_search_templates(self):
        from app.core.workflow.templates import get_template_registry

        registry = get_template_registry()
        results = registry.search("research")
        assert len(results) >= 1

    def test_instantiate_template(self):
        from app.core.workflow.templates import get_template_registry

        registry = get_template_registry()
        workflow = registry.instantiate(
            template_id="research_report",
            instance_id="test_inst",
            inputs={"topic": "AI", "depth": 3}
        )
        assert workflow is not None
        assert "test_inst" in workflow.id


class TestVisualization:
    """可视化测试。"""

    def test_generate_dag_visualization(self):
        from app.core.workflow.visualization import generate_dag_visualization
        from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType

        workflow = Workflow(
            id="test_wf",
            name="测试工作流",
            nodes={
                "start": WorkflowNode(id="start", type=NodeType.START, name="开始"),
                "process": WorkflowNode(id="process", type=NodeType.AGENT, name="处理"),
                "end": WorkflowNode(id="end", type=NodeType.END, name="结束"),
            },
            edges=[
                WorkflowEdge(id="e1", source="start", target="process"),
                WorkflowEdge(id="e2", source="process", target="end"),
            ],
            entry_node="start",
        )

        result = generate_dag_visualization(workflow)
        assert "nodes" in result
        assert "edges" in result
        assert len(result["nodes"]) == 3
        assert len(result["edges"]) == 2


class TestWorkflowExecutor:
    """执行器集成测试。"""

    @pytest.mark.asyncio
    async def test_run_simple_workflow(self):
        from app.core.workflow.executor import WorkflowExecutor
        from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType

        # 创建一个简单的两节点工作流
        workflow = Workflow(
            id="simple_wf",
            name="简单工作流",
            nodes={
                "start": WorkflowNode(id="start", type=NodeType.START, name="开始"),
                "end": WorkflowNode(id="end", type=NodeType.END, name="结束"),
            },
            edges=[
                WorkflowEdge(id="e1", source="start", target="end"),
            ],
            entry_node="start",
        )

        executor = WorkflowExecutor()
        run = await executor.run(workflow, {"input": "test"})

        assert run.status == "completed"
        assert run.output is not None
