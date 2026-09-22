"""Structured calculation-formula engine for CALCULATE nodes.

Replaces free-text arithmetic expressions (formerly parsed by
expr_engine.SafeExpr) with a small operand tree the UI builds entirely by
selecting fields, constants and operations — never typed, mirroring how
LOOKUP join keys and field mappings are built from dropdowns rather than
strings. A formula is a plain nested dict (matching the loose
`Dict[str, Any]` shape already used for a transform config elsewhere in
this module), one of:

  {"kind": "field", "field": "<column name>"}
  {"kind": "constant", "value": <number>}
  {"kind": "operation", "op": "<op>", "operands": [<formula>, ...], "precision": <int, round only>}

`op` is one of: add, subtract, multiply, divide, modulus, min, max
(n-ary — 2+ operands) or abs, round, sqrt (unary — exactly 1 operand).
A DeriveSpec's `formula` is always an "operation" node at the root; a
bare field/constant with no operation isn't a calculation, so the UI
never needs to represent one.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

UNARY_OPS = {"abs", "round", "sqrt"}
N_ARY_OPS = {"add", "subtract", "multiply", "divide", "modulus", "min", "max"}
ALL_OPS = UNARY_OPS | N_ARY_OPS


class CalcError(ValueError):
    pass


def _to_number(v: Any) -> float:
    if v is None:
        raise CalcError("null value in formula")
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        raise CalcError(f"cannot coerce {v!r} to a number")


def apply_calc_op(op: str, values: List[float], precision: int = 2) -> float:
    if op == "add":
        return sum(values)
    if op == "subtract":
        if not values:
            raise CalcError("subtract requires at least one operand")
        result = values[0]
        for v in values[1:]:
            result -= v
        return result
    if op == "multiply":
        result = 1.0
        for v in values:
            result *= v
        return result
    if op == "divide":
        if not values:
            raise CalcError("divide requires at least one operand")
        result = values[0]
        for v in values[1:]:
            if v == 0:
                raise CalcError("division by zero")
            result /= v
        return result
    if op == "modulus":
        if not values:
            raise CalcError("modulus requires at least one operand")
        result = values[0]
        for v in values[1:]:
            if v == 0:
                raise CalcError("division by zero")
            result %= v
        return result
    if op == "min":
        if not values:
            raise CalcError("min requires at least one operand")
        return min(values)
    if op == "max":
        if not values:
            raise CalcError("max requires at least one operand")
        return max(values)
    if op == "abs":
        if not values:
            raise CalcError("abs requires exactly one operand")
        return abs(values[0])
    if op == "round":
        if not values:
            raise CalcError("round requires exactly one operand")
        return round(values[0], precision)
    if op == "sqrt":
        if not values:
            raise CalcError("sqrt requires exactly one operand")
        if values[0] < 0:
            raise CalcError("sqrt of a negative number")
        return values[0] ** 0.5
    raise CalcError(f"unknown operation '{op}'")


def evaluate_formula(operand: Dict[str, Any], record: Dict[str, Any]) -> float:
    """Recursively evaluate an operand tree against a record. Raises
    CalcError on anything unusable, never a bare KeyError/TypeError, so
    callers can catch one exception type the way workflow_engine already
    does for every other node kind."""
    if not operand:
        raise CalcError("formula is empty")
    kind = operand.get("kind")
    if kind == "field":
        field = operand.get("field")
        if not field:
            raise CalcError("field operand is missing a field name")
        if field not in record:
            raise CalcError(f"field '{field}' not available at this stage")
        return _to_number(record.get(field))
    if kind == "constant":
        return _to_number(operand.get("value"))
    if kind == "operation":
        op = operand.get("op")
        if op not in ALL_OPS:
            raise CalcError(f"unknown operation '{op}'")
        operands = operand.get("operands") or []
        values = [evaluate_formula(o, record) for o in operands]
        precision_raw = operand.get("precision")
        precision = int(precision_raw) if precision_raw not in (None, "") else 2
        return apply_calc_op(op, values, precision)
    raise CalcError(f"unknown operand kind '{kind}'")


def fields_referenced(operand: Dict[str, Any]) -> Set[str]:
    """Every field name this formula reads, recursively — used to
    pre-validate availability the same way expr_engine's
    SafeExpr.fields_referenced did for free-text expressions."""
    if not operand:
        return set()
    kind = operand.get("kind")
    if kind == "field":
        f = operand.get("field")
        return {f} if f else set()
    if kind == "operation":
        out: Set[str] = set()
        for o in operand.get("operands") or []:
            out |= fields_referenced(o)
        return out
    return set()


_OP_SYMBOL = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/", "modulus": "%"}


def describe_formula(operand: Dict[str, Any]) -> str:
    """Human-readable rendering for the dry-run trail detail, the
    free-text explanation and the workflow canvas — e.g.
    '(abs((booked_price - reference_price)) / reference_price) * 100'."""
    if not operand:
        return "?"
    kind = operand.get("kind")
    if kind == "field":
        return str(operand.get("field") or "?")
    if kind == "constant":
        return str(operand.get("value"))
    if kind == "operation":
        op = operand.get("op")
        parts = [describe_formula(o) for o in (operand.get("operands") or [])]
        if op in _OP_SYMBOL:
            return "(" + f" {_OP_SYMBOL[op]} ".join(parts) + ")"
        if op == "round":
            return f"round({parts[0] if parts else '?'}, {operand.get('precision', 2)})"
        return f"{op}({', '.join(parts)})"
    return "?"


def validate_formula(operand: Dict[str, Any]) -> List[str]:
    """Structural checks independent of any record/dataset — arity,
    unknown kinds/ops, missing field/value — the same role
    ConditionBuilder's arity checks play for conditions. Returns a list of
    error strings; empty means structurally sound (a field-availability
    check against the dataset schema is layered on top by the caller)."""
    errors: List[str] = []
    if not operand:
        return ["formula is empty"]
    kind = operand.get("kind")
    if kind == "field":
        if not operand.get("field"):
            errors.append("a field operand has no field selected")
    elif kind == "constant":
        if operand.get("value") in (None, ""):
            errors.append("a constant operand has no value")
    elif kind == "operation":
        op = operand.get("op")
        if op not in ALL_OPS:
            errors.append(f"unknown operation '{op}'")
        operands = operand.get("operands") or []
        if op in UNARY_OPS and len(operands) != 1:
            errors.append(f"'{op}' requires exactly one operand")
        elif op in N_ARY_OPS and len(operands) < 2:
            errors.append(f"'{op}' requires at least two operands")
        for o in operands:
            errors.extend(validate_formula(o))
    else:
        errors.append(f"unknown operand kind '{kind}'")
    return errors
