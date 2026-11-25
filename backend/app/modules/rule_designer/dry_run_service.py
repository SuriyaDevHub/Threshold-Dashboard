"""Dry run (spec §22-27): execute a rule's workflow against a chosen
dataset without touching production configuration, with record-level
explainability and enrichment diagnostics. Still runs synchronously —
datasets are already cached server-side (app.core.store), and the actual
per-record evaluation (workflow_engine.run_workflow()) is the same O(n)
pass a background job would also have to make, so a job queue wouldn't
make a single dry run faster, only move where it blocks; the response
shape already matches what §46's progress UI would need if a real async
job runner is swapped in later for that reason alone (freeing the request
thread on a very large production dataset, not raw throughput).

What a large production dataset DOES need, and what's bounded here: the
"full" sample mode (process every record) still computes summary counts
and per-lookup match-rate diagnostics over the ENTIRE dataset, but caps
how many individual records' full per-node trace gets kept and returned
for display (_FULL_MODE_EXPLAIN_CAP) — see run_dry_run()'s explain_sample_cap
and run_workflow()'s own docstring for why keeping every record's trail
used to be assumed cheap.

Results are deliberately NOT persisted to S3 (or anywhere durable) — a dry
run is a throwaway exploration, not part of the rule's record of truth, so
results live only in this process's memory (bounded — see
_MAX_STORED_DRY_RUNS) and are gone on the next backend restart.
"""
from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Optional

from app.core import store as dataset_store
from app.modules.rule_designer import condition_engine, reference_store, workflow_engine
from app.modules.rule_designer.models import ConditionGroup, DryRunResult, Rule

_lock = threading.Lock()
_results: Dict[str, DryRunResult] = {}

# A long-running backend process never restarts between dry runs the way a
# test's isolated_storage fixture does — without a bound here, every dry run
# ever triggered (each one potentially holding thousands of full per-record
# traces, see _FULL_MODE_EXPLAIN_CAP below) stays in memory for the life of
# the process. Oldest evicted first; cheap O(n) scan is fine since n is
# bounded by this constant, not by dataset size.
_MAX_STORED_DRY_RUNS = 200

# "full" sample mode (the default — "entire dataset" in the UI) can mean
# hundreds of thousands of production records. Keeping a complete per-node
# trail + enriched final_record for every one of them (what leaving
# explain_sample_cap unset does — see run_workflow()'s own default) is what
# actually dominates a large dry run's time and response size, not file I/O
# (already cached per day/version — see day_partitioned_source.py's and
# reference_store.py's index caches). Summary counts and per-lookup
# match-rate diagnostics are computed over EVERY record regardless of this
# cap (run_workflow()'s matched/not_matched/enrich_stats counters are
# unconditional); only the detailed, UI-facing record list is bounded, and
# matched/not-matched detail are capped independently so a low match rate on
# a huge dataset still surfaces this many examples of EACH outcome rather
# than only whichever is more common. The explicit sample_1000/sample_10000/
# specific/date_range modes already select a bounded input before this ever
# runs, so they're left uncapped here and keep showing full detail for
# whatever the admin explicitly chose.
_FULL_MODE_EXPLAIN_CAP = 2000


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

    cap = _FULL_MODE_EXPLAIN_CAP if sample_mode == "full" else None
    records, diagnostics, summary = workflow_engine.run_workflow(
        rule.workflow, rows, reference_store.reference_loader, record_id_field=record_id_field,
        explain_sample_cap=cap,
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
    with _lock:
        _results[result.id] = result
        if len(_results) > _MAX_STORED_DRY_RUNS:
            oldest_id = min(_results, key=lambda k: _results[k].created_at)
            del _results[oldest_id]


def get_dry_run(dry_run_id: str) -> Optional[DryRunResult]:
    with _lock:
        return _results.get(dry_run_id)


def list_dry_runs(rule_id: Optional[str] = None) -> List[DryRunResult]:
    with _lock:
        out = list(_results.values())
    if rule_id:
        out = [r for r in out if r.rule_id == rule_id]
    return sorted(out, key=lambda r: r.created_at, reverse=True)
