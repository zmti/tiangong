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

        # 客服流程模板
        self.register(WorkflowTemplate(
            id="customer_service",
            name="客服流程",
            description="接收客户消息 → 问题分类 → 智能回复 → 记录对话",
            input_schema={
                "customer_message": {"type": "string", "description": "客户消息内容"},
                "customer_id": {"type": "string", "description": "客户ID"},
                "context": {"type": "object", "description": "对话上下文", "default": {}},
            },
            output_schema={
                "reply": {"type": "string", "description": "回复内容"},
                "category": {"type": "string", "description": "问题分类"},
                "confidence": {"type": "number", "description": "分类置信度"},
                "requires_human": {"type": "boolean", "description": "是否需要人工介入"},
            },
            nodes={
                "classify": {
                    "id": "classify",
                    "type": "agent",
                    "name": "问题分类",
                    "config": {
                        "input": "请对以下客户消息进行分类，类别包括：技术支持、销售咨询、投诉建议、一般咨询。返回JSON格式：{\"category\": \"类别\", \"confidence\": 0.95, \"summary\": \"简要摘要\"}\n\n客户消息：${customer_message}"
                    }
                },
                "route": {
                    "id": "route",
                    "type": "condition",
                    "name": "路由分发",
                    "config": {
                        "conditions": [
                            {"expr": "classify.category == '技术支持'", "target": "tech_support"},
                            {"expr": "classify.category == '销售咨询'", "target": "sales"},
                            {"expr": "classify.category == '投诉建议'", "target": "complaint"},
                            {"expr": "true", "target": "general"}
                        ]
                    }
                },
                "tech_support": {
                    "id": "tech_support",
                    "type": "agent",
                    "name": "技术支持回复",
                    "config": {
                        "input": "你是技术支持专家。请根据以下客户消息和问题摘要，提供专业、详细的解决方案：\n\n客户消息：${customer_message}\n问题摘要：{{classify.summary}}"
                    }
                },
                "sales": {
                    "id": "sales",
                    "type": "agent",
                    "name": "销售咨询回复",
                    "config": {
                        "input": "你是销售顾问。请根据以下客户咨询，提供友好、专业的销售建议：\n\n客户咨询：${customer_message}\n问题摘要：{{classify.summary}}"
                    }
                },
                "complaint": {
                    "id": "complaint",
                    "type": "agent",
                    "name": "投诉处理回复",
                    "config": {
                        "input": "你是客户关系专家。请根据以下客户投诉，提供诚恳、有效的解决方案，并判断是否需要人工介入：\n\n客户投诉：${customer_message}\n问题摘要：{{classify.summary}}"
                    }
                },
                "general": {
                    "id": "general",
                    "type": "agent",
                    "name": "一般咨询回复",
                    "config": {
                        "input": "请根据以下客户消息，提供友好、准确的回复：\n\n客户消息：${customer_message}"
                    }
                },
                "format_reply": {
                    "id": "format_reply",
                    "type": "agent",
                    "name": "格式化回复",
                    "config": {
                        "input": "请将以下回复内容格式化为最终回复，保持专业友好的语气：\n{{tech_support.answer}}{{sales.answer}}{{complaint.answer}}{{general.answer}}"
                    }
                },
                "end": {
                    "id": "end",
                    "type": "end",
                    "name": "结束"
                }
            },
            edges=[
                {"source": "classify", "target": "route"},
                {"source": "route", "target": "tech_support", "condition": "classify.category == '技术支持'"},
                {"source": "route", "target": "sales", "condition": "classify.category == '销售咨询'"},
                {"source": "route", "target": "complaint", "condition": "classify.category == '投诉建议'"},
                {"source": "route", "target": "general", "condition": "true"},
                {"source": "tech_support", "target": "format_reply"},
                {"source": "sales", "target": "format_reply"},
                {"source": "complaint", "target": "format_reply"},
                {"source": "general", "target": "format_reply"},
                {"source": "format_reply", "target": "end"},
            ],
            tags=["customer", "service", "support"],
        ))

        # 内容生成模板
        self.register(WorkflowTemplate(
            id="content_generation",
            name="内容生成",
            description="主题分析 → 大纲生成 → 内容创作 → 质量检查",
            input_schema={
                "topic": {"type": "string", "description": "内容主题"},
                "content_type": {"type": "string", "description": "内容类型（文章/营销文案/技术文档）", "default": "文章"},
                "target_audience": {"type": "string", "description": "目标受众", "default": "一般读者"},
                "word_count": {"type": "integer", "description": "期望字数", "default": 1000},
            },
            output_schema={
                "content": {"type": "string", "description": "生成的内容"},
                "outline": {"type": "array", "description": "内容大纲"},
                "quality_score": {"type": "number", "description": "质量评分"},
                "suggestions": {"type": "array", "description": "改进建议"},
            },
            nodes={
                "analyze_topic": {
                    "id": "analyze_topic",
                    "type": "agent",
                    "name": "主题分析",
                    "config": {
                        "input": "请分析以下主题，并生成一个结构化的大纲（包含主要章节和要点）：\n\n主题：${topic}\n内容类型：${content_type}\n目标受众：${target_audience}\n\n请返回JSON格式：{\"outline\": [\"章节1\", \"章节2\", ...], \"key_points\": [\"要点1\", \"要点2\", ...], \"tone\": \"建议语气\"}"
                    }
                },
                "generate_content": {
                    "id": "generate_content",
                    "type": "agent",
                    "name": "内容生成",
                    "config": {
                        "input": "请根据以下大纲和要求，生成一篇高质量的${content_type}：\n\n主题：${topic}\n大纲：{{analyze_topic.outline}}\n关键要点：{{analyze_topic.key_points}}\n目标受众：${target_audience}\n期望字数：${word_count}\n语气建议：{{analyze_topic.tone}}"
                    }
                },
                "quality_check": {
                    "id": "quality_check",
                    "type": "agent",
                    "name": "质量检查",
                    "config": {
                        "input": "请对以下内容进行质量检查，评估可读性、准确性、完整性，并提供改进建议：\n\n${content_type}内容：{{generate_content.content}}\n\n请返回JSON格式：{\"quality_score\": 8.5, \"suggestions\": [\"建议1\", \"建议2\"], \"strengths\": [\"优点1\", \"优点2\"]}"
                    }
                },
                "improve_content": {
                    "id": "improve_content",
                    "type": "agent",
                    "name": "内容优化",
                    "config": {
                        "input": "请根据质量检查结果，优化以下内容：\n\n原始内容：{{generate_content.content}}\n质量评分：{{quality_check.quality_score}}\n改进建议：{{quality_check.suggestions}}"
                    }
                },
                "end": {
                    "id": "end",
                    "type": "end",
                    "name": "结束"
                }
            },
            edges=[
                {"source": "analyze_topic", "target": "generate_content"},
                {"source": "generate_content", "target": "quality_check"},
                {"source": "quality_check", "target": "improve_content"},
                {"source": "improve_content", "target": "end"},
            ],
            tags=["content", "writing", "generation"],
        ))

        # 多步骤表单模板
        self.register(WorkflowTemplate(
            id="multi_step_form",
            name="多步骤表单处理",
            description="表单验证 → 数据处理 → 确认提交 → 结果通知",
            input_schema={
                "form_data": {"type": "object", "description": "表单数据"},
                "form_type": {"type": "string", "description": "表单类型（注册/订单/反馈）"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
            output_schema={
                "validation_result": {"type": "object", "description": "验证结果"},
                "submission_id": {"type": "string", "description": "提交ID"},
                "status": {"type": "string", "description": "处理状态"},
                "message": {"type": "string", "description": "处理消息"},
            },
            nodes={
                "validate_form": {
                    "id": "validate_form",
                    "type": "script",
                    "name": "表单验证",
                    "config": {
                        "code": """
# 表单验证脚本
import json
from datetime import datetime

form_data = ${form_data}
form_type = ${form_type}
user_id = ${user_id}

errors = []
warnings = []

# 基础验证
if not user_id:
    errors.append("用户ID不能为空")

if not form_data:
    errors.append("表单数据不能为空")
else:
    # 根据表单类型进行特定验证
    if form_type == "registration":
        required_fields = ["username", "email", "password"]
        for field in required_fields:
            if field not in form_data or not form_data[field]:
                errors.append(f"注册表单缺少必填字段: {field}")
        
        if "email" in form_data:
            import re
            email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if not re.match(email_pattern, form_data["email"]):
                errors.append("邮箱格式不正确")
        
        if "password" in form_data and len(form_data["password"]) < 8:
            warnings.append("密码长度建议至少8位")
    
    elif form_type == "order":
        required_fields = ["product_id", "quantity", "address"]
        for field in required_fields:
            if field not in form_data or not form_data[field]:
                errors.append(f"订单表单缺少必填字段: {field}")
        
        if "quantity" in form_data:
            try:
                qty = int(form_data["quantity"])
                if qty <= 0:
                    errors.append("数量必须大于0")
                elif qty > 100:
                    warnings.append("数量较大，可能需要特殊处理")
            except ValueError:
                errors.append("数量必须是数字")
    
    elif form_type == "feedback":
        if "rating" in form_data:
            try:
                rating = int(form_data["rating"])
                if rating < 1 or rating > 5:
                    errors.append("评分必须在1-5之间")
            except ValueError:
                errors.append("评分必须是数字")

is_valid = len(errors) == 0
result = {
    "is_valid": is_valid,
    "errors": errors,
    "warnings": warnings,
    "validated_at": datetime.now().isoformat(),
    "form_type": form_type,
    "user_id": user_id,
    "field_count": len(form_data) if form_data else 0
}

return {"validation_result": result}
"""
                    }
                },
                "check_validation": {
                    "id": "check_validation",
                    "type": "condition",
                    "name": "验证检查",
                    "config": {
                        "conditions": [
                            {"expr": "validate_form.validation_result.is_valid == true", "target": "process_data"},
                            {"expr": "true", "target": "handle_error"}
                        ]
                    }
                },
                "process_data": {
                    "id": "process_data",
                    "type": "agent",
                    "name": "数据处理",
                    "config": {
                        "input": "请处理以下${form_type}表单数据，并生成提交ID：\n\n用户ID：${user_id}\n表单数据：${form_data}\n验证结果：{{validate_form.validation_result}}\n\n请返回JSON格式：{\"submission_id\": \"sub_\" + 随机ID, \"processed_data\": {...}, \"status\": \"pending\"}"
                    }
                },
                "handle_error": {
                    "id": "handle_error",
                    "type": "agent",
                    "name": "错误处理",
                    "config": {
                        "input": "请根据以下验证错误，生成友好的错误提示：\n\n表单类型：${form_type}\n验证错误：{{validate_form.validation_result.errors}}\n验证警告：{{validate_form.validation_result.warnings}}"
                    }
                },
                "send_notification": {
                    "id": "send_notification",
                    "type": "tool",
                    "name": "发送通知",
                    "config": {
                        "tool_name": "send_notification",
                        "params": {
                            "user_id": "${user_id}",
                            "message": "您的${form_type}已成功提交",
                            "submission_id": "{{process_data.submission_id}}"
                        }
                    }
                },
                "end": {
                    "id": "end",
                    "type": "end",
                    "name": "结束"
                }
            },
            edges=[
                {"source": "validate_form", "target": "check_validation"},
                {"source": "check_validation", "target": "process_data", "condition": "validate_form.validation_result.is_valid == true"},
                {"source": "check_validation", "target": "handle_error", "condition": "true"},
                {"source": "process_data", "target": "send_notification"},
                {"source": "send_notification", "target": "end"},
                {"source": "handle_error", "target": "end"},
            ],
            tags=["form", "validation", "multi-step"],
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
