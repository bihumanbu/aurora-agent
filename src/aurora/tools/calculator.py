"""calculator — 安全四则运算求值。

通过 AST 白名单而非 eval 直接执行，阻断任意代码注入：
    - 只允许数值常量、四则/幂/取模/整除运算符
    - 拒绝函数调用（call）、属性访问（attribute）、导入、名称访问
    - 数学常量 pi/e 通过白名单提供，避免 Name 一律拒绝
"""

from __future__ import annotations

import ast
import math
from typing import Any

from aurora.exceptions import ToolArgumentError

# 允许的安全二进制/一元运算
_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_ALLOWED_UNOPS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}
# 白名单常量（仅少数数学常量，不含任何可调用对象）
_ALLOWED_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "inf": math.inf,
    "nan": math.nan,
}


class UnsafeExpressionError(ValueError):
    """表达式包含不允许的语法（调用/导入/属性访问等）。"""


def evaluate(expression: str) -> float:
    """安全求值算术表达式，返回数值。非法/危险表达式抛 UnsafeExpressionError。"""
    if not isinstance(expression, str) or not expression.strip():
        raise UnsafeExpressionError("表达式不能为空")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise UnsafeExpressionError(f"表达式语法错误: {e}") from e
    value = _eval_node(tree.body)
    if not isinstance(value, (int, float)):
        raise UnsafeExpressionError("表达式结果不是数值")
    return float(value)


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        return node.value
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_BINOPS:
            raise UnsafeExpressionError(f"不允许的运算符: {op_type.__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _ALLOWED_BINOPS[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_UNOPS:
            raise UnsafeExpressionError(f"不允许的一元运算: {op_type.__name__}")
        return _ALLOWED_UNOPS[op_type](_eval_node(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTANTS:
            return _ALLOWED_CONSTANTS[node.id]
        raise UnsafeExpressionError(f"不允许的变量/常数: {node.id}")
    # 其余节点（Call、Attribute、Import、Subscript、List、Dict 等）一律拒绝
    raise UnsafeExpressionError(f"不允许的表达式节点: {type(node).__name__}")


def calculator(expression: str) -> dict[str, Any]:
    """注册到工具表的实现（供 LLM 调用）。危险表达式以异常形式抛出。"""
    try:
        result = evaluate(expression)
    except UnsafeExpressionError as e:
        raise ToolArgumentError(
            str(e),
            details={"expression": expression, "hint": "只支持数值四则运算（+ - * / // % ** 及括号）"},
        ) from e
    return {"expression": expression, "result": result, "success": True}