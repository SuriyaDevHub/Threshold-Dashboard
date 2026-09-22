"""Workflow execution engine — the single engine used by dry-run, impact
analysis and (eventually) production execution. Visual-built and
free-text-built rules both compile to the same `Workflow` and run through
this exact code path (spec §53).

Pipeline per spec's INPUT -> PRE-PROCESSING -> LOOKUP/ENRICHMENT ->
DERIVED COLUMNS -> BUSINESS CONDITIONS -> OUTCOME shape (§ intro / §18):
nodes execute in topological order (falling back to declared order when no
edges are given); a FILTER/CONDITION/GROUP node that evaluates false stops
that record's pipeline right there (not matched) without raising; a LOOKUP
node whose `missing_strategy` is `reject` also stops the record, marked as
rejected rather than errored.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.modules.rule_designer import condition_engine, expr_engine, lookup_engine
from app.modules.rule_designer.transform_ops import apply_transform_op
from app.modules.rule_designer.models import (
    DryRunSummary, EnrichmentDiagnostic, NodeType, RecordResult, RecordTraceStep,
    ValueRef, Workflow, WorkflowNode,
)

ReferenceLoader = Callable[[str, Optional[int]], Optional[List[dict]]]


def topo_order(workflow: Workflow) -> List[WorkflowNode]:
    if not workflow.edges:
        return list(workflow.nodes)
    nodes_by_id = {n.id: n for n in workflow.nodes}
    indegree = {n.id: 0 for n in workflow.nodes}
    adj: Dict[str, List[str]] = {n.id: [] for n in workflow.nodes}
    for e in workflow.edges:
        if e.source in adj and e.target in indegree:
            adj[e.source].append(e.target)
            indegree[e.target] += 1
    queue = deque([nid for nid, d in indegree.items() if d == 0])
    order: List[str] = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for nxt in adj.get(nid, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(workflow.nodes):
        # cycle — fall back to declared order rather than silently drop nodes
        return list(workflow.nodes)
    return [nodes_by_id[nid] for nid in order]


def fields_before_node(workflow: Workflow, node_id: str, base_fields: List[str]) -> List[str]:
    """Fields available to a node BEFORE it runs — base columns plus every
    upstream LOOKUP/CALCULATE/TRANSFORM output. Used by validation and by
    the UI to scope field dropdowns per pipeline stage (spec §10/§16)."""
    available = list(base_fields)
    for node in topo_order(workflow):
        if node.id == node_id:
            break
        available.extend(node_outputs(node))
    return available


def all_workflow_outputs(workflow: Workflow, base_fields: List[str]) -> List[str]:
    available = list(base_fields)
    for node in topo_order(workflow):
        available.extend(node_outputs(node))
    return available


def node_outputs(node: WorkflowNode) -> List[str]:
    if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup:
        return [fm.output_field for fm in node.lookup.fields] + (
            [node.lookup.flag_field] if node.lookup.flag_field else []
        )
    if node.type == NodeType.CALCULATE and node.calculate:
        return [node.calculate.output_field]
    if node.type == NodeType.TRANSFORM and node.transform:
        return [node.transform.get("output_field")] if node.transform.get("output_field") else []
    return []


@dataclass
class _NodeIndex:
    lookup_idx: Optional[lookup_engine.LookupIndex] = None
    fallback_idx: Optional[lookup_engine.LookupIndex] = None


def _build_indexes(workflow: Workflow, reference_loader: ReferenceLoader) -> Dict[str, _NodeIndex]:
    indexes: Dict[str, _NodeIndex] = {}
    for node in workflow.nodes:
        if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup:
            cfg = node.lookup
            rows = reference_loader(cfg.reference_file_id, cfg.reference_version) or []
            idx = lookup_engine.build_index(rows, cfg)
            fb_idx = None
            if cfg.fallback_reference_file_id:
                fb_rows = reference_loader(cfg.fallback_reference_file_id, None) or []
                fb_idx = lookup_engine.build_index(fb_rows, cfg)
            indexes[node.id] = _NodeIndex(lookup_idx=idx, fallback_idx=fb_idx)
    return indexes


def _resolve_value(value: Optional[ValueRef], record: dict) -> Any:
    if value is None:
        return None
    if value.type == "static":
        return value.value
    if value.type == "template":
        return expr_engine.render_template(str(value.value), record)
    return record.get(value.name)


def _apply_transform(node: WorkflowNode, record: dict) -> Tuple[str, Dict[str, Any]]:
    cfg = node.transform or {}
    op = cfg.get("op")

    if op == "coalesce":
        # Ordered fallback across candidate fields — generalizes the legacy
        # `_pick(trade, canonical_key, epe_raw_key)` helper to any number of
        # fallback keys, not just a canonical/raw pair.
        out = cfg.get("output_field")
        for candidate in cfg.get("fields", []):
            v = record.get(candidate)
            if v is not None and v != "":
                return "ok", {out: v}
        return "ok", {out: None}

    src = cfg.get("field")
    out = cfg.get("output_field", src)
    val = record.get(src)
    try:
        val = apply_transform_op(op, val, cfg)
        return "ok", {out: val}
    except (TypeError, ValueError, IndexError):
        return "error", {}


def run_workflow(
    workflow: Workflow,
    rows: List[dict],
    reference_loader: ReferenceLoader,
    record_id_field: Optional[str] = None,
    explain_sample_cap: int = 500,
) -> Tuple[List[RecordResult], List[EnrichmentDiagnostic], DryRunSummary]:
    started = time.time()
    order = topo_order(workflow)
    indexes = _build_indexes(workflow, reference_loader)
    has_outcome_node = any(n.type == NodeType.OUTCOME for n in workflow.nodes)

    enrich_stats: Dict[str, Dict[str, Any]] = {
        n.id: {"input": 0, "success": 0, "failures": 0, "unmatched": []}
        for n in workflow.nodes if n.type in (NodeType.LOOKUP, NodeType.ENRICHMENT)
    }

    results: List[RecordResult] = []
    total = matched_ct = not_matched_ct = lookup_failure_ct = error_ct = 0

    for i, raw_record in enumerate(rows):
        total += 1
        wr = dict(raw_record)
        rid = raw_record.get(record_id_field) if record_id_field else i
        trail: List[RecordTraceStep] = []
        reached_outcome = False
        matched = True
        error: Optional[str] = None
        had_lookup_failure = False

        for node in order:
            if node.type == NodeType.INPUT:
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Input dataset", status="ok"))
                continue

            if node.type in (NodeType.FILTER, NodeType.GROUP):
                grp = node.filter or node.condition
                if grp is None:
                    continue
                out = condition_engine.eval_tree(grp, wr)
                status = "ok" if out.result else "skipped"
                detail = "; ".join(e.text for e in out.explain)
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Filter", status=status, detail=detail))
                if not out.result:
                    matched = False
                    break
                continue

            if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup:
                stats = enrich_stats[node.id]
                stats["input"] += 1
                nidx = indexes[node.id]
                outcome = lookup_engine.apply_lookup(wr, nidx.lookup_idx, nidx.fallback_idx)
                wr.update(outcome.fields_added)
                if outcome.status == "matched":
                    stats["success"] += 1
                    status = "ok"
                else:
                    stats["failures"] += 1
                    had_lookup_failure = True
                    status = "lookup_miss"
                    if len(stats["unmatched"]) < 50:
                        keys = [wr.get(jk["source"]) for jk in node.lookup.join_keys] \
                            if node.lookup.lookup_type.value in ("exact", "composite") else \
                            [wr.get(node.lookup.range_field or node.lookup.date_field)]
                        stats["unmatched"].append(keys[0] if len(keys) == 1 else keys)
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Lookup", status=status,
                                              detail=outcome.detail, fields_added=outcome.fields_added))
                if outcome.reject:
                    matched = False
                    error = f"rejected: {outcome.detail}"
                    break
                continue

            if node.type == NodeType.CALCULATE and node.calculate:
                try:
                    expr = expr_engine.parse(node.calculate.expression)
                    value = expr.evaluate(wr)
                    wr[node.calculate.output_field] = value
                    trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                                  label=node.label or node.calculate.output_field,
                                                  status="ok",
                                                  detail=f"{node.calculate.output_field} = {node.calculate.expression} = {value}",
                                                  fields_added={node.calculate.output_field: value}))
                except expr_engine.ExpressionError as exc:
                    error_ct_local = str(exc)
                    trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                                  label=node.label or node.calculate.output_field,
                                                  status="error", detail=error_ct_local))
                continue

            if node.type == NodeType.CONDITION and node.condition:
                out = condition_engine.eval_tree(node.condition, wr)
                status = "ok" if out.result else "skipped"
                detail = "; ".join(e.text for e in out.explain)
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Business condition",
                                              status=status, detail=detail))
                if not out.result:
                    matched = False
                    break
                continue

            if node.type == NodeType.TRANSFORM and node.transform:
                status, fields = _apply_transform(node, wr)
                wr.update(fields)
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Transform", status=status,
                                              fields_added=fields))
                continue

            if node.type == NodeType.VALIDATION and node.validation:
                required = node.validation.get("required_columns", [])
                missing = [c for c in required if wr.get(c) is None]
                status = "ok" if not missing else "error"
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Validation", status=status,
                                              detail=f"missing: {missing}" if missing else ""))
                continue

            if node.type == NodeType.OUTCOME and node.outcomes:
                outcome_fields = {}
                for action in node.outcomes:
                    v = _resolve_value(action.value, wr)
                    wr[action.field] = v
                    outcome_fields[action.field] = v
                trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                              label=node.label or "Outcome", status="ok",
                                              fields_added=outcome_fields))
                reached_outcome = True
                continue

        is_matched = matched and (reached_outcome if has_outcome_node else True) and error is None
        if is_matched:
            matched_ct += 1
        else:
            not_matched_ct += 1
        if had_lookup_failure:
            lookup_failure_ct += 1
        if error:
            error_ct += 1

        outcome_out = {}
        for node in workflow.nodes:
            if node.type == NodeType.OUTCOME and node.outcomes:
                for action in node.outcomes:
                    if action.field in wr:
                        outcome_out[action.field] = wr[action.field]

        if i < explain_sample_cap:
            results.append(RecordResult(
                record_id=rid, matched=is_matched, trail=trail,
                outcome=outcome_out if is_matched else {}, final_record=wr, error=error,
            ))

    elapsed = time.time() - started
    summary = DryRunSummary(
        total_records=total, matched=matched_ct, not_matched=not_matched_ct,
        lookup_failures=lookup_failure_ct, errors=error_ct, execution_time_s=round(elapsed, 4),
        match_rate_pct=round((matched_ct / total * 100.0), 4) if total else 0.0,
    )

    diagnostics: List[EnrichmentDiagnostic] = []
    nodes_by_id = {n.id: n for n in workflow.nodes}
    for node_id, stats in enrich_stats.items():
        node = nodes_by_id[node_id]
        dup = indexes[node_id].lookup_idx.duplicate_keys if indexes[node_id].lookup_idx else {}
        diagnostics.append(EnrichmentDiagnostic(
            node_id=node_id, label=node.label or "Lookup",
            input_records=stats["input"], successful_lookups=stats["success"],
            lookup_failures=stats["failures"],
            match_rate_pct=round(stats["success"] / stats["input"] * 100, 2) if stats["input"] else 0.0,
            duplicate_keys=dup, unmatched_keys=stats["unmatched"],
        ))

    return results, diagnostics, summary
