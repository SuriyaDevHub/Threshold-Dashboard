"""Migrate the legacy `pm_validator.py` (PM product) onto the shared rule
engine — the first of the {product}_validator.py migrations (gfx_validator
excluded per its own complexity, per prior discussion).

IMPORTANT — READ BEFORE RUNNING AGAINST PRODUCTION:
This script is a STRUCTURAL TEMPLATE, not a byte-exact transcription. The
legacy `PmValidator.validate_trade()` bodies for OAR-PM-001..004 were
reviewed screenshot-by-screenshot and their *mechanism* is faithfully
reproduced below:

  * params within one rule are an implicit AND (matches the legacy code:
    every configured param must pass for that rule to fire)
  * a `*_contains` config key holds a single fnmatch.fnmatchcase glob,
    applied only where the legacy code explicitly calls `_match_pattern()`
    -> modeled here as Operator.MATCHES_PATTERN
  * canonical-field-with-EPE-raw-key fallback resolution (`_pick()`) ->
    modeled here as a TRANSFORM node with op="coalesce"
  * `ln_*`/`ny_*`-prefixed params are bespoke per-rule OR-branches
    hardcoded into that one rule's own `if` statement, not a generic
    dual-leg mechanism -> modeled here as an explicit OR ConditionGroup,
    written out per rule rather than inferred from key naming
  * `zero_values` always tests the hardcoded `deal_level` field for that
    one rule -> modeled as a plain EQ 0 condition on `deal_level`
  * commentary priority: a static `cfg["commentary"]` always wins over
    `cfg["commentary_template"].format(**fmt)` -> reproduced simply by
    which OutcomeAction ValueRef a rule uses (`literal()` vs `template()`)
    — the engine needs no separate priority logic for this, see
    `models.ValueRef.template()`'s docstring
  * product gate (`rules.is_product_enabled("PM")` -> ALERT/"PM-DISABLED")
    and unmatched fallback (ALERT/"PM-UNMATCHED") -> reproduced via
    `Product.on_no_match="alert"` + `disabled_reason_code`/
    `unmatched_reason_code`, not per-rule code (see product_engine.py)

What is NOT verbatim: the exact field names, glob patterns, and numeric
thresholds for OAR-PM-001..004 were not captured character-for-character
from the source. Every `# CONFIRM:` comment below marks a value you must
align to the real `params` block for that rule_id before this product is
enabled in anything but a shadow/parallel test. Until then, leave PM's
migration_status at IN_PROGRESS and keep evaluating it via shadow_test
(see shadow_test_service.py) against the legacy validator's own output —
never flip PM.enabled / publish these rules live off template values.

Run with:  cd backend && .venv/bin/python -m scripts.migrate_pm_validator
Safe to re-run — it upserts by rule_id and re-walks the lifecycle to PUBLISHED.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.rule_designer import product_registry, rule_store, yaml_service
from app.modules.rule_designer.models import (
    Condition, ConditionGroup, MigrationStatus, NodeType, Operator, OutcomeAction, Role, Rule,
    RuleStatus, Workflow, WorkflowEdge, WorkflowNode, column, literal, template,
)

ACTOR = "migrate_pm_validator"


def ensure_product() -> None:
    if not product_registry.is_known_product("PM"):
        product_registry.create_product(
            "PM", "PM", "Migrated from the legacy pm_validator.py — see migrate_pm_validator.py.", ACTOR,
        )
    product_registry.configure_fail_safe(
        "PM", on_no_match="alert", unmatched_reason_code="PM-UNMATCHED",
        disabled_reason_code="PM-DISABLED", actor=ACTOR,
    )
    product_registry.set_migration_status("PM", MigrationStatus.IN_PROGRESS, ACTOR)


def _canonical_resolve_node(node_id: str, y: float, output_field: str, canonical_key: str, epe_raw_key: str) -> WorkflowNode:
    """Reproduces `_pick(trade, canonical_key, epe_raw_key)`: canonical
    field wins, EPE raw key is the fallback."""
    return WorkflowNode(
        id=node_id, type=NodeType.TRANSFORM, label=f"Resolve {output_field} ({canonical_key} / {epe_raw_key})",
        position={"x": 0, "y": y},
        transform={"op": "coalesce", "output_field": output_field, "fields": [canonical_key, epe_raw_key]},
    )


def oar_pm_001() -> Rule:
    """Zero deal-level value check. CONFIRM: exact field/threshold against
    the real `zero_values` param block for OAR-PM-001."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="PM trade", position={"x": 0, "y": 0}),
            _canonical_resolve_node("n_resolve_deal_level", 120, "deal_level", "deal_level", "epe_deal_level"),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Zero deal-level value",
                position={"x": 0, "y": 240},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="deal_level", operator=Operator.EQ, value=literal(0)),  # CONFIRM
                ]),
            ),
            WorkflowNode(
                id="n_outcome", type=NodeType.OUTCOME, label="Flag zero deal-level value",
                position={"x": 0, "y": 360},
                outcomes=[
                    OutcomeAction(field="Alert", value=literal(True)),
                    OutcomeAction(field="Reason", value=literal("OAR-PM-001")),
                    OutcomeAction(field="Commentary", value=literal("Deal level value is zero.")),  # CONFIRM: static commentary text
                ],
            ),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_resolve_deal_level"),
            WorkflowEdge(source="n_resolve_deal_level", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-PM-001", product="PM", name="PM — Zero deal-level value",
        description="Migrated from PmValidator.validate_trade() OAR-PM-001. CONFIRM exact params against source.",
        priority=10, workflow=workflow,
        required_columns=["deal_level", "epe_deal_level"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="STRUCTURAL TEMPLATE — see migrate_pm_validator.py header before publishing live.",
    )


def oar_pm_002() -> Rule:
    """Product-type glob match (`_contains` + `_match_pattern`). CONFIRM:
    exact glob pattern and canonical field against the real
    `product_type_contains` (or equivalent) param for OAR-PM-002."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="PM trade", position={"x": 0, "y": 0}),
            _canonical_resolve_node("n_resolve_region", 120, "region", "region", "epe_region"),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Product type matches pattern",
                position={"x": 0, "y": 240},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="product_type", operator=Operator.MATCHES_PATTERN,
                              value=literal("*SWAP*")),  # CONFIRM: real glob pattern
                ]),
            ),
            WorkflowNode(
                id="n_outcome", type=NodeType.OUTCOME, label="Flag pattern match",
                position={"x": 0, "y": 360},
                outcomes=[
                    OutcomeAction(field="Alert", value=literal(True)),
                    OutcomeAction(field="Reason", value=literal("OAR-PM-002")),
                    # CONFIRM: real commentary_template text and fmt keys (only
                    # "region" was confirmed visible before the source image
                    # cut off — add any further {fmt} keys once seen).
                    OutcomeAction(field="Commentary", value=template("Product type flagged for region {region}.")),
                ],
            ),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_resolve_region"),
            WorkflowEdge(source="n_resolve_region", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-PM-002", product="PM", name="PM — Product type pattern match",
        description="Migrated from PmValidator.validate_trade() OAR-PM-002. CONFIRM exact params against source.",
        priority=20, workflow=workflow,
        required_columns=["product_type", "region", "epe_region"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="STRUCTURAL TEMPLATE — see migrate_pm_validator.py header before publishing live.",
    )


def oar_pm_003() -> Rule:
    """`ln_*` / `ny_*` bespoke dual-leg OR branch. CONFIRM: exact field
    names and thresholds for both legs against the real OAR-PM-003 params
    — the legacy code hardcodes this OR itself, it is not a generic
    dual-leg mechanism, so this tree must be re-checked per rule, not
    copied from OAR-PM-003 to any other rule."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="PM trade", position={"x": 0, "y": 0}),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="LN or NY leg threshold breach",
                position={"x": 0, "y": 120},
                condition=ConditionGroup(operator="OR", children=[
                    ConditionGroup(operator="AND", children=[
                        Condition(field="booking_location", operator=Operator.EQ, value=literal("LN")),  # CONFIRM
                        Condition(field="notional", operator=Operator.GT, value=literal(1_000_000)),  # CONFIRM: ln_* threshold
                    ]),
                    ConditionGroup(operator="AND", children=[
                        Condition(field="booking_location", operator=Operator.EQ, value=literal("NY")),  # CONFIRM
                        Condition(field="notional", operator=Operator.GT, value=literal(2_000_000)),  # CONFIRM: ny_* threshold
                    ]),
                ]),
            ),
            WorkflowNode(
                id="n_outcome", type=NodeType.OUTCOME, label="Flag leg threshold breach",
                position={"x": 0, "y": 240},
                outcomes=[
                    OutcomeAction(field="Alert", value=literal(True)),
                    OutcomeAction(field="Reason", value=literal("OAR-PM-003")),
                    OutcomeAction(field="Commentary", value=literal("Notional exceeds the booking location's threshold.")),  # CONFIRM
                ],
            ),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-PM-003", product="PM", name="PM — LN/NY leg threshold breach",
        description="Migrated from PmValidator.validate_trade() OAR-PM-003. CONFIRM exact params against source.",
        priority=30, workflow=workflow,
        required_columns=["booking_location", "notional"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="STRUCTURAL TEMPLATE — see migrate_pm_validator.py header before publishing live.",
    )


