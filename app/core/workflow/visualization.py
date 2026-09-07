"""工作流可视化数据生成器。

支持:
- 生成 DAG 可视化数据(JSON 格式)
- 执行轨迹快照
- 性能数据
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.core.workflow.models import Workflow, WorkflowRun, NodeStatus, NodeType

logger = logging.getLogger(__name__)


def generate_dag_visualization(workflow: Workflow) -> dict:
    """生成 DAG 可视化数据。

    Returns:
        {
            "nodes": [{"id": str, "type": str, "label": str, "x": float, "y": float}],
            "edges": [{"source": str, "target": str, "label": str}],
            "layout": str  # "dagre" / "radial" / "manual"
        }
    """
    nodes = []
    edges = []

    # 计算布局位置(简单的层次布局)
    layers = _compute_layers(workflow)

    layer_y = {layer_idx: -layer_idx * 100 for layer_idx in range(len(layers))}
    node_x = {}
    for layer_idx, layer_nodes in enumerate(layers):
        for i, node in enumerate(layer_nodes):
            node_x[node.id] = i * 150

    for node_id, node in workflow.nodes.items():
        layer_idx = _get_layer(node_id, layers)
        nodes.append({
            "id": node_id,
            "type": node.type.value,
            "label": node.name,
            "description": node.description,
            "x": node_x.get(node_id, 0),
            "y": layer_y.get(layer_idx, 0),
            "config": node.config,
            "error_strategy": node.error_strategy.value if hasattr(node.error_strategy, 'value') else str(node.error_strategy),
        })

    for edge in workflow.edges:
        condition_label = None
        if edge.condition_expr:
            condition_label = edge.condition_expr[:50] + ("..." if len(edge.condition_expr) > 50 else "")
        elif edge.condition:
            condition_label = edge.condition[:50] + ("..." if len(edge.condition) > 50 else "")

        edges.append({
            "source": edge.source,
            "target": edge.target,
            "label": condition_label,
            "id": edge.id,
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "layout": "dagre",
        "meta": {
            "workflow_id": workflow.id,
            "name": workflow.name,
            "nodes_count": len(nodes),
            "edges_count": len(edges),
        }
    }


def generate_execution_snapshot(run: WorkflowRun) -> dict:
    """生成执行快照(用于实时可视化)。

    Returns:
        {
            "run_id": str,
            "status": str,
            "progress": float,
            "nodes": [{"id": str, "status": str, "duration": float, "result": dict}],
            "timeline": [{"time": float, "event": str, "node_id": str}]
        }
    """
    total_nodes = len(run.node_states)
    completed_nodes = sum(
        1 for state in run.node_states.values()
        if state.get("status") in (NodeStatus.SUCCESS.value, NodeStatus.FAILED.value, NodeStatus.SKIPPED.value)
    )
    progress = completed_nodes / max(1, total_nodes)

    nodes = []
    for node_id, state in run.node_states.items():
        nodes.append({
            "id": node_id,
            "status": state.get("status", "pending"),
            "duration": state.get("duration", 0),
            "attempt": state.get("attempt", 1),
            "result_preview": _preview_result(state.get("result")),
            "error": state.get("error"),
        })

    # 时间线
    timeline = []
    for event in run.history:
        timeline.append({
            "time": event.get("timestamp", 0),
            "event": event.get("action", ""),
            "node_id": event.get("node_id", ""),
            "duration": event.get("duration"),
        })

    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "status": run.status,
        "progress": round(progress, 4),
        "completed_nodes": completed_nodes,
        "total_nodes": total_nodes,
        "nodes": nodes,
        "timeline": timeline,
        "duration": run.duration,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def generate_performance_report(run: WorkflowRun) -> dict:
    """生成性能报告。

    Returns:
        {
            "total_duration": float,
            "avg_node_duration": float,
            "slowest_nodes": [{"id": str, "duration": float}],
            "retry_stats": {"total_retries": int, "nodes_retried": int},
            "parallelism": float  # 平均并行度
        }
    """
    durations = []
    retries = 0
    retried_nodes = set()

    for state in run.node_states.values():
        dur = state.get("duration", 0)
        if dur > 0:
            durations.append(dur)
        attempt = state.get("attempt", 1)
        if attempt > 1:
            retries += attempt - 1
            retried_nodes.add(state.get("node_id", ""))

    # 最慢节点
    sorted_durations = sorted(durations, reverse=True)
    slowest_nodes = []
    for node_id, state in run.node_states.items():
        dur = state.get("duration", 0)
        if dur > 0:
            slowest_nodes.append({"id": node_id, "duration": round(dur, 3)})
    slowest_nodes.sort(key=lambda x: x["duration"], reverse=True)

    # 计算并行度(简化:用总耗时/串行耗时)
    total_duration = run.duration
    serial_duration = sum(durations) if durations else 1
    parallelism = serial_duration / max(0.001, total_duration) if total_duration > 0 else 1.0

    return {
        "total_duration": round(total_duration, 3),
        "avg_node_duration": round(sum(durations) / len(durations), 3) if durations else 0,
        "slowest_nodes": slowest_nodes[:5],
        "retry_stats": {
            "total_retries": retries,
            "nodes_retried": len(retried_nodes),
        },
        "parallelism": round(parallelism, 2),
        "node_count": len(run.node_states),
    }


def _compute_layers(workflow: Workflow) -> list[list[str]]:
    """计算 DAG 层次(用于布局)。"""
    in_degree = {nid: 0 for nid in workflow.nodes}
    for edge in workflow.edges:
        if edge.target in in_degree:
            in_degree[edge.target] += 1

    layers = []
    remaining = dict(in_degree)

    while remaining:
        layer = [nid for nid, deg in remaining.items() if deg == 0]
        if not layer:
            # 剩余的是循环,全部加入最后一层
            layer = list(remaining.keys())
        layers.append(layer)
        for nid in layer:
            del remaining[nid]
            # 减少后继节点的入度
            for edge in workflow.edges:
                if edge.source == nid and edge.target in remaining:
                    remaining[edge.target] -= 1

    return layers


def _get_layer(node_id: str, layers: list[list[str]]) -> int:
    """获取节点所在的层。"""
    for i, layer in enumerate(layers):
        if node_id in layer:
            return i
    return 0


def _preview_result(result: Any, max_len: int = 200) -> Optional[str]:
    """结果预览(截断)。"""
    if result is None:
        return None
    if isinstance(result, str):
        return result[:max_len] + ("..." if len(result) > max_len else "")
    try:
        preview = result.get("answer") or result.get("output") or result.get("result")
        if isinstance(preview, str):
            return preview[:max_len] + ("..." if len(preview) > max_len else "")
        return str(preview)[:max_len]
    except Exception:
        return str(result)[:max_len]
