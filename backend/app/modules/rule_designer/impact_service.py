"""Impact analysis (spec §26): current production rule vs. the proposed
draft, run over the same dataset, diffed at the record level."""
from __future__ import annotations

from typing import Optional

from app.core import store as dataset_store
from app.modules.rule_designer import reference_store, version_service, workflow_engine, yaml_service
from app.modules.rule_designer.models import ImpactResult, Rule


def _published_rule_from_version(rule_id: str, version: int) -> Optional[Rule]:
    text = version_service.get_version_yaml_text(version)
    if text is None:
        return None
    raw = yaml_service._yaml.load(text)  # noqa: SLF001
    for item in raw.get("rules", []) or []:
        plain = yaml_service._plain(item)  # noqa: SLF001
        if plain.get("rule_id") == rule_id:
            return Rule.model_validate(plain)
    return None


def run_impact_analysis(proposed_rule: Rule, dataset_id: str, actor: str,
                         record_id_field: Optional[str] = None) -> ImpactResult:
    rows = dataset_store.get_rows(dataset_id)
    if rows is None:
        raise ValueError(f"dataset '{dataset_id}' not found")

    versions = [v for v in version_service.list_versions() if proposed_rule.rule_id in v.rule_ids_changed]
    current_version_no = versions[-1].version if versions else None
    current_rule = _published_rule_from_version(proposed_rule.rule_id, current_version_no) if current_version_no else None

    proposed_records, _, proposed_summary = workflow_engine.run_workflow(
        proposed_rule.workflow, rows, reference_store.reference_loader, record_id_field=record_id_field,
    )
    proposed_matched_ids = {r.record_id for r in proposed_records if r.matched}

    if current_rule is not None:
        current_records, _, current_summary = workflow_engine.run_workflow(
            current_rule.workflow, rows, reference_store.reference_loader, record_id_field=record_id_field,
        )
        current_matched_ids = {r.record_id for r in current_records if r.matched}
    else:
        current_matched_ids = set()
        current_summary = None

    new_ids = proposed_matched_ids - current_matched_ids
    removed_ids = current_matched_ids - proposed_matched_ids
    unchanged_ids = proposed_matched_ids & current_matched_ids

    outcome_changes = []
    if current_rule is not None:
        current_by_id = {r.record_id: r for r in current_records}
        proposed_by_id = {r.record_id: r for r in proposed_records}
        for rid in unchanged_ids:
            c, p = current_by_id.get(rid), proposed_by_id.get(rid)
            if c and p and c.outcome != p.outcome:
                outcome_changes.append({"record_id": rid, "before": c.outcome, "after": p.outcome})

    result = ImpactResult(
        rule_id=proposed_rule.rule_id, dataset_id=dataset_id,
        current_version=current_version_no, proposed_version=proposed_rule.version,
        current_matches=len(current_matched_ids), proposed_matches=len(proposed_matched_ids),
        new_matches=len(new_ids), removed_matches=len(removed_ids), unchanged_matches=len(unchanged_ids),
        outcome_changes=outcome_changes[:200],
        new_match_ids=list(new_ids)[:200], removed_match_ids=list(removed_ids)[:200],
    )
    return result
