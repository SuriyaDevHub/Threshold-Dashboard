"""Pure-Python test suite for the Rule Designer engines and services.

Run with:  cd backend && .venv/bin/python -m pytest tests/test_rule_designer.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.core import store as dataset_store
from app.modules.rule_designer import (
    condition_engine, explain_service, expr_engine, lookup_engine, product_engine, product_registry,
    reference_store, rule_store, shadow_test_service, validation_service, version_service, workflow_engine,
    yaml_service,
)
from app.modules.rule_designer.models import (
    Condition, ConditionGroup, ConflictHandling, DeriveSpec, LookupConfig, LookupFieldMap, LookupType,
    MissingLookupStrategy, NodeType, Operator, OutcomeAction, PriorityStrategy, Rule, RuleStatus,
    ShadowComparisonCategory, Role, ValueRef, Workflow, WorkflowEdge, WorkflowNode,
)

TEST_PRODUCT = "TESTPROD"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Every test gets its own rules/versions/reference_data/audit dirs so
    tests never see each other's state or the real seeded data, plus a
    clean product registry seeded with one TESTPROD entry."""
    monkeypatch.setattr(yaml_service, "RULES_DIR", str(tmp_path / "rules"))
    monkeypatch.setattr(version_service, "VERSIONS_DIR", str(tmp_path / "versions"))
    monkeypatch.setattr(reference_store, "BASE_DIR", str(tmp_path / "reference_data"))
    monkeypatch.setattr(product_registry, "REGISTRY_PATH", str(tmp_path / "rules" / "products.json"))

    import app.modules.rule_designer.audit_service as audit_service
    monkeypatch.setattr(audit_service, "AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(audit_service, "AUDIT_LOG", str(tmp_path / "audit" / "audit_log.jsonl"))

    monkeypatch.setattr(shadow_test_service, "SHADOW_DIR", str(tmp_path / "shadow_tests"))
    monkeypatch.setattr(dataset_store.get_settings(), "DATA_DIR", str(tmp_path / "_datasets"))
    dataset_store._MEM.clear()  # noqa: SLF001 — in-memory dataset store is process-global

    os.makedirs(tmp_path / "rules", exist_ok=True)
    product_registry.create_product(TEST_PRODUCT, "Test Product", "seeded for tests", "tester")
    yield
    dataset_store._MEM.clear()  # noqa: SLF001


def _rule(**kw) -> Rule:
    kw.setdefault("product", TEST_PRODUCT)
    return Rule(**kw)


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
    rule = _rule(rule_id="R1", name="R1", required_columns=["Currency"], workflow=Workflow(
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
    rule = _rule(rule_id="R2", name="R2", required_columns=["x"], workflow=Workflow(
        nodes=[WorkflowNode(id="in", type=NodeType.INPUT),
               WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(
                   operator="AND", children=[Condition(field="x", operator=Operator.BETWEEN)]))],
        edges=[WorkflowEdge(source="in", target="cond")],
    ))
    result = validation_service.validate_workflow(rule, {})
    assert not result.ok


# --------------------------------------------------------------------------
# yaml_service — round trip, atomic write, preserving unrelated content,
# per-product isolation, and the rule_id -> product index
# --------------------------------------------------------------------------

def test_yaml_round_trip_and_preserves_unrelated_keys():
    rule = _rule(rule_id="A1", name="Rule A1")
    yaml_service.save_rules([rule], actor="tester")

    raw = yaml_service.load_raw(TEST_PRODUCT)
    raw["some_unrelated_section"] = {"kept": True}
    raw["rules"].append({"rule_id": "LEGACY", "product": TEST_PRODUCT, "name": "Legacy rule", "status": "DRAFT",
                          "enabled": True, "priority": 100, "conflict_handling": "first_match",
                          "authoring_mode": "visual", "interpretation_notes": [],
                          "workflow": {"nodes": [], "edges": []}, "required_columns": [],
                          "version": 1, "created_by": "x", "created_at": 0, "updated_by": "x",
                          "updated_at": 0, "notes": "", "approvals": [], "depends_on": []})
    with open(yaml_service._rules_file(TEST_PRODUCT), "w") as fh:  # noqa: SLF001
        yaml_service._yaml.dump(raw, fh)  # noqa: SLF001

    rule.name = "Rule A1 (edited)"
    yaml_service.save_rules([rule], actor="tester2")

    raw2 = yaml_service.load_raw(TEST_PRODUCT)
    assert raw2["some_unrelated_section"] == {"kept": True}
    ids = {r["rule_id"] for r in raw2["rules"]}
    assert ids == {"A1", "LEGACY"}
    a1 = next(r for r in raw2["rules"] if r["rule_id"] == "A1")
    assert a1["name"] == "Rule A1 (edited)"


def test_yaml_inspect_reports_parse_errors_without_crashing():
    os.makedirs(yaml_service._product_dir(TEST_PRODUCT), exist_ok=True)  # noqa: SLF001
    with open(yaml_service._rules_file(TEST_PRODUCT), "w") as fh:  # noqa: SLF001
        fh.write("rules:\n  - rule_id: 'bad rule!'\n    name: Bad\n")  # invalid rule_id chars
    report = yaml_service.inspect_yaml(TEST_PRODUCT)
    assert report["rule_count"] == 1
    assert report["parsed_ok"] == 0
    assert len(report["parse_errors"]) == 1


def test_yaml_products_are_isolated_files():
    product_registry.create_product("OTHERPROD", "Other Product", "", "tester")
    yaml_service.save_rules([_rule(rule_id="P1", name="P1")], actor="tester")
    yaml_service.save_rules([_rule(rule_id="P2", name="P2", product="OTHERPROD")], actor="tester")

    testprod_rules, _ = yaml_service.load_rules(TEST_PRODUCT)
    otherprod_rules, _ = yaml_service.load_rules("OTHERPROD")
    assert {r.rule_id for r in testprod_rules} == {"P1"}
    assert {r.rule_id for r in otherprod_rules} == {"P2"}


def test_rule_id_to_product_index_resolves_without_scanning():
    yaml_service.save_rules([_rule(rule_id="IDX1", name="Indexed")], actor="tester")
    assert yaml_service.resolve_product("IDX1") == TEST_PRODUCT
    assert yaml_service.resolve_product("NOPE") is None


# --------------------------------------------------------------------------
# rule_store — lifecycle transitions, product-scoped lookup
# --------------------------------------------------------------------------

def test_get_rule_resolves_product_automatically():
    rule_store.upsert_rule(_rule(rule_id="L0", name="L0"), "tester")
    found = rule_store.get_rule("L0")  # no product passed — resolved via index
    assert found is not None and found.product == TEST_PRODUCT


def test_lifecycle_illegal_transition_rejected():
    rule = _rule(rule_id="L1", name="L1")
    rule_store.upsert_rule(rule, "tester")
    with pytest.raises(ValueError):
        rule_store.transition(rule, RuleStatus.PUBLISHED, "tester", Role.ADMIN)


def test_lifecycle_happy_path():
    rule = _rule(rule_id="L2", name="L2")
    rule_store.upsert_rule(rule, "tester")
    rule = rule_store.transition(rule, RuleStatus.VALIDATED, "admin", Role.ADMIN)
    rule = rule_store.transition(rule, RuleStatus.DRY_RUN_COMPLETED, "admin", Role.ADMIN)
    rule = rule_store.transition(rule, RuleStatus.PENDING_APPROVAL, "admin", Role.ADMIN)
    rule = rule_store.transition(rule, RuleStatus.APPROVED, "admin", Role.ADMIN)
    rule = rule_store.transition(rule, RuleStatus.PUBLISHED, "admin", Role.ADMIN)
    assert rule.status == RuleStatus.PUBLISHED
    assert len(rule.approvals) == 5


def test_next_rule_id_starts_at_001_for_a_fresh_product():
    assert rule_store.next_rule_id(TEST_PRODUCT) == f"OAR-{TEST_PRODUCT}-001"


def test_next_rule_id_ignores_draft_rules():
    rule = _rule(rule_id=f"OAR-{TEST_PRODUCT}-001", name="draft only")
    rule_store.upsert_rule(rule, "tester")  # stays DRAFT — never approved
    assert rule_store.next_rule_id(TEST_PRODUCT) == f"OAR-{TEST_PRODUCT}-001"


def test_next_rule_id_increments_past_approved_and_published_rules():
    r1 = _rule(rule_id=f"OAR-{TEST_PRODUCT}-001", name="one")
    rule_store.upsert_rule(r1, "tester")
    r1 = rule_store.transition(r1, RuleStatus.VALIDATED, "admin", Role.ADMIN)
    r1 = rule_store.transition(r1, RuleStatus.DRY_RUN_COMPLETED, "admin", Role.ADMIN)
    r1 = rule_store.transition(r1, RuleStatus.PENDING_APPROVAL, "admin", Role.ADMIN)
    r1 = rule_store.transition(r1, RuleStatus.APPROVED, "admin", Role.ADMIN)  # approved, not yet published
    assert rule_store.next_rule_id(TEST_PRODUCT) == f"OAR-{TEST_PRODUCT}-002"

    r1 = rule_store.transition(r1, RuleStatus.PUBLISHED, "admin", Role.ADMIN)
    assert rule_store.next_rule_id(TEST_PRODUCT) == f"OAR-{TEST_PRODUCT}-002"  # unchanged by publish itself

    r2 = _rule(rule_id=f"OAR-{TEST_PRODUCT}-002", name="two", status=RuleStatus.PUBLISHED)
    rule_store.upsert_rule(r2, "tester")
    assert rule_store.next_rule_id(TEST_PRODUCT) == f"OAR-{TEST_PRODUCT}-003"


def test_set_enabled_is_independent_of_lifecycle_status():
    rule = _rule(rule_id="L3", name="L3")
    rule_store.upsert_rule(rule, "tester")
    assert rule.enabled is True
    rule = rule_store.set_enabled(rule, False, "admin")
    assert rule.enabled is False
    assert rule.status == RuleStatus.DRAFT  # unaffected


def test_version_publish_and_rollback_is_additive():
    rule = _rule(rule_id="V1", name="V1")
    rule_store.upsert_rule(rule, "tester")
    v1 = version_service.publish_snapshot(TEST_PRODUCT, "tester", "first", ["V1"])
    assert v1.version == 1
    v2 = version_service.rollback_to(TEST_PRODUCT, 1, "admin")
    assert v2.version == 2  # rollback creates a new version, never deletes v1
    assert version_service.get_version(TEST_PRODUCT, 1) is not None


def test_versions_are_independent_per_product():
    product_registry.create_product("OTHERPROD2", "Other 2", "", "tester")
    rule_store.upsert_rule(_rule(rule_id="VP1", name="VP1"), "tester")
    rule_store.upsert_rule(_rule(rule_id="VP2", name="VP2", product="OTHERPROD2"), "tester")
    version_service.publish_snapshot(TEST_PRODUCT, "tester", "d", ["VP1"])
    version_service.publish_snapshot(TEST_PRODUCT, "tester", "d2", ["VP1"])
    version_service.publish_snapshot("OTHERPROD2", "tester", "d", ["VP2"])
    assert version_service.next_version_number(TEST_PRODUCT) == 3
    assert version_service.next_version_number("OTHERPROD2") == 2


# --------------------------------------------------------------------------
# product_registry
# --------------------------------------------------------------------------

def test_product_registry_seeded_from_asset_classes():
    codes = {p.code for p in product_registry.list_products()}
    assert {"CASH_BONDS", "GFX_CASH", "IRD", "MM"} <= codes


def test_product_enable_disable():
    product_registry.set_enabled(TEST_PRODUCT, False, "admin")
    assert product_registry.get_product(TEST_PRODUCT).enabled is False
    product_registry.set_enabled(TEST_PRODUCT, True, "admin")
    assert product_registry.get_product(TEST_PRODUCT).enabled is True


def test_product_create_duplicate_rejected():
    with pytest.raises(ValueError):
        product_registry.create_product(TEST_PRODUCT, "dup", "", "tester")


# --------------------------------------------------------------------------
# product_engine — the generic_validator: multi-rule evaluation + fail-safe
# --------------------------------------------------------------------------

def _pub_rule(rule_id: str, threshold: float, priority: int = 100,
              conflict: ConflictHandling = ConflictHandling.FIRST_MATCH) -> Rule:
    rule = _rule(rule_id=rule_id, name=rule_id, priority=priority, conflict_handling=conflict, workflow=Workflow(
        nodes=[
            WorkflowNode(id="in", type=NodeType.INPUT),
            WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(operator="AND", children=[
                Condition(field="deviation", operator=Operator.GT, value=ValueRef(type="static", value=threshold)),
            ])),
            WorkflowNode(id="out", type=NodeType.OUTCOME, outcomes=[
                OutcomeAction(field="Reason", value=ValueRef(type="static", value=rule_id)),
            ]),
        ],
        edges=[WorkflowEdge(source="in", target="cond"), WorkflowEdge(source="cond", target="out")],
    ))
    rule_store.upsert_rule(rule, "tester")
    rule.status = RuleStatus.PUBLISHED
    return rule_store.upsert_rule(rule, "tester")


def _dataset(rows):
    return dataset_store.put("TEST", {"product_type": TEST_PRODUCT}, rows).id


def test_product_engine_fail_safe_when_disabled():
    _pub_rule("PR1", threshold=5.0)
    product_registry.set_enabled(TEST_PRODUCT, False, "admin")
    ds = _dataset([{"id": 1, "deviation": 1.0}, {"id": 2, "deviation": 9.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.summary.fail_safe_triggered is True
    assert result.summary.matched == 2  # alerts everything


def test_product_engine_fail_safe_when_no_active_rules():
    ds = _dataset([{"id": 1, "deviation": 1.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.summary.fail_safe_triggered is True
    assert "no active" in result.summary.fail_safe_reason


def test_product_engine_first_match_priority_order():
    _pub_rule("LOW_PRIORITY_NUM_WINS", threshold=1.0, priority=1)   # matches deviation=9 first (evaluated first)
    _pub_rule("HIGH_PRIORITY_NUM_LOSES", threshold=1.0, priority=50)
    ds = _dataset([{"id": 1, "deviation": 9.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.summary.fail_safe_triggered is False
    assert result.records[0].matched_rule_id == "LOW_PRIORITY_NUM_WINS"


def test_product_engine_all_matching_merges_outcomes():
    _pub_rule("RULE_A", threshold=1.0, priority=1, conflict=ConflictHandling.ALL_MATCHING)
    _pub_rule("RULE_B", threshold=1.0, priority=2, conflict=ConflictHandling.ALL_MATCHING)
    ds = _dataset([{"id": 1, "deviation": 9.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert set(result.records[0].matched_rule_ids) == {"RULE_A", "RULE_B"}


def test_product_engine_disabled_rule_excluded_from_active_set():
    rule = _pub_rule("TOGGLE_ME", threshold=1.0)
    rule_store.set_enabled(rule, False, "admin")
    ds = _dataset([{"id": 1, "deviation": 9.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.summary.fail_safe_triggered is True  # zero *active* (published+enabled) rules left


# --------------------------------------------------------------------------
# condition_engine — MATCHES_PATTERN (legacy _match_pattern/fnmatch parity)
# --------------------------------------------------------------------------

def test_matches_pattern_glob_is_case_sensitive():
    cond = Condition(field="product_type", operator=Operator.MATCHES_PATTERN, value=ValueRef(type="static", value="*SWAP*"))
    assert condition_engine.eval_condition(cond, {"product_type": "IR_SWAP"}).result is True
    assert condition_engine.eval_condition(cond, {"product_type": "ir_swap"}).result is False
    assert condition_engine.eval_condition(cond, {"product_type": "BOND"}).result is False


# --------------------------------------------------------------------------
# workflow_engine — coalesce transform (legacy _pick canonical/raw fallback)
# --------------------------------------------------------------------------

def test_coalesce_transform_prefers_canonical_then_falls_back():
    node = WorkflowNode(id="n", type=NodeType.TRANSFORM,
                         transform={"op": "coalesce", "output_field": "deal_level",
                                    "fields": ["deal_level", "epe_deal_level"]})
    status, fields = workflow_engine._apply_transform(node, {"deal_level": None, "epe_deal_level": 7})
    assert status == "ok" and fields == {"deal_level": 7}

    status, fields = workflow_engine._apply_transform(node, {"deal_level": 0, "epe_deal_level": 7})
    assert fields == {"deal_level": 0}  # 0 is a real value, not treated as missing

    status, fields = workflow_engine._apply_transform(node, {})
    assert fields == {"deal_level": None}


def _transform_node(**cfg):
    return WorkflowNode(id="n", type=NodeType.TRANSFORM, transform=cfg)


def test_trim_transform_strips_whitespace():
    node = _transform_node(op="trim", field="book", output_field="book")
    status, fields = workflow_engine._apply_transform(node, {"book": "  HKFXO_052  "})
    assert status == "ok" and fields == {"book": "HKFXO_052"}


def test_substring_transform_start_and_end():
    node = _transform_node(op="substring", field="book", output_field="prefix", start=0, end=3)
    status, fields = workflow_engine._apply_transform(node, {"book": "HKFXO_052"})
    assert fields == {"prefix": "HKF"}


def test_substring_transform_open_ended_when_no_end():
    node = _transform_node(op="substring", field="book", output_field="suffix", start=6)
    status, fields = workflow_engine._apply_transform(node, {"book": "HKFXO_052"})
    assert fields == {"suffix": "052"}


def test_split_transform_picks_segment_by_index():
    node = _transform_node(op="split", field="book", output_field="suffix", delimiter="_", index=1)
    status, fields = workflow_engine._apply_transform(node, {"book": "HKFXO_052"})
    assert fields == {"suffix": "052"}


def test_split_transform_out_of_range_index_yields_none_not_error():
    node = _transform_node(op="split", field="book", output_field="suffix", delimiter="_", index=5)
    status, fields = workflow_engine._apply_transform(node, {"book": "HKFXO_052"})
    assert status == "ok" and fields == {"suffix": None}


def test_replace_transform():
    node = _transform_node(op="replace", field="book", output_field="book", find="_", replace_with="-")
    status, fields = workflow_engine._apply_transform(node, {"book": "HKFXO_052"})
    assert fields == {"book": "HKFXO-052"}


def test_round_transform_precision_falls_back_when_blank():
    node = _transform_node(op="round", field="pct", output_field="pct", precision="")
    status, fields = workflow_engine._apply_transform(node, {"pct": 2.34567})
    assert status == "ok" and fields == {"pct": 2.35}


# --------------------------------------------------------------------------
# ValueRef.type=="template" — outcome commentary rendering
# --------------------------------------------------------------------------

def test_outcome_template_value_renders_against_record():
    workflow = Workflow(
        nodes=[
            WorkflowNode(id="in", type=NodeType.INPUT),
            WorkflowNode(id="out", type=NodeType.OUTCOME, outcomes=[
                OutcomeAction(field="Commentary", value=ValueRef(type="template", value="Region {region} breached.")),
            ]),
        ],
        edges=[WorkflowEdge(source="in", target="out")],
    )
    records, _, _ = workflow_engine.run_workflow(workflow, [{"region": "EMEA"}], lambda *_: None)
    assert records[0].outcome["Commentary"] == "Region EMEA breached."


# --------------------------------------------------------------------------
# explain_service — business-friendly explanation must reflect template
# outcome values (Reason/Commentary authored via the Outcome tab), not
# render them as the literal string "None"
# --------------------------------------------------------------------------

def test_explanation_renders_template_outcome_values_not_none():
    rule = _rule(
        rule_id="EXPLAIN1", name="Explain template outcome",
        workflow=Workflow(
            nodes=[
                WorkflowNode(id="in", type=NodeType.INPUT),
                WorkflowNode(id="out", type=NodeType.OUTCOME, outcomes=[
                    OutcomeAction(field="Alert", value=ValueRef(type="static", value=True)),
                    OutcomeAction(field="Reason", value=ValueRef(type="template", value="OAR-TEST-001")),
                    OutcomeAction(field="Commentary", value=ValueRef(type="template", value="Region {region} breached.")),
                ]),
            ],
            edges=[WorkflowEdge(source="in", target="out")],
        ),
    )
    text = explain_service.generate_explanation(rule)
    assert "Reason = OAR-TEST-001" in text
    assert "Commentary = Region {region} breached." in text
    assert "None" not in text


# --------------------------------------------------------------------------
# product_engine — on_no_match fail-closed posture + evaluate_record
# (the {product}_validator.py migration contract: legacy validators alert
# a fully-unmatched record rather than silently clearing it)
# --------------------------------------------------------------------------

def test_on_no_match_alert_flags_unmatched_records_with_configured_reason_code():
    product_registry.configure_fail_safe(TEST_PRODUCT, on_no_match="alert",
                                          unmatched_reason_code="TP-UNMATCHED",
                                          disabled_reason_code="TP-DISABLED", actor="admin")
    _pub_rule("PR1", threshold=5.0)
    ds = _dataset([{"id": 1, "deviation": 1.0}])  # does not breach threshold -> no rule matches
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.summary.fail_safe_triggered is False  # rules WERE active — this isn't the empty-ruleset fail-safe
    assert result.records[0].matched is True
    assert result.records[0].outcome == {"Alert": True, "Reason": "TP-UNMATCHED"}


def test_on_no_match_clear_is_the_default_and_leaves_unmatched_silent():
    _pub_rule("PR1", threshold=5.0)
    ds = _dataset([{"id": 1, "deviation": 1.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.records[0].matched is False
    assert result.records[0].outcome == {}


def test_disabled_reason_code_used_in_fail_safe_result():
    product_registry.configure_fail_safe(TEST_PRODUCT, on_no_match="alert",
                                          unmatched_reason_code="TP-UNMATCHED",
                                          disabled_reason_code="TP-DISABLED", actor="admin")
    _pub_rule("PR1", threshold=5.0)
    product_registry.set_enabled(TEST_PRODUCT, False, "admin")
    ds = _dataset([{"id": 1, "deviation": 9.0}])
    result = product_engine.evaluate_product(TEST_PRODUCT, ds, "tester", record_id_field="id")
    assert result.records[0].outcome["Reason"] == "TP-DISABLED"


def test_evaluate_record_single_trade_matches_a_rule():
    _pub_rule("PR1", threshold=5.0)
    result = product_engine.evaluate_record(TEST_PRODUCT, {"deviation": 9.0}, "tester")
    assert result.matched is True
    assert result.matched_rule_id == "PR1"


def test_evaluate_record_fail_safe_when_product_disabled():
    product_registry.configure_fail_safe(TEST_PRODUCT, on_no_match="clear",
                                          unmatched_reason_code=None, disabled_reason_code="TP-DISABLED",
                                          actor="admin")
    _pub_rule("PR1", threshold=5.0)
    product_registry.set_enabled(TEST_PRODUCT, False, "admin")
    result = product_engine.evaluate_record(TEST_PRODUCT, {"deviation": 9.0}, "tester")
    assert result.matched is True
    assert result.outcome == {"Alert": True, "Reason": "TP-DISABLED"}


def test_evaluate_record_no_match_respects_on_no_match_clear():
    _pub_rule("PR1", threshold=5.0)
    result = product_engine.evaluate_record(TEST_PRODUCT, {"deviation": 1.0}, "tester")
    assert result.matched is False


# --------------------------------------------------------------------------
# shadow_test_service — new engine vs. legacy validator output
# --------------------------------------------------------------------------

def _shadow_rule(threshold: float = 5.0) -> Rule:
    return _rule(rule_id="SH1", name="Shadow test rule", required_columns=["trade_id", "deviation"],
                 workflow=Workflow(
        nodes=[
            WorkflowNode(id="in", type=NodeType.INPUT),
            WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(operator="AND", children=[
                Condition(field="deviation", operator=Operator.GT, value=ValueRef(type="static", value=threshold)),
            ])),
            WorkflowNode(id="out", type=NodeType.OUTCOME, outcomes=[
                OutcomeAction(field="Alert", value=ValueRef(type="static", value=True)),
            ]),
        ],
        edges=[WorkflowEdge(source="in", target="cond"), WorkflowEdge(source="cond", target="out")],
    ))


def _put_dataset(rows):
    meta = dataset_store.put("TEST", {"product_type": "TEST"}, rows)
    return meta.id


def test_shadow_test_agreement_and_mismatch_categories():
    rule = _shadow_rule(threshold=5.0)
    ds_id = _put_dataset([
        {"trade_id": "T1", "deviation": 8.0},   # new: alert
        {"trade_id": "T2", "deviation": 1.0},   # new: no alert
        {"trade_id": "T3", "deviation": 9.0},   # new: alert
        {"trade_id": "T4", "deviation": 2.0},   # new: no alert
    ])
    legacy_csv = (
        "record_id,status,reason_code\n"
        "T1,ALERTED,DEV_HIGH\n"     # agree_alert
        "T2,CLEARED,\n"             # agree_clear
        "T3,CLEARED,\n"             # new_only (new alerts, legacy didn't)
        "T4,ALERTED,DEV_HIGH\n"     # legacy_only (legacy alerted, new didn't) — the regression case
    )
    legacy_rows = shadow_test_service.parse_legacy_csv(legacy_csv)
    result = shadow_test_service.run_shadow_test(
        rule, ds_id, legacy_rows, legacy_alert_values=["ALERTED"], actor="tester", record_id_field="trade_id",
    )
    assert result.summary.total_compared == 4
    assert result.summary.agree_alert == 1
    assert result.summary.agree_clear == 1
    assert result.summary.new_only == 1
    assert result.summary.legacy_only == 1
    assert result.summary.agreement_rate_pct == 50.0

    # legacy_only sorted first — it's the regression-risk category
    assert result.mismatches[0].category == ShadowComparisonCategory.LEGACY_ONLY
    assert result.mismatches[0].record_id == "T4"
    assert result.mismatches[1].category == ShadowComparisonCategory.NEW_ONLY
    assert result.mismatches[1].record_id == "T3"
    # every mismatch carries the new engine's explainability trail
    assert result.mismatches[0].trail


def test_shadow_test_reports_unmatched_ids_without_crashing():
    rule = _shadow_rule(threshold=5.0)
    ds_id = _put_dataset([{"trade_id": "T1", "deviation": 8.0}])
    legacy_rows = shadow_test_service.parse_legacy_csv("record_id,status\nT1,ALERTED\nGHOST,ALERTED\n")
    result = shadow_test_service.run_shadow_test(
        rule, ds_id, legacy_rows, legacy_alert_values=["ALERTED"], actor="tester", record_id_field="trade_id",
    )
    assert result.summary.total_compared == 1
    assert result.summary.legacy_results_without_dataset_record == 1
    assert result.summary.dataset_records_without_legacy_result == 0


def test_shadow_test_missing_dataset_raises():
    rule = _shadow_rule()
    with pytest.raises(ValueError):
        shadow_test_service.run_shadow_test(
            rule, "nonexistent_ds", [], [], "tester", "trade_id",
        )
