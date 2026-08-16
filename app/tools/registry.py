"""工具注册器:装饰器注册、JSON Schema 校验、执行超时、统一输出。

用法:
    @tool("查询天气", "根据城市名查询天气")
    def weather(city: str = Field(description="城市名"), days: int = Field(1, description="天数")):
        ...
"""
from __future__ import annotations

import asyncio
import inspect
import json
from functools import wraps
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, TypeAdapter, create_model
from pydantic.fields import FieldInfo


class ToolError(Exception):
    pass


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        timeout: float = 30.0,
        params_model: Optional[type[BaseModel]] = None,
        json_schema: Optional[dict] = None,
        enabled: bool = True,
        terminal: bool = False,
    ):
        self.name = name
        self.description = description
        self.func = func
        self.timeout = timeout
        self.params_model = params_model
        self.json_schema = json_schema or {"type": "object", "properties": {}}
        self.enabled = enabled
        self.terminal = terminal
        self.is_async = asyncio.iscoroutinefunction(func)

    def to_openai_schema(self) -> dict:
        """转换为 OpenAI function calling 格式的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema,
            },
        }

    async def run(self, arguments: str | dict) -> str:
        """执行工具,统一返回 JSON 字符串结果。"""
        if not self.enabled:
            return json.dumps({"error": f"工具 {self.name} 已被禁用"}, ensure_ascii=False)

        # 参数解析
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                raise ToolError(f"工具 {self.name} 参数解析失败: {e}") from e
        else:
            args = arguments

        # 参数必须是 JSON 对象
        if not isinstance(args, dict):
            raise ToolError(
                f"工具 {self.name} 参数格式错误: 应为 JSON 对象(如 {self.json_schema.get('required', [])}),"
                f"收到 {type(args).__name__}"
            )

        # 参数校验
        if self.params_model is not None:
            try:
                validated = self.params_model.model_validate(args)
                args = validated.model_dump()
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"工具 {self.name} 参数校验失败: {e}") from e

        # 执行(带超时)
        try:
            if self.is_async:
                result = await asyncio.wait_for(self.func(**args), timeout=self.timeout)
            else:
                result = await asyncio.wait_for(asyncio.to_thread(self.func, **args), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise ToolError(f"工具 {self.name} 执行超时({self.timeout}s)") from exc
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"工具 {self.name} 执行出错: {e}") from e

        # 统一序列化结果
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            return str(result)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._imported = False

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self, enabled_only: bool = True) -> list[Tool]:
        tools = list(self._tools.values())
        if enabled_only:
            tools = [t for t in tools if t.enabled]
        return tools

    def to_openai_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self.list()]

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)


def _build_params_model(func: Callable) -> tuple[Optional[type[BaseModel]], dict]:
    """从函数签名构建 pydantic 参数模型和 JSON Schema。"""
    sig = inspect.signature(func)
    if not sig.parameters:
        return None, {"type": "object", "properties": {}, "additionalProperties": False}

    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
        default = param.default
        if isinstance(default, FieldInfo):
            fields[name] = (annotation, default)
        elif default is inspect.Parameter.empty:
            fields[name] = (annotation, Field(...))
        else:
            fields[name] = (annotation, Field(default))

    model = create_model(f"{func.__name__}_params", **fields)
    schema = TypeAdapter(model).json_schema()
    # pydantic v2 schema 是 $defs 风格,简化成直接 properties
    simplified = {"type": "object", "properties": schema.get("properties", {}), "additionalProperties": False}
    required = schema.get("required", [])
    if required:
        simplified["required"] = required
    return model, simplified


def tool(name: Optional[str] = None, description: str = "", timeout: float = 30.0, enabled: bool = True, terminal: bool = False):
    """工具装饰器:注册到全局默认注册器。支持 @tool 和 @tool(...) 两种用法。

    terminal=True 表示工具执行成功后即为最终结果,agent 不再调用 LLM 收尾(适用于图片生成等返回体直接展示的工具)。
    """

    def _decorate(func: Callable) -> Tool:
        tool_name = name if isinstance(name, str) else func.__name__
        tool_desc = description or (inspect.getdoc(func) or "").strip() or f"执行 {tool_name}"
        params_model, json_schema = _build_params_model(func)
        t = Tool(
            name=tool_name,
            description=tool_desc,
            func=func,
            timeout=timeout,
            params_model=params_model,
            json_schema=json_schema,
            enabled=enabled,
            terminal=terminal,
        )
        default_registry.register(t)
        return t

    # 用法一: @tool
    if callable(name):
        func = name
        name = None
        return _decorate(func)
    # 用法二: @tool(...)
    return _decorate


default_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """获取全局注册器,首次调用时导入内置工具。"""
    if not default_registry._imported:
        from app.tools import builtin  # noqa: F401  # 触发装饰器注册
        from app.tools import code_tool  # noqa: F401  # 注册 run_code 工具
        from app.tools import image_tool  # noqa: F401  # 注册 generate_image 工具
        from app.tools import video_tool  # noqa: F401  # 注册 generate_video 工具
        from app.tools import web_tool  # noqa: F401  # 注册网页抓取工具
        from app.tools import db_tool  # noqa: F401  # 注册数据库查询工具
        from app.tools import mcp_tool  # noqa: F401  # MCP 工具支持
        from app.tools import schedule_tool  # noqa: F401  # 注册定时任务工具
        from app.tools import email_tool  # noqa: F401  # 注册邮件工具
        from app.tools import note_tool  # noqa: F401  # 注册笔记工具
        from app.tools import browser_tool  # noqa: F401  # 注册浏览器控制工具
        from app.tools import chart_tool  # noqa: F401  # 注册图表生成工具
        from app.tools import vision_tool  # noqa: F401  # 注册多模态理解工具
        from app.tools import collaboration_tool  # noqa: F401  # 注册人机协作工具
        from app.tools import file_tool  # noqa: F401  # 注册本地文件搜索/读取工具
        from app.tools import todo_tool  # noqa: F401  # 注册待办工具
        from app.tools import plan_tool  # noqa: F401  # 注册任务计划工具
        from app.tools import memory_tool  # noqa: F401  # 注册长期记忆工具
        from app.tools import terminal_tool  # noqa: F401  # 注册终端命令工具
        from app.tools import git_tool  # noqa: F401  # 注册 git 工具集
        from app.tools import code_search_tool  # noqa: F401  # 注册代码库理解工具
        from app.tools import delegation_tool  # noqa: F401  # 注册子智能体委派工具
        from app.agents import handoff_tool  # noqa: F401  # 注册角色委派/团队工具(list_roles/handoff_to_role)
        from app.rag import documents  # noqa: F401  # 注册 rag_search 工具

        default_registry._imported = True
    return default_registry
