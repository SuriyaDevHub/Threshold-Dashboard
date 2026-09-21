"""Enterprise Business Rule Designer — plugin module.

A "Business Rule Studio": visual + free-text rule authoring compiling into
one canonical Workflow model, pre-processing/enrichment (lookup) design,
dry-run with record-level explainability, before/after impact analysis,
review/approval/publish lifecycle, YAML versioning, and a full audit
trail — all sitting on top of the existing Data Fetch dataset store so
rules can be tested against real pulled datasets (spec, full document).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import store as dataset_store
from app.modules.rule_designer import (
    audit_service, diff_service, dry_run_service, explain_service, impact_service,
    nlp_parser, reference_store, rule_store, validation_service, version_service, yaml_service,
)
from app.modules.rule_designer.models import (
    LEGAL_TRANSITIONS, ROLE_ALLOWED_ACTIONS, ConditionGroup, NodeType, Operator, OPERATORS_BY_TYPE,
    Role, Rule, RuleStatus, Workflow, FieldType,
)
from app.modules.rule_designer.schema import infer_schema, schema_to_wire

META = {
    "id": "rule-designer",
    "name": "Rule Designer",
    "description": "Author, enrich, test, review, approve and publish business rules — no hand-edited YAML.",
    "icon": "git-branch",
}

router = APIRouter()


# --------------------------------------------------------------------------
# Access control helper (no login system in this app; the caller states who
# they are and what role they're acting as, matching every other module's
# no-auth-by-design convention — see spec §34's role list).
# --------------------------------------------------------------------------

class Actor(BaseModel):
    actor: str = "unknown"
    role: Role = Role.RULE_CREATOR


def _require(actor: Actor, action: str) -> None:
    allowed = ROLE_ALLOWED_ACTIONS.get(actor.role, [])
    if action not in allowed:
        raise HTTPException(403, f"role '{actor.role.value}' cannot '{action}' (allowed: {allowed})")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@router.get("/dashboard")
async def dashboard():
    rules = rule_store.list_rules()
    by_status: Dict[str, int] = {}
    for r in rules:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
    recent = sorted(rules, key=lambda r: r.updated_at, reverse=True)[:8]
    recent_audit = audit_service.query(limit=10)
    return {
        "total_rules": len(rules),
        "published": by_status.get("PUBLISHED", 0),
        "draft": by_status.get("DRAFT", 0),
        "pending_approval": by_status.get("PENDING_APPROVAL", 0),
        "by_status": by_status,
        "recent_rules": [{"rule_id": r.rule_id, "name": r.name, "status": r.status.value,
                           "updated_at": r.updated_at, "updated_by": r.updated_by} for r in recent],
        "recent_activity": [e.model_dump() for e in recent_audit],
        "reference_files": len(reference_store.list_files()),
        "versions_published": len(version_service.list_versions()),
    }


# --------------------------------------------------------------------------
# Datasets (reuses the app-wide dataset store Data Fetch already writes to)
# --------------------------------------------------------------------------

@router.get("/datasets")
async def list_datasets():
    metas = dataset_store.list_datasets()
    return {"datasets": [
        {"id": m.id, "label": m.label, "source": m.source, "product_type": m.product_type,
         "row_count": m.row_count, "fetched_at": m.fetched_at}
        for m in metas
    ]}


@router.get("/datasets/{dataset_id}/schema")
async def dataset_schema(dataset_id: str):
    rows = dataset_store.get_rows(dataset_id)
    if rows is None:
        raise HTTPException(404, "dataset not found")
    schema = infer_schema(rows)
    return {"dataset_id": dataset_id, "columns": schema_to_wire(schema), "row_count": len(rows)}


@router.get("/datasets/{dataset_id}/preview")
async def dataset_preview(dataset_id: str, limit: int = 50):
    rows = dataset_store.get_rows(dataset_id)
    if rows is None:
        raise HTTPException(404, "dataset not found")
    return {"dataset_id": dataset_id, "rows": rows[:limit], "total": len(rows)}


class UploadDatasetBody(BaseModel):
    label: str
    csv_text: str


@router.post("/datasets/upload")
async def upload_dataset(body: UploadDatasetBody):
    rows = reference_store.parse_csv_text(body.csv_text)
    if not rows:
        raise HTTPException(400, "no rows parsed from CSV")
    meta = dataset_store.put("UPLOAD", {"product_type": body.label}, rows)
    return {"id": meta.id, "row_count": meta.row_count, "label": meta.label}


# --------------------------------------------------------------------------
# Reference / lookup files
# --------------------------------------------------------------------------

@router.get("/reference-files")
async def list_reference_files():
    return {"files": [f.model_dump() for f in reference_store.list_files()]}


@router.get("/reference-files/{file_id}")
async def get_reference_file(file_id: str):
    f = reference_store.get_file(file_id)
    if f is None:
        raise HTTPException(404, "reference file not found")
    return f.model_dump()


class UploadReferenceBody(Actor):
    name: str
    csv_text: str


@router.post("/reference-files/upload")
async def upload_reference_file(body: UploadReferenceBody):
    _require(body, "manage_lookups")
    rows = reference_store.parse_csv_text(body.csv_text)
    if not rows:
        raise HTTPException(400, "no rows parsed from CSV")
    f = reference_store.upload_version(body.name, rows, body.actor)
    audit_service.log(body.actor, "CREATE", role=body.role.value,
                       detail=f"uploaded reference file '{body.name}' v{f.latest.version}",
                       lookup_files=[f.id])
    return f.model_dump()


@router.get("/reference-files/{file_id}/rows")
async def reference_file_rows(file_id: str, version: Optional[int] = None, limit: int = 100):
    rows = reference_store.get_rows(file_id, version)
    if rows is None:
        raise HTTPException(404, "reference file/version not found")
    return {"rows": rows[:limit], "total": len(rows)}


@router.delete("/reference-files/{file_id}")
async def delete_reference_file(file_id: str, actor: str = "unknown"):
    ok = reference_store.delete_file(file_id)
    if not ok:
        raise HTTPException(404, "not found")
    audit_service.log(actor, "DELETE", detail=f"deleted reference file '{file_id}'", lookup_files=[file_id])
    return {"deleted": True}


# --------------------------------------------------------------------------
# Metadata for the UI (operator catalog, node types, roles)
# --------------------------------------------------------------------------

@router.get("/meta")
async def meta():
    return {
        "node_types": [t.value for t in NodeType],
        "operators": {ft.value: [op.value for op in ops] for ft, ops in OPERATORS_BY_TYPE.items()},
        "operator_arity": {op.value: op.arity for op in Operator},
        "roles": [r.value for r in Role],
        "role_actions": {r.value: acts for r, acts in ROLE_ALLOWED_ACTIONS.items()},
        "statuses": [s.value for s in RuleStatus],
        "legal_transitions": {s.value: [t.value for t in ts] for s, ts in LEGAL_TRANSITIONS.items()},
    }


# --------------------------------------------------------------------------
# Rules CRUD
# --------------------------------------------------------------------------

@router.get("/rules")
async def list_rules():
    return {"rules": [r.model_dump(mode="json") for r in rule_store.list_rules()]}


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str):
    r = rule_store.get_rule(rule_id)
    if r is None:
        raise HTTPException(404, "rule not found")
    return r.model_dump(mode="json")


class CreateRuleBody(Actor):
    rule: Dict[str, Any]


@router.post("/rules")
async def create_rule(body: CreateRuleBody):
    _require(body, "create")
    if rule_store.get_rule(body.rule.get("rule_id", "")):
        raise HTTPException(409, "rule_id already exists")
    body.rule.setdefault("created_by", body.actor)
    body.rule.setdefault("updated_by", body.actor)
    rule = Rule.model_validate(body.rule)
    rule_store.upsert_rule(rule, body.actor)
    audit_service.log(body.actor, "CREATE", role=body.role.value, rule_id=rule.rule_id,
                       rule_name=rule.name, new_version=rule.version)
    return rule.model_dump(mode="json")


class UpdateRuleBody(Actor):
    rule: Dict[str, Any]


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, body: UpdateRuleBody):
    _require(body, "edit")
    existing = rule_store.get_rule(rule_id)
    if existing is None:
        raise HTTPException(404, "rule not found")
    body.rule["rule_id"] = rule_id
    prev_version = existing.version
    rule = Rule.model_validate(body.rule)
    if rule.status in (RuleStatus.APPROVED, RuleStatus.PUBLISHED):
        rule.status = RuleStatus.DRAFT  # any edit re-opens the lifecycle
    rule_store.upsert_rule(rule, body.actor)
    audit_service.log(body.actor, "EDIT", role=body.role.value, rule_id=rule.rule_id,
                       rule_name=rule.name, previous_version=prev_version, new_version=rule.version)
    return rule.model_dump(mode="json")


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, actor: str = "unknown", role: Role = Role.ADMIN):
    _require(Actor(actor=actor, role=role), "delete")
    ok = rule_store.delete_rule(rule_id, actor)
    if not ok:
        raise HTTPException(404, "rule not found")
    audit_service.log(actor, "DELETE", role=role.value, rule_id=rule_id)
    return {"deleted": True}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@router.post("/rules/{rule_id}/validate")
async def validate_rule_endpoint(rule_id: str, actor: str = "unknown"):
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    schema = None
    if rule.dataset_id:
        rows = dataset_store.get_rows(rule.dataset_id)
        if rows:
            schema = infer_schema(rows)
            ds_result = validation_service.validate_dataset(rows, rule.required_columns)
        else:
            ds_result = validation_service.ValidationResult()
            ds_result.errors.append(f"dataset '{rule.dataset_id}' not found")
    else:
        ds_result = validation_service.ValidationResult()
        ds_result.warnings.append("no dataset bound to this rule yet")

    wf_result = validation_service.validate_workflow(rule, schema)
    errors = ds_result.errors + wf_result.errors
    warnings = ds_result.warnings + wf_result.warnings

    if not errors and rule.status == RuleStatus.DRAFT:
        rule.status = RuleStatus.VALIDATED
        rule_store.upsert_rule(rule, actor)
    audit_service.log(actor, "EDIT", rule_id=rule_id,
                       detail=f"validated: {len(errors)} error(s), {len(warnings)} warning(s)")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "status": rule.status.value}


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

class DryRunBody(Actor):
    dataset_id: str
    sample_mode: str = "full"  # full | sample_1000 | sample_10000 | specific | date_range
    filter: Optional[Dict[str, Any]] = None
    specific_ids: Optional[List[Any]] = None
    record_id_field: Optional[str] = None


@router.post("/rules/{rule_id}/dry-run")
async def dry_run(rule_id: str, body: DryRunBody):
    _require(body, "dry_run")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    filt = ConditionGroup.model_validate(body.filter) if body.filter else None
    try:
        result = dry_run_service.run_dry_run(
            rule, body.dataset_id, body.actor, body.sample_mode, filt,
            body.specific_ids, body.record_id_field,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    rule.dataset_id = rule.dataset_id or body.dataset_id
    rule.last_dry_run_id = result.id
    if rule.status == RuleStatus.VALIDATED:
        rule.status = RuleStatus.DRY_RUN_COMPLETED
    rule_store.upsert_rule(rule, body.actor)
    audit_service.log(body.actor, "DRY_RUN", role=body.role.value, rule_id=rule_id,
                       dataset_id=body.dataset_id, dry_run_result_id=result.id,
                       detail=f"{result.summary.matched}/{result.summary.total_records} matched")
    return result.model_dump(mode="json")


@router.get("/dry-runs/{dry_run_id}")
async def get_dry_run(dry_run_id: str):
    r = dry_run_service.get_dry_run(dry_run_id)
    if r is None:
        raise HTTPException(404, "dry run not found")
    return r.model_dump(mode="json")


@router.get("/rules/{rule_id}/dry-runs")
async def list_dry_runs(rule_id: str):
    return {"dry_runs": [r.model_dump(mode="json") for r in dry_run_service.list_dry_runs(rule_id)]}


# --------------------------------------------------------------------------
# Impact analysis
# --------------------------------------------------------------------------

class ImpactBody(Actor):
    dataset_id: str
    record_id_field: Optional[str] = None


@router.post("/rules/{rule_id}/impact-analysis")
async def impact_analysis(rule_id: str, body: ImpactBody):
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        result = impact_service.run_impact_analysis(rule, body.dataset_id, body.actor, body.record_id_field)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result.model_dump(mode="json")


# --------------------------------------------------------------------------
# Explanation + diff
# --------------------------------------------------------------------------

@router.get("/rules/{rule_id}/explanation")
async def explanation(rule_id: str):
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    return {"explanation": explain_service.generate_explanation(rule)}


@router.get("/rules/{rule_id}/diff")
async def rule_diff(rule_id: str, against_version: Optional[int] = None):
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    before_rule = None
    before_text = ""
    versions = [v for v in version_service.list_versions() if rule_id in v.rule_ids_changed]
    target_version = against_version or (versions[-1].version if versions else None)
    if target_version:
        before_text = version_service.get_version_yaml_text(target_version) or ""
        before_rule = impact_service._published_rule_from_version(rule_id, target_version)
    after_text = yaml_service.rules_yaml_text()
    return {
        "against_version": target_version,
        "business_logic": diff_service.business_logic_diff(before_rule, rule),
        "enrichment": diff_service.enrichment_diff(before_rule, rule),
        "yaml_diff": diff_service.yaml_diff(before_text, after_text),
    }


# --------------------------------------------------------------------------
# Free-text / AI rule builder
# --------------------------------------------------------------------------

class InterpretBody(BaseModel):
    text: str
    dataset_id: str


@router.post("/free-text/interpret")
async def interpret_free_text(body: InterpretBody):
    rows = dataset_store.get_rows(body.dataset_id)
    if rows is None:
        raise HTTPException(404, "dataset not found")
    schema = infer_schema(rows)
    ref_candidates = {
        f.name: f.latest.columns for f in reference_store.list_files() if f.latest
    }
    result = nlp_parser.interpret(body.text, list(schema.keys()), schema, ref_candidates)
    return {
        "workflow": result.workflow.model_dump(mode="json"),
        "notes": result.notes,
        "confidence": result.confidence,
        "interpreted_summary": result.interpreted_summary,
    }


# --------------------------------------------------------------------------
# Lifecycle: submit / approve / reject / publish / rollback
# --------------------------------------------------------------------------

class TransitionBody(Actor):
    comment: str = ""


@router.post("/rules/{rule_id}/submit")
async def submit_rule(rule_id: str, body: TransitionBody):
    _require(body, "submit")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        rule = rule_store.transition(rule, RuleStatus.PENDING_APPROVAL, body.actor, body.role, body.comment)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit_service.log(body.actor, "SUBMIT", role=body.role.value, rule_id=rule_id, new_version=rule.version)
    return rule.model_dump(mode="json")


@router.post("/rules/{rule_id}/approve")
async def approve_rule(rule_id: str, body: TransitionBody):
    _require(body, "approve")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        rule = rule_store.transition(rule, RuleStatus.APPROVED, body.actor, body.role, body.comment)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit_service.log(body.actor, "APPROVE", role=body.role.value, rule_id=rule_id, new_version=rule.version)
    return rule.model_dump(mode="json")


@router.post("/rules/{rule_id}/reject")
async def reject_rule(rule_id: str, body: TransitionBody):
    _require(body, "reject")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        rule = rule_store.transition(rule, RuleStatus.REJECTED, body.actor, body.role, body.comment)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit_service.log(body.actor, "REJECT", role=body.role.value, rule_id=rule_id, detail=body.comment)
    return rule.model_dump(mode="json")


@router.post("/rules/{rule_id}/publish")
async def publish_rule(rule_id: str, body: TransitionBody):
    _require(body, "publish")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        rule = rule_store.transition(rule, RuleStatus.PUBLISHED, body.actor, body.role, body.comment)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    snapshot = version_service.publish_snapshot(
        created_by=body.actor, description=body.comment or f"Publish {rule.name}",
        rule_ids_changed=[rule_id], dry_run_dataset_id=rule.dataset_id,
        dry_run_result_id=rule.last_dry_run_id, approved_by=body.actor,
    )
    audit_service.log(body.actor, "PUBLISH", role=body.role.value, rule_id=rule_id,
                       new_version=snapshot.version, dataset_id=rule.dataset_id,
                       dry_run_result_id=rule.last_dry_run_id)
    return {"rule": rule.model_dump(mode="json"), "version": snapshot.model_dump(mode="json")}


class RollbackBody(Actor):
    version: int


@router.post("/rollback")
async def rollback(body: RollbackBody):
    _require(body, "rollback")
    try:
        snapshot = version_service.rollback_to(body.version, body.actor)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    audit_service.log(body.actor, "ROLLBACK", role=body.role.value,
                       new_version=snapshot.version, detail=f"rolled back to v{body.version}")
    return snapshot.model_dump(mode="json")


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

@router.get("/versions")
async def list_versions():
    return {"versions": [v.model_dump(mode="json") for v in version_service.list_versions()]}


@router.get("/versions/{version}")
async def get_version(version: int):
    v = version_service.get_version(version)
    if v is None:
        raise HTTPException(404, "version not found")
    return v.model_dump(mode="json")


@router.get("/versions/{version}/yaml")
async def get_version_yaml(version: int):
    text = version_service.get_version_yaml_text(version)
    if text is None:
        raise HTTPException(404, "version not found")
    return {"version": version, "yaml": text}


# --------------------------------------------------------------------------
# YAML inspection (Phase 1 of the build — "inspect before assuming")
# --------------------------------------------------------------------------

@router.get("/yaml/inspect")
async def yaml_inspect():
    return yaml_service.inspect_yaml()


@router.get("/yaml/current")
async def yaml_current():
    return {"yaml": yaml_service.rules_yaml_text()}


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

@router.get("/audit")
async def audit(rule_id: Optional[str] = None, action: Optional[str] = None,
                 actor: Optional[str] = None, limit: int = 200):
    return {"entries": [e.model_dump(mode="json") for e in
                         audit_service.query(rule_id, action, actor, limit)]}
