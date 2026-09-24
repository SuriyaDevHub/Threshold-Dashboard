"""Business-friendly rule explanation (spec §44) — generated from the
canonical model itself, not from the original free text, so it always
reflects what the rule actually does after edits."""
from __future__ import annotations

import string as _string_mod
from typing import List, Optional

from app.modules.rule_designer import calc_ops, reference_store
from app.modules.rule_designer.models import Condition, ConditionGroup, LookupType, NodeType, Rule, ValueRef

_MIGRATION_NOTE_PREFIX = "Migrated from "

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
    if v.type in ("static", "template"):
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
            steps.append(f"{step_no}. Calculate {node.calculate.output_field} = "
                         f"{calc_ops.describe_formula(node.calculate.formula)}.")
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


def _outcome_text(node) -> str:
    parts = [f"{a.field} = {_value_text(a.value)}" for a in (node.outcomes or [])]
    return "; ".join(parts) if parts else "an outcome"


def generate_summary(rule: Rule) -> str:
    """One sentence, for a list/table view — the compact counterpart to
    generate_explanation()'s numbered walkthrough. Prefers rule.description
    when it's an actual authored sentence; a migrate_*.py-generated
    description (always literally "Migrated from ...") is provenance, not a
    business explanation, so those fall through to a summary composed from
    the workflow itself instead.

    Every FILTER/GROUP/CONDITION node's text is included, joined with AND —
    a record only reaches the Outcome by passing ALL of them in sequence,
    so a rule with e.g. a broad region FILTER followed by a more specific
    booking/trade-id CONDITION (a real production pattern: OOS Product
    Code rules narrow by region, then by a bookname/trade-id-prefix match)
    needs every stage represented, not just the first one it hits — an
    earlier version stopped at the first condition node and silently
    dropped the rest, giving a summary that looked complete but wasn't."""
    desc = (rule.description or "").strip()
    if desc and not desc.startswith(_MIGRATION_NOTE_PREFIX):
        return desc

    group_by = None
    cond_texts: List[str] = []
    outcome_text = None
    for node in rule.workflow.nodes:
        if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup \
                and node.lookup.lookup_type == LookupType.SELF_GROUP and group_by is None:
            group_by = node.lookup.group_by_field
        elif node.type in (NodeType.FILTER, NodeType.GROUP, NodeType.CONDITION) \
                and (node.filter or node.condition):
            cond_texts.append(_condition_text(node.filter or node.condition))
        elif node.type == NodeType.OUTCOME and node.outcomes and outcome_text is None:
            outcome_text = _outcome_text(node)

    cond_text = " AND ".join(cond_texts) if cond_texts else None
    if cond_text is None and outcome_text is None:
        return "No logic configured yet."

    prefix = f"Grouped by {group_by}: " if group_by else ""
    if cond_text and outcome_text:
        return f"{prefix}If {cond_text} → {outcome_text}."
    if cond_text:
        return f"{prefix}Applies where {cond_text}."
    return f"{prefix}→ {outcome_text}."


def _has_template_placeholder(text: str) -> bool:
    """True if `text` contains an unresolved `{field}`-style placeholder,
    matching expr_engine.render_template()'s str.format_map() syntax
    exactly (so this agrees with what actually happens at evaluation
    time). string.Formatter().parse() correctly handles escaped `{{`/`}}`
    literal braces, unlike a naive '{' in text check. A malformed format
    string (stray unmatched brace) is treated as a placeholder rather than
    guessed to be a plain string."""
    try:
        return any(field_name is not None for _, field_name, _, _ in _string_mod.Formatter().parse(text))
    except ValueError:
        return True


def extract_reason_code(rule: Rule) -> Optional[str]:
    """The Outcome node's Reason value, for a list/table view — the same
    "REASON CODE" column the legacy OAR Business Rules admin screen showed,
    read from the canonical workflow instead of a separately stored field
    (there isn't one; this IS where a rule's reason code lives).

    A `static` value always counts as fixed. A `template` value counts as
    fixed too, UNLESS its text actually contains a `{field}` placeholder —
    the Outcome tab UI always stores Reason/Commentary as `type: "template"`
    (see NodeConfigPanel.jsx's setReason()/setCommentary()) even when the
    author never typed a placeholder, purely so `{field}` interpolation is
    available if they want it later. Treating only `static` as "fixed"
    meant every UI-authored rule's reason code showed as "(dynamic)" here
    even though the text plainly wasn't — a `lookup`/`derived`/`column`
    Reason is the only case that's genuinely dynamic and shows that way."""
    for node in rule.workflow.nodes:
        if node.type != NodeType.OUTCOME or not node.outcomes:
            continue
        for action in node.outcomes:
            if action.field != "Reason" or action.value is None:
                continue
            if action.value.type == "static":
                return str(action.value.value)
            if action.value.type == "template":
                text = str(action.value.value)
                return None if _has_template_placeholder(text) else text
    return None
