"""测试编程能力工具:AST 编辑、代码审阅、调试辅助、依赖管理。"""
import os
import uuid

import pytest

from app.config import settings


class TestASTEditor:
    """测试 ast_editor 模块。"""

    def test_replace_function(self):
        from app.core.ast_editor import ast_edit

        source = '''\
def hello():
    print("hello")


def world():
    print("world")
'''
        result = ast_edit(
            source,
            [{"op": "replace_function", "name": "hello", "source": 'def hello():\n    return "replaced"\n'}],
        )
        assert result["ok"]
        assert '"replaced"' in result["new_source"]
        assert 'print("hello")' not in result["new_source"]

    def test_add_function(self):
        from app.core.ast_editor import ast_edit

        source = '''\
def existing():
    pass
'''
        result = ast_edit(
            source,
            [{"op": "add_function", "name": "new_func", "source": 'def new_func():\n    return 42\n'}],
        )
        assert result["ok"]
        assert "def new_func" in result["new_source"]

    def test_remove_function(self):
        from app.core.ast_editor import ast_edit

        source = '''\
def to_remove():
    pass


def keep():
    pass
'''
        result = ast_edit(
            source,
            [{"op": "remove_function", "name": "to_remove"}],
        )
        assert result["ok"]
        assert "def to_remove" not in result["new_source"]
        assert "def keep" in result["new_source"]

    def test_extract_function_source(self):
        from app.core.ast_editor import extract_function_source

        source = '''\
def my_func(x, y):
    return x + y


class MyClass:
    def method(self):
        pass
'''
        func_src = extract_function_source(source, "my_func")
        assert func_src is not None
        assert "return x + y" in func_src

    def test_syntax_error_input(self):
        from app.core.ast_editor import ast_edit

        source = "def broken(\n"
        result = ast_edit(source, [])
        assert result["operations_applied"] == 0


class TestCodeReview:
    """测试代码审阅工具。"""

    def test_review_detects_hardcoded_password(self):
        from app.tools.code_review_tool import code_review

        source = '''\
password = "secret123"
db_password = 'admin'
'''
        result = code_review.func(source_code=source)
        assert result["critical"] > 0
        assert any("硬编码" in i["message"] for i in result["issues"])

    def test_review_detects_todo_fixme(self):
        from app.tools.code_review_tool import code_review

        source = '''\
# TODO: fix this later
x = 1  # FIXME: temporary
'''
        result = code_review.func(source_code=source)
        assert result["warnings"] > 0

    def test_review_empty_source(self):
        from app.tools.code_review_tool import code_review

        result = code_review.func(source_code="")
        assert result["total_issues"] == 0

    def test_review_long_lines(self):
        from app.tools.code_review_tool import code_review

        source = "x = " + "a" * 200 + "\n"
        result = code_review.func(source_code=source)
        assert result["style"] > 0

    def test_review_no_issues(self):
        from app.tools.code_review_tool import code_review

        source = '''\
def hello():
    return "world"
'''
        result = code_review.func(source_code=source)
        assert result["total_issues"] == 0


class TestDebugAssist:
    """测试调试辅助工具。"""

    def test_analyze_typeerror(self):
        from app.tools.debug_assist_tool import analyze_traceback_tool

        tb = '''\
Traceback (most recent call last):
  File "test.py", line 5, in <module>
    obj.method()
AttributeError: 'NoneType' object has no attribute 'method'
'''
        result = analyze_traceback_tool.func(traceback_str=tb)
        assert result["error_type"] == "AttributeError"
        assert len(result["suggestions"]) > 0

    def test_analyze_keyerror(self):
        from app.tools.debug_assist_tool import analyze_traceback_tool

        tb = '''\
Traceback (most recent call last):
  File "test.py", line 3, in <module>
    val = d["missing"]
KeyError: 'missing'
'''
        result = analyze_traceback_tool.func(traceback_str=tb)
        assert result["error_type"] == "KeyError"

    def test_find_mutable_default(self):
        from app.tools.debug_assist_tool import find_potential_bugs_tool

        source = '''\
def add_item(item, list=[]):
    list.append(item)
    return list
'''
        issues = find_potential_bugs_tool.func(source_code=source)
        assert any(i["type"] == "mutable_default" for i in issues)

    def test_find_bare_except(self):
        from app.tools.debug_assist_tool import find_potential_bugs_tool

        source = '''\
try:
    do_something()
except:
    pass
'''
        issues = find_potential_bugs_tool.func(source_code=source)
        assert any(i["type"] == "bare_except" for i in issues)

    def test_find_no_bugs_clean_code(self):
        from app.tools.debug_assist_tool import find_potential_bugs_tool

        source = '''\
def hello():
    return "world"
'''
        issues = find_potential_bugs_tool.func(source_code=source)
        assert len(issues) == 0


class TestDependencyTool:
    """测试依赖管理工具。"""

    def test_list_packages_empty_dir(self, tmp_path):
        from app.tools.dependency_tool import list_packages

        result = list_packages.func(path=str(tmp_path))
        assert result is not None

    def test_install_package_empty_string(self):
        from app.tools.dependency_tool import install_package

        result = install_package.func(packages="")
        assert result["ok"] is False
        assert "包名不能为空" in result.get("error", "")


class TestAStarSymbols:
    """测试 AST 符号提取增强版。"""

    def test_python_multiline_function(self):
        from app.core.ast_symbols import extract_symbols

        source = '''\
def my_function(
    arg1,
    arg2,
):
    pass
'''
        symbols = extract_symbols(source, ".py")
        assert any(s["name"] == "my_function" for s in symbols)

    def test_skip_comments(self):
        from app.core.ast_symbols import extract_symbols

        source = '''\
# def fake_function():
    pass
'''
        symbols = extract_symbols(source, ".py")
        assert not any("fake_function" in s["name"] for s in symbols)

    def test_typescript_interface(self):
        from app.core.ast_symbols import extract_symbols

        source = '''\
export interface User {
    name: string;
}
'''
        symbols = extract_symbols(source, ".ts")
        assert any(s["kind"] == "interface" for s in symbols)
