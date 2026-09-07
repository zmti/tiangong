"""动态边路由评估器。

支持基于运行时变量的动态边激活、模板变量替换、路径访问。
用于替代静态条件表达式,实现真正的动态 DAG 路由。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Jinja2 风格模板变量模式: {{var_path}} 或 {{var_path.field}}
_TEMPLATE_PATTERN = re.compile(r'\{\{([^}]+)\}\}')


class DynamicEdgeEvaluator:
    """动态边评估器:根据运行时上下文计算边是否应激活。

    支持:
    - 模板变量替换: {{node_id.output.field}} -> 从上下文获取值
    - 安全表达式求值: 使用 safe_eval 评估条件表达式
    - 默认分支: 无匹配条件时路由到 default_target
    """

    def __init__(
        self,
        variables: dict[str, Any],
        node_results: Optional[dict[str, Any]] = None,
    ):
        """
        Args:
            variables: 工作流运行变量
            node_results: 已执行节点的结果 {node_id: result}
        """
        self.variables = variables
        self.node_results = node_results or {}
        # 合并为统一上下文
        self._context: dict[str, Any] = {**self.variables}
        self._context.update(self.node_results)

    def evaluate(self, condition_expr: str) -> bool:
        """评估条件表达式,返回是否应激活该边。

        Args:
            condition_expr: 条件表达式(支持模板变量)

        Returns:
            True = 边应激活, False = 边应跳过
        """
        if not condition_expr or not condition_expr.strip():
            return True  # 无条件边

        # 1. 模板变量替换
        rendered = self._render_template(condition_expr)

        # 2. 安全求值
        try:
            from app.core.workflow.safe_eval import safe_eval
            result = safe_eval(rendered, self._context)
            return bool(result)
        except Exception as e:
            logger.warning(
                "动态边条件评估失败: expr=%s, error=%s",
                condition_expr, e,
            )
            return False

    def find_active_targets(
        self,
        source_node_id: str,
        edges: list,
    ) -> list[str]:
        """找出源节点的所有激活目标节点。

        Args:
            source_node_id: 源节点 ID
            edges: 从该节点出发的边列表

        Returns:
            激活的目标节点 ID 列表
        """
        targets = []
        default_target = None

        for edge in edges:
            cond = getattr(edge, 'condition_expr', None) or getattr(edge, 'condition', None)
            if not cond:
                # 无条件边,直接激活
                targets.append(edge.target)
            else:
                if self.evaluate(cond):
                    targets.append(edge.target)
                elif getattr(edge, 'is_default', False):
                    default_target = edge.target

        # 如果没有边激活且有默认目标,路由到默认
        if not targets and default_target:
            targets.append(default_target)

        return targets

    def _render_template(self, text: str) -> str:
        """渲染模板变量。

        支持格式:
        - {{node_id}} -> 节点结果
        - {{node_id.output}} -> 节点结果的 output 字段
        - {{node_id.output.result.value}} -> 嵌套路径访问
        - {{variable_name}} -> 工作流变量
        """
        def replace_match(m: re.Match) -> str:
            path = m.group(1).strip()
            value = self._resolve_path(path)
            if value is None:
                return m.group(0)  # 保留原样
            return json.dumps(value, ensure_ascii=False)

        return _TEMPLATE_PATTERN.sub(replace_match, text)

    def _resolve_path(self, path: str) -> Any:
        """解析点分路径,如 'node_1.output.answer'。"""
        parts = path.split('.')
        current = self._context

        for part in parts:
            if isinstance(current, dict):
                if part in current:
                    current = current[part]
                else:
                    return None
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                return None

        return current

    def get_context_summary(self) -> dict:
        """获取当前上下文的摘要(用于调试)。"""
        summary = {}
        for key, value in self._context.items():
            if isinstance(value, dict):
                summary[key] = {k: str(v)[:100] for k, v in list(value.items())[:5]}
            elif isinstance(value, (str, int, float, bool)):
                summary[key] = str(value)[:200]
            else:
                summary[key] = type(value).__name__
        return summary


class RouteRule:
    """路由规则定义。"""

    def __init__(
        self,
        condition: str,
        targets: list[str],
        is_default: bool = False,
        description: str = "",
    ):
        self.condition = condition
        self.targets = targets
        self.is_default = is_default
        self.description = description

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "targets": self.targets,
            "is_default": self.is_default,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RouteRule":
        return cls(
            condition=data.get("condition", ""),
            targets=data.get("targets", []),
            is_default=data.get("is_default", False),
            description=data.get("description", ""),
        )
