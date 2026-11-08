"""Safe arithmetic evaluation for deterministic calculations.

Only numbers, parentheses and the arithmetic operators are accepted, so a
"calculation" request never reaches the model and never executes Python.
"""

from __future__ import annotations

import ast
import math
import operator

MAX_EXPRESSION_CHARS = 120
MAX_EXPONENT = 12

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalculationError(ValueError):
  """Raised when an expression is empty, unsupported or unsafe."""


def evaluate(expression):
  """Evaluate a numeric expression and return an int or float."""
  text = (expression or "").strip()
  if not text:
    raise CalculationError("empty expression")
  if len(text) > MAX_EXPRESSION_CHARS:
    raise CalculationError("expression is too long")
  try:
    tree = ast.parse(text, mode="eval")
  except SyntaxError as exc:
    raise CalculationError("not a valid arithmetic expression") from exc
  return _evaluate_node(tree.body)


def _evaluate_node(node):
  if isinstance(node, ast.Constant):
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      raise CalculationError("only numbers are allowed")
    if isinstance(value, float):
      if math.isnan(value) or math.isinf(value):
        raise CalculationError("number is out of range")
      if abs(value) > 1e15:
        raise CalculationError("number is out of range")
    return value

  if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
    operand = _evaluate_node(node.operand)
    return _UNARY_OPERATORS[type(node.op)](operand)

  if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
    left = _evaluate_node(node.left)
    right = _evaluate_node(node.right)
    if isinstance(node.op, ast.Pow):
      if abs(right) > MAX_EXPONENT or abs(left) > 1e6:
        raise CalculationError("exponent is too large")
    try:
      result = _BINARY_OPERATORS[type(node.op)](left, right)
    except ZeroDivisionError as exc:
      raise CalculationError("division by zero") from exc
    except OverflowError as exc:
      raise CalculationError("result is out of range") from exc
    if isinstance(result, complex):
      raise CalculationError("result is not a real number")
    if isinstance(result, float) and (
        math.isnan(result) or math.isinf(result) or abs(result) > 1e15
    ):
      raise CalculationError("result is out of range")
    return result

  raise CalculationError("unsupported expression")


def format_number(value):
  """Render a result without trailing zeros or exponent notation."""
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise CalculationError("not a number")
  if isinstance(value, int):
    return str(value)
  if value == int(value) and abs(value) < 1e15:
    return str(int(value))
  return f"{value:.6f}".rstrip("0").rstrip(".")
