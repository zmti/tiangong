"""工具注册器测试。"""
import json

import pytest
from pydantic import Field

from app.tools.registry import Tool, ToolRegistry, tool


def test_tool_decorator_registers():
    reg = ToolRegistry()

    @tool
    def add(a: int = Field(description="加数"), b: int = Field(1, description="被加数")) -> str:
        return {"sum": a + b}

    # 装饰器注册到 default_registry, 这里检查 schema 生成
    assert add.json_schema["properties"]["a"]["description"] == "加数"
    assert add.json_schema["required"] == ["a"]


def test_tool_execute_and_validate():
    reg = ToolRegistry()

    @tool
    def add(a: int = Field(description="加数"), b: int = Field(1, description="被加数")) -> str:
        return {"sum": a + b}

    # 正确参数
    import asyncio

    result = asyncio.run(add.run('{"a": 2, "b": 3}'))
    data = json.loads(result)
    assert data["sum"] == 5

    # 缺参数校验失败
    with pytest.raises(Exception):
        asyncio.run(add.run('{"b": 3}'))


def test_tool_schema_format():
    @tool
    def hello(name: str = Field(description="名字")) -> str:
        return f"hello {name}"

    schema = hello.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "hello"
