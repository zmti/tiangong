"""安全的表达式评估器。

替代 eval()，只允许安全的操作符和函数。
"""
from __future__ import annotations

import ast
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 允许的操作符类型
ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)
ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
ALLOWED_COMPARATORS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
ALLOWED_BOOLOPS = (ast.And, ast.Or)
ALLOWED_BUILTINS = {'len', 'str', 'int', 'float', 'bool', 'abs', 'min', 'max', 'sum', 'round'}


class UnsafeExpressionError(Exception):
    """表达式包含不安全操作。"""
    pass


def safe_eval(expression: str, variables: dict[str, Any] | None = None) -> Any:
    """安全地评估表达式。
    
    Args:
        expression: 表达式字符串
        variables: 变量字典
    
    Returns:
        表达式结果
    
    Raises:
        UnsafeExpressionError: 表达式包含不允许的操作
        ValueError: 表达式语法错误
    """
    if not expression or not expression.strip():
        return True
    
    variables = variables or {}
    
    try:
        tree = ast.parse(expression, mode='eval')
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e}")
    
    # 验证 AST
    _validate_ast(tree.body)
    
    # 评估
    return _eval_node(tree.body, variables)


def _validate_ast(node: ast.AST):
    """验证 AST 节点是否安全。"""
    for child in ast.walk(node):
        # 检查二元操作
        if isinstance(child, ast.BinOp):
            if not isinstance(child.op, ALLOWED_BINOPS):
                raise UnsafeExpressionError(f"不允许的操作符: {type(child.op).__name__}")
        # 检查一元操作
        elif isinstance(child, ast.UnaryOp):
            if not isinstance(child.op, ALLOWED_UNARYOPS):
                raise UnsafeExpressionError(f"不允许的一元操作符: {type(child.op).__name__}")
        # 检查比较操作
        elif isinstance(child, ast.Compare):
            for op in child.ops:
                if not isinstance(op, ALLOWED_COMPARATORS):
                    raise UnsafeExpressionError(f"不允许的比较操作符: {type(op).__name__}")
        # 检查布尔操作
        elif isinstance(child, ast.BoolOp):
            if not isinstance(child.op, ALLOWED_BOOLOPS):
                raise UnsafeExpressionError(f"不允许的布尔操作符: {type(child.op).__name__}")
        # 检查函数调用
        elif isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                if child.func.id not in ALLOWED_BUILTINS:
                    raise UnsafeExpressionError(f"不允许的函数: {child.func.id}")
            else:
                raise UnsafeExpressionError("不允许的属性访问")
        # 检查属性访问 — 允许从变量取值(如 result.success / result.output.field)
        elif isinstance(child, ast.Attribute):
            # 允许链式属性访问(obj.attr.attr...),其根必须是已解析的变量名 Name
            root = child
            while isinstance(root, ast.Attribute):
                root = root.value
            if not isinstance(root, ast.Name):
                raise UnsafeExpressionError("不允许的属性访问")
            # 在 _eval_node 中统一处理，此处仅通过
        # 检查子脚本
        elif isinstance(child, ast.Subscript):
            raise UnsafeExpressionError("不允许的下标访问")
        # 检查 lambda
        elif isinstance(child, ast.Lambda):
            raise UnsafeExpressionError("不允许的 lambda")
        # 检查推导式
        elif isinstance(child, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            raise UnsafeExpressionError("不允许的推导式")


def _eval_node(node: ast.AST, variables: dict[str, Any]) -> Any:
    """评估 AST 节点。"""
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        raise ValueError(f"未定义的变量: {node.id}")
    elif isinstance(node, ast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        return _apply_binop(left, node.op, right)
    elif isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, variables)
        return _apply_unaryop(node.op, operand)
    elif isinstance(node, ast.Compare):
        left = _eval_node(node.left, variables)
        results = []
        for op, right in zip(node.ops, node.comparators):
            right_val = _eval_node(right, variables)
            results.append(_apply_compareop(left, op, right_val))
            left = right_val
        return all(results)
    elif isinstance(node, ast.BoolOp):
        values = [_eval_node(v, variables) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        else:
            return any(values)
    elif isinstance(node, ast.Call):
        func_name = node.func.id
        args = [_eval_node(a, variables) for a in node.args]
        return _call_builtin(func_name, args)
    elif isinstance(node, ast.Attribute):
        # 属性访问: obj.attr / obj.attr.sub(链式,根必须是已定义变量,由 _validate_ast 保证)
        obj = _eval_node(node.value, variables)
        # 优先使用 getattr(支持对象属性)
        if hasattr(obj, node.attr):
            return getattr(obj, node.attr)
        # 回退到 dict 式访问 (支持 JSON 对象)
        if isinstance(obj, dict) and node.attr in obj:
            return obj[node.attr]
        raise ValueError(f"对象无属性/键: {node.attr}")
    else:
        raise UnsafeExpressionError(f"不允许的表达式类型: {type(node).__name__}")


def _apply_binop(left: Any, op: ast.AST, right: Any) -> Any:
    """应用二元操作符。"""
    if isinstance(op, ast.Add):
        return left + right
    elif isinstance(op, ast.Sub):
        return left - right
    elif isinstance(op, ast.Mult):
        return left * right
    elif isinstance(op, ast.Div):
        return left / right if right != 0 else float('inf')
    elif isinstance(op, ast.Mod):
        return left % right if right != 0 else 0
    elif isinstance(op, ast.Pow):
        return left ** right
    else:
        raise ValueError(f"不支持的操作符: {type(op).__name__}")


def _apply_unaryop(op: ast.AST, operand: Any) -> Any:
    """应用一元操作符。"""
    if isinstance(op, ast.UAdd):
        return +operand
    elif isinstance(op, ast.USub):
        return -operand
    else:
        raise ValueError(f"不支持的一元操作符: {type(op).__name__}")


def _apply_compareop(left: Any, op: ast.AST, right: Any) -> bool:
    """应用比较操作符。"""
    if isinstance(op, ast.Eq):
        return left == right
    elif isinstance(op, ast.NotEq):
        return left != right
    elif isinstance(op, ast.Lt):
        return left < right
    elif isinstance(op, ast.LtE):
        return left <= right
    elif isinstance(op, ast.Gt):
        return left > right
    elif isinstance(op, ast.GtE):
        return left >= right
    else:
        raise ValueError(f"不支持的比较操作符: {type(op).__name__}")


def _call_builtin(func_name: str, args: list) -> Any:
    """调用内置函数。"""
    if func_name == 'len':
        return len(args[0])
    elif func_name == 'str':
        return str(args[0])
    elif func_name == 'int':
        return int(args[0])
    elif func_name == 'float':
        return float(args[0])
    elif func_name == 'bool':
        return bool(args[0])
    elif func_name == 'abs':
        return abs(args[0])
    elif func_name == 'min':
        return min(args)
    elif func_name == 'max':
        return max(args)
    elif func_name == 'sum':
        return sum(args[0])
    elif func_name == 'round':
        return round(args[0], args[1] if len(args) > 1 else 0)
    else:
        raise ValueError(f"不支持的内置函数: {func_name}")


# 别名
safe_eval_expression = safe_eval


def is_safe_expression(expression: str) -> bool:
    """检查表达式是否安全。"""
    try:
        tree = ast.parse(expression, mode='eval')
        _validate_ast(tree.body)
        return True
    except (UnsafeExpressionError, SyntaxError):
        return False
