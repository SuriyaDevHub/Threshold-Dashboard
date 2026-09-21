"""Pure-Python test suite for the Rule Designer engines and services.

Run with:  cd backend && .venv/bin/python -m pytest tests/test_rule_designer.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.modules.rule_designer import (
    condition_engine, expr_engine, lookup_engine, reference_store, rule_store,
    validation_service, version_service, workflow_engine, yaml_service,
)
from app.modules.rule_designer.models import (
    Condition, ConditionGroup, DeriveSpec, LookupConfig, LookupFieldMap, LookupType,
    MissingLookupStrategy, NodeType, Operator, OutcomeAction, PriorityStrategy, Rule, RuleStatus,
    Role, ValueRef, Workflow, WorkflowEdge, WorkflowNode,
)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Every test gets its own rules/versions/reference_data/audit dirs so
    tests never see each other's state or the real seeded data."""
    rules_dir = tmp_path / "rules"
    monkeypatch.setattr(yaml_service, "RULES_DIR", str(rules_dir))
    monkeypatch.setattr(yaml_service, "HISTORY_DIR", str(rules_dir / "_history"))
    monkeypatch.setattr(yaml_service, "RULES_FILE", str(rules_dir / "business_rules.yml"))

    monkeypatch.setattr(version_service, "VERSIONS_DIR", str(tmp_path / "versions"))
    monkeypatch.setattr(reference_store, "BASE_DIR", str(tmp_path / "reference_data"))

    import app.modules.rule_designer.audit_service as audit_service
    monkeypatch.setattr(audit_service, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(audit_service, "AUDIT_LOG", str(tmp_path / "audit" / "audit_log.jsonl"))
    yield


# --------------------------------------------------------------------------
# expr_engine — safety + correctness
# --------------------------------------------------------------------------

def test_expr_engine_basic_arithmetic():
    expr = expr_engine.parse("Notional * Price")
    assert expr.evaluate({"Notional": 10, "Price": 2.5}) == 25.0


def test_expr_engine_abs_and_division():
    expr = expr_engine.parse("abs(booked - ref) / ref * 100")
    assert round(expr.evaluate({"booked": 98, "ref": 100}), 4) == 2.0


def test_expr_engine_rejects_unsafe_constructs():
    for bad in ["__import__('os').system('ls')", "open('x')", "[].__class__",
                "a.b", "lambda: 1", "1; 2"]:
        with pytest.raises(expr_engine.ExpressionError):
            expr_engine.parse(bad)


def test_expr_engine_division_by_zero_is_an_expression_error():
    expr = expr_engine.parse("a / b")
    with pytest.raises(expr_engine.ExpressionError):
        expr.evaluate({"a": 1, "b": 0})


def test_expr_engine_missing_field_raises_expression_error_not_crash():
    expr = expr_engine.parse("a + b")
    with pytest.raises(expr_engine.ExpressionError):
        expr.evaluate({"a": 1})


# --------------------------------------------------------------------------
# condition_engine — nested AND/OR/NOT, all operator families
# --------------------------------------------------------------------------

def test_condition_simple_gt():
    cond = Condition(field="Deviation", operator=Operator.GT, value=ValueRef(type="static", value=5))
    out = condition_engine.eval_tree(cond, {"Deviation": 6.2})
    assert out.result is True
    out2 = condition_engine.eval_tree(cond, {"Deviation": 3.2})
    assert out2.result is False


def test_condition_nested_and_or_not():
    tree = ConditionGroup(operator="AND", children=[
        Condition(field="Currency", operator=Operator.EQ, value=ValueRef(type="static", value="USD")),
        ConditionGroup(operator="OR", children=[
            Condition(field="Deviation", operator=Operator.GT, value=ValueRef(type="static", value=5)),
            Condition(field="Notional", operator=Operator.GT, value=ValueRef(type="static", value=1_000_000)),
        ]),
        ConditionGroup(operator="NOT", children=[
            Condition(field="LegalEntity", operator=Operator.EQ, value=ValueRef(type="static", value="ABC")),
        ]),
    ])
    record = {"Currency": "USD", "Deviation": 1, "Notional": 2_000_000, "LegalEntity": "XYZ"}
    assert condition_engine.eval_tree(tree, record).result is True
    record2 = {"Currency": "USD", "Deviation": 1, "Notional": 2_000_000, "LegalEntity": "ABC"}
    assert condition_engine.eval_tree(tree, record2).result is False


def test_condition_missing_field_is_false_not_error():
    cond = Condition(field="Ghost", operator=Operator.GT, value=ValueRef(type="static", value=1))
    out = condition_engine.eval_tree(cond, {})
    assert out.result is False


def test_condition_between_and_in():
    between = Condition(field="x", operator=Operator.BETWEEN,
                         value=ValueRef(type="static", value=1), value2=ValueRef(type="static", value=10))
    assert condition_engine.eval_tree(between, {"x": 5}).result is True
    assert condition_engine.eval_tree(between, {"x": 15}).result is False

    in_cond = Condition(field="ccy", operator=Operator.IN,
                         values=[ValueRef(type="static", value="USD"), ValueRef(type="static", value="EUR")])
    assert condition_engine.eval_tree(in_cond, {"ccy": "EUR"}).result is True
    assert condition_engine.eval_tree(in_cond, {"ccy": "GBP"}).result is False


# --------------------------------------------------------------------------
# lookup_engine — exact/composite/range/date, join semantics, duplicates
# --------------------------------------------------------------------------

def _cfg(**kw) -> LookupConfig:
    base = dict(lookup_type=LookupType.EXACT, reference_file_id="ref1",
                join_keys=[{"source": "currency", "reference": "Currency"}],
                fields=[LookupFieldMap(source_column="Threshold", output_field="Threshold")])
    base.update(kw)
    return LookupConfig(**base)


def test_lookup_exact_match_and_miss():
    ref_rows = [{"Currency": "USD", "Threshold": 2.0}, {"Currency": "EUR", "Threshold": 2.5}]
    idx = lookup_engine.build_index(ref_rows, _cfg())
    ok = lookup_engine.apply_lookup({"currency": "USD"}, idx)
    assert ok.status == "matched" and ok.fields_added["Threshold"] == 2.0

    miss = lookup_engine.apply_lookup({"currency": "JPY"}, idx)
    assert miss.status == "missing_null"


def test_lookup_missing_strategy_reject():
    ref_rows = [{"Currency": "USD", "Threshold": 2.0}]
    idx = lookup_engine.build_index(ref_rows, _cfg(missing_strategy=MissingLookupStrategy.REJECT))
    out = lookup_engine.apply_lookup({"currency": "JPY"}, idx)
    assert out.status == "missing_rejected" and out.reject is True


def test_lookup_missing_strategy_default():
    ref_rows = [{"Currency": "USD", "Threshold": 2.0}]
    idx = lookup_engine.build_index(ref_rows, _cfg(missing_strategy=MissingLookupStrategy.DEFAULT,
                                                     default_values={"Threshold": 5.0}))
    out = lookup_engine.apply_lookup({"currency": "JPY"}, idx)
    assert out.status == "missing_default" and out.fields_added["Threshold"] == 5.0


def test_lookup_duplicate_key_detection_and_priority():
    ref_rows = [{"Currency": "USD", "Threshold": 2.0, "prio": 1},
                {"Currency": "USD", "Threshold": 9.0, "prio": 5}]
    cfg = _cfg(priority_strategy=PriorityStrategy.HIGHEST_PRIORITY, priority_field="prio")
    idx = lookup_engine.build_index(ref_rows, cfg)
    assert idx.duplicate_keys == {"USD": 2}
    out = lookup_engine.apply_lookup({"currency": "USD"}, idx)
    assert out.fields_added["Threshold"] == 9.0  # prio=5 wins


def test_lookup_composite_key():
    ref_rows = [{"Currency": "USD", "Entity": "LE1", "Threshold": 2.0},
                {"Currency": "USD", "Entity": "LE2", "Threshold": 4.0}]
    cfg = _cfg(lookup_type=LookupType.COMPOSITE,
               join_keys=[{"source": "currency", "reference": "Currency"},
                          {"source": "entity", "reference": "Entity"}])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"currency": "USD", "entity": "LE2"}, idx)
    assert out.fields_added["Threshold"] == 4.0


