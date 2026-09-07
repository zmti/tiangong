"""工作流单元测试 - Python 版本。"""
import pytest


def test_workflow_import():
    """测试工作流模块导入。"""
    from app.core.workflow.models import Workflow, WorkflowNode, NodeType
    assert Workflow is not None
    assert NodeType is not None


def test_workflow_creation():
    """测试创建工作流。"""
    from app.core.workflow.models import Workflow
    wf = Workflow("test_id", "测试工作流")
    assert wf.id == "test_id"
    assert wf.name == "测试工作流"
    assert wf.nodes == {}
    assert wf.edges == []


def test_workflow_add_node():
    """测试添加节点。"""
    from app.core.workflow.models import Workflow, WorkflowNode
    wf = Workflow("test_id", "测试")
    node = WorkflowNode(id="n1", type="start", name="开始")
    wf.nodes["n1"] = node
    assert "n1" in wf.nodes


def test_workflow_add_edge():
    """测试添加边。"""
    from app.core.workflow.models import Workflow, WorkflowNode
    wf = Workflow("test_id", "测试")
    wf.nodes["start"] = WorkflowNode(id="start", type="start", name="开始")
    wf.nodes["end"] = WorkflowNode(id="end", type="end", name="结束")
    wf.edges.append({"source": "start", "target": "end"})
    assert len(wf.edges) == 1


def test_workflow_validate():
    """测试工作流验证。"""
    from app.core.workflow.models import Workflow, WorkflowNode
    wf = Workflow("test_id", "测试")
    wf.nodes["start"] = WorkflowNode(id="start", type="start", name="开始")
    wf.entry_node = "start"
    errors = wf.validate()
    assert errors == []


def test_workflow_executor_import():
    """测试执行器导入。"""
    from app.core.workflow.executor import WorkflowExecutor
    assert WorkflowExecutor is not None


def test_workflow_templates():
    """测试模板数据。"""
    import json
    from pathlib import Path
    templates_file = Path(__file__).parent.parent / "desktop" / "src" / "ui" / "workflow" / "templates.js"
    if templates_file.exists():
        content = templates_file.read_text(encoding="utf-8")
        assert "simple_chat" in content
        assert "code_review" in content


def test_workflow_designer_import():
    """测试设计器文件存在。"""
    from pathlib import Path
    designer_file = Path(__file__).parent.parent / "desktop" / "src" / "ui" / "workflow" / "designer.js"
    assert designer_file.exists()
    content = designer_file.read_text(encoding="utf-8")
    assert "WorkflowDesigner" in content


def test_tool_discovery():
    """测试工具发现模块。"""
    from app.core.tool_discovery import ToolDiscovery
    assert ToolDiscovery is not None


def test_confirmation_manager():
    """测试确认管理器。"""
    from app.core.confirmation import ConfirmationManager
    assert ConfirmationManager is not None