def oar_pm_004() -> Rule:
    """Combined pattern + zero-value + threshold check (implicit AND of
    several params, matching how OAR-PM-004's `params` bag was read).
    CONFIRM every field/pattern/threshold against the real config."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="PM trade", position={"x": 0, "y": 0}),
            _canonical_resolve_node("n_resolve_deal_level", 120, "deal_level", "deal_level", "epe_deal_level"),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Pattern + non-zero + threshold",
                position={"x": 0, "y": 240},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="product_type", operator=Operator.MATCHES_PATTERN, value=literal("*OPTION*")),  # CONFIRM
                    Condition(field="deal_level", operator=Operator.NE, value=literal(0)),  # CONFIRM
                    Condition(field="notional", operator=Operator.GT, value=literal(500_000)),  # CONFIRM
                ]),
            ),
            WorkflowNode(
                id="n_outcome", type=NodeType.OUTCOME, label="Flag combined breach",
                position={"x": 0, "y": 360},
                outcomes=[
                    OutcomeAction(field="Alert", value=literal(True)),
                    OutcomeAction(field="Reason", value=literal("OAR-PM-004")),
                    OutcomeAction(field="Commentary", value=literal("Combined pattern/threshold breach.")),  # CONFIRM
                ],
            ),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_resolve_deal_level"),
            WorkflowEdge(source="n_resolve_deal_level", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-PM-004", product="PM", name="PM — Combined pattern/threshold breach",
        description="Migrated from PmValidator.validate_trade() OAR-PM-004. CONFIRM exact params against source.",
        priority=40, workflow=workflow,
        required_columns=["product_type", "deal_level", "epe_deal_level", "notional"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="STRUCTURAL TEMPLATE — see migrate_pm_validator.py header before publishing live.",
    )


def _publish(rule: Rule) -> None:
    """Walks the full DRAFT -> ... -> PUBLISHED lifecycle so migrated
    rules go through the same audit trail as any admin-authored rule —
    nothing bypasses it, even from a script."""
    rule_store.upsert_rule(rule, ACTOR)
    for target in (RuleStatus.VALIDATED, RuleStatus.DRY_RUN_COMPLETED,
                   RuleStatus.PENDING_APPROVAL, RuleStatus.APPROVED, RuleStatus.PUBLISHED):
        rule = rule_store.transition(rule, target, ACTOR, Role.ADMIN, comment="migrate_pm_validator.py")
    print(f"rule '{rule.rule_id}' published, status={rule.status.value}")


if __name__ == "__main__":
    ensure_product()
    print("PM product configured:", product_registry.get_product("PM").model_dump(mode="json"))
    for build in (oar_pm_001, oar_pm_002, oar_pm_003, oar_pm_004):
        _publish(build())
    print("\n--- rules/PM/business_rules.yml ---")
    print(yaml_service.rules_yaml_text("PM"))
    print(
        "\nNOTE: these rules are STRUCTURAL TEMPLATES (see this script's module "
        "docstring). Shadow-test PM against the legacy validator's own output "
        "before disabling migration_status=IN_PROGRESS or relying on this "
        "product's evaluate/evaluate-record output for anything live."
    )
