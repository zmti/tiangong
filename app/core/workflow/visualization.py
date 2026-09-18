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
        # _compute_layers 返回的是节点 ID 字符串列表(非节点对象), 直接按 ID 取 x 坐标
        for i, node_id in enumerate(layer_nodes):
            node_x[node_id] = i * 150
    # 兜底: 未参与层次布局的节点也给坐标, 避免前端拿到缺失值导致 SVG path 出现 NaN
    for index, node_id in enumerate(workflow.nodes):
        node_x.setdefault(node_id, index * 150)

    node_width = 180
    node_height = 96
    for node_id, node in workflow.nodes.items():
        layer_idx = _get_layer(node_id, layers)
        position = node.position or {}
        x = _safe_float(position.get("x"), node_x.get(node_id, 0))
        y = _safe_float(position.get("y"), layer_y.get(layer_idx, 0))
        # 纯函数语义: 可视化数据独立计算, 不原地修改调用方 Workflow 对象的节点坐标
        # (旧实现在此处写回 node.position, 导致调用方后续读取时拿到"被可视化改写"的坐标)
        nodes.append({
            "id": node_id,
            "type": node.type.value,
            "label": node.name,
            "description": node.description,
            "x": x,
            "y": y,
            "width": node_width,
            "height": node_height,
            "position": {"x": x, "y": y, "width": node_width, "height": node_height},
            "config": node.config,
            "error_strategy": node.error_strategy.value if hasattr(node.error_strategy, 'value') else str(node.error_strategy),
        })

    node_ids = set(workflow.nodes)
    node_by_id = {n["id"]: n for n in nodes}
    for edge in workflow.edges:
        condition_label = None
        if edge.condition_expr:
            condition_label = edge.condition_expr[:50] + ("..." if len(edge.condition_expr) > 50 else "")
        elif edge.condition:
            condition_label = edge.condition[:50] + ("..." if len(edge.condition) > 50 else "")

        source_node = node_by_id.get(edge.source)
        target_node = node_by_id.get(edge.target)
        edge_item = {
            "source": edge.source,
            "target": edge.target,
            "label": condition_label,
            "id": edge.id,
            "valid": edge.source in node_ids and edge.target in node_ids,
            "path": _build_edge_path(source_node, target_node, node_width, node_height),
        }
        edges.append(edge_item)

    x_values = [n["x"] for n in nodes]
    y_values = [n["y"] for n in nodes]
    min_x = min(x_values) if x_values else 0.0
    min_y = min(y_values) if y_values else 0.0
    max_x = max((n["x"] + n["width"] for n in nodes), default=min_x + node_width)
    max_y = max((n["y"] + n["height"] for n in nodes), default=min_y + node_height)
    padding = 60
    viewbox = {
        "x": round(_safe_float(min_x, 0.0) - padding, 2),
        "y": round(_safe_float(min_y, 0.0) - padding, 2),
        "width": round(_safe_float(max_x, 0.0) - _safe_float(min_x, 0.0) + padding * 2, 2),
        "height": round(_safe_float(max_y, 0.0) - _safe_float(min_y, 0.0) + padding * 2, 2),
    }
    if viewbox["width"] <= 0:
        viewbox["width"] = float(node_width + padding * 2)
    if viewbox["height"] <= 0:
        viewbox["height"] = float(node_height + padding * 2)

    return {
        "nodes": nodes,
        "edges": edges,
        "viewBox": viewbox,
        "layout": "manual" if all(node.position and _is_finite_number(node.position.get("x")) and _is_finite_number(node.position.get("y")) for node in workflow.nodes.values()) else "dagre",
        "meta": {
            "workflow_id": workflow.id,
            "name": workflow.name,
            "nodes_count": len(nodes),
            "edges_count": len(edges),
            "has_missing_positions": False,
        }
    }


def _build_edge_path(source_node: Optional[dict], target_node: Optional[dict], node_width: int, node_height: int) -> str:
    """生成可直接赋给 SVG path d 属性的连线路径。"""
    if not source_node or not target_node:
        return ""
    source_x = _safe_float(source_node.get("x"), None)
    source_y = _safe_float(source_node.get("y"), None)
    target_x = _safe_float(target_node.get("x"), None)
    target_y = _safe_float(target_node.get("y"), None)
    if source_x is None or source_y is None or target_x is None or target_y is None:
        return ""
    start_x = source_x + node_width
    start_y = source_y + node_height / 2
    end_x = target_x
    end_y = target_y + node_height / 2
    mid_x = (start_x + end_x) / 2
    return (
        f"M {start_x:.2f} {start_y:.2f} "
        f"C {mid_x:.2f} {start_y:.2f}, "
        f"{mid_x:.2f} {end_y:.2f}, "
        f"{end_x:.2f} {end_y:.2f}"
    )


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """把节点坐标转成有限 float, 异常/缺失时回退默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _is_finite_number(value: Any) -> bool:
    """判断值是否为有限数字。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


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
