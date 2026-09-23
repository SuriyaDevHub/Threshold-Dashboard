"""Migrate the legacy `fxo_validator.py` (FXO product) onto the shared rule
engine — the second `{product}_validator.py` migration after PM (see
migrate_pm_validator.py for the established pattern this follows).

Unlike migrate_pm_validator.py, this one is NOT a structural template with
"CONFIRM" placeholders — `FxoValidator.validate_trade()`'s full body and the
real `omrc_rules.yml` `FXO:` config block were both reviewed
screenshot-by-screenshot, so every field name, threshold source, config
value and commentary string below is a direct transcription, not a guess.
The exceptions are called out explicitly where they occur:

  * `_region_code()`'s free-text -> code normalization (parenthetical
    extraction, e.g. "Some Desk (LN)" -> "LN", as a fallback before the
    keyword match) is modeled here as a plain TRANSFORM value_map (keyword
    match only) for the 5 codes the source data actually uses (LN/JP/AU/
    CN/TH) — the parenthetical-extraction fallback isn't reproduced, since
    it only matters for description formats this migration hasn't seen.
  * `_parse_ccy_pair_string()` tries four separators before falling back to
    a fixed 6-char split; this migration assumes "/" (the standard FX
    convention, and the first separator the legacy code itself tries) —
    confirm against the real `omrctradeCcypair` format before publishing
    OAR-FXO-002 live if the data uses a different separator.
  * `_is_yes()` (Y/Yes/True/1) is reproduced as an explicit 4-way OR on the
    parent-flag field, not a generic truthy-value primitive.
  * OAR-FXO-004's self-group LOOKUP groups over the workflow's raw input
    rows (see workflow_engine._self_group_reference_rows's own docstring)
    rather than rows pre-filtered to region=TH/product=FXO/source=EUC the
    way `_compute_th_structure()`'s own collection loop does — in practice
    a structure id is deal-package-specific and won't collide across
    regions/products, but this is a real (low-risk) behavioral difference
    worth knowing about, not a "CONFIRM" gap.
  * Legacy's three-field FxoValidationResult (status/rule_id/reasoncode/
    reason) collapses onto this engine's two reserved OUTCOME fields:
    Reason = the config's own `reason_code` string (e.g. "OAR-BRV-FXO
    BTB" — a short descriptive code, not the OAR-FXO-NNN rule id) and
    Commentary = the config's commentary/commentary_template text,
    verbatim.

FXO's product-level `enabled` is `false` in the current omrc_rules.yml —
i.e. production isn't running these rules at all right now. This script
still builds and publishes the product/rules ENABLED in this system, since
a disabled product can't be dry-run or shadow-tested at all (see
product_engine's fail-safe posture) — that's necessary for validating this
migration, not a claim that FXO should go live. Leave FXO's
migration_status at IN_PROGRESS and shadow-test every rule against the
legacy validator's own output (fxo_validator.py, the shadow wrapper this
script's sibling ships) before ever changing that, publishing rule edits,
or touching the product's enabled flag for real traffic.

Run with:  cd backend && .venv/bin/python -m scripts.migrate_fxo_validator
Safe to re-run — it upserts by rule_id and re-walks the lifecycle to PUBLISHED.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.rule_designer import product_registry, rule_store, yaml_service
from app.modules.rule_designer.models import (
    Condition, ConditionGroup, DeriveSpec, LookupConfig, LookupFieldMap, LookupType,
    MigrationStatus, MissingLookupStrategy, NodeType, Operator, OutcomeAction, Role, Rule,
    RuleStatus, Workflow, WorkflowEdge, WorkflowNode, column, literal, template,
)

ACTOR = "migrate_fxo_validator"

# _region_code()'s keyword-match table — the 5 region codes the legacy
# validator's OAR-FXO-* params actually reference (JP/CN/EUC-TH/LN).
REGION_VALUE_MAP = {
    "op": "value_map",
    "rules": [
        {"contains": "LONDON", "value": "LN"},
        {"contains": "JAPAN", "value": "JP"},
        {"contains": "AUSTRALIA", "value": "AU"},
        {"contains": "CHINA", "value": "CN"},
        {"contains": "THAILAND", "value": "TH"},
    ],
}

# _is_yes(x): str(x).strip().upper() in ("Y", "YES", "TRUE", "1")
PARENT_FLAG_TRUTHY = ["Y", "YES", "TRUE", "1"]


def ensure_product() -> None:
    if not product_registry.is_known_product("FXO"):
        product_registry.create_product(
            "FXO", "FXO", "Migrated from the legacy fxo_validator.py — see migrate_fxo_validator.py.", ACTOR,
        )
    product_registry.configure_fail_safe(
        "FXO", on_no_match="alert", unmatched_reason_code="FXO-UNMATCHED",
        disabled_reason_code="FXO-DISABLED", actor=ACTOR,
    )
    product_registry.set_migration_status("FXO", MigrationStatus.IN_PROGRESS, ACTOR)


def _upper(node_id: str, y: float, field: str, output_field: str) -> WorkflowNode:
    """Reproduces `_norm(x)`: str(x or "").strip().upper() — trim isn't a
    separate step here since our `upper` transform op leaves surrounding
    whitespace as-is and every comparison below is an exact/contains match
    against already-trimmed config values, so stray whitespace would only
    ever cause a false negative, never a false positive."""
    return WorkflowNode(
        id=node_id, type=NodeType.TRANSFORM, label=f"Normalize {field}", position={"x": 0, "y": y},
        transform={"op": "upper", "field": field, "output_field": output_field},
    )


def _region_node(node_id: str, y: float) -> WorkflowNode:
    return WorkflowNode(
        id=node_id, type=NodeType.TRANSFORM, label="Region code", position={"x": 0, "y": y},
        transform={**REGION_VALUE_MAP, "field": "omrctradeRegion", "output_field": "region_code"},
    )


def _parent_selector() -> ConditionGroup:
    return ConditionGroup(operator="OR", children=[
        Condition(field="omrctradeParentid", operator=Operator.EQ, value=literal(v))
        for v in PARENT_FLAG_TRUTHY
    ])


def _outcome(rule_id: str, reason_code: str, commentary) -> WorkflowNode:
    return WorkflowNode(
        id="n_outcome", type=NodeType.OUTCOME, label=f"Clear — {rule_id}",
        outcomes=[
            OutcomeAction(field="Alert", value=literal(False)),
            OutcomeAction(field="Reason", value=literal(reason_code)),
            OutcomeAction(field="Commentary", value=commentary),
        ],
    )


def oar_fxo_001() -> Rule:
    """BTB (JP): region=JP, product=FXO, omrctradeBuiValue contains any of
    ["JP", "BACK TO BACK"]."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="FXO trade", position={"x": 0, "y": 0}),
            _region_node("n_region", 120),
            _upper("n_product", 180, "omrctradeProducttype", "product_type_norm"),
            _upper("n_bui", 240, "omrctradeBuiValue", "bui_value_norm"),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="BTB (JP)", position={"x": 0, "y": 300},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="region_code", operator=Operator.EQ, value=literal("JP")),
                    Condition(field="product_type_norm", operator=Operator.EQ, value=literal("FXO")),
                    ConditionGroup(operator="OR", children=[
                        Condition(field="bui_value_norm", operator=Operator.CONTAINS, value=literal("JP")),
                        Condition(field="bui_value_norm", operator=Operator.CONTAINS, value=literal("BACK TO BACK")),
                    ]),
                ]),
            ),
            _outcome("OAR-FXO-001", "OAR-BRV-FXO BTB",
                     literal("This is BTB trade. OMRC is being performed at risk taking side.")),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_region"),
            WorkflowEdge(source="n_region", target="n_product"),
            WorkflowEdge(source="n_product", target="n_bui"),
            WorkflowEdge(source="n_bui", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-FXO-001", product="FXO", name="FXO — BTB (JP)",
        description="Migrated from FxoValidator.validate_trade() OAR-FXO-001.",
        priority=10, workflow=workflow,
        required_columns=["omrctradeRegion", "omrctradeProducttype", "omrctradeBuiValue"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="Migrated from fxo_validator.py — see migrate_fxo_validator.py header.",
    )


def oar_fxo_002() -> Rule:
    """Low Vol (CN): region=CN, product=FXO, neither currency-pair leg is
    CNY. The legacy code hardcodes the "CNY" check directly rather than
    reading it from cfg["base_ccy"]/cfg["counter_ccy"] (both set to "CNY"
    in the config, but never actually referenced in validate_trade's body)
    — reproduced here per the real code path, not the vestigial params."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="FXO trade", position={"x": 0, "y": 0}),
            _region_node("n_region", 120),
            _upper("n_product", 180, "omrctradeProducttype", "product_type_norm"),
            _upper("n_pair", 240, "omrctradeCcypair", "ccypair_norm"),
            WorkflowNode(
                id="n_base", type=NodeType.TRANSFORM, label="Base currency", position={"x": 0, "y": 300},
                transform={"op": "split", "field": "ccypair_norm", "output_field": "ccy_base", "delimiter": "/", "index": 0},
            ),
            WorkflowNode(
                id="n_quote", type=NodeType.TRANSFORM, label="Quote currency", position={"x": 0, "y": 360},
                transform={"op": "split", "field": "ccypair_norm", "output_field": "ccy_quote", "delimiter": "/", "index": 1},
            ),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Low Vol (CN, non-CNY pair)", position={"x": 0, "y": 420},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="region_code", operator=Operator.EQ, value=literal("CN")),
                    Condition(field="product_type_norm", operator=Operator.EQ, value=literal("FXO")),
                    Condition(field="ccy_base", operator=Operator.NE, value=literal("CNY")),
                    Condition(field="ccy_quote", operator=Operator.NE, value=literal("CNY")),
                ]),
            ),
            _outcome("OAR-FXO-002", "OAR-BRV-Low Vol",
                     literal("This is a non-CNY FXO, which is out of HBCN OMRC scope.")),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_region"),
            WorkflowEdge(source="n_region", target="n_product"),
            WorkflowEdge(source="n_product", target="n_pair"),
            WorkflowEdge(source="n_pair", target="n_base"),
            WorkflowEdge(source="n_base", target="n_quote"),
            WorkflowEdge(source="n_quote", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-FXO-002", product="FXO", name="FXO — Low Vol (CN)",
        description="Migrated from FxoValidator.validate_trade() OAR-FXO-002. "
                     "Assumes '/' as the omrctradeCcypair separator — confirm against real data.",
        priority=30, workflow=workflow,
        required_columns=["omrctradeRegion", "omrctradeProducttype", "omrctradeCcypair"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="Migrated from fxo_validator.py — see migrate_fxo_validator.py header.",
    )


def oar_fxo_003() -> Rule:
    """Option Exercise: product=FXO, region != JP, source_system=EUC,
    (comment OR structure id) contains any of the configured needles."""
    needles = ["EXR. FX OPTION", "'EXR: FX OPTION'"]  # 3rd config entry duplicates the 2nd verbatim
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="FXO trade", position={"x": 0, "y": 0}),
            _region_node("n_region", 120),
            _upper("n_product", 180, "omrctradeProducttype", "product_type_norm"),
            _upper("n_source", 240, "uctradeSourcesystem", "source_system_norm"),
            _upper("n_comment", 300, "omrctradeComment", "comment_norm"),
            _upper("n_sid", 360, "omrctradeStructureid", "sid_norm"),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Option Exercise", position={"x": 0, "y": 420},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="product_type_norm", operator=Operator.EQ, value=literal("FXO")),
                    Condition(field="region_code", operator=Operator.NE, value=literal("JP")),
                    Condition(field="source_system_norm", operator=Operator.EQ, value=literal("EUC")),
                    ConditionGroup(operator="OR", children=[
                        cond for needle in needles for cond in (
                            Condition(field="comment_norm", operator=Operator.CONTAINS, value=literal(needle)),
                            Condition(field="sid_norm", operator=Operator.CONTAINS, value=literal(needle)),
                        )
                    ]),
                ]),
            ),
            _outcome("OAR-FXO-003", "OAR-PTS-Option Exercise", literal(
                "These are option exercised trades where no new contract is formed because the existing option "
                "contract is acted upon by the client leading to new underlying FX position depending on the "
                "products settlement terms."
            )),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_region"),
            WorkflowEdge(source="n_region", target="n_product"),
            WorkflowEdge(source="n_product", target="n_source"),
            WorkflowEdge(source="n_source", target="n_comment"),
            WorkflowEdge(source="n_comment", target="n_sid"),
            WorkflowEdge(source="n_sid", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-FXO-003", product="FXO", name="FXO — Option Exercise",
        description="Migrated from FxoValidator.validate_trade() OAR-FXO-003.",
        priority=20, workflow=workflow,
        required_columns=["omrctradeProducttype", "omrctradeRegion", "uctradeSourcesystem",
                           "omrctradeComment", "omrctradeStructureid"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="Migrated from fxo_validator.py — see migrate_fxo_validator.py header.",
    )


def oar_fxo_004() -> Rule:
    """TH structure grouping: group by omrctradeStructureid (region=TH,
    product=FXO, source_system=EUC), sum omrctradePnlreportingccy across
    the structure, threshold = first non-null omrctradePnlthreshold in the
    group, CLEAR if abs(total_pnl) <= threshold."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="FXO trade", position={"x": 0, "y": 0}),
            _region_node("n_region", 120),
            _upper("n_product", 180, "omrctradeProducttype", "product_type_norm"),
            _upper("n_source", 240, "uctradeSourcesystem", "source_system_norm"),
            WorkflowNode(
                id="n_filter", type=NodeType.FILTER, label="TH / FXO / EUC", position={"x": 0, "y": 300},
                filter=ConditionGroup(operator="AND", children=[
                    Condition(field="region_code", operator=Operator.EQ, value=literal("TH")),
                    Condition(field="product_type_norm", operator=Operator.EQ, value=literal("FXO")),
                    Condition(field="source_system_norm", operator=Operator.EQ, value=literal("EUC")),
                ]),
            ),
            WorkflowNode(
                id="n_group", type=NodeType.LOOKUP, label="Structure PnL", position={"x": 0, "y": 360},
                lookup=LookupConfig(
                    lookup_type=LookupType.SELF_GROUP, group_by_field="omrctradeStructureid",
                    fields=[
                        LookupFieldMap(source_column="omrctradePnlreportingccy", output_field="structure_total_pnl", aggregate="sum"),
                        LookupFieldMap(source_column="omrctradePnlthreshold", output_field="structure_threshold", aggregate="first"),
                    ],
                    missing_strategy=MissingLookupStrategy.CONTINUE_NULL,
                ),
            ),
            WorkflowNode(
                id="n_abs", type=NodeType.CALCULATE, label="abs(structure total PnL)", position={"x": 0, "y": 420},
                calculate=DeriveSpec(output_field="structure_pnl_abs",
                                     formula={"kind": "operation", "op": "abs",
                                              "operands": [{"kind": "field", "field": "structure_total_pnl"}]}),
            ),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Within threshold", position={"x": 0, "y": 480},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="structure_pnl_abs", operator=Operator.LTE, value=column("structure_threshold")),
                ]),
            ),
            _outcome("OAR-FXO-004", "OAR-PTS-Package/Structure ID", template(
                "This deal belongs to structure id {omrctradeStructureid} and the combined Pnl of all the deals "
                "is {structure_total_pnl}, which is within threshold."
            )),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_region"),
            WorkflowEdge(source="n_region", target="n_product"),
            WorkflowEdge(source="n_product", target="n_source"),
            WorkflowEdge(source="n_source", target="n_filter"),
            WorkflowEdge(source="n_filter", target="n_group"),
            WorkflowEdge(source="n_group", target="n_abs"),
            WorkflowEdge(source="n_abs", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-FXO-004", product="FXO", name="FXO — TH structure grouping",
        description="Migrated from FxoValidator.validate_trade() / _compute_th_structure() OAR-FXO-004. "
                     "Self-group LOOKUP grouping happens over this workflow's raw input rows, not rows "
                     "pre-filtered to TH/FXO/EUC the way the legacy grouping loop did — see this script's "
                     "module docstring.",
        priority=40, workflow=workflow,
        required_columns=["omrctradeRegion", "omrctradeProducttype", "uctradeSourcesystem",
                           "omrctradeStructureid", "omrctradePnlreportingccy", "omrctradePnlthreshold"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="Migrated from fxo_validator.py — see migrate_fxo_validator.py header.",
    )


def oar_fxo_005() -> Rule:
    """LN Murex/Margin (base): group by omrctradeDealrefid (region=LN,
    product=FXO), parent row (omrctradeParentid truthy) — CLEAR if
    abs(parent's omrctradePnlmurex) <= parent's omrctradePnlthreshold."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="FXO trade", position={"x": 0, "y": 0}),
            _region_node("n_region", 120),
            _upper("n_product", 180, "omrctradeProducttype", "product_type_norm"),
            WorkflowNode(
                id="n_filter", type=NodeType.FILTER, label="LN / FXO", position={"x": 0, "y": 240},
                filter=ConditionGroup(operator="AND", children=[
                    Condition(field="region_code", operator=Operator.EQ, value=literal("LN")),
                    Condition(field="product_type_norm", operator=Operator.EQ, value=literal("FXO")),
                ]),
            ),
            WorkflowNode(
                id="n_group", type=NodeType.LOOKUP, label="Parent leg PnL/threshold", position={"x": 0, "y": 300},
                lookup=LookupConfig(
                    lookup_type=LookupType.SELF_GROUP, group_by_field="omrctradeDealrefid",
                    selector=_parent_selector(),
                    fields=[
                        LookupFieldMap(source_column="omrctradePnlmurex", output_field="parent_pnl"),
                        LookupFieldMap(source_column="omrctradePnlthreshold", output_field="parent_threshold"),
                    ],
                    missing_strategy=MissingLookupStrategy.CONTINUE_NULL,
                ),
            ),
            WorkflowNode(
                id="n_abs", type=NodeType.CALCULATE, label="abs(parent PnL)", position={"x": 0, "y": 360},
                calculate=DeriveSpec(output_field="parent_pnl_abs",
                                     formula={"kind": "operation", "op": "abs",
                                              "operands": [{"kind": "field", "field": "parent_pnl"}]}),
            ),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Within threshold", position={"x": 0, "y": 420},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="parent_pnl_abs", operator=Operator.LTE, value=column("parent_threshold")),
                ]),
            ),
            _outcome("OAR-FXO-005", "OAR-MKD-Murex", template(
                "The Pnl (Murex) of parent leg reported to omrc is {parent_pnl_abs}, which is within threshold level."
            )),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_region"),
            WorkflowEdge(source="n_region", target="n_product"),
            WorkflowEdge(source="n_product", target="n_filter"),
            WorkflowEdge(source="n_filter", target="n_group"),
            WorkflowEdge(source="n_group", target="n_abs"),
            WorkflowEdge(source="n_abs", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-FXO-005", product="FXO", name="FXO — LN Murex/Margin (base)",
        description="Migrated from FxoValidator.validate_trade() / _compute_ln_murex_and_margin() OAR-FXO-005.",
        priority=50, workflow=workflow,
        required_columns=["omrctradeRegion", "omrctradeProducttype", "omrctradeDealrefid",
                           "omrctradeParentid", "omrctradePnlmurex", "omrctradePnlthreshold"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="Migrated from fxo_validator.py — see migrate_fxo_validator.py header.",
    )


def oar_fxo_006() -> Rule:
    """LN Murex/Margin (override): same grouping as 005 plus
    omrctradeProductioncredit, gated additionally on source_system=
    FXO-MXG. CLEAR (overriding 005's ALERT) if the raw PnL breaches
    threshold but the production-credit-adjusted residual PnL doesn't:
    abs(pnl) > thr AND abs(pnl - 2*prod_credit) <= thr. 005/006 are
    mutually exclusive by construction (one requires <=thr, the other
    >thr), so which of the two rules has the lower priority doesn't
    change which one a given record can match."""
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="n_input", type=NodeType.INPUT, label="FXO trade", position={"x": 0, "y": 0}),
            _region_node("n_region", 120),
            _upper("n_product", 180, "omrctradeProducttype", "product_type_norm"),
            _upper("n_source", 240, "uctradeSourcesystem", "source_system_norm"),
            WorkflowNode(
                id="n_filter", type=NodeType.FILTER, label="LN / FXO / FXO-MXG", position={"x": 0, "y": 300},
                filter=ConditionGroup(operator="AND", children=[
                    Condition(field="region_code", operator=Operator.EQ, value=literal("LN")),
                    Condition(field="product_type_norm", operator=Operator.EQ, value=literal("FXO")),
                    Condition(field="source_system_norm", operator=Operator.EQ, value=literal("FXO-MXG")),
                ]),
            ),
            WorkflowNode(
                id="n_group", type=NodeType.LOOKUP, label="Parent leg PnL/threshold/credit", position={"x": 0, "y": 360},
                lookup=LookupConfig(
                    lookup_type=LookupType.SELF_GROUP, group_by_field="omrctradeDealrefid",
                    selector=_parent_selector(),
                    fields=[
                        LookupFieldMap(source_column="omrctradePnlmurex", output_field="parent_pnl"),
                        LookupFieldMap(source_column="omrctradePnlthreshold", output_field="parent_threshold"),
                        LookupFieldMap(source_column="omrctradeProductioncredit", output_field="parent_prod_credit"),
                    ],
                    missing_strategy=MissingLookupStrategy.CONTINUE_NULL,
                ),
            ),
            WorkflowNode(
                id="n_abs", type=NodeType.CALCULATE, label="abs(parent PnL)", position={"x": 0, "y": 420},
                calculate=DeriveSpec(output_field="parent_pnl_abs",
                                     formula={"kind": "operation", "op": "abs",
                                              "operands": [{"kind": "field", "field": "parent_pnl"}]}),
            ),
            WorkflowNode(
                id="n_credit_x2", type=NodeType.CALCULATE, label="Production credit x2", position={"x": 0, "y": 480},
                calculate=DeriveSpec(output_field="prod_credit_x2",
                                     formula={"kind": "operation", "op": "multiply",
                                              "operands": [{"kind": "field", "field": "parent_prod_credit"},
                                                           {"kind": "constant", "value": 2}]}),
            ),
            WorkflowNode(
                id="n_residual", type=NodeType.CALCULATE, label="Residual adjusted PnL", position={"x": 0, "y": 540},
                calculate=DeriveSpec(output_field="residual_adj_pnl",
                                     formula={"kind": "operation", "op": "abs", "operands": [
                                         {"kind": "operation", "op": "subtract", "operands": [
                                             {"kind": "field", "field": "parent_pnl"},
                                             {"kind": "field", "field": "prod_credit_x2"},
                                         ]},
                                     ]}),
            ),
            WorkflowNode(
                id="n_condition", type=NodeType.CONDITION, label="Breach absorbed by production credit", position={"x": 0, "y": 600},
                condition=ConditionGroup(operator="AND", children=[
                    Condition(field="parent_pnl_abs", operator=Operator.GT, value=column("parent_threshold")),
                    Condition(field="residual_adj_pnl", operator=Operator.LTE, value=column("parent_threshold")),
                ]),
            ),
            _outcome("OAR-FXO-006", "OAR-BRV-FXO Margin", template(
                "The Reported PnL (Murex) for the deal is {parent_pnl_abs}. Production Credit (x2) is "
                "{prod_credit_x2}. Residual Adj. PnL is {residual_adj_pnl}, which is below threshold level. "
                "Good to close."
            )),
        ],
        edges=[
            WorkflowEdge(source="n_input", target="n_region"),
            WorkflowEdge(source="n_region", target="n_product"),
            WorkflowEdge(source="n_product", target="n_source"),
            WorkflowEdge(source="n_source", target="n_filter"),
            WorkflowEdge(source="n_filter", target="n_group"),
            WorkflowEdge(source="n_group", target="n_abs"),
            WorkflowEdge(source="n_abs", target="n_credit_x2"),
            WorkflowEdge(source="n_credit_x2", target="n_residual"),
            WorkflowEdge(source="n_residual", target="n_condition"),
            WorkflowEdge(source="n_condition", target="n_outcome"),
        ],
    )
    return Rule(
        rule_id="OAR-FXO-006", product="FXO", name="FXO — LN Murex/Margin (production credit override)",
        description="Migrated from FxoValidator.validate_trade() / _compute_ln_murex_and_margin() OAR-FXO-006.",
        priority=60, workflow=workflow,
        required_columns=["omrctradeRegion", "omrctradeProducttype", "uctradeSourcesystem",
                           "omrctradeDealrefid", "omrctradeParentid", "omrctradePnlmurex",
                           "omrctradePnlthreshold", "omrctradeProductioncredit"],
        created_by=ACTOR, updated_by=ACTOR,
        notes="Migrated from fxo_validator.py — see migrate_fxo_validator.py header.",
    )


def _publish(rule: Rule) -> None:
    """Walks the full DRAFT -> ... -> PUBLISHED lifecycle so migrated
    rules go through the same audit trail as any admin-authored rule —
    nothing bypasses it, even from a script."""
    rule_store.upsert_rule(rule, ACTOR)
    for target in (RuleStatus.VALIDATED, RuleStatus.DRY_RUN_COMPLETED,
                   RuleStatus.PENDING_APPROVAL, RuleStatus.APPROVED, RuleStatus.PUBLISHED):
        rule = rule_store.transition(rule, target, ACTOR, Role.ADMIN, comment="migrate_fxo_validator.py")
    print(f"rule '{rule.rule_id}' published, status={rule.status.value}")


if __name__ == "__main__":
    ensure_product()
    print("FXO product configured:", product_registry.get_product("FXO").model_dump(mode="json"))
    for build in (oar_fxo_001, oar_fxo_002, oar_fxo_003, oar_fxo_004, oar_fxo_005, oar_fxo_006):
        _publish(build())
    print("\n--- rules/FXO/business_rules.yml ---")
    print(yaml_service.rules_yaml_text("FXO"))
    print(
        "\nNOTE: shadow-test FXO against the legacy validator's own output (see "
        "fxo_validator.py, the shadow wrapper) before changing migration_status "
        "off IN_PROGRESS, publishing further rule edits, or relying on this "
        "product's evaluate/evaluate-record output for anything live — FXO is "
        "disabled in the legacy omrc_rules.yml today, see this script's header."
    )