def test_lookup_range():
    ref_rows = [{"Risk_Group": "LOW", "Min": 0, "Max": 2}, {"Risk_Group": "MEDIUM", "Min": 2, "Max": 5},
                {"Risk_Group": "HIGH", "Min": 5, "Max": 100}]
    cfg = _cfg(lookup_type=LookupType.RANGE, join_keys=[], range_field="deviation",
               range_low_column="Min", range_high_column="Max",
               fields=[LookupFieldMap(source_column="Risk_Group", output_field="Risk_Group")])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"deviation": 3.5}, idx)
    assert out.fields_added["Risk_Group"] == "MEDIUM"


def test_lookup_date_range():
    ref_rows = [{"Rate": 1.1, "From": "2024-01-01", "To": "2024-06-30"},
                {"Rate": 1.2, "From": "2024-07-01", "To": "2024-12-31"}]
    cfg = _cfg(lookup_type=LookupType.DATE, join_keys=[], date_field="trade_date",
               date_from_column="From", date_to_column="To",
               fields=[LookupFieldMap(source_column="Rate", output_field="Rate")])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"trade_date": "2024-08-15"}, idx)
    assert out.fields_added["Rate"] == 1.2


# --------------------------------------------------------------------------
# workflow_engine — end-to-end pipeline execution
# --------------------------------------------------------------------------

