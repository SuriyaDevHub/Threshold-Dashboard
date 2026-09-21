"""Product-level evaluation — this IS the `generic_validator` from the
{product}_validator.py migration plan: one function, dispatched by
product, with no product- or rule-ID-specific code in it. Every
PUBLISHED and enabled rule for a product runs against the dataset, in
priority order, combined per that product's `conflict_handling`.

Fail-safe default (non-negotiable, carried over from the legacy
validators' own behavior): a disabled product, or a product with zero
active rules, alerts every record rather than silently passing them.
This is the function a migrated `{product}_validator.py` should end up
calling instead of its own hardcoded branches.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from app.core import store as dataset_store
from app.modules.rule_designer import product_registry, reference_store, rule_store, workflow_engine
from app.modules.rule_designer.models import (
    ConflictHandling, ProductEvaluationResult, ProductEvaluationSummary, ProductRecordResult, RuleStatus,
)


def active_rules_for_product(product: str) -> List:
    rules = rule_store.list_rules(product)
    active = [r for r in rules if r.status == RuleStatus.PUBLISHED and r.enabled]
    return sorted(active, key=lambda r: r.priority)


def evaluate_product(product: str, dataset_id: str, actor: str,
                      record_id_field: Optional[str] = None,
                      record_sample_cap: int = 500) -> ProductEvaluationResult:
    prod = product_registry.get_product(product)
    if prod is None:
        raise ValueError(f"product '{product}' is not registered")

    rows = dataset_store.get_rows(dataset_id)
    if rows is None:
        raise ValueError(f"dataset '{dataset_id}' not found")

    total = len(rows)

    if not prod.enabled:
        return _fail_safe_result(product, dataset_id, actor, rows, record_id_field,
                                  record_sample_cap, reason="product is disabled")

    active_rules = active_rules_for_product(product)
    if not active_rules:
        return _fail_safe_result(product, dataset_id, actor, rows, record_id_field,
                                  record_sample_cap, reason="no active (published, enabled) rules for product")

    per_rule_by_record: Dict[str, Dict[str, object]] = {}
    for rule in active_rules:
        records, _, _ = workflow_engine.run_workflow(
            rule.workflow, rows, reference_store.reference_loader,
            record_id_field=record_id_field, explain_sample_cap=total,
        )
        per_rule_by_record[rule.rule_id] = {str(r.record_id): r for r in records}

    conflict = active_rules[0].conflict_handling  # product-level: first active rule's setting governs
    results: List[ProductRecordResult] = []
    matched_ct = 0

    for i, raw_row in enumerate(rows):
        rid = raw_row.get(record_id_field) if record_id_field else i
        rid_key = str(rid)
        matches = []  # [(rule, record_result)] in priority order, only where matched
        for rule in active_rules:
            rec = per_rule_by_record[rule.rule_id].get(rid_key)
            if rec is not None and rec.matched:
                matches.append((rule, rec))

        if not matches:
            if i < record_sample_cap:
                results.append(ProductRecordResult(record_id=rid, matched=False))
            continue

        matched_ct += 1
        if conflict == ConflictHandling.ALL_MATCHING:
            outcome: Dict = {}
            for _, rec in matches:
                outcome.update(rec.outcome)
            chosen_trail = matches[0][1].trail
            matched_ids = [r.rule_id for r, _ in matches]
            primary_rule_id = matches[0][0].rule_id
        elif conflict == ConflictHandling.LAST_MATCH:
            rule, rec = matches[-1]
            outcome, chosen_trail, matched_ids, primary_rule_id = rec.outcome, rec.trail, [rule.rule_id], rule.rule_id
        else:  # FIRST_MATCH / HIGHEST_PRIORITY — active_rules already sorted by priority ascending
            rule, rec = matches[0]
            outcome, chosen_trail, matched_ids, primary_rule_id = rec.outcome, rec.trail, [rule.rule_id], rule.rule_id

        if i < record_sample_cap:
            results.append(ProductRecordResult(
                record_id=rid, matched=True, matched_rule_id=primary_rule_id,
                matched_rule_ids=matched_ids, outcome=outcome, trail=chosen_trail,
            ))

    summary = ProductEvaluationSummary(
        product=product.upper(), product_enabled=True, active_rule_count=len(active_rules),
        total_records=total, matched=matched_ct, not_matched=total - matched_ct,
        match_rate_pct=round(matched_ct / total * 100, 4) if total else 0.0,
        fail_safe_triggered=False,
    )
    return ProductEvaluationResult(
        product=product.upper(), dataset_id=dataset_id, created_by=actor, summary=summary, records=results,
    )


def _fail_safe_result(product: str, dataset_id: str, actor: str, rows: List[dict],
                       record_id_field: Optional[str], record_sample_cap: int, reason: str) -> ProductEvaluationResult:
    total = len(rows)
    results = []
    for i, raw_row in enumerate(rows[:record_sample_cap]):
        rid = raw_row.get(record_id_field) if record_id_field else i
        results.append(ProductRecordResult(record_id=rid, matched=True, outcome={"Alert": True, "Reason": reason}))
    summary = ProductEvaluationSummary(
        product=product.upper(), product_enabled=False if "disabled" in reason else True,
        active_rule_count=0, total_records=total, matched=total, not_matched=0,
        match_rate_pct=100.0 if total else 0.0, fail_safe_triggered=True, fail_safe_reason=reason,
    )
    return ProductEvaluationResult(
        product=product.upper(), dataset_id=dataset_id, created_by=actor, summary=summary, records=results,
    )
