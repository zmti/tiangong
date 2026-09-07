"""工作流模板系统。

支持:
- 模板定义(可复用的子图)
- 模板实例化(生成独立工作流)
- 模板变量替换
- 内置模板注册表
"""
from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.workflow.models import Workflow, WorkflowNode, WorkflowEdge, NodeType

logger = logging.getLogger(__name__)


@dataclass
class WorkflowTemplate:
    """工作流模板(可复用的子图定义)。"""
    id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    author: str = ""

    # 模板节点和边
    nodes: dict[str, dict] = field(default_factory=dict)  # 模板内节点定义
    edges: list[dict] = field(default_factory=list)       # 模板内边定义

    # 输入输出 schema
    input_schema: dict[str, dict] = field(default_factory=dict)   # {变量名: {"type": str, "default": Any}}
    output_schema: dict[str, dict] = field(default_factory=dict)  # {变量名: {"type": str}}

    # 元数据
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "nodes": self.nodes,
            "edges": self.edges,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowTemplate":
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            author=data.get("author", ""),
            nodes=data.get("nodes", {}),
            edges=data.get("edges", []),
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


class WorkflowTemplateRegistry:
    """工作流模板注册表。"""

    def __init__(self):
        self._templates: dict[str, WorkflowTemplate] = {}
        self._load_builtin_templates()

    def _load_builtin_templates(self) -> None:
        """加载内置模板。"""
        # 研究 + 报告生成模板
        self.register(WorkflowTemplate(
            id="research_report",
            name="研究分析报告",
            description="搜索信息 → 分析总结 → 生成报告",
            input_schema={
                "topic": {"type": "string", "description": "研究主题"},
                "depth": {"type": "integer", "description": "研究深度(1-5)", "default": 3},
                "format": {"type": "string", "description": "输出格式", "default": "markdown"},
            },
            output_schema={
                "report": {"type": "string", "description": "生成的报告"},
                "sources": {"type": "array", "description": "参考来源"},
                "summary": {"type": "string", "description": "摘要"},
            },
            nodes={
                "search": {
                    "id": "search",
                    "type": "tool",
                    "name": "信息搜索",
                    "config": {
                        "tool_name": "web_search",
                        "params": {"query": "${topic}", "num_results": "${depth}"}
                    }
                },
                "analyze": {
                    "id": "analyze",
                    "type": "agent",
                    "name": "分析总结",
                    "config": {
                        "input": "请分析以下搜索结果,提取关键信息,生成结构化摘要:\n{{search.result}}"
                    }
                },
                "report": {
                    "id": "report",
                    "type": "agent",
                    "name": "生成报告",
                    "config": {
                        "input": "请根据以下分析结果生成${format}格式的报告:\n{{analyze.answer}}"
                    }
                },
                "end": {
                    "id": "end",
                    "type": "end",
                    "name": "结束"
                }
            },
            edges=[
                {"source": "search", "target": "analyze"},
                {"source": "analyze", "target": "report"},
                {"source": "report", "target": "end"},
            ],
            tags=["research", "report", "analysis"],
        ))

        # 代码审查模板
        self.register(WorkflowTemplate(
            id="code_review",
            name="代码审查",
            description="静态分析 → 同行评审 → 生成报告",
            input_schema={
                "code": {"type": "string", "description": "待审查的代码"},
                "language": {"type": "string", "description": "编程语言", "default": "python"},
            },
            output_schema={
                "issues": {"type": "array", "description": "发现的问题"},
                "suggestions": {"type": "array", "description": "改进建议"},
                "overall_score": {"type": "number", "description": "整体评分"},
            },
            nodes={
                "static_analysis": {
                    "id": "static_analysis",
                    "type": "tool",
                    "name": "静态分析",
                    "config": {
                        "tool_name": "check_syntax",
                        "params": {"code": "${code}", "language": "${language}"}
                    }
                },
                "peer_review": {
                    "id": "peer_review",
                    "type": "agent",
                    "name": "同行评审",
                    "config": {
                        "input": "请作为代码评审专家,审查以下代码:\n\n{{static_analysis.result}}\n\n请列出发现的问题和改进建议。"
                    }
                },
                "report": {
                    "id": "report",
                    "type": "agent",
                    "name": "生成审查报告",
                    "config": {
                        "input": "请生成代码审查报告,包含问题列表、改进建议和整体评分。"
                    }
                },
                "end": {
                    "id": "end",
                    "type": "end",
                    "name": "结束"
                }
            },
            edges=[
                {"source": "static_analysis", "target": "peer_review"},
                {"source": "peer_review", "target": "report"},
                {"source": "report", "target": "end"},
            ],
            tags=["code", "review", "quality"],
        ))

        # 数据预处理模板
        self.register(WorkflowTemplate(
            id="data_pipeline",
            name="数据预处理管道",
            description="数据加载 → 清洗 → 转换 → 验证",
            input_schema={
                "data_source": {"type": "string", "description": "数据来源"},
                "processing_steps": {"type": "array", "description": "处理步骤列表"},
            },
            output_schema={
                "processed_data": {"type": "array", "description": "处理后的数据"},
                "validation_result": {"type": "object", "description": "验证结果"},
            },
            nodes={
                "load": {
                    "id": "load",
                    "type": "tool",
                    "name": "数据加载",
                    "config": {"tool_name": "read_file", "params": {"path": "${data_source}"}}
                },
                "clean": {
                    "id": "clean",
                    "type": "script",
                    "name": "数据清洗",
                    "config": {
                        "code": """
# 数据清洗脚本
import json
data = ${data}
# 移除空值
data = [d for d in data if d is not None]
# 移除重复项
seen = set()
unique_data = []
for d in data:
    key = json.dumps(d, sort_keys=True)
    if key not in seen:
        seen.add(key)
        unique_data.append(d)
return {"cleaned_data": unique_data, "removed_duplicates": len(data) - len(unique_data)}
"""
                    }
                },
                "validate": {
                    "id": "validate",
                    "type": "agent",
                    "name": "数据验证",
                    "config": {"input": "请验证以下数据是否符合要求:\n{{clean.result}}"
                    }
                },
                "end": {"id": "end", "type": "end", "name": "结束"}
            },
            edges=[
                {"source": "load", "target": "clean"},
                {"source": "clean", "target": "validate"},
                {"source": "validate", "target": "end"},
            ],
            tags=["data", "pipeline", "ETL"],
        ))

        logger.info(f"已加载 {len(self._templates)} 个内置模板")

    def register(self, template: WorkflowTemplate) -> None:
        """注册模板。"""
        self._templates[template.id] = template
        logger.info(f"模板已注册: {template.id} ({template.name})")

    def get(self, template_id: str) -> Optional[WorkflowTemplate]:
        """获取模板。"""
        return self._templates.get(template_id)

    def list_all(self) -> list[WorkflowTemplate]:
        """列出所有模板。"""
        return list(self._templates.values())

    def search(self, keyword: str, limit: int = 20) -> list[WorkflowTemplate]:
        """搜索模板。"""
        keyword = keyword.lower()
        results = []
        for template in self._templates.values():
            if (keyword in template.id.lower() or
                keyword in template.name.lower() or
                keyword in template.description.lower() or
                any(keyword in tag.lower() for tag in template.tags)):
                results.append(template)
        return results[:limit]

    def instantiate(
        self,
        template_id: str,
        instance_id: str,
        inputs: dict[str, Any],
    ) -> Workflow:
        """实例化模板,生成独立工作流。

        Args:
            template_id: 模板 ID
            instance_id: 实例 ID
            inputs: 输入变量

        Returns:
            生成的工作流
        """
        template = self._templates.get(template_id)
        if not template:
            raise ValueError(f"模板不存在: {template_id}")

        # 深拷贝节点和边
        nodes = copy.deepcopy(template.nodes)
        edges = copy.deepcopy(template.edges)

        # 替换模板变量
        nodes = self._substitute_variables(nodes, inputs)
        edges = self._substitute_variables(edges, inputs)

        # 重新生成节点 ID(添加实例前缀)
        prefix = f"{instance_id}_"
        id_map = {}
        for old_id in list(nodes.keys()):
            new_id = prefix + old_id
            id_map[old_id] = new_id
            nodes[new_id] = nodes.pop(old_id)
            nodes[new_id]["id"] = new_id

        # 更新边的引用
        for edge in edges:
            edge["source"] = id_map.get(edge["source"], edge["source"])
            edge["target"] = id_map.get(edge["target"], edge["target"])

        # 构建 Workflow 对象
        workflow_nodes = {}
        for node_data in nodes.values():
            node = WorkflowNode(
                id=node_data["id"],
                type=NodeType(node_data["type"]),
                name=node_data.get("name", node_data["id"]),
                config=node_data.get("config", {}),
            )
            workflow_nodes[node.id] = node

        workflow_edges = [
            WorkflowEdge(
                id=f"edge_{i}",
                source=e["source"],
                target=e["target"],
                condition=e.get("condition"),
                condition_expr=e.get("condition_expr"),
            )
            for i, e in enumerate(edges)
        ]

        # 找到入口节点
        entry_node = None
        for node_id, node_data in nodes.items():
            has_incoming = any(e["target"] == node_id for e in edges)
            if not has_incoming:
                entry_node = node_id
                break

        return Workflow(
            id=f"wf_inst_{instance_id}",
            name=f"{template.name}_{instance_id}",
            description=template.description,
            nodes=workflow_nodes,
            edges=workflow_edges,
            entry_node=entry_node,
            version=f"{template.version}.inst",
            metadata={"template_id": template_id, "inputs": inputs},
        )

    def _substitute_variables(self, obj: Any, inputs: dict[str, Any]) -> Any:
        """递归替换模板变量。"""
        if isinstance(obj, str):
            # 简单字符串替换
            result = obj
            for key, value in inputs.items():
                placeholder = f"${{{key}}}"
                if placeholder in result:
                    result = result.replace(placeholder, str(value))
            # 也支持 Jinja2 风格
            result = result.replace("${", "{{").replace("}", "}}")
            # 再次尝试简单替换
            for key, value in inputs.items():
                template_var = f"{{{{{key}}}}}"
                if template_var in result:
                    result = result.replace(template_var, str(value))
            return result
        elif isinstance(obj, dict):
            return {k: self._substitute_variables(v, inputs) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_variables(item, inputs) for item in obj]
        return obj


# 全局模板注册表
_template_registry: Optional[WorkflowTemplateRegistry] = None


def get_template_registry() -> WorkflowTemplateRegistry:
    """获取全局模板注册表。"""
    global _template_registry
    if _template_registry is None:
        _template_registry = WorkflowTemplateRegistry()
    return _template_registry
