"""Enterprise Business Rule Designer — plugin module.

A "Business Rule Studio": visual + free-text rule authoring compiling into
one canonical Workflow model, pre-processing/enrichment (lookup) design,
dry-run with record-level explainability, before/after impact analysis,
review/approval/publish lifecycle, per-product YAML versioning, and a
full audit trail — all sitting on top of the existing Data Fetch dataset
store so rules can be tested against real pulled datasets.

Every rule belongs to a product (spec: rules are authored "based on the
product"); every product owns its own YAML file, its own version history,
and an admin-only enable/disable kill switch (spec: "enable or disable
the complete product's rules"). `product_engine.evaluate_product()` is
the generic, product-agnostic validator a migrated `{product}_validator.py`
should end up calling.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import store as dataset_store
from app.modules.rule_designer import (
    audit_service, diff_service, dry_run_service, explain_service, impact_service,
    nlp_parser, product_engine, product_registry, reference_store, rule_store, shadow_test_service,
    validation_service, version_service, yaml_service,
)
from app.modules.rule_designer.models import (
    LEGAL_TRANSITIONS, ROLE_ALLOWED_ACTIONS, ConditionGroup, MigrationStatus, NodeType, Operator,
    OPERATORS_BY_TYPE, Role, Rule, RuleStatus, FieldType,
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
# Access control helper. Two roles (Admin/User), matching the production
# login. The caller states who they are and what role they're acting as —
# this app has no login of its own. When this module sits behind the real
# auth module, replace `Actor` with a FastAPI dependency that derives
# {actor, role} from the session/JWT server-side instead of trusting the
# client-supplied value; every endpoint below already takes `actor`/`role`
# as a single object, so that's a swap at the dependency, not a rewrite of
# every route.
# --------------------------------------------------------------------------

class Actor(BaseModel):
    actor: str = "unknown"
    role: Role = Role.USER


def _require(actor: Actor, action: str) -> None:
    allowed = ROLE_ALLOWED_ACTIONS.get(actor.role, [])
    if action not in allowed:
        raise HTTPException(403, f"role '{actor.role.value}' cannot '{action}' (allowed: {allowed})")


def _require_product(product: str) -> None:
    if not product_registry.is_known_product(product):
        raise HTTPException(404, f"product '{product}' is not registered")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@router.get("/dashboard")
async def dashboard(product: Optional[str] = None):
    rules = rule_store.list_rules(product)
    by_status: Dict[str, int] = {}
    by_product: Dict[str, int] = {}
    for r in rules:
        by_status[r.status.value] = by_status.get(r.status.value, 0) + 1
        by_product[r.product] = by_product.get(r.product, 0) + 1
    recent = sorted(rules, key=lambda r: r.updated_at, reverse=True)[:8]
    recent_audit = audit_service.query(limit=10)
    products = product_registry.list_products()
    return {
        "total_rules": len(rules),
        "published": by_status.get("PUBLISHED", 0),
        "draft": by_status.get("DRAFT", 0),
        "pending_approval": by_status.get("PENDING_APPROVAL", 0),
        "by_status": by_status,
        "by_product": by_product,
        "products": [{"code": p.code, "name": p.name, "enabled": p.enabled,
                       "migration_status": p.migration_status.value,
                       "rule_count": by_product.get(p.code, 0)} for p in products],
        "recent_rules": [{"rule_id": r.rule_id, "product": r.product, "name": r.name,
                           "status": r.status.value, "updated_at": r.updated_at,
                           "updated_by": r.updated_by} for r in recent],
        "recent_activity": [e.model_dump() for e in recent_audit],
        "reference_files": len(reference_store.list_files()),
        "versions_published": len(version_service.list_all_versions()),
    }


# --------------------------------------------------------------------------
# Products — the unit rules are scoped by, and the admin enable/disable
# kill switch for a whole product's rule set.
# --------------------------------------------------------------------------

@router.get("/products")
async def list_products():
    return {"products": [p.model_dump(mode="json") for p in product_registry.list_products()]}


@router.get("/products/{code}")
async def get_product(code: str):
    p = product_registry.get_product(code)
    if p is None:
        raise HTTPException(404, "product not found")
    active_rules = product_engine.active_rules_for_product(code)
    return {**p.model_dump(mode="json"), "active_rule_count": len(active_rules)}


class CreateProductBody(Actor):
    code: str
    name: str
    description: str = ""


@router.post("/products")
async def create_product(body: CreateProductBody):
    _require(body, "manage_products")
    try:
        p = product_registry.create_product(body.code, body.name, body.description, body.actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit_service.log(body.actor, "CREATE", role=body.role.value, detail=f"created product '{p.code}'")
    return p.model_dump(mode="json")


class ProductEnabledBody(Actor):
    enabled: bool


@router.post("/products/{code}/enabled")
async def set_product_enabled(code: str, body: ProductEnabledBody):
    _require(body, "manage_products")
    try:
        p = product_registry.set_enabled(code, body.enabled, body.actor)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    audit_service.log(body.actor, "EDIT", role=body.role.value,
                       detail=f"{'enabled' if body.enabled else 'DISABLED'} product '{code}' "
                              f"({'fail-safe: all records now alert' if not body.enabled else 'rule engine active'})")
    return p.model_dump(mode="json")


class ProductMigrationBody(Actor):
    migration_status: MigrationStatus


@router.post("/products/{code}/migration-status")
async def set_product_migration_status(code: str, body: ProductMigrationBody):
    _require(body, "manage_products")
    try:
        p = product_registry.set_migration_status(code, body.migration_status, body.actor)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return p.model_dump(mode="json")


class EvaluateProductBody(Actor):
    dataset_id: str
    record_id_field: Optional[str] = None


@router.post("/products/{code}/evaluate")
async def evaluate_product(code: str, body: EvaluateProductBody):
    """The generic_validator entry point: every active rule for this
    product, evaluated together, fail-safe if disabled or empty."""
    _require(body, "dry_run")
    try:
        result = product_engine.evaluate_product(code, body.dataset_id, body.actor, body.record_id_field)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result.model_dump(mode="json")


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
        "migration_statuses": [m.value for m in MigrationStatus],
    }


# --------------------------------------------------------------------------
# Rules CRUD
# --------------------------------------------------------------------------

@router.get("/rules")
async def list_rules(product: Optional[str] = None):
    return {"rules": [r.model_dump(mode="json") for r in rule_store.list_rules(product)]}


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
    product = body.rule.get("product")
    if not product:
        raise HTTPException(400, "rule.product is required")
    _require_product(product)
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
    body.rule.setdefault("product", existing.product)
    if body.rule["product"] != existing.product:
        raise HTTPException(400, "a rule's product cannot be changed after creation")
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


class EnabledBody(Actor):
    enabled: bool


@router.post("/rules/{rule_id}/enabled")
async def set_rule_enabled(rule_id: str, body: EnabledBody):
    """The admin's instant on/off switch for a single published rule —
    distinct from the draft/publish lifecycle (spec: "enable or disable
    rule")."""
    _require(body, "manage_rules")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    rule = rule_store.set_enabled(rule, body.enabled, body.actor)
    audit_service.log(body.actor, "EDIT", role=body.role.value, rule_id=rule_id,
                       detail=f"{'enabled' if body.enabled else 'disabled'} rule")
    return rule.model_dump(mode="json")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@router.post("/rules/{rule_id}/validate")
async def validate_rule_endpoint(rule_id: str, actor: str = "unknown", role: Role = Role.ADMIN):
    _require(Actor(actor=actor, role=role), "edit")
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
    _require(body, "dry_run")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        result = impact_service.run_impact_analysis(rule, body.dataset_id, body.actor, body.record_id_field)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result.model_dump(mode="json")


# --------------------------------------------------------------------------
# Shadow / parallel test — new engine vs. an already-produced legacy
# validator output (spec: migration off hardcoded {product}_validator.py)
# --------------------------------------------------------------------------

class ShadowTestBody(Actor):
    dataset_id: str
    record_id_field: str
    legacy_csv_text: str
    legacy_alert_values: List[str]
    record_id_col: str = "record_id"
    status_col: str = "status"
    reason_col: Optional[str] = "reason_code"
    commentary_col: Optional[str] = "commentary"


@router.post("/rules/{rule_id}/shadow-test")
async def shadow_test(rule_id: str, body: ShadowTestBody):
    _require(body, "dry_run")
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    try:
        legacy_rows = shadow_test_service.parse_legacy_csv(
            body.legacy_csv_text, body.record_id_col, body.status_col, body.reason_col, body.commentary_col,
        )
        if not legacy_rows:
            raise HTTPException(400, "no legacy rows parsed — check record_id_col/status_col")
        result = shadow_test_service.run_shadow_test(
            rule, body.dataset_id, legacy_rows, body.legacy_alert_values, body.actor, body.record_id_field,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit_service.log(body.actor, "DRY_RUN", role=body.role.value, rule_id=rule_id,
                       dataset_id=body.dataset_id,
                       detail=f"shadow test vs legacy: {result.summary.agreement_rate_pct}% agreement, "
                              f"{result.summary.legacy_only} legacy-only, {result.summary.new_only} new-only")
    return result.model_dump(mode="json")


@router.get("/shadow-tests/{shadow_id}")
async def get_shadow_test(shadow_id: str):
    r = shadow_test_service.get_shadow_test(shadow_id)
    if r is None:
        raise HTTPException(404, "shadow test not found")
    return r.model_dump(mode="json")


@router.get("/rules/{rule_id}/shadow-tests")
async def list_shadow_tests(rule_id: str):
    return {"shadow_tests": [r.model_dump(mode="json") for r in shadow_test_service.list_shadow_tests(rule_id)]}


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
    versions = [v for v in version_service.list_versions(rule.product) if rule_id in v.rule_ids_changed]
    target_version = against_version or (versions[-1].version if versions else None)
    if target_version:
        before_text = version_service.get_version_yaml_text(rule.product, target_version) or ""
        before_rule = impact_service._published_rule_from_version(rule.product, rule_id, target_version)
    after_text = yaml_service.rules_yaml_text(rule.product)
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
        product=rule.product, created_by=body.actor, description=body.comment or f"Publish {rule.name}",
        rule_ids_changed=[rule_id], dry_run_dataset_id=rule.dataset_id,
        dry_run_result_id=rule.last_dry_run_id, approved_by=body.actor,
    )
    audit_service.log(body.actor, "PUBLISH", role=body.role.value, rule_id=rule_id,
                       new_version=snapshot.version, dataset_id=rule.dataset_id,
                       dry_run_result_id=rule.last_dry_run_id)
    return {"rule": rule.model_dump(mode="json"), "version": snapshot.model_dump(mode="json")}


class RollbackBody(Actor):
    product: str
    version: int


@router.post("/rollback")
async def rollback(body: RollbackBody):
    _require(body, "rollback")
    try:
        snapshot = version_service.rollback_to(body.product, body.version, body.actor)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    audit_service.log(body.actor, "ROLLBACK", role=body.role.value,
                       new_version=snapshot.version, detail=f"{body.product} rolled back to v{body.version}")
    return snapshot.model_dump(mode="json")


# --------------------------------------------------------------------------
# Versions (per product, plus a cross-product view)
# --------------------------------------------------------------------------

@router.get("/versions")
async def list_versions(product: Optional[str] = None):
    if product:
        return {"versions": [v.model_dump(mode="json") for v in version_service.list_versions(product)]}
    return {"versions": [v.model_dump(mode="json") for v in version_service.list_all_versions()]}


@router.get("/products/{product}/versions/{version}")
async def get_version(product: str, version: int):
    v = version_service.get_version(product, version)
    if v is None:
        raise HTTPException(404, "version not found")
    return v.model_dump(mode="json")


@router.get("/products/{product}/versions/{version}/yaml")
async def get_version_yaml(product: str, version: int):
    text = version_service.get_version_yaml_text(product, version)
    if text is None:
        raise HTTPException(404, "version not found")
    return {"product": product, "version": version, "yaml": text}


# --------------------------------------------------------------------------
# YAML inspection (Phase 1 of the build — "inspect before assuming")
# --------------------------------------------------------------------------

@router.get("/yaml/inspect")
async def yaml_inspect(product: Optional[str] = None):
    if product:
        return yaml_service.inspect_yaml(product)
    return {"products": {p.code: yaml_service.inspect_yaml(p.code) for p in product_registry.list_products()}}


@router.get("/yaml/current")
async def yaml_current(product: str):
    _require_product(product)
    return {"product": product, "yaml": yaml_service.rules_yaml_text(product)}


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

@router.get("/audit")
async def audit(rule_id: Optional[str] = None, action: Optional[str] = None,
                 actor: Optional[str] = None, limit: int = 200):
    return {"entries": [e.model_dump(mode="json") for e in
                         audit_service.query(rule_id, action, actor, limit)]}
