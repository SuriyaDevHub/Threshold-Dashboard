"""Dry run (spec §22-27): execute a rule's workflow against a chosen
dataset without touching production configuration, with record-level
explainability and enrichment diagnostics. Runs synchronously — datasets
in this app are already cached server-side (app.core.store) and are small
enough (mock/sample scale) that a background job queue would be pure
overhead; the response shape already matches what §46's progress UI would
need if a real async job runner is swapped in later.
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

from app.core import store as dataset_store
from app.modules.rule_designer import condition_engine, reference_store, workflow_engine
from app.modules.rule_designer.models import ConditionGroup, DryRunResult, Rule

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
DRYRUN_DIR = os.path.join(_BACKEND_DIR, "_dry_runs")


def _ensure_dir() -> None:
    os.makedirs(DRYRUN_DIR, exist_ok=True)


def _select_sample(rows: List[dict], sample_mode: str, filter_condition: Optional[ConditionGroup],
                    specific_ids: Optional[List[Any]], record_id_field: Optional[str], seed: int = 42) -> List[dict]:
    if filter_condition is not None:
        rows = [r for r in rows if condition_engine.eval_tree(filter_condition, r).result]
    if sample_mode == "specific" and specific_ids and record_id_field:
        wanted = {str(x) for x in specific_ids}
        return [r for r in rows if str(r.get(record_id_field)) in wanted]
    if sample_mode == "sample_1000":
        return _sample(rows, 1000, seed)
    if sample_mode == "sample_10000":
        return _sample(rows, 10000, seed)
    return rows


def _sample(rows: List[dict], n: int, seed: int) -> List[dict]:
    if len(rows) <= n:
        return rows
    rnd = random.Random(seed)
    return rnd.sample(rows, n)


def run_dry_run(rule: Rule, dataset_id: str, actor: str, sample_mode: str = "full",
                 filter_condition: Optional[ConditionGroup] = None,
                 specific_ids: Optional[List[Any]] = None,
                 record_id_field: Optional[str] = None) -> DryRunResult:
    all_rows = dataset_store.get_rows(dataset_id)
    if all_rows is None:
        raise ValueError(f"dataset '{dataset_id}' not found")

    rows = _select_sample(all_rows, sample_mode, filter_condition, specific_ids, record_id_field)

    records, diagnostics, summary = workflow_engine.run_workflow(
        rule.workflow, rows, reference_store.reference_loader, record_id_field=record_id_field,
    )

    unmatched_by_field: Dict[str, List[Any]] = {}
    for d in diagnostics:
        if d.unmatched_keys:
            unmatched_by_field[d.label] = d.unmatched_keys[:20]

    result = DryRunResult(
        rule_id=rule.rule_id, rule_version=rule.version, dataset_id=dataset_id,
        sample_mode=sample_mode, created_by=actor,
        summary=summary, enrichment=diagnostics, records=records,
    )
    _save(result)
    return result


def _save(result: DryRunResult) -> None:
    _ensure_dir()
    with open(os.path.join(DRYRUN_DIR, f"{result.id}.json"), "w") as fh:
        fh.write(result.model_dump_json())


def get_dry_run(dry_run_id: str) -> Optional[DryRunResult]:
    path = os.path.join(DRYRUN_DIR, f"{dry_run_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return DryRunResult.model_validate(json.load(fh))


def list_dry_runs(rule_id: Optional[str] = None) -> List[DryRunResult]:
    _ensure_dir()
    out = []
    for fn in os.listdir(DRYRUN_DIR):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(DRYRUN_DIR, fn)) as fh:
            r = DryRunResult.model_validate(json.load(fh))
        if rule_id and r.rule_id != rule_id:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.created_at, reverse=True)
