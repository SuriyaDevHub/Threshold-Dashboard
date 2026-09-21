"""Free-text -> canonical Rule Model (spec §3-4, §42-43): the "AI Builder".

This is a deterministic, keyword/pattern-based interpreter — not a call to
an external LLM (no such credential/infra exists in this deployment). It
is written so an LLM call could be swapped in behind `interpret()` later
without changing anything downstream: the output is the same canonical
`Workflow`/`Condition` model either way, and it goes through the exact same
validation, dry-run, review and approval path (spec §4, §53).

Guardrails enforced here directly (spec §43):
  * Never invents a dataset column — a clause whose field can't be matched
    against the dataset's actual schema is reported as unresolved, not
    guessed.
  * Never invents a lookup field — if a mentioned reference-file column
    (e.g. "threshold") matches more than one candidate across registered
    reference files, every candidate is surfaced for the user to pick
    rather than the parser silently choosing one.
  * Everything it produces is a draft: the caller must show it to the user
    for review/edit before it is validated, dry-run, or published.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.modules.rule_designer.models import (
    Condition, ConditionGroup, FieldType, LookupConfig, LookupFieldMap, LookupType,
    NodeType, Operator, OutcomeAction, ValueRef, Workflow, WorkflowEdge, WorkflowNode,
)

_NUM_WORD = {"thousand": 1_000, "k": 1_000, "million": 1_000_000, "m": 1_000_000,
             "billion": 1_000_000_000, "bn": 1_000_000_000}

_NUM_RE = re.compile(
    r"(-?\d[\d,]*\.?\d*)\s*(thousand|million|billion|bn|k|m)?\s*(%|percent)?", re.IGNORECASE
)

_OP_PATTERNS: List[Tuple[str, Operator]] = [
    (r"is\s+at\s+least|>=|greater\s+than\s+or\s+equal\s+to", Operator.GTE),
    (r"is\s+at\s+most|<=|less\s+than\s+or\s+equal\s+to", Operator.LTE),
    (r"greater\s+than|above|exceeds|more\s+than|over|>", Operator.GT),
    (r"less\s+than|below|under|<", Operator.LT),
    (r"is\s+not|!=|<>|not\s+equal(s)?\s+to|isn't", Operator.NE),
    (r"contains", Operator.CONTAINS),
    (r"does\s+not\s+contain", Operator.NOT_CONTAINS),
    (r"starts\s+with", Operator.STARTS_WITH),
    (r"ends\s+with", Operator.ENDS_WITH),
    (r"is\s+one\s+of|in\s+\(", Operator.IN),
    (r"is\s+not\s+one\s+of|not\s+in\s+\(", Operator.NOT_IN),
    (r"is\s+missing|is\s+null|is\s+blank", Operator.IS_NULL),
    (r"is\s+present|is\s+not\s+null", Operator.IS_NOT_NULL),
    (r"before", Operator.BEFORE),
    (r"after", Operator.AFTER),
    (r"is|equals|equal\s+to|=", Operator.EQ),
]

_OUTCOME_ANCHORS = [
    r"then\s+(?:the\s+system\s+should\s+)?(.+)$",
    r"classify\s+(?:the\s+trade|it|them|the\s+trades)\s+as\s+(.+)$",
    r"mark\s+(?:the\s+trade|it|them)?\s*as\s+(.+)$",
    r"mark\s+them\s+as\s+(.+)$",
    r"flag\s+(?:it|them)?\s*as\s+(.+)$",
]

_LOOKUP_HINT_RE = re.compile(
    r"look\s*up\s+(?:the\s+applicable\s+)?(?P<what>[\w %]+?)\s+using\s+(?P<join>[\w ]+?)\s+from\s+the\s+reference\s+file",
    re.IGNORECASE,
)


@dataclass
class ParseResult:
    workflow: Workflow
    notes: List[str] = field(default_factory=list)
    confidence: float = 1.0
    interpreted_summary: str = ""


def _parse_number(text: str) -> Optional[float]:
    m = _NUM_RE.search(text)
    if not m or not m.group(1):
        return None
    n = float(m.group(1).replace(",", ""))
    mult = m.group(2)
    if mult:
        n *= _NUM_WORD.get(mult.lower(), 1)
    return n


def _best_field_match(mention: str, columns: List[str]) -> Optional[Tuple[str, float]]:
    mention_norm = re.sub(r"[^a-z0-9]", "", mention.lower())
    if not mention_norm:
        return None
    best: Optional[Tuple[str, float]] = None
    for col in columns:
        col_norm = re.sub(r"[^a-z0-9]", "", col.lower())
        if not col_norm:
            continue
        if mention_norm == col_norm:
            score = 1.0
        elif mention_norm in col_norm or col_norm in mention_norm:
            score = min(len(mention_norm), len(col_norm)) / max(len(mention_norm), len(col_norm))
        else:
            continue
        if best is None or score > best[1]:
            best = (col, score)
    return best


def _split_top_level(text: str, sep: str) -> List[str]:
    return re.split(rf"\s+{sep}\s+", text, flags=re.IGNORECASE)


def _extract_outcome(text: str) -> Tuple[str, Optional[str]]:
    for pattern in _OUTCOME_ANCHORS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return text[: m.start()].strip(" ,"), m.group(1).strip(" .")
    return text, None


def _extract_lookup_hint(text: str) -> Tuple[str, Optional[Dict[str, str]]]:
    m = _LOOKUP_HINT_RE.search(text)
    if not m:
        return text, None
    hint = {"what": m.group("what").strip(), "join": m.group("join").strip()}
    cleaned = (text[: m.start()] + " " + text[m.end():]).strip()
    cleaned = re.sub(r"^\s*then\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*,\s*", "", cleaned)
    return cleaned, hint


def _parse_condition_clause(clause: str, columns: List[str], schema: Dict[str, FieldType],
                             derived_fields: List[str], notes: List[str]) -> Optional[Condition]:
    clause = clause.strip()
    clause = re.sub(r"^(where|for|if)\s+", "", clause, flags=re.IGNORECASE)
    clause = re.sub(r"^(the|a|an)\s+", "", clause, flags=re.IGNORECASE)

    for pattern, op in _OP_PATTERNS:
        m = re.search(pattern, clause, re.IGNORECASE)
        if not m:
            continue
        field_text = clause[: m.start()].strip()
        value_text = clause[m.end():].strip()
        field_text = re.sub(r"^(the|is|are)\s+", "", field_text, flags=re.IGNORECASE).strip()

        all_fields = list(columns) + list(derived_fields)
        match = _best_field_match(field_text, all_fields)
        if not match or match[1] < 0.5:
            notes.append(f"Could not confidently match field for clause '{clause}' — skipped, please add manually.")
            return None
        field_name, score = match
        if score < 0.85:
            notes.append(f"Interpreted '{field_text}' as column '{field_name}' ({score:.0%} confidence).")

        if op in (Operator.IS_NULL, Operator.IS_NOT_NULL):
            return Condition(field=field_name, operator=op)

        if "that threshold" in value_text.lower() or "the threshold" in value_text.lower():
            return Condition(field=field_name, operator=op, value=ValueRef(type="lookup", name="Threshold"))

        num = _parse_number(value_text)
        ftype = schema.get(field_name, FieldType.STRING)
        if num is not None and ftype in (FieldType.NUMERIC,):
            if "%" in value_text or "percent" in value_text.lower():
                notes.append(f"'{value_text.strip()}' interpreted as the number {num:g} "
                              f"(assuming {field_name} is stored as a plain number, not a fraction).")
            return Condition(field=field_name, operator=op, value=ValueRef(type="static", value=num))

        str_val = re.sub(r"[.,;]+$", "", value_text).strip().strip("'\"")
        return Condition(field=field_name, operator=op, value=ValueRef(type="static", value=str_val))

    notes.append(f"Could not interpret clause '{clause}' as a condition — skipped, please add manually.")
    return None


def interpret(
    text: str,
    dataset_columns: List[str],
    dataset_schema: Dict[str, FieldType],
    reference_candidates: Optional[Dict[str, List[str]]] = None,  # reference_file_name -> columns
) -> ParseResult:
    notes: List[str] = []
    reference_candidates = reference_candidates or {}
    nodes: List[WorkflowNode] = [WorkflowNode(id="n_input", type=NodeType.INPUT, label="Input dataset",
                                               position={"x": 0, "y": 0})]
    edges: List[WorkflowEdge] = []
    prev_id = "n_input"
    y = 120

    condition_part, outcome_text = _extract_outcome(text)
    condition_part, lookup_hint = _extract_lookup_hint(condition_part)

    derived_fields: List[str] = []
    if lookup_hint:
        what = lookup_hint["what"]
        join_field_match = _best_field_match(lookup_hint["join"], dataset_columns)
        candidates = []
        for ref_name, ref_cols in reference_candidates.items():
            match = _best_field_match(what, ref_cols)
            if match and match[1] >= 0.5:
                candidates.append((ref_name, match[0], match[1]))
        if not join_field_match:
            notes.append(f"Lookup join field '{lookup_hint['join']}' not found in the dataset — "
                          "please configure the lookup manually.")
        elif not candidates:
            notes.append(f"No reference file has a column matching '{what}' — "
                          "please configure the lookup manually or upload the reference file.")
        elif len(candidates) > 1:
            options = ", ".join(f"{name}.{col}" for name, col, _ in candidates)
            notes.append(f"'{what}' matches more than one reference field ({options}) — "
                         "please choose which one this rule should use.")
        else:
            ref_name, ref_col, _ = candidates[0]
            node = WorkflowNode(
                id="n_lookup", type=NodeType.LOOKUP, label=f"Lookup {what.title()} from {ref_name}",
                position={"x": 0, "y": y},
                lookup=LookupConfig(
                    lookup_type=LookupType.EXACT, reference_file_id=ref_name,
                    join_keys=[{"source": join_field_match[0], "reference": join_field_match[0]}],
                    fields=[LookupFieldMap(source_column=ref_col, output_field="Threshold")],
                ),
            )
            nodes.append(node)
            edges.append(WorkflowEdge(source=prev_id, target=node.id))
            prev_id = node.id
            y += 120
            derived_fields.append("Threshold")

    clauses: List[str] = []
    for part in _split_top_level(condition_part, "AND"):
        clauses.extend(_split_top_level(part, "OR"))  # simple: no mixed precedence beyond flat AND/OR
    is_or = bool(re.search(r"\s+OR\s+", condition_part, re.IGNORECASE)) and not \
        re.search(r"\s+AND\s+", condition_part, re.IGNORECASE)

    conditions: List[Condition] = []
    for clause in clauses:
        if not clause.strip():
            continue
        cond = _parse_condition_clause(clause, dataset_columns, dataset_schema, derived_fields, notes)
        if cond:
            conditions.append(cond)

    summary_parts = [c.field + " " + c.operator.value + " " + str(_display_value(c)) for c in conditions]
    joiner = " OR " if is_or else " AND "
    interpreted_summary = joiner.join(summary_parts) if summary_parts else "(no conditions recognized)"

    if conditions:
        group = ConditionGroup(operator="OR" if is_or else "AND", children=conditions)
        cond_node = WorkflowNode(id="n_condition", type=NodeType.CONDITION, label="Business condition",
                                  position={"x": 0, "y": y}, condition=group)
        nodes.append(cond_node)
        edges.append(WorkflowEdge(source=prev_id, target=cond_node.id))
        prev_id = cond_node.id
        y += 120

    outcome_actions = _parse_outcome(outcome_text, notes) if outcome_text else []
    if outcome_text and not outcome_actions:
        notes.append(f"Could not interpret outcome '{outcome_text}' — please set the outcome fields manually.")
    if outcome_actions:
        out_node = WorkflowNode(id="n_outcome", type=NodeType.OUTCOME, label="Outcome",
                                 position={"x": 0, "y": y}, outcomes=outcome_actions)
        nodes.append(out_node)
        edges.append(WorkflowEdge(source=prev_id, target=out_node.id))

    confidence = 1.0
    if notes:
        confidence = max(0.2, 1.0 - 0.15 * len(notes))

    workflow = Workflow(nodes=nodes, edges=edges)
    return ParseResult(workflow=workflow, notes=notes, confidence=round(confidence, 2),
                        interpreted_summary=interpreted_summary)


def _display_value(c: Condition) -> Any:
    if c.value is None:
        return ""
    return c.value.value if c.value.type == "static" else f"<{c.value.name}>"


_OUTCOME_FIELD_HINTS = [
    (r"high\s*risk", "Risk_Level", "HIGH"),
    (r"medium\s*risk", "Risk_Level", "MEDIUM"),
    (r"low\s*risk", "Risk_Level", "LOW"),
    (r"alert", "Alert", True),
    (r"breach", "Status", "BREACH"),
]


def _parse_outcome(text: str, notes: List[str]) -> List[OutcomeAction]:
    text_l = text.lower()
    actions: List[OutcomeAction] = []

    m = re.match(r"([\w ]+?)\s*(?:to|=)\s*([\w .%-]+)$", text.strip(), re.IGNORECASE)
    if m and " " not in m.group(1).strip().replace("_", ""):
        field_name = m.group(1).strip().replace(" ", "_")
        value = m.group(2).strip()
        actions.append(OutcomeAction(field=field_name, value=ValueRef(type="static", value=value)))
        return actions

    for pattern, field_name, value in _OUTCOME_FIELD_HINTS:
        if re.search(pattern, text_l):
            actions.append(OutcomeAction(field=field_name, value=ValueRef(type="static", value=value)))
    if not actions and text.strip():
        actions.append(OutcomeAction(field="Alert", value=ValueRef(type="static", value=True)))
        notes.append(f"Outcome text '{text.strip()}' was generic — defaulted to Alert = TRUE; please confirm.")
    return actions
