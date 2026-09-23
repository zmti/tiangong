"""工具注册器:装饰器注册、JSON Schema 校验、执行超时、统一输出。

用法:
    @tool("查询天气", "根据城市名查询天气")
    def weather(city: str = Field(description="城市名"), days: int = Field(1, description="天数")):
        ...
"""
from __future__ import annotations

import asyncio
import threading
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

    def __call__(self, *args, **kwargs):
        """让 Tool 实例能像原函数一样被直接调用(转发到 self.func)。

        @tool 装饰器会把模块里的函数替换成 Tool 实例。若另一个工具在函数体里
        `from app.tools import x` 后像普通函数一样调用 x(...),会得到
        "'Tool' object is not callable" —— read_url / fetch_page 正是这样
        100% 失败的(2026-09-23 真实 trace)。
        """
        return self.func(*args, **kwargs)

    # 部分模型/网关会把真实参数再包一层信封,形如:
    #   {"arguments": "{\"code\": \"print(1)\"}"}   或   {"input": {...}}
    # 直接送去 pydantic 校验会变成「参数校验失败: xxx Field required」,
    # 模型看不出是自己包错了,只会原样重试到连续失败保护触发。
    _ENVELOPE_KEYS = ("arguments", "input", "parameters", "args")
    # 信封旁边的"元数据"键(形如 {"name": "run_code", "arguments": "{...}"})
    _ENVELOPE_META_KEYS = frozenset({"name", "id", "index", "type", "function", "tool", "tool_name"})

    def _unwrap_envelope(self, args: dict) -> dict:
        """摊平 {"arguments": <真实参数对象>} 这类嵌套信封(最多 3 层)。

        只在这两个条件同时成立时才解:
          1. 存在信封键,且该名字**不是**本工具声明的形参(已确认无工具用这些名字);
          2. 除信封键外的其余键都是元数据(name/id/index/type/function...)。
        这样既覆盖 {"arguments": "..."} 和 {"arguments": {...}},
        也覆盖 {"name": ..., "arguments": ...},且不会误伤正常参数。
        """
        for _ in range(3):
            if not isinstance(args, dict):
                return args
            props = (self.json_schema or {}).get("properties") or {}
            env_keys = [k for k in args if k in self._ENVELOPE_KEYS and k not in props]
            if len(env_keys) != 1:
                return args
            key = env_keys[0]
            rest = set(args) - {key}
            if not rest <= self._ENVELOPE_META_KEYS:
                return args
            inner = args[key]
            if isinstance(inner, str):
                s = inner.strip()
                if not (s.startswith("{") and s.endswith("}")):
                    return args
                try:
                    inner = json.loads(s)
                except json.JSONDecodeError:
                    return args
            if not isinstance(inner, dict):
                return args
            args = inner
        return args

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

        # 摊平可能的嵌套信封(如 {"arguments": "{\"code\": ...}"} 之类)
        args = self._unwrap_envelope(args)

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
                # 复制当前 contextvars 到子线程(解决 asyncio.to_thread 不传播 ContextVar 的问题)
                import contextvars
                ctx = contextvars.copy_context()
                result = await asyncio.wait_for(
                    asyncio.to_thread(ctx.run, self.func, **args),
                    timeout=self.timeout,
                )
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
        # 使用 asyncio.Lock 保护工具注册操作
        self._lock = threading.RLock()

    def register(self, tool: Tool) -> None:
        with self._lock:
            # 同名工具重复注册处理:
            # 旧实现直接覆盖(self._tools[name] = tool),导致后导入的模块"抢占"同名工具,
            # LLM 按 OpenAI schema 看到的参数签名可能是先注册版本的,实际执行的却是
            # 后注册版本的 func → 参数对不上 → 工具报 "edits 不是合法 JSON" 这类失败。
            # 现改为:已存在同名工具时保留先注册的(稳定优先),并记 warning,避免静默覆盖。
            if tool.name in self._tools:
                import logging
                logging.getLogger("tools.registry").warning(
                    "工具 %s 重复注册,保留先注册的实现(新实现来自 %s 被忽略)",
                    tool.name, getattr(tool.func, "__module__", "?"))
                return
            self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_sync(self, enabled_only: bool = True) -> list[Tool]:
        # 同步版本，使用 RLock 代替 asyncio.Lock
        with self._lock:
            tools = list(self._tools.values())
            if enabled_only:
                tools = [t for t in tools if t.enabled]
            return tools

    async def list(self, enabled_only: bool = True):
        """异步兼容接口:返回异步迭代器(供 async for / await 调用)。"""
        for tool in self.list_sync(enabled_only):
            yield tool

    def to_openai_schemas(self) -> list[dict]:
        tools = self.list_sync()
        return [t.to_openai_schema() for t in tools]

    def unregister(self, name: str) -> None:
        with self._lock:
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
        # 使用同步注册，因为装饰器在模块导入时调用
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
        from app.tools import video_enhance_tool  # noqa: F401  # 注册 enhance_video 工具(配音/字幕/水印)
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
        from app.tools import file_watcher_tool  # noqa: F401  # 注册文件监听工具
        from app.tools import feedback_tool  # noqa: F401  # 注册用户反馈工具
        from app.tools import lsp_tool  # noqa: F401  # 注册 LSP 工具(悬停/定义/引用)
        from app.tools import build_tool  # noqa: F401  # 注册构建感知工具
        from app.tools import git_pr_tool  # noqa: F401  # 注册 Git PR 工作流工具
        from app.tools import code_review_tool  # noqa: F401  # 注册代码审阅工具
        from app.tools import debug_assist_tool  # noqa: F401  # 注册调试辅助工具
        from app.tools import dependency_tool  # noqa: F401  # 注册依赖管理工具
        from app.tools import project_tool  # noqa: F401  # 注册项目管理工具(scaffold/analyze/testgen/docs/build)
        from app.tools import code_file  # noqa: F401  # 注册结构化代码编辑工具
        # 说明: code_file 的 edit_file 与 file_tool 同名但带独立前缀(code_edit_file),
        # 避免"后注册者覆盖先注册者"导致 LLM 实际调用时拿到参数签名不一致的实现,
        # 表现为"执行任务时读写文件失败: edits 不是合法 JSON"。
        # 详见 ToolRegistry._register 的重复名处理逻辑。
        from app.tools import cost_tool  # noqa: F401  # 注册成本追踪工具(cost_summary/cost_report)
        from app.tools import image_ocr  # noqa: F401  # 注册图片 OCR 工具(image_ocr/screenshot_ocr)
        from app.tools import search_tool  # noqa: F401  # 注册实时搜索工具(web_search/news_search)
        from app.tools import multimodal_tool  # noqa: F401  # 注册增强多模态工具(analyze_image_v2/transcribe_audio/synthesize_speech/cross_modal_reason)
        from app.tools import document_tool  # noqa: F401  # 注册文档深度解析工具(parse_document/document_summary)
        from app.tools import computer_use  # noqa: F401  # 注册 Computer Use 视觉驾驶工具
        from app.tools import workflow_tool  # noqa: F401  # 注册工作流工具(run_workflow/list_workflows/get_workflow)
        from app.tools import web_test_tool  # noqa: F401  # 注册 H5/网页自动测试工具(web_auto_test)
        from app.agents import handoff_tool  # noqa: F401  # 注册角色委派/团队工具(list_roles/handoff_to_role)
        from app.rag import documents  # noqa: F401  # 注册 rag_search 工具

        default_registry._imported = True
    return default_registry
