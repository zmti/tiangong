"""调试辅助工具:分析错误 traceback，定位根因，提供修复建议。"""
from __future__ import annotations

import ast
import re

from pydantic import Field

from app.tools.registry import tool


def analyze_traceback(traceback_str: str, source_code: str = "") -> dict:
    """分析错误 traceback，返回结构化诊断信息。"""
    result = {
        "error_type": "",
        "error_message": "",
        "traceback_lines": [],
        "root_cause": "",
        "suggestions": [],
    }

    # 解析 traceback
    lines = traceback_str.strip().split("\n")
    result["traceback_lines"] = lines[-10:]  # 只保留最后 10 行

    # 提取错误类型和消息
    if lines:
        last_line = lines[-1]
        match = re.match(r"(\w+Error?): (.+)", last_line)
        if match:
            result["error_type"] = match.group(1)
            result["error_message"] = match.group(2)

    # 分析可能的根因
    result["root_cause"] = _infer_root_cause(result["error_type"], result["error_message"], source_code)
    result["suggestions"] = _generate_suggestions(result["error_type"], result["error_message"])

    return result


def _infer_root_cause(error_type: str, error_message: str, source: str) -> str:
    """根据错误类型和消息推断根因。"""
    if "TypeError" in error_type:
        if "NoneType" in error_message:
            return "代码试图在 None 对象上调用方法或访问属性"
        if "unsupported operand" in error_message:
            return "操作数类型不匹配"
    elif "AttributeError" in error_type:
        return "对象没有指定的属性或方法"
    elif "KeyError" in error_type:
        return "字典键不存在"
    elif "IndexError" in error_type:
        return "列表/序列索引超出范围"
    elif "ValueError" in error_type:
        return "参数值不符合预期"
    elif "FileNotFoundError" in error_type:
        return "文件路径不存在"
    elif "PermissionError" in error_type:
        return "权限不足"
    elif "ModuleNotFoundError" in error_type:
        return "缺少依赖包"
    return ""


def _generate_suggestions(error_type: str, error_message: str) -> list[str]:
    """根据错误类型生成修复建议。"""
    suggestions = []

    if "TypeError" in error_type and "NoneType" in error_message:
        suggestions.append("在使用对象前添加 None 检查: `if obj is not None:`")
        suggestions.append("检查函数返回值是否可能为 None")

    if "AttributeError" in error_type:
        suggestions.append("使用 hasattr() 检查属性是否存在")
        suggestions.append("检查对象类型是否正确")

    if "KeyError" in error_type:
        suggestions.append("使用 dict.get(key, default) 替代直接索引")
        suggestions.append("先检查 key 是否存在: `if key in dict:`")

    if "IndexError" in error_type:
        suggestions.append("检查列表长度后再访问")
        suggestions.append("使用 try-except 捕获越界")

    if "ModuleNotFoundError" in error_type:
        suggestions.append("运行 pip install 安装缺失的包")
        suggestions.append("检查虚拟环境是否正确激活")

    if not suggestions:
        suggestions.append("查看完整 traceback，定位出错行")
        suggestions.append("添加调试日志打印相关变量值")

    return suggestions


def analyze_syntax_errors(source_code: str) -> dict:
    """分析源码中的语法错误。"""
    try:
        ast.parse(source_code)
        return {"has_errors": False, "errors": []}
    except SyntaxError as e:
        return {
            "has_errors": True,
            "errors": [{
                "line": e.lineno,
                "col": e.offset,
                "msg": e.msg,
                "text": e.text,
            }],
        }


def find_potential_bugs(source_code: str) -> list[dict]:
    """静态分析源码，找出潜在 bug。"""
    issues = []

    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return issues

    # 检查常见的反模式
    for node in ast.walk(tree):
        # 可变默认参数
        if isinstance(node, ast.FunctionDef):
            for default in node.args.defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    issues.append({
                        "line": node.lineno,
                        "type": "mutable_default",
                        "message": f"函数 {node.name} 使用了可变默认参数，可能导致意外共享状态",
                    })

            # 裸 except
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler) and child.type is None:
                    issues.append({
                        "line": child.lineno,
                        "type": "bare_except",
                        "message": "使用裸 except 会捕获所有异常，包括 KeyboardInterrupt 和 SystemExit",
                    })

        # 未使用的导入
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in source_code.split(f"import {alias.name}")[1]:
                    issues.append({
                        "line": node.lineno,
                        "type": "unused_import",
                        "message": f"导入 {alias.name} 可能未使用",
                    })

    # 也检查模块级裸 except(不在函数内部的)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            already = any(i["type"] == "bare_except" and i["line"] == node.lineno for i in issues)
            if not already:
                issues.append({
                    "line": node.lineno,
                    "type": "bare_except",
                    "message": "使用裸 except 会捕获所有异常，包括 KeyboardInterrupt 和 SystemExit",
                })

    return issues


@tool(
    "analyze_traceback",
    "分析错误 traceback，定位根因并提供修复建议。适合调试代码错误",
    timeout=30,
)
def analyze_traceback_tool(
    traceback_str: str = Field(description="错误 traceback 文本"),
    source_code: str = Field("", description="相关源代码(可选，帮助定位上下文)"),
) -> dict:
    """分析错误 traceback，返回结构化诊断信息。"""
    result = {
        "error_type": "",
        "error_message": "",
        "traceback_lines": [],
        "root_cause": "",
        "suggestions": [],
    }

    # 解析 traceback
    lines = traceback_str.strip().split("\n")
    result["traceback_lines"] = lines[-10:]  # 只保留最后 10 行

    # 提取错误类型和消息
    if lines:
        last_line = lines[-1]
        match = re.match(r"(\w+Error?): (.+)", last_line)
        if match:
            result["error_type"] = match.group(1)
            result["error_message"] = match.group(2)

    # 分析可能的根因
    result["root_cause"] = _infer_root_cause(result["error_type"], result["error_message"], source_code)
    result["suggestions"] = _generate_suggestions(result["error_type"], result["error_message"])

    return result


@tool(
    "find_potential_bugs",
    "静态分析源码，找出潜在 bug(可变默认参数、裸 except、未使用导入等)",
    timeout=30,
)
def find_potential_bugs_tool(
    source_code: str = Field(description="要分析的源代码"),
) -> list[dict]:
    """静态分析源码，找出潜在 bug。"""
    issues = []

    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return issues

    # 检查常见的反模式
    for node in ast.walk(tree):
        # 可变默认参数
        if isinstance(node, ast.FunctionDef):
            for default in node.args.defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    issues.append({
                        "line": node.lineno,
                        "type": "mutable_default",
                        "message": f"函数 {node.name} 使用了可变默认参数，可能导致意外共享状态",
                    })

            # 裸 except
            for child in ast.walk(node):
                if isinstance(child, ast.ExceptHandler) and child.type is None:
                    issues.append({
                        "line": child.lineno,
                        "type": "bare_except",
                        "message": "使用裸 except 会捕获所有异常，包括 KeyboardInterrupt 和 SystemExit",
                    })

        # 未使用的导入
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in source_code.split(f"import {alias.name}")[1]:
                    issues.append({
                        "line": node.lineno,
                        "type": "unused_import",
                        "message": f"导入 {alias.name} 可能未使用",
                    })

    # 也检查模块级裸 except
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            already = any(i["type"] == "bare_except" and i["line"] == node.lineno for i in issues)
            if not already:
                issues.append({
                    "line": node.lineno,
                    "type": "bare_except",
                    "message": "使用裸 except 会捕获所有异常，包括 KeyboardInterrupt 和 SystemExit",
                })

    return issues
