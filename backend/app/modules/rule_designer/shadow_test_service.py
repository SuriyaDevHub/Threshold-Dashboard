"""Shadow / parallel testing — the migration safety net.

Where `impact_service` compares two versions of a rule *inside* this
system, this compares the new rule engine's decision against a legacy
validator's already-produced output, so a `{product}_validator.py`
migration can be proven safe before cutover without importing or running
any of that legacy code here. The legacy side is just data: you export
what your existing validator decided for a dataset (record_id + status,
however your vocabulary spells "alerted" for that product) and upload it;
this joins it against the new engine's run over the same dataset.

The one category that matters most is `legacy_only`: a record the legacy
validator alerted on that the new rule set does not. That's a silent
regression if it ships, so it's what the UI leads with.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

from app.core import store as dataset_store
from app.modules.rule_designer import reference_store, workflow_engine
from app.modules.rule_designer.models import (
    LegacyResultRow, Rule, ShadowComparisonCategory, ShadowRecordComparison,
    ShadowTestResult, ShadowTestSummary,
)

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
SHADOW_DIR = os.path.join(_BACKEND_DIR, "_shadow_tests")


def _ensure_dir() -> None:
    os.makedirs(SHADOW_DIR, exist_ok=True)


def parse_legacy_csv(csv_text: str, record_id_col: str = "record_id",
                      status_col: str = "status", reason_col: Optional[str] = "reason_code",
                      commentary_col: Optional[str] = "commentary") -> List[LegacyResultRow]:
    rows = reference_store.parse_csv_text(csv_text)
    out: List[LegacyResultRow] = []
    for r in rows:
        rid = r.get(record_id_col)
        if rid is None or rid == "":
            continue
        out.append(LegacyResultRow(
            record_id=str(rid),
            legacy_status=str(r.get(status_col, "")),
            legacy_reason_code=r.get(reason_col) if reason_col else None,
            legacy_commentary=r.get(commentary_col) if commentary_col else None,
        ))
    return out


def run_shadow_test(
    rule: Rule,
    dataset_id: str,
    legacy_rows: List[LegacyResultRow],
    legacy_alert_values: List[str],
    actor: str,
    record_id_field: str,
    mismatch_cap: int = 500,
) -> ShadowTestResult:
    dataset_rows = dataset_store.get_rows(dataset_id)
    if dataset_rows is None:
        raise ValueError(f"dataset '{dataset_id}' not found")

    new_records, _, _ = workflow_engine.run_workflow(
        rule.workflow, dataset_rows, reference_store.reference_loader,
        record_id_field=record_id_field, explain_sample_cap=len(dataset_rows),
    )
    new_by_id: Dict[str, object] = {str(r.record_id): r for r in new_records}

    alert_set = {v.strip().upper() for v in legacy_alert_values if v.strip()}
    legacy_by_id: Dict[str, LegacyResultRow] = {row.record_id: row for row in legacy_rows}

    dataset_ids = set(new_by_id.keys())
    legacy_ids = set(legacy_by_id.keys())
    compared_ids = dataset_ids & legacy_ids

    summary = ShadowTestSummary(
        total_compared=len(compared_ids),
        dataset_records_without_legacy_result=len(dataset_ids - legacy_ids),
        legacy_results_without_dataset_record=len(legacy_ids - dataset_ids),
    )
    mismatches: List[ShadowRecordComparison] = []

    for rid in compared_ids:
        new_rec = new_by_id[rid]
        legacy_row = legacy_by_id[rid]
        legacy_matched = legacy_row.legacy_status.strip().upper() in alert_set
        new_matched = bool(new_rec.matched)

        if new_matched and legacy_matched:
            category = ShadowComparisonCategory.AGREE_ALERT
            summary.agree_alert += 1
        elif not new_matched and not legacy_matched:
            category = ShadowComparisonCategory.AGREE_CLEAR
            summary.agree_clear += 1
        elif new_matched and not legacy_matched:
            category = ShadowComparisonCategory.NEW_ONLY
            summary.new_only += 1
        else:
            category = ShadowComparisonCategory.LEGACY_ONLY
            summary.legacy_only += 1

        if category in (ShadowComparisonCategory.NEW_ONLY, ShadowComparisonCategory.LEGACY_ONLY) \
                and len(mismatches) < mismatch_cap:
            mismatches.append(ShadowRecordComparison(
                record_id=rid, category=category,
                legacy_status=legacy_row.legacy_status, legacy_reason_code=legacy_row.legacy_reason_code,
                new_matched=new_matched, new_outcome=new_rec.outcome, trail=new_rec.trail,
            ))

    summary.agreement_rate_pct = round(
        (summary.agree_alert + summary.agree_clear) / summary.total_compared * 100, 4
    ) if summary.total_compared else 0.0

    # legacy_only first — the regression-risk category — then new_only
    mismatches.sort(key=lambda m: 0 if m.category == ShadowComparisonCategory.LEGACY_ONLY else 1)

    result = ShadowTestResult(
        rule_id=rule.rule_id, rule_version=rule.version, dataset_id=dataset_id,
        legacy_alert_values=sorted(alert_set), created_by=actor,
        summary=summary, mismatches=mismatches,
    )
    _save(result)
    return result


def _save(result: ShadowTestResult) -> None:
    _ensure_dir()
    with open(os.path.join(SHADOW_DIR, f"{result.id}.json"), "w") as fh:
        fh.write(result.model_dump_json())


def get_shadow_test(shadow_id: str) -> Optional[ShadowTestResult]:
    path = os.path.join(SHADOW_DIR, f"{shadow_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return ShadowTestResult.model_validate(json.load(fh))


def list_shadow_tests(rule_id: Optional[str] = None) -> List[ShadowTestResult]:
    _ensure_dir()
    out = []
    for fn in os.listdir(SHADOW_DIR):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(SHADOW_DIR, fn)) as fh:
            r = ShadowTestResult.model_validate(json.load(fh))
        if rule_id and r.rule_id != rule_id:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.created_at, reverse=True)
