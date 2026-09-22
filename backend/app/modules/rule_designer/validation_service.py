"""Validation (spec §28): dataset, lookup, rule and workflow checks that
must all pass before a dry run is allowed to count towards publish, and
before publish is allowed at all. Collects every error found rather than
stopping at the first (so the UI can show the whole list at once).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from app.modules.rule_designer import calc_ops, reference_store, workflow_engine
from app.modules.rule_designer.models import (
    Condition, ConditionGroup, FieldType, NodeType, OPERATORS_BY_TYPE, Rule, ValueRef, Workflow,
)


class ValidationResult:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def _collect_condition_fields(node) -> Set[str]:
    fields: Set[str] = set()
    if node is None:
        return fields
    if isinstance(node, Condition) or getattr(node, "kind", None) == "condition":
        if not isinstance(node, Condition):
            node = Condition.model_validate(node)
        fields.add(node.field)
        for v in (node.value, node.value2, *(node.values or [])):
            if isinstance(v, ValueRef) and v.type in ("column", "derived", "lookup"):
                fields.add(v.name)
        return fields
    if not isinstance(node, ConditionGroup):
        node = ConditionGroup.model_validate(node)
    for child in node.children:
        fields |= _collect_condition_fields(child)
    return fields


def _validate_condition_tree(node, available: Set[str], where: str, result: ValidationResult) -> None:
    if node is None:
        return
    if isinstance(node, Condition) or getattr(node, "kind", None) == "condition":
        if not isinstance(node, Condition):
            node = Condition.model_validate(node)
        if node.field not in available:
            result.errors.append(f"{where}: field '{node.field}' is not available at this stage")
        arity = node.operator.arity
        if arity == "between" and (node.value is None or node.value2 is None):
            result.errors.append(f"{where}: {node.operator.value} on '{node.field}' requires two values")
        if arity == "list" and not node.values:
            result.errors.append(f"{where}: {node.operator.value} on '{node.field}' requires a non-empty list")
        if arity == "binary" and node.value is None:
            result.errors.append(f"{where}: {node.operator.value} on '{node.field}' requires a value")
        if arity == "none" and node.value is not None:
            result.warnings.append(f"{where}: {node.operator.value} on '{node.field}' ignores the supplied value")
        for v in (node.value, node.value2, *(node.values or [])):
            if isinstance(v, ValueRef) and v.type in ("column", "derived", "lookup") and v.name not in available:
                result.errors.append(f"{where}: operand field '{v.name}' is not available at this stage")
        return
    if not isinstance(node, ConditionGroup):
        node = ConditionGroup.model_validate(node)
    if node.operator == "NOT" and len(node.children) != 1:
        result.errors.append(f"{where}: NOT group must have exactly one child")
    for child in node.children:
        _validate_condition_tree(child, available, where, result)


def validate_dataset(rows: List[dict], required_columns: List[str]) -> ValidationResult:
    result = ValidationResult()
    if rows is None:
        result.errors.append("dataset is not readable")
        return result
    if not rows:
        result.warnings.append("dataset has zero rows")
        return result
    present = set(rows[0].keys())
    for col in required_columns:
        if col not in present:
            result.errors.append(f"required column '{col}' is missing from the dataset")
    return result


def validate_lookup_config(node_label: str, lookup_cfg, result: ValidationResult) -> None:
    ref = reference_store.get_file(lookup_cfg.reference_file_id)
    if ref is None or not ref.versions:
        result.errors.append(f"{node_label}: reference file '{lookup_cfg.reference_file_id}' does not exist")
        return
    ver = next((v for v in ref.versions if v.version == lookup_cfg.reference_version), None) \
        if lookup_cfg.reference_version else ref.latest
    if ver is None:
        result.errors.append(f"{node_label}: reference version not found")
        return
    ref_cols = set(ver.columns)

    if lookup_cfg.lookup_type.value in ("exact", "composite"):
        if not lookup_cfg.join_keys:
            result.errors.append(f"{node_label}: no join keys defined")
        for jk in lookup_cfg.join_keys:
            if jk.get("reference") not in ref_cols:
                result.errors.append(f"{node_label}: reference has no column '{jk.get('reference')}'")
    elif lookup_cfg.lookup_type.value == "range":
        for c in (lookup_cfg.range_low_column, lookup_cfg.range_high_column):
            if c not in ref_cols:
                result.errors.append(f"{node_label}: reference has no range column '{c}'")
    elif lookup_cfg.lookup_type.value == "date":
        for c in (lookup_cfg.date_from_column, lookup_cfg.date_to_column):
            if c not in ref_cols:
                result.errors.append(f"{node_label}: reference has no date column '{c}'")

    for fm in lookup_cfg.fields:
        if fm.source_column not in ref_cols:
            result.errors.append(f"{node_label}: reference has no column '{fm.source_column}' to enrich with")

    if lookup_cfg.missing_strategy.value == "default":
        missing = [fm.output_field for fm in lookup_cfg.fields if fm.output_field not in lookup_cfg.default_values]
        if missing:
            result.warnings.append(f"{node_label}: no default value configured for {missing}")
    if lookup_cfg.missing_strategy.value == "flag" and not lookup_cfg.flag_field:
        result.errors.append(f"{node_label}: missing_strategy=flag requires flag_field")
    if lookup_cfg.missing_strategy.value == "fallback" and not lookup_cfg.fallback_reference_file_id:
        result.errors.append(f"{node_label}: missing_strategy=fallback requires fallback_reference_file_id")

    # duplicate key detection (never silently pick one, spec §29)
    if lookup_cfg.lookup_type.value in ("exact", "composite"):
        rows = reference_store.get_rows(lookup_cfg.reference_file_id, lookup_cfg.reference_version)
        if rows:
            from app.modules.rule_designer.lookup_engine import build_index
            idx = build_index(rows, lookup_cfg)
            if idx.duplicate_keys:
                sample = list(idx.duplicate_keys.items())[:5]
                result.warnings.append(
                    f"{node_label}: duplicate lookup keys detected {sample} — resolved via "
                    f"'{lookup_cfg.priority_strategy.value}'"
                )


def validate_workflow(rule: Rule, dataset_schema: Optional[Dict[str, FieldType]] = None) -> ValidationResult:
    result = ValidationResult()
    workflow = rule.workflow

    if not workflow.nodes:
        result.errors.append("workflow has no nodes")
        return result
    if not any(n.type == NodeType.INPUT for n in workflow.nodes):
        result.warnings.append("workflow has no explicit INPUT node — the dataset is used as the implicit start")

    order = workflow_engine.topo_order(workflow)
    if len(order) != len(workflow.nodes):
        result.errors.append("workflow has a circular dependency between nodes")
        return result

    node_ids = {n.id for n in workflow.nodes}
    for e in workflow.edges:
        if e.source not in node_ids or e.target not in node_ids:
            result.errors.append(f"edge references an unknown node ({e.source} -> {e.target})")

    base_fields = list(rule.required_columns) if rule.required_columns else (
        list(dataset_schema.keys()) if dataset_schema else []
    )
    if not base_fields:
        result.warnings.append("no dataset bound yet — field-availability checks are skipped until one is")

    output_seen: Dict[str, str] = {}
    for node in order:
        available = set(workflow_engine.fields_before_node(workflow, node.id, base_fields))
        label = f"[{node.type.value}] {node.label or node.id}"

        if node.type in (NodeType.FILTER, NodeType.GROUP) and (node.filter or node.condition):
            if base_fields:
                _validate_condition_tree(node.filter or node.condition, available, label, result)

        elif node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup:
            for jk in node.lookup.join_keys:
                if base_fields and jk.get("source") not in available:
                    result.errors.append(f"{label}: source field '{jk.get('source')}' not available at this stage")
            if node.lookup.range_field and base_fields and node.lookup.range_field not in available:
                result.errors.append(f"{label}: source field '{node.lookup.range_field}' not available at this stage")
            if node.lookup.date_field and base_fields and node.lookup.date_field not in available:
                result.errors.append(f"{label}: source field '{node.lookup.date_field}' not available at this stage")
            validate_lookup_config(label, node.lookup, result)
            for fm in node.lookup.fields:
                if fm.output_field in output_seen:
                    result.errors.append(
                        f"{label}: output field '{fm.output_field}' duplicates output from {output_seen[fm.output_field]}"
                    )
                output_seen[fm.output_field] = label

        elif node.type == NodeType.CALCULATE and node.calculate:
            formula_errors = calc_ops.validate_formula(node.calculate.formula)
            for err in formula_errors:
                result.errors.append(f"{label}: {err}")
            if not formula_errors and base_fields:
                missing = calc_ops.fields_referenced(node.calculate.formula) - available
                if missing:
                    result.errors.append(f"{label}: formula references unavailable field(s) {sorted(missing)}")
            if node.calculate.output_field in output_seen:
                result.errors.append(
                    f"{label}: output field '{node.calculate.output_field}' duplicates output from "
                    f"{output_seen[node.calculate.output_field]}"
                )
            output_seen[node.calculate.output_field] = label

        elif node.type == NodeType.CONDITION and node.condition:
            if base_fields:
                _validate_condition_tree(node.condition, available, label, result)

        elif node.type == NodeType.OUTCOME:
            if not node.outcomes:
                result.errors.append(f"{label}: outcome node defines no actions")
            else:
                for action in node.outcomes:
                    if action.value and action.value.type in ("column", "derived", "lookup") \
                            and base_fields and action.value.name not in available:
                        result.errors.append(
                            f"{label}: outcome for '{action.field}' references unavailable field "
                            f"'{action.value.name}'"
                        )

    if not any(n.type == NodeType.OUTCOME for n in workflow.nodes):
        result.warnings.append("workflow defines no OUTCOME node — dry-run will report condition matches only")

    return result
