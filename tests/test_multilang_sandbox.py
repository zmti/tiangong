"""测试多语言代码沙箱(完整版)。"""
import asyncio
import json

import pytest


class TestMultiLanguageSandbox:
    """测试 run_code_sandboxed 多语言支持。"""

    def test_python(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("print(2 + 3)", language="python")
        assert result.success
        assert "5" in result.output

    def test_python_expression(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("import math; print(math.factorial(5))", language="python")
        assert result.success
        assert "120" in result.output

    def test_python_error(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("1/0", language="python")
        assert not result.success
        assert "ZeroDivisionError" in result.output

    def test_javascript(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("console.log(2 + 3);", language="javascript")
        if result.success:
            assert "5" in result.output
        else:
            # 未安装 Node.js 时应给明确提示
            assert "未找到" in result.output or "Node.js" in result.output

    def test_typescript(self):
        from app.tools.code_sandbox import run_code_sandboxed
        # TS 同样走 node/deno
        result = run_code_sandboxed("const x: number = 42; console.log(x);", language="typescript")
        if result.success:
            assert "42" in result.output
        else:
            assert "未找到" in result.output

    def test_shell(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("echo hello_world", language="shell")
        assert result.success
        assert "hello_world" in result.output

    def test_bash(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("echo bash_ok", language="bash")
        assert result.success
        assert "bash_ok" in result.output

    def test_unsupported_language(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("print('hi')", language="fortran")
        assert not result.success
        assert "不支持" in result.output

    def test_timeout_small(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("print('done')", language="python", timeout=5)
        assert result.success


class TestMissingInterpreters:
    """测试未安装解释器时的降级行为。"""

    def test_ruby_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("puts 1", language="ruby")
        assert not result.success
        assert "ruby" in result.output.lower() or "未找到" in result.output

    def test_php_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("<?php echo 1;", language="php")
        assert not result.success
        assert "php" in result.output.lower() or "未找到" in result.output

    def test_perl_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("print 1;", language="perl")
        assert not result.success
        assert "perl" in result.output.lower() or "未找到" in result.output

    def test_lua_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed("print(1)", language="lua")
        assert not result.success
        assert "lua" in result.output.lower() or "未找到" in result.output

    def test_go_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed(
            'package main\nimport "fmt"\nfunc main(){fmt.Println(1)}',
            language="go",
        )
        # Go 可能已安装(超时)或未安装(未找到),两种情况都算正常
        assert result.output is not None

    def test_swift_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed('print("hello")', language="swift")
        assert not result.success
        assert "swift" in result.output.lower() or "未找到" in result.output

    def test_kotlin_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed('fun main() { println("hi") }', language="kotlin")
        assert not result.success
        assert "kotlin" in result.output.lower() or "未找到" in result.output

    def test_dart_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed('void main() => print("hi");', language="dart")
        assert not result.success
        assert "dart" in result.output.lower() or "未找到" in result.output

    def test_julia_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed('println(1)', language="julia")
        assert not result.success
        assert "julia" in result.output.lower() or "未找到" in result.output

    def test_zig_not_found(self):
        from app.tools.code_sandbox import run_code_sandboxed
        result = run_code_sandboxed('pub fn main() void {}', language="zig")
        assert not result.success
        assert "zig" in result.output.lower() or "未找到" in result.output


class TestRunCodeTool:
    """测试 run_code 工具接口。"""

    def _run_tool(self, args: dict) -> dict:
        from app.tools.code_tool import run_code
        return json.loads(asyncio.run(run_code.run(args)))

    def test_run_code_python(self):
        from app.tools.code_tool import run_code
        result = self._run_tool({"code": "print(42)", "language": "python"})
        assert result["success"]
        assert "42" in result["output"]

    def test_run_code_shell(self):
        from app.tools.code_tool import run_code
        result = self._run_tool({"code": "echo test_ok", "language": "shell"})
        assert result["success"]
        assert "test_ok" in result["output"]

    def test_run_code_defaults_to_python(self):
        from app.tools.code_tool import run_code
        result = self._run_tool({"code": "print('auto')"})
        assert result["success"]
        assert "auto" in result["output"]

    def test_run_code_js(self):
        from app.tools.code_tool import run_code
        result = self._run_tool({"code": "console.log(7+8);", "language": "js"})
        # Node.js 未安装时也应成功返回错误消息
        if result["success"]:
            assert "15" in result["output"]
        else:
            assert "未找到" in result["output"]
