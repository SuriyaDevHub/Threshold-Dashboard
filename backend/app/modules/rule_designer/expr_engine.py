"""Safe restricted-AST expression evaluator for derived/calculated columns.

This is deliberately NOT eval()/exec() over user input. `parse()` walks a
Python `ast.Expression` and rejects anything outside a small whitelist
(numeric/string literals, +-*/%**, comparisons, unary +-, and a short list
of pure functions) before `evaluate()` is ever called against a record. No
name in an expression can resolve to anything other than a field already
present on the record at that point in the pipeline — there is no import,
no attribute access, no call to anything not in ALLOWED_FUNCS.
"""
from __future__ import annotations

import ast
import math
from datetime import date, datetime
from typing import Any, Dict, Set

ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv)
ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)
ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
ALLOWED_BOOLOPS = (ast.And, ast.Or)

ALLOWED_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "len": len,
}


class ExpressionError(ValueError):
    pass


def _to_number(v: Any) -> float:
    if v is None:
        raise ExpressionError("null value in numeric expression")
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        raise ExpressionError(f"cannot coerce {v!r} to a number")


class SafeExpr:
    """A parsed, validated expression. Construction fails fast on anything
    outside the whitelist; `fields_referenced` lets callers pre-validate
    column availability before execution ever happens."""

    def __init__(self, source: str):
        self.source = source
        try:
            self._tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"invalid expression syntax: {exc}") from exc
        self.fields_referenced: Set[str] = set()
        self._validate(self._tree.body)

    def _validate(self, node: ast.AST) -> None:
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, ALLOWED_BINOPS):
                raise ExpressionError(f"operator {type(node.op).__name__} not allowed")
            self._validate(node.left)
            self._validate(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, ALLOWED_UNARYOPS):
                raise ExpressionError(f"unary operator {type(node.op).__name__} not allowed")
            self._validate(node.operand)
        elif isinstance(node, ast.Compare):
            for op in node.ops:
                if not isinstance(op, ALLOWED_CMPOPS):
                    raise ExpressionError(f"comparison {type(op).__name__} not allowed")
            self._validate(node.left)
            for c in node.comparators:
                self._validate(c)
        elif isinstance(node, ast.BoolOp):
            if not isinstance(node.op, ALLOWED_BOOLOPS):
                raise ExpressionError("boolean operator not allowed")
            for v in node.values:
                self._validate(v)
        elif isinstance(node, ast.IfExp):
            self._validate(node.test)
            self._validate(node.body)
            self._validate(node.orelse)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS:
                raise ExpressionError("only whitelisted functions may be called")
            if node.keywords:
                raise ExpressionError("keyword arguments not allowed")
            for a in node.args:
                self._validate(a)
        elif isinstance(node, ast.Name):
            self.fields_referenced.add(node.id)
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float, str, bool)) and node.value is not None:
                raise ExpressionError("unsupported literal type")
        else:
            raise ExpressionError(f"expression element '{type(node).__name__}' is not allowed")

    def evaluate(self, record: Dict[str, Any]) -> Any:
        return self._eval(self._tree.body, record)

    def _eval(self, node: ast.AST, record: Dict[str, Any]) -> Any:
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left, record), self._eval(node.right, record)
            l, r = _to_number(left), _to_number(right)
            if isinstance(node.op, ast.Add):
                return l + r
            if isinstance(node.op, ast.Sub):
                return l - r
            if isinstance(node.op, ast.Mult):
                return l * r
            if isinstance(node.op, ast.Div):
                if r == 0:
                    raise ExpressionError("division by zero")
                return l / r
            if isinstance(node.op, ast.FloorDiv):
                if r == 0:
                    raise ExpressionError("division by zero")
                return l // r
            if isinstance(node.op, ast.Mod):
                if r == 0:
                    raise ExpressionError("division by zero")
                return l % r
            if isinstance(node.op, ast.Pow):
                return l ** r
        elif isinstance(node, ast.UnaryOp):
            v = _to_number(self._eval(node.operand, record))
            return v if isinstance(node.op, ast.UAdd) else -v
        elif isinstance(node, ast.Compare):
            left = self._eval(node.left, record)
            result = True
            for op, comp_node in zip(node.ops, node.comparators):
                right = self._eval(comp_node, record)
                result = result and _compare(op, left, right)
                left = right
            return result
        elif isinstance(node, ast.BoolOp):
            vals = [self._eval(v, record) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        elif isinstance(node, ast.IfExp):
            return (self._eval(node.body, record) if self._eval(node.test, record)
                    else self._eval(node.orelse, record))
        elif isinstance(node, ast.Call):
            fn = ALLOWED_FUNCS[node.func.id]
            args = [self._eval(a, record) for a in node.args]
            return fn(*args)
        elif isinstance(node, ast.Name):
            if node.id not in record:
                raise ExpressionError(f"field '{node.id}' not available at this stage")
            return record[node.id]
        elif isinstance(node, ast.Constant):
            return node.value
        raise ExpressionError(f"cannot evaluate '{type(node).__name__}'")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, (ast.Eq, ast.NotEq)):
        eq = str(left) == str(right)
        try:
            eq = _to_number(left) == _to_number(right)
        except ExpressionError:
            pass
        return eq if isinstance(op, ast.Eq) else not eq
    l, r = _to_number(left), _to_number(right)
    if isinstance(op, ast.Lt):
        return l < r
    if isinstance(op, ast.LtE):
        return l <= r
    if isinstance(op, ast.Gt):
        return l > r
    if isinstance(op, ast.GtE):
        return l >= r
    raise ExpressionError("unsupported comparison")


_PARSE_CACHE: Dict[str, SafeExpr] = {}


def parse(expression: str) -> SafeExpr:
    cached = _PARSE_CACHE.get(expression)
    if cached is None:
        cached = SafeExpr(expression)
        _PARSE_CACHE[expression] = cached
    return cached


def render_template(template: str, record: Dict[str, Any]) -> str:
    """Render `{field}` placeholders in commentary/reason text. Never
    executes anything — pure str.format against the prepared record."""
    class _Default(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    try:
        return template.format_map(_Default(record))
    except (ValueError, IndexError):
        return template
