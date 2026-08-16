"""代码沙箱测试。"""
import pytest

from app.tools.code_sandbox import (
    docker_available,
    run_code_sandboxed,
    run_in_subprocess,
)


def test_run_simple_code():
    result = run_in_subprocess("print('hello sandbox')", timeout=10)
    assert result.success
    assert "hello sandbox" in result.output


def test_run_code_math():
    result = run_in_subprocess("print(6 * 7)", timeout=10)
    assert result.success
    assert "42" in result.output


def test_run_code_error_captured():
    result = run_in_subprocess("raise ValueError('boom')", timeout=10)
    assert not result.success
    assert "boom" in result.output


def test_run_code_timeout():
    result = run_in_subprocess("import time; time.sleep(5)", timeout=1)
    assert not result.success
    assert "超时" in result.output


def test_run_code_business_module_isolated():
    # 沙箱内不应能导入业务代码
    result = run_in_subprocess("import app", timeout=10)
    assert not result.success


def test_unified_entry_subprocess():
    result = run_code_sandboxed("print('ok')", timeout=10, use_docker=False)
    assert result.success
    assert result.mode == "subprocess"


def test_tool_registered():
    from app.tools.registry import get_registry

    registry = get_registry()
    t = registry.get("run_code")
    assert t is not None
    assert "code" in t.json_schema["properties"]
    assert "timeout" in t.json_schema["properties"]
