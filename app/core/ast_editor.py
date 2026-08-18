"""AST 级代码编辑:基于 Python 内置 ast 模块的结构性代码修改。

与 edit_file 的区别:
- edit_file 是文本替换(基于字符串匹配)
- ast_edit 是基于 AST 的结构性编辑,能正确处理:
  - 多行函数签名
  - 嵌套括号
  - 文档字符串
  - 装饰器
  - 泛型类型注解

支持的语言: Python (通过 ast 模块)
扩展其他语言可替换为 tree-sitter 或其他 AST 解析器。
"""
from __future__ import annotations

import ast
import textwrap
from typing import Optional


class ASTEditOp:
    """单个 AST 编辑操作。"""
    def __init__(self, op_type: str, target: str, **kwargs):
        self.op_type = op_type  # "replace_function", "add_function", "remove_function"
        self.target = target
        self.kwargs = kwargs


def _find_function_node(tree: ast.Module, name: str) -> Optional[ast.FunctionDef]:
    """在 AST 中查找指定名称的函数节点。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _find_class_node(tree: ast.Module, name: str) -> Optional[ast.ClassDef]:
    """在 AST 中查找指定名称的类节点。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _get_source_segment(source: list[str], node: ast.AST) -> tuple[int, int]:
    """获取 AST 节点在源代码中的行范围。"""
    return (node.lineno, node.end_lineno or node.lineno)


def _apply_replace_function(
    source_lines: list[str],
    name: str,
    new_source: str,
) -> list[str]:
    """替换函数定义。"""
    # 重新解析以获取新的 AST
    try:
        new_tree = ast.parse(new_source)
    except SyntaxError:
        return source_lines  # 新代码有语法错误，保持不变

    # 找到原函数的行范围
    orig_tree = ast.parse("".join(source_lines))
    func_node = _find_function_node(orig_tree, name)
    if func_node is None:
        return source_lines

    start_line = func_node.lineno - 1
    end_line = func_node.end_lineno or func_node.lineno

    # 处理装饰器
    if func_node.decorator_list:
        decorator_node = func_node.decorator_list[0]
        start_line = decorator_node.lineno - 1

    # 替换
    new_lines = new_source.splitlines(keepends=True)
    result = source_lines[:start_line] + new_lines + source_lines[end_line:]

    # 清理多余的换行
    while result and result[-1].strip() == "" and len(result) > 1:
        result.pop()

    return result


def _apply_add_function(
    source_lines: list[str],
    name: str,
    new_source: str,
    insert_after: Optional[str] = None,
) -> list[str]:
    """在指定位置后添加新函数。"""
    target_line = len(source_lines)  # 默认插入末尾

    if insert_after:
        # 找到目标函数所在行
        tree = ast.parse("".join(source_lines))
        target_node = _find_function_node(tree, insert_after)
        if target_node:
            target_line = target_node.end_lineno or target_node.lineno
        else:
            # 尝试找类方法
            target_node = None
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == insert_after:
                    target_node = node
                    break
            if target_node:
                target_line = target_node.end_lineno or target_node.lineno

    new_lines = new_source.splitlines(keepends=True)
    # 确保有换行
    if target_line < len(source_lines) and source_lines[target_line].strip():
        result = source_lines[:target_line] + ["\n"] + new_lines + source_lines[target_line:]
    else:
        result = source_lines[:target_line] + new_lines + source_lines[target_line:]

    return result


def ast_edit(
    source_code: str,
    operations: list[dict],
    filepath: str = "",
) -> dict:
    """执行 AST 级代码编辑。

    Args:
        source_code: 原始源代码
        operations: 编辑操作列表，每项格式:
            {
                "op": "replace_function" | "add_function" | "remove_function",
                "name": "函数名",
                "source": "新源代码(仅 add/replace 时需要)",
                "insert_after": "插入到哪个函数之后(可选)"
            }
        filepath: 文件路径(用于错误报告)

    Returns:
        {"ok": bool, "new_source": str, "operations_applied": int, "errors": list}
    """
    errors = []
    applied = 0
    lines = source_code.splitlines(keepends=True)

    for i, op_def in enumerate(operations):
        op_type = op_def.get("op", "")
        name = op_def.get("name", "")

        if op_type == "replace_function":
            new_source = op_def.get("source", "")
            if not new_source:
                errors.append(f"操作 {i+1}: replace_function 需要 source 字段")
                continue
            try:
                lines = _apply_replace_function(lines, name, new_source)
                applied += 1
            except Exception as e:
                errors.append(f"操作 {i+1} 替换函数 {name} 失败: {e}")

        elif op_type == "add_function":
            new_source = op_def.get("source", "")
            insert_after = op_def.get("insert_after")
            if not new_source:
                errors.append(f"操作 {i+1}: add_function 需要 source 字段")
                continue
            try:
                lines = _apply_add_function(lines, name, new_source, insert_after)
                applied += 1
            except Exception as e:
                errors.append(f"操作 {i+1} 添加函数 {name} 失败: {e}")

        elif op_type == "remove_function":
            tree = ast.parse("".join(lines))
            func_node = _find_function_node(tree, name)
            if func_node:
                start = func_node.lineno - 1
                end = func_node.end_lineno or func_node.lineno
                # 也删除装饰器
                if func_node.decorator_list:
                    start = func_node.decorator_list[0].lineno - 1
                lines = lines[:start] + lines[end:]
                applied += 1
            else:
                errors.append(f"操作 {i+1}: 未找到函数 {name}")

        else:
            errors.append(f"操作 {i+1}: 未知操作类型 {op_type}")

    new_source = "".join(lines)

    # 验证新代码语法
    try:
        ast.parse(new_source)
        syntax_ok = True
    except SyntaxError as e:
        syntax_ok = False
        errors.append(f"生成的代码有语法错误: {e}")

    return {
        "ok": len(errors) == 0 and applied > 0,
        "new_source": new_source,
        "operations_applied": applied,
        "operations_total": len(operations),
        "errors": errors,
        "syntax_valid": syntax_ok,
    }


def extract_function_source(source: str, func_name: str) -> Optional[str]:
    """从源代码中提取指定函数的完整源码。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    func_node = _find_function_node(tree, func_name)
    if not func_node:
        return None

    lines = source.splitlines(keepends=True)
    start = func_node.lineno - 1
    end = func_node.end_lineno or func_node.lineno

    # 包含装饰器
    if func_node.decorator_list:
        start = func_node.decorator_list[0].lineno - 1

    return "".join(lines[start:end])


def find_callers(source: str, target_func: str) -> list[dict]:
    """查找调用指定函数的所有位置。"""
    callers = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return callers

    # 找到目标函数定义
    target_node = _find_function_node(tree, target_func)
    if not target_node:
        return callers

    target_start = target_node.lineno
    target_end = target_node.end_lineno or target_node.lineno

    # 搜索所有 Name/Attribute 节点，检查是否是目标函数
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call,)):
            if isinstance(node.func, ast.Name) and node.func.id == target_func:
                callers.append({
                    "line": node.lineno,
                    "col": node.col_offset,
                    "type": "direct_call",
                })
            elif isinstance(node.func, ast.Attribute) and node.func.attr == target_func:
                callers.append({
                    "line": node.lineno,
                    "col": node.col_offset,
                    "type": "method_call",
                })

    return callers