def _demo_workflow(ref_id: str) -> Workflow:
    nodes = [
        WorkflowNode(id="in", type=NodeType.INPUT),
        WorkflowNode(id="lk", type=NodeType.LOOKUP, lookup=LookupConfig(
            lookup_type=LookupType.EXACT, reference_file_id=ref_id,
            join_keys=[{"source": "currency", "reference": "Currency"}],
            fields=[LookupFieldMap(source_column="Threshold", output_field="Threshold")],
            missing_strategy=MissingLookupStrategy.DEFAULT, default_values={"Threshold": 5.0},
        )),
        WorkflowNode(id="calc", type=NodeType.CALCULATE,
                     calculate=DeriveSpec(output_field="Ratio", expression="Deviation / Threshold")),
        WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(operator="AND", children=[
            Condition(field="Deviation", operator=Operator.GT, value=ValueRef(type="column", name="Threshold")),
        ])),
        WorkflowNode(id="out", type=NodeType.OUTCOME, outcomes=[
            OutcomeAction(field="Alert", value=ValueRef(type="static", value=True)),
        ]),
    ]
    edges = [WorkflowEdge(source="in", target="lk"), WorkflowEdge(source="lk", target="calc"),
             WorkflowEdge(source="calc", target="cond"), WorkflowEdge(source="cond", target="out")]
    return Workflow(nodes=nodes, edges=edges)


def test_workflow_engine_end_to_end(tmp_path):
    ref = reference_store.upload_version(
        "ccy_ref", [{"Currency": "USD", "Threshold": 2.0}, {"Currency": "EUR", "Threshold": 3.0}], "tester")
    rows = [
        {"trade_id": 1, "currency": "USD", "Deviation": 6.0},   # breaches 2.0 -> match
        {"trade_id": 2, "currency": "USD", "Deviation": 1.0},   # under threshold -> no match
        {"trade_id": 3, "currency": "JPY", "Deviation": 6.0},   # no ref row -> default 5.0, 6>5 -> match
    ]
    wf = _demo_workflow(ref.id)
    results, diagnostics, summary = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader,
                                                                   record_id_field="trade_id")
    assert summary.total_records == 3
    assert summary.matched == 2
    matched_ids = {r.record_id for r in results if r.matched}
    assert matched_ids == {1, 3}
    assert diagnostics[0].successful_lookups == 2
    assert diagnostics[0].lookup_failures == 1


def test_workflow_topo_order_falls_back_on_cycle():
    nodes = [WorkflowNode(id="a", type=NodeType.INPUT), WorkflowNode(id="b", type=NodeType.INPUT)]
    edges = [WorkflowEdge(source="a", target="b"), WorkflowEdge(source="b", target="a")]
    wf = Workflow(nodes=nodes, edges=edges)
    order = workflow_engine.topo_order(wf)
    assert len(order) == 2  # doesn't drop nodes even on a cycle


# --------------------------------------------------------------------------
# validation_service
# --------------------------------------------------------------------------

def test_validation_catches_missing_dataset_column():
    result = validation_service.validate_dataset([{"a": 1}], ["a", "b"])
    assert not result.ok
    assert any("b" in e for e in result.errors)


def test_validation_workflow_flags_unavailable_field():
    rule = Rule(rule_id="R1", name="R1", required_columns=["Currency"], workflow=Workflow(
        nodes=[WorkflowNode(id="in", type=NodeType.INPUT),
               WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(
                   operator="AND", children=[Condition(field="Nonexistent", operator=Operator.EQ,
                                                        value=ValueRef(type="static", value=1))]))],
        edges=[WorkflowEdge(source="in", target="cond")],
    ))
    result = validation_service.validate_workflow(rule, {})
    assert not result.ok
    assert any("Nonexistent" in e for e in result.errors)


