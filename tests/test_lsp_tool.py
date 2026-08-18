"""LSP 工具测试(无实际服务器时验证配置和降级逻辑)。"""
import os
import tempfile

import pytest

from app.tools.lsp_tool import (
    _detect_language,
    _get_lsp_client,
    _resolve_lsp_config,
    lsp_definition,
    lsp_hover,
    lsp_references,
)


def test_detect_language():
    assert _detect_language("/path/to/main.py") == "python"
    assert _detect_language("/path/to/app.ts") == "typescript"
    assert _detect_language("/path/to/index.tsx") == "typescript"
    assert _detect_language("/path/to/script.js") == "typescript"
    assert _detect_language("/path/to/file.jsx") == "typescript"
    assert _detect_language("/path/to/main.go") == "go"
    assert _detect_language("/path/to/main.rs") == "rust"
    assert _detect_language("/path/to/unknown.xyz") is None
    assert _detect_language("/path/to/Main.kt") == "kotlin"
    assert _detect_language("/path/to/App.swift") == "swift"


def test_resolve_lsp_config_uses_defaults():
    # 未配置 AGENT_LSP_SERVERS 时使用默认值
    config = _resolve_lsp_config("python")
    assert config is not None
    assert "command" in config

    config_ts = _resolve_lsp_config("typescript")
    assert config_ts is not None


def test_lsp_hover_no_server_available():
    # 无 LSP 服务器运行时,应返回明确错误而非崩溃
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write("def foo(): pass\n")
        tmp = f.name
    try:
        result = lsp_hover(tmp, line=1)
        assert "error" in result or "hover" in result
    finally:
        os.unlink(tmp)


def test_lsp_references_no_server_available():
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write("x = 1\n")
        tmp = f.name
    try:
        result = lsp_references(tmp, line=1)
        assert "error" in result or "references" in result
    finally:
        os.unlink(tmp)


def test_lsp_definition_no_server_available():
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write("y = 2\n")
        tmp = f.name
    try:
        result = lsp_definition(tmp, line=1)
        assert "error" in result or "definition" in result
    finally:
        os.unlink(tmp)


def test_lsp_hover_unsupported_language():
    result = lsp_hover("/path/to/file.xyz", line=1)
    assert "error" in result
    assert "不支持的语言" in result["error"]
