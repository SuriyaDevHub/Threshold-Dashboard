"""Business-friendly rule explanation (spec §44) — generated from the
canonical model itself, not from the original free text, so it always
reflects what the rule actually does after edits."""
from __future__ import annotations

from typing import List

from app.modules.rule_designer import reference_store
from app.modules.rule_designer.models import Condition, ConditionGroup, NodeType, Rule, ValueRef

_OP_TEXT = {
    "eq": "is", "ne": "is not", "gt": "is greater than", "lt": "is less than",
    "gte": "is at least", "lte": "is at most", "between": "is between",
    "not_between": "is not between", "contains": "contains", "not_contains": "does not contain",
    "starts_with": "starts with", "ends_with": "ends with", "in": "is one of",
    "not_in": "is not one of", "regex": "matches the pattern", "is_null": "is missing",
    "is_not_null": "is present", "before": "is before", "after": "is after", "on": "is on",
}


def _value_text(v: ValueRef) -> str:
    if v is None:
        return ""
    if v.type == "static":
        return str(v.value)
    if v.type == "lookup":
        return f"the looked-up '{v.name}'"
    if v.type == "derived":
        return f"the calculated '{v.name}'"
    return f"'{v.name}'"


def _condition_text(node) -> str:
    if isinstance(node, Condition) or getattr(node, "kind", None) == "condition":
        if not isinstance(node, Condition):
            node = Condition.model_validate(node)
        op_text = _OP_TEXT.get(node.operator.value, node.operator.value)
        if node.operator.value in ("between", "not_between"):
            return f"{node.field} {op_text} {_value_text(node.value)} and {_value_text(node.value2)}"
        if node.operator.value in ("in", "not_in"):
            vals = ", ".join(_value_text(v) for v in (node.values or []))
            return f"{node.field} {op_text} [{vals}]"
        if node.operator.value in ("is_null", "is_not_null"):
            return f"{node.field} {op_text}"
        return f"{node.field} {op_text} {_value_text(node.value)}"

    if not isinstance(node, ConditionGroup):
        node = ConditionGroup.model_validate(node)
    if not node.children:
        return "(always true)"
    if node.operator == "NOT":
        return f"NOT ({_condition_text(node.children[0])})"
    joiner = " AND " if node.operator == "AND" else " OR "
    return "(" + joiner.join(_condition_text(c) for c in node.children) + ")"


def generate_explanation(rule: Rule) -> str:
    lines: List[str] = [f"**{rule.name}**", ""]
    if rule.description:
        lines.append(rule.description)
        lines.append("")

    steps: List[str] = []
    step_no = 1
    for node in rule.workflow.nodes:
        if node.type == NodeType.INPUT:
            continue
        if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup:
            added = ", ".join(fm.output_field for fm in node.lookup.fields) or "fields"
            keys = ", ".join(jk.get("source", "") for jk in node.lookup.join_keys) or node.lookup.range_field \
                or node.lookup.date_field or ""
            ref = reference_store.get_file(node.lookup.reference_file_id)
            ref_name = ref.name if ref else node.lookup.reference_file_id
            steps.append(
                f"{step_no}. Look up {keys} in the '{ref_name}' reference data "
                f"and add {added} to the record "
                f"({node.lookup.join_type.value} join; if no match, {node.lookup.missing_strategy.value.replace('_', ' ')})."
            )
            step_no += 1
        elif node.type == NodeType.CALCULATE and node.calculate:
            steps.append(f"{step_no}. Calculate {node.calculate.output_field} = {node.calculate.expression}.")
            step_no += 1
        elif node.type in (NodeType.FILTER, NodeType.GROUP) and (node.filter or node.condition):
            steps.append(f"{step_no}. Keep only records where {_condition_text(node.filter or node.condition)}.")
            step_no += 1
        elif node.type == NodeType.CONDITION and node.condition:
            steps.append(f"{step_no}. Identify records where {_condition_text(node.condition)}.")
            step_no += 1
        elif node.type == NodeType.TRANSFORM and node.transform:
            steps.append(f"{step_no}. Transform {node.transform.get('field')} ({node.transform.get('op')}).")
            step_no += 1
        elif node.type == NodeType.OUTCOME and node.outcomes:
            actions = "; ".join(f"{a.field} = {_value_text(a.value)}" for a in node.outcomes)
            steps.append(f"{step_no}. Classify the resulting trades: {actions}.")
            step_no += 1

    if steps:
        lines.append("This rule:")
        lines.extend(steps)
    else:
        lines.append("This rule has no steps configured yet.")

    lines.append("")
    lines.append(f"Priority {rule.priority}, conflict handling: {rule.conflict_handling.value.replace('_', ' ')}.")
    return "\n".join(lines)
