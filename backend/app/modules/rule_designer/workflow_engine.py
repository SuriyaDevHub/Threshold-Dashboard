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

from app.modules.rule_designer import calc_ops, condition_engine, expr_engine, lookup_engine
from app.modules.rule_designer.transform_ops import apply_transform_op
from app.modules.rule_designer.models import (
    DryRunSummary, EnrichmentDiagnostic, LookupType, NodeType, RecordResult, RecordTraceStep,
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


def _to_num_or_none(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _aggregate_value(op: str, rows: List[dict], field: str) -> Optional[float]:
    """sum/count/min/max/first of `field` across every row in a self_group
    group — e.g. summing a structure's total PnL across all its deals,
    where a plain representative-row broadcast would only give you one
    deal's own PnL. Non-numeric/missing values are skipped; an empty
    result is None (not 0), so a group with no numeric data at all reads
    as "no data" rather than a misleading zero. "first" is the first
    numeric value in row order — for a field that's expected to just be
    duplicated across a group (e.g. a shared threshold), this tolerates
    a representative row whose own value happens to be blank by reading
    whichever group member has one first, rather than broadcasting None."""
    values = [v for v in (_to_num_or_none(r.get(field)) for r in rows) if v is not None]
    if op == "count":
        return float(len(values))
    if not values:
        return None
    if op == "sum":
        return sum(values)
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    if op == "first":
        return values[0]
    return None


def _self_group_reference_rows(input_rows: List[dict], cfg) -> Tuple[List[dict], Any]:
    """Build a self_group LOOKUP's "reference rows" by grouping the input
    dataset itself by `cfg.group_by_field` and picking one representative
    row per group via `cfg.selector` (the first row satisfying it; the
    group's first row if no selector is set or nothing satisfies it). Any
    field mapping with `aggregate` set (sum/count/min/max) has its value
    on the representative row overwritten with that aggregate computed
    across every row in the group — e.g. a structure's total PnL summed
    over all its deals, not just the representative deal's own PnL.
    Grouping always happens over the raw input rows the dry-run/dataset
    started with, not any upstream CALCULATE/TRANSFORM output within this
    same run — matching how a reference-file lookup's index is also built
    once, up front, independent of per-record pipeline state.

    Returns (representative_rows, effective_config) — effective_config is
    `cfg` with `join_keys` auto-derived from `group_by_field` (self_group
    has no user-configured join_keys; the group key doubles as the join
    key on both sides, since a representative row and the rows it enriches
    share the exact same schema)."""
    groups: Dict[Any, List[dict]] = {}
    field_name = cfg.group_by_field
    for row in input_rows:
        if not field_name:
            continue
        key = row.get(field_name)
        if key is None or key == "":
            continue
        groups.setdefault(key, []).append(row)

    agg_fields = [fm for fm in cfg.fields if fm.aggregate]

    representatives = []
    for rows_in_group in groups.values():
        rep = rows_in_group[0]
        if cfg.selector is not None:
            rep = next(
                (r for r in rows_in_group if condition_engine.eval_tree(cfg.selector, r).result),
                None,
            )
        if rep is None:
            continue
        if agg_fields:
            rep = dict(rep)  # copy — never mutate the caller's own input rows
            for fm in agg_fields:
                # A synthetic per-mapping key, not `fm.source_column` itself —
                # two mappings commonly aggregate the same source_column with
                # different ops (e.g. sum AND count of the same PnL field),
                # and both writing "pnl" would let one silently clobber the
                # other. lookup_engine.apply_lookup() reads this same key.
                rep[lookup_engine.self_group_agg_key(fm.output_field)] = \
                    _aggregate_value(fm.aggregate, rows_in_group, fm.source_column)
        representatives.append(rep)

    effective_cfg = cfg.model_copy(update={
        "join_keys": [{"source": field_name, "reference": field_name}] if field_name else [],
    })
    return representatives, effective_cfg


def _build_indexes(workflow: Workflow, rows: List[dict], reference_loader: ReferenceLoader) -> Dict[str, _NodeIndex]:
    indexes: Dict[str, _NodeIndex] = {}
    for node in workflow.nodes:
        if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup:
            cfg = node.lookup
            if cfg.lookup_type == LookupType.SELF_GROUP:
                ref_rows, cfg = _self_group_reference_rows(rows, cfg)
            else:
                ref_rows = reference_loader(cfg.reference_file_id, cfg.reference_version) or []
            idx = lookup_engine.build_index(ref_rows, cfg)
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
    explain_sample_cap: Optional[int] = None,
) -> Tuple[List[RecordResult], List[EnrichmentDiagnostic], DryRunSummary]:
    started = time.time()
    # No cap by default: datasets here are cached server-side and mock/sample
    # scale (see module docstring), so keeping every record's trace is cheap
    # and is what lets a reviewer actually validate a dry run's results
    # end-to-end rather than spot-checking whatever fell inside a sample. A
    # caller can still pass an explicit cap for an unusually large pull.
    cap = explain_sample_cap if explain_sample_cap is not None else len(rows)
    order = topo_order(workflow)
    indexes = _build_indexes(workflow, rows, reference_loader)
    has_outcome_node = any(n.type == NodeType.OUTCOME for n in workflow.nodes)

    enrich_stats: Dict[str, Dict[str, Any]] = {
        n.id: {"input": 0, "success": 0, "failures": 0, "unmatched": []}
        for n in workflow.nodes if n.type in (NodeType.LOOKUP, NodeType.ENRICHMENT)
    }

    results: List[RecordResult] = []
    total = matched_ct = not_matched_ct = lookup_failure_ct = error_ct = 0
    matched_kept = not_matched_kept = 0

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
                    value = calc_ops.evaluate_formula(node.calculate.formula, wr)
                    wr[node.calculate.output_field] = value
                    trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                                  label=node.label or node.calculate.output_field,
                                                  status="ok",
                                                  detail=f"{node.calculate.output_field} = "
                                                         f"{calc_ops.describe_formula(node.calculate.formula)} = {value}",
                                                  fields_added={node.calculate.output_field: value}))
                except calc_ops.CalcError as exc:
                    trail.append(RecordTraceStep(node_id=node.id, node_type=node.type,
                                                  label=node.label or node.calculate.output_field,
                                                  status="error", detail=str(exc)))
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

        # Cap matched and not-matched traces independently, rather than
        # keeping only the first `explain_sample_cap` rows by raw dataset
        # position: on a large dataset where matches (or lookup failures)
        # are sparse and happen to fall past that position, an index-based
        # cap could silently exclude every one of them from the
        # explainability list even though the summary counts them —
        # exactly the failure mode that makes "N matched" in the summary
        # disagree with what the record-level drill-down actually shows.
        if is_matched and matched_kept < cap:
            matched_kept += 1
            results.append(RecordResult(
                record_id=rid, matched=is_matched, trail=trail,
                outcome=outcome_out, final_record=wr, error=error,
            ))
        elif not is_matched and not_matched_kept < cap:
            not_matched_kept += 1
            results.append(RecordResult(
                record_id=rid, matched=is_matched, trail=trail,
                outcome={}, final_record=wr, error=error,
            ))

    elapsed = time.time() - started
    summary = DryRunSummary(
        total_records=total, matched=matched_ct, not_matched=not_matched_ct,
        lookup_failures=lookup_failure_ct, errors=error_ct, execution_time_s=round(elapsed, 4),
        match_rate_pct=round((matched_ct / total * 100.0), 4) if total else 0.0,
        matched_shown=matched_kept, not_matched_shown=not_matched_kept,
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