def test_validation_between_requires_two_values():
    rule = Rule(rule_id="R2", name="R2", required_columns=["x"], workflow=Workflow(
        nodes=[WorkflowNode(id="in", type=NodeType.INPUT),
               WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(
                   operator="AND", children=[Condition(field="x", operator=Operator.BETWEEN)]))],
        edges=[WorkflowEdge(source="in", target="cond")],
    ))
    result = validation_service.validate_workflow(rule, {})
    assert not result.ok


# --------------------------------------------------------------------------
# yaml_service — round trip, atomic write, preserving unrelated content
# --------------------------------------------------------------------------

def test_yaml_round_trip_and_preserves_unrelated_keys():
    rule = Rule(rule_id="A1", name="Rule A1")
    yaml_service.save_rules([rule], actor="tester")

    raw = yaml_service.load_raw()
    raw["some_unrelated_section"] = {"kept": True}
    raw["rules"].append({"rule_id": "LEGACY", "name": "Legacy rule", "status": "DRAFT",
                          "priority": 100, "conflict_handling": "first_match",
                          "authoring_mode": "visual", "interpretation_notes": [],
                          "workflow": {"nodes": [], "edges": []}, "required_columns": [],
                          "version": 1, "created_by": "x", "created_at": 0, "updated_by": "x",
                          "updated_at": 0, "notes": "", "approvals": [], "depends_on": []})
    import io
    with open(yaml_service.RULES_FILE, "w") as fh:
        yaml_service._yaml.dump(raw, fh)  # noqa: SLF001

    rule.name = "Rule A1 (edited)"
    yaml_service.save_rules([rule], actor="tester2")

    raw2 = yaml_service.load_raw()
    assert raw2["some_unrelated_section"] == {"kept": True}
    ids = {r["rule_id"] for r in raw2["rules"]}
    assert ids == {"A1", "LEGACY"}
    a1 = next(r for r in raw2["rules"] if r["rule_id"] == "A1")
    assert a1["name"] == "Rule A1 (edited)"


def test_yaml_inspect_reports_parse_errors_without_crashing():
    os = __import__("os")
    os.makedirs(yaml_service.RULES_DIR, exist_ok=True)
    with open(yaml_service.RULES_FILE, "w") as fh:
        fh.write("rules:\n  - rule_id: 'bad rule!'\n    name: Bad\n")  # invalid rule_id chars
    report = yaml_service.inspect_yaml()
    assert report["rule_count"] == 1
    assert report["parsed_ok"] == 0
    assert len(report["parse_errors"]) == 1


# --------------------------------------------------------------------------
# rule_store — lifecycle transitions
# --------------------------------------------------------------------------

def test_lifecycle_illegal_transition_rejected():
    rule = Rule(rule_id="L1", name="L1")
    rule_store.upsert_rule(rule, "tester")
    with pytest.raises(ValueError):
        rule_store.transition(rule, RuleStatus.PUBLISHED, "tester", Role.ADMIN)


def test_lifecycle_happy_path():
    rule = Rule(rule_id="L2", name="L2")
    rule_store.upsert_rule(rule, "tester")
    rule = rule_store.transition(rule, RuleStatus.VALIDATED, "tester", Role.RULE_CREATOR)
    rule = rule_store.transition(rule, RuleStatus.DRY_RUN_COMPLETED, "tester", Role.RULE_CREATOR)
    rule = rule_store.transition(rule, RuleStatus.PENDING_APPROVAL, "tester", Role.RULE_CREATOR)
    rule = rule_store.transition(rule, RuleStatus.APPROVED, "approver", Role.APPROVER)
    rule = rule_store.transition(rule, RuleStatus.PUBLISHED, "approver", Role.APPROVER)
    assert rule.status == RuleStatus.PUBLISHED
    assert len(rule.approvals) == 5


def test_version_publish_and_rollback_is_additive():
    rule = Rule(rule_id="V1", name="V1")
    rule_store.upsert_rule(rule, "tester")
    v1 = version_service.publish_snapshot("tester", "first", ["V1"])
    assert v1.version == 1
    v2 = version_service.rollback_to(1, "admin")
    assert v2.version == 2  # rollback creates a new version, never deletes v1
    assert version_service.get_version(1) is not None
