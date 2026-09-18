"""工作流手动排版(节点坐标持久化)修复验证。

背景: 旧实现里节点坐标(_x/_y)只存在于前端内存, 任何 CRUD 后 _reloadWorkflow()
重新拉取后端数据就会把所有节点重置回默认网格 → "排版"功能形同虚设。
修复后 WorkflowNode 带 position 字段, 端到端(DB → API → 前端)持久化坐标。
"""
import json
import time

import pytest


def test_workflow_node_has_position_field():
    """WorkflowNode 必须带 position 字段(默认 None, 表示未手动排版过)。"""
    from app.core.workflow.models import WorkflowNode, NodeType
    n = WorkflowNode(id="n1", type=NodeType.AGENT, name="测试")
    assert n.position is None
    n.position = {"x": 120.0, "y": 60.0}
    assert n.position == {"x": 120.0, "y": 60.0}


def test_db_roundtrip_persists_position(tmp_path):
    """节点坐标必须能在 SQLite 里完整存取(保存后重新加载不丢失)。"""
    from app.core.workflow.db import WorkflowDB
    from app.core.workflow.models import Workflow, WorkflowNode, NodeType, WorkflowEdge

    db = WorkflowDB(db_path=tmp_path / "wf_layout.db")
    wf = Workflow(
        id="wf_layout_test",
        name="排版测试",
        nodes={
            "a": WorkflowNode(id="a", type=NodeType.START, name="开始", position={"x": 60, "y": 60}),
            "b": WorkflowNode(id="b", type=NodeType.AGENT, name="处理", position={"x": 300, "y": 60}),
        },
        edges=[WorkflowEdge(id="edge_0", source="a", target="b")],
        entry_node="a",
    )
    db.save_workflow(wf)

    loaded = db.load_workflow("wf_layout_test")
    assert loaded is not None
    assert loaded.nodes["a"].position == {"x": 60.0, "y": 60.0}
    assert loaded.nodes["b"].position == {"x": 300.0, "y": 60.0}
    # 边的稳定 id 也要能 round-trip(前端双击删边依赖它)
    assert loaded.edges[0].id == "edge_0"


def test_db_legacy_row_without_position_loads_safely(tmp_path):
    """旧数据库里没存过 position 列的工作流, 加载后 position 应为 None 而不是报错。"""
    from app.core.workflow.db import WorkflowDB
    from app.core.workflow.models import Workflow, WorkflowNode, NodeType, WorkflowEdge

    db = WorkflowDB(db_path=tmp_path / "wf_layout_old.db")
    old_wf = Workflow(
        id="wf_old",
        name="旧版工作流",
        nodes={"s": WorkflowNode(id="s", type=NodeType.START, name="开始")},
        edges=[],
        entry_node="s",
    )
    db.save_workflow(old_wf)
    loaded = db.load_workflow("wf_old")
    assert loaded is not None
    assert loaded.nodes["s"].position is None


def test_api_parse_node_normalizes_position():
    """_parse_node 对脏 position 数据必须容错(字符串/缺字段/非数字都归一为合法 dict 或 None)。"""
    from app.api.workflow import _parse_node

    ok = _parse_node({"id": "n", "type": "agent", "position": {"x": 12, "y": 34}})
    assert ok.position == {"x": 12.0, "y": 34.0}

    str_num = _parse_node({"id": "n", "type": "agent", "position": {"x": "12", "y": "34"}})
    assert str_num.position == {"x": 12.0, "y": 34.0}

    bad = _parse_node({"id": "n", "type": "agent", "position": "not-a-dict"})
    assert bad.position is None

    missing = _parse_node({"id": "n", "type": "agent"})
    assert missing.position is None

    partial = _parse_node({"id": "n", "type": "agent", "position": {"x": 1}})
    # y 缺失时按 0 兜底, 不能抛异常
    assert partial.position == {"x": 1.0, "y": 0.0}
