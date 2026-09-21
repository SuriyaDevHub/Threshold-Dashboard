"""Evaluate the recursive Condition / ConditionGroup tree against a record.

Never raises on missing data — a condition referencing an unavailable field
evaluates to False (and is annotated in the explain trail as such), matching
the "a pre-step miss is a skip, not an error" behavior the domain prototypes
already established. Every leaf evaluation is recorded so dry-run can render
per-record explainability (spec §24): "Deviation = 6.2 > Threshold = 5 -> ✓".
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, List

from app.modules.rule_designer.models import Condition, ConditionGroup, Operator, ValueRef


@dataclass
class ConditionExplain:
    text: str
    result: bool
    field: str = ""
    operator: str = ""
    error: str = ""


@dataclass
class EvalOutcome:
    result: bool
    explain: List[ConditionExplain] = field(default_factory=list)


def _resolve(value: ValueRef, record: dict) -> Any:
    if value is None:
        return None
    if value.type == "static":
        return value.value
    # column | derived | lookup all resolve the same way: a field on the
    # record built up so far by the pipeline.
    return record.get(value.name)


def _num(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("not numeric")
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(",", ""))


def _to_date(v: Any) -> _dt.date:
    if isinstance(v, _dt.date):
        return v
    s = str(v)[:10]
    return _dt.datetime.strptime(s, "%Y-%m-%d").date()


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def eval_condition(cond: Condition, record: dict) -> ConditionExplain:
    actual = record.get(cond.field)
    op = cond.operator
    try:
        if op == Operator.IS_NULL:
            result = actual is None or actual == ""
            return ConditionExplain(f"{cond.field} IS NULL  ({_fmt(actual)})", result, cond.field, op.value)
        if op == Operator.IS_NOT_NULL:
            result = not (actual is None or actual == "")
            return ConditionExplain(f"{cond.field} IS NOT NULL  ({_fmt(actual)})", result, cond.field, op.value)

        if actual is None or actual == "":
            return ConditionExplain(f"{cond.field} is missing -> {op.value} skipped", False,
                                     cond.field, op.value, error="field missing")

        if op in (Operator.IN, Operator.NOT_IN):
            values = [_resolve(v, record) for v in (cond.values or [])]
            hit = actual in values
            result = hit if op == Operator.IN else not hit
            return ConditionExplain(f"{cond.field} ({_fmt(actual)}) {op.value} {values}", result,
                                     cond.field, op.value)

        if op in (Operator.BETWEEN, Operator.NOT_BETWEEN):
            lo = _resolve(cond.value, record)
            hi = _resolve(cond.value2, record)
            try:
                a, l, h = _num(actual), _num(lo), _num(hi)
                inside = l <= a <= h
            except (ValueError, TypeError):
                da, dl, dh = _to_date(actual), _to_date(lo), _to_date(hi)
                inside = dl <= da <= dh
            result = inside if op == Operator.BETWEEN else not inside
            return ConditionExplain(f"{cond.field} ({_fmt(actual)}) BETWEEN {_fmt(lo)} AND {_fmt(hi)}",
                                     result, cond.field, op.value)

        expected = _resolve(cond.value, record)

        if op == Operator.EQ:
            result = str(actual) == str(expected)
        elif op == Operator.NE:
            result = str(actual) != str(expected)
        elif op == Operator.CONTAINS:
            result = str(expected) in str(actual)
        elif op == Operator.NOT_CONTAINS:
            result = str(expected) not in str(actual)
        elif op == Operator.STARTS_WITH:
            result = str(actual).startswith(str(expected))
        elif op == Operator.ENDS_WITH:
            result = str(actual).endswith(str(expected))
        elif op == Operator.REGEX:
            result = re.search(str(expected), str(actual)) is not None
        elif op == Operator.BEFORE:
            result = _to_date(actual) < _to_date(expected)
        elif op == Operator.AFTER:
            result = _to_date(actual) > _to_date(expected)
        elif op == Operator.ON:
            result = _to_date(actual) == _to_date(expected)
        elif op in (Operator.GT, Operator.LT, Operator.GTE, Operator.LTE):
            a, e = _num(actual), _num(expected)
            result = {Operator.GT: a > e, Operator.LT: a < e,
                      Operator.GTE: a >= e, Operator.LTE: a <= e}[op]
        else:
            return ConditionExplain(f"unsupported operator {op.value}", False, cond.field, op.value,
                                     error="unsupported operator")

        symbol = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "=", "ne": "!="}.get(op.value, op.value)
        text = f"{cond.field} ({_fmt(actual)}) {symbol} {_fmt(expected)}"
        return ConditionExplain(text, result, cond.field, op.value)
    except (ValueError, TypeError) as exc:
        return ConditionExplain(f"{cond.field}: could not evaluate ({exc})", False, cond.field, op.value,
                                 error=str(exc))


def eval_tree(node, record: dict) -> EvalOutcome:
    if isinstance(node, Condition) or getattr(node, "kind", None) == "condition":
        if not isinstance(node, Condition):
            node = Condition.model_validate(node)
        ex = eval_condition(node, record)
        return EvalOutcome(ex.result, [ex])

    if not isinstance(node, ConditionGroup):
        node = ConditionGroup.model_validate(node)

    if not node.children:
        return EvalOutcome(True, [])

    if node.operator == "NOT":
        inner = eval_tree(node.children[0], record)
        return EvalOutcome(not inner.result, inner.explain)

    explains: List[ConditionExplain] = []
    results: List[bool] = []
    for child in node.children:
        out = eval_tree(child, record)
        explains.extend(out.explain)
        results.append(out.result)

    result = all(results) if node.operator == "AND" else any(results)
    return EvalOutcome(result, explains)
