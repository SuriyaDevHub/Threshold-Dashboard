"""Seed the Rule Designer with one worked example end to end.

There was no pre-existing rules YAML in this repository for the Rule
Designer to inspect (the app's prior "business rules" lived as Python
if/else inside the threshold_analysis calibrators, not as data — see
REQUIREMENTS_DOCUMENT.md). This script creates:

  1. A reference/lookup file (`currency_reference`) — Currency, Risk_Group,
     Threshold, Market_Region — exactly the enrichment example from the
     spec (INPUT -> LOOKUP Currency -> ENRICH Risk_Group/Threshold/
     Market_Region -> CALCULATE -> CONDITION -> OUTCOME).
  2. One demo rule, `FX_DEVIATION_HIGH_RISK`, wired against the real BRV
     trade dataset shape (currency, booked_price, reference_price,
     notional — see app/core/clients/mock.py) so it dry-runs against
     genuine sample data, not fabricated fields.

Run with:  cd backend && .venv/bin/python -m scripts.seed_rule_designer
Safe to re-run — it upserts by name/rule_id.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.rule_designer import reference_store, rule_store, yaml_service
from app.modules.rule_designer.models import (
    Condition, ConditionGroup, DeriveSpec, LookupConfig, LookupFieldMap, LookupType,
    MissingLookupStrategy, NodeType, Operator, OutcomeAction, Rule, ValueRef, Workflow,
    WorkflowEdge, WorkflowNode,
)

CURRENCY_REFERENCE = [
    {"Currency": "USD", "Risk_Group": "LOW", "Threshold": 2.0, "Market_Region": "AMERICAS"},
    {"Currency": "EUR", "Risk_Group": "LOW", "Threshold": 2.5, "Market_Region": "EMEA"},
    {"Currency": "GBP", "Risk_Group": "MEDIUM", "Threshold": 3.0, "Market_Region": "EMEA"},
    {"Currency": "JPY", "Risk_Group": "MEDIUM", "Threshold": 3.5, "Market_Region": "APAC"},
    {"Currency": "CHF", "Risk_Group": "LOW", "Threshold": 2.0, "Market_Region": "EMEA"},
    {"Currency": "AUD", "Risk_Group": "HIGH", "Threshold": 4.0, "Market_Region": "APAC"},
]


def seed_reference_data() -> str:
    f = reference_store.upload_version("currency_reference", CURRENCY_REFERENCE, "seed_script")
    print(f"reference file '{f.name}' ({f.id}) v{f.latest.version} — {f.latest.records} rows")
    return f.id


def build_demo_workflow(reference_file_id: str) -> Workflow:
    nodes = [
        WorkflowNode(id="n_input", type=NodeType.INPUT, label="Trade dataset", position={"x": 0, "y": 0}),
        WorkflowNode(
            id="n_calc_dev", type=NodeType.CALCULATE, label="Deviation %", position={"x": 0, "y": 120},
            calculate=DeriveSpec(
                output_field="Deviation_Pct",
                expression="abs(booked_price - reference_price) / reference_price * 100",
                description="Booked vs. reference price deviation, as a percentage.",
            ),
        ),
        WorkflowNode(
            id="n_lookup_ccy", type=NodeType.LOOKUP, label="Currency reference lookup",
            position={"x": 0, "y": 240},
            lookup=LookupConfig(
                lookup_type=LookupType.EXACT,
                reference_file_id=reference_file_id,
                join_keys=[{"source": "currency", "reference": "Currency"}],
                fields=[
                    LookupFieldMap(source_column="Risk_Group", output_field="Risk_Group"),
                    LookupFieldMap(source_column="Threshold", output_field="Threshold"),
                    LookupFieldMap(source_column="Market_Region", output_field="Market_Region"),
                ],
                missing_strategy=MissingLookupStrategy.DEFAULT,
                default_values={"Risk_Group": "UNKNOWN", "Threshold": 5.0, "Market_Region": "UNKNOWN"},
            ),
        ),
        WorkflowNode(
            id="n_calc_adj", type=NodeType.CALCULATE, label="Adjusted deviation",
            position={"x": 0, "y": 360},
            calculate=DeriveSpec(output_field="Adjusted_Deviation", expression="Deviation_Pct / Threshold"),
        ),
        WorkflowNode(
            id="n_condition", type=NodeType.CONDITION, label="High-risk FX deviation",
            position={"x": 0, "y": 480},
            condition=ConditionGroup(operator="AND", children=[
                Condition(field="currency", operator=Operator.EQ, value=ValueRef(type="static", value="USD")),
                Condition(field="Deviation_Pct", operator=Operator.GT, value=ValueRef(type="column", name="Threshold")),
                Condition(field="notional", operator=Operator.GT, value=ValueRef(type="static", value=1_000_000)),
            ]),
        ),
        WorkflowNode(
            id="n_outcome", type=NodeType.OUTCOME, label="Flag as high risk",
            position={"x": 0, "y": 600},
            outcomes=[
                OutcomeAction(field="Alert", value=ValueRef(type="static", value=True)),
                OutcomeAction(field="Risk_Level", value=ValueRef(type="static", value="HIGH")),
                OutcomeAction(field="Reason", value=ValueRef(type="static", value="Deviation exceeds applicable threshold")),
            ],
        ),
    ]
    edges = [
        WorkflowEdge(source="n_input", target="n_calc_dev"),
        WorkflowEdge(source="n_calc_dev", target="n_lookup_ccy"),
        WorkflowEdge(source="n_lookup_ccy", target="n_calc_adj"),
        WorkflowEdge(source="n_calc_adj", target="n_condition"),
        WorkflowEdge(source="n_condition", target="n_outcome"),
    ]
    return Workflow(nodes=nodes, edges=edges)


def seed_rule(reference_file_id: str) -> Rule:
    rule = Rule(
        rule_id="FX_DEVIATION_HIGH_RISK",
        name="USD FX Deviation — High Risk",
        description=(
            "USD trades whose booked price deviates from the market reference price by "
            "more than the currency's applicable threshold (looked up from the currency "
            "reference file) are classified as HIGH risk, provided notional exceeds 1,000,000."
        ),
        priority=10,
        workflow=build_demo_workflow(reference_file_id),
        required_columns=["trade_id", "currency", "booked_price", "reference_price", "notional"],
        created_by="seed_script", updated_by="seed_script",
        notes="Seeded worked example — see scripts/seed_rule_designer.py.",
    )
    rule_store.upsert_rule(rule, "seed_script")
    print(f"rule '{rule.rule_id}' seeded, status={rule.status.value}")
    return rule


if __name__ == "__main__":
    ref_id = seed_reference_data()
    seed_rule(ref_id)
    print("\n--- rules/business_rules.yml ---")
    print(yaml_service.rules_yaml_text())
