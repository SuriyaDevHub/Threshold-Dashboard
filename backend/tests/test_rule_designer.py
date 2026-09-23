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
    calc_ops, condition_engine, explain_service, lookup_engine, product_engine, product_registry,
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
# calc_ops — CALCULATE nodes' structured formula tree (field/constant/
# operation, never free text) — safety + correctness
# --------------------------------------------------------------------------

def _field(name):
    return {"kind": "field", "field": name}


def _const(value):
    return {"kind": "constant", "value": value}


def _op(op, operands, precision=None):
    node = {"kind": "operation", "op": op, "operands": operands}
    if precision is not None:
        node["precision"] = precision
    return node


def test_calc_ops_basic_arithmetic():
    formula = _op("multiply", [_field("Notional"), _field("Price")])
    assert calc_ops.evaluate_formula(formula, {"Notional": 10, "Price": 2.5}) == 25.0


def test_calc_ops_nested_abs_and_division():
    # abs(booked - ref) / ref * 100
    formula = _op("multiply", [
        _op("divide", [_op("abs", [_op("subtract", [_field("booked"), _field("ref")])]), _field("ref")]),
        _const(100),
    ])
    assert round(calc_ops.evaluate_formula(formula, {"booked": 98, "ref": 100}), 4) == 2.0


def test_calc_ops_division_by_zero_is_a_calc_error():
    formula = _op("divide", [_field("a"), _field("b")])
    with pytest.raises(calc_ops.CalcError):
        calc_ops.evaluate_formula(formula, {"a": 1, "b": 0})


def test_calc_ops_missing_field_raises_calc_error_not_crash():
    formula = _op("add", [_field("a"), _field("b")])
    with pytest.raises(calc_ops.CalcError):
        calc_ops.evaluate_formula(formula, {"a": 1})


def test_calc_ops_unknown_op_raises_calc_error():
    with pytest.raises(calc_ops.CalcError):
        calc_ops.evaluate_formula(_op("frobnicate", [_field("a")]), {"a": 1})


def test_calc_ops_round_uses_precision():
    formula = _op("round", [_field("x")], precision=1)
    assert calc_ops.evaluate_formula(formula, {"x": 1.2345}) == 1.2


def test_calc_ops_validate_formula_catches_arity_errors():
    # subtract needs >= 2 operands, abs needs exactly 1
    assert calc_ops.validate_formula(_op("subtract", [_field("a")]))
    assert calc_ops.validate_formula(_op("abs", [_field("a"), _field("b")]))
    assert calc_ops.validate_formula(_op("divide", [_field("a"), _field("b")])) == []


def test_calc_ops_fields_referenced_recurses_into_nested_operations():
    formula = _op("multiply", [
        _op("divide", [_op("abs", [_op("subtract", [_field("booked"), _field("ref")])]), _field("ref")]),
        _const(100),
    ])
    assert calc_ops.fields_referenced(formula) == {"booked", "ref"}


def test_calc_ops_describe_formula_renders_readable_text():
    formula = _op("divide", [_field("Deviation_Pct"), _field("Threshold")])
    assert calc_ops.describe_formula(formula) == "(Deviation_Pct / Threshold)"


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


def test_lookup_field_transform_applied_to_looked_up_value():
    ref_rows = [{"Currency": "USD", "Threshold": 2.0, "Desk": "  ny_rates  "}]
    cfg = _cfg(fields=[
        LookupFieldMap(source_column="Threshold", output_field="Threshold"),
        LookupFieldMap(source_column="Desk", output_field="Desk", transform={"op": "trim"}),
    ])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"currency": "USD"}, idx)
    assert out.fields_added["Threshold"] == 2.0  # untouched — no transform configured
    assert out.fields_added["Desk"] == "ny_rates"  # trimmed


def test_lookup_field_transform_split_and_upper():
    ref_rows = [{"Currency": "USD", "Book": "hkfxo_052"}]
    cfg = _cfg(fields=[LookupFieldMap(
        source_column="Book", output_field="BookSuffix",
        transform={"op": "split", "delimiter": "_", "index": 1},
    )])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"currency": "USD"}, idx)
    assert out.fields_added["BookSuffix"] == "052"


def test_lookup_field_transform_error_falls_back_to_raw_value():
    ref_rows = [{"Currency": "USD", "Threshold": "not-a-number"}]
    cfg = _cfg(fields=[LookupFieldMap(
        source_column="Threshold", output_field="Threshold", transform={"op": "cast_numeric"},
    )])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"currency": "USD"}, idx)
    assert out.status == "matched"
    assert out.fields_added["Threshold"] == "not-a-number"  # transform failed, raw value kept


def test_join_key_transform_normalizes_case_mismatch():
    # Record has lowercase currency, reference file has uppercase — an
    # "upper" join-key transform should make them match even though
    # neither side is in the other's native case.
    ref_rows = [{"Currency": "USD", "Threshold": 2.0}]
    cfg = _cfg(join_keys=[{"source": "currency", "reference": "Currency", "transform": {"op": "upper"}}])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"currency": "usd"}, idx)
    assert out.status == "matched" and out.fields_added["Threshold"] == 2.0


def test_join_key_transform_applies_to_reference_side_too():
    # Reference file has whitespace-padded keys — a "trim" transform
    # applied when building the index (not just when matching a record)
    # is what makes this resolve.
    ref_rows = [{"Currency": "  USD  ", "Threshold": 2.0}]
    cfg = _cfg(join_keys=[{"source": "currency", "reference": "Currency", "transform": {"op": "trim"}}])
    idx = lookup_engine.build_index(ref_rows, cfg)
    out = lookup_engine.apply_lookup({"currency": "USD"}, idx)
    assert out.status == "matched" and out.fields_added["Threshold"] == 2.0


def test_join_key_without_transform_is_unaffected():
    ref_rows = [{"Currency": "USD", "Threshold": 2.0}]
    idx = lookup_engine.build_index(ref_rows, _cfg())  # no transform on the join key
    out = lookup_engine.apply_lookup({"currency": "usd"}, idx)
    assert out.status == "missing_null"  # case mismatch, no transform configured -> no match


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
                     calculate=DeriveSpec(output_field="Ratio", formula=_op("divide", [_field("Deviation"), _field("Threshold")]))),
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


def _self_group_workflow(group_by_field: str, selector=None, missing_strategy=MissingLookupStrategy.CONTINUE_NULL,
                          default_values=None) -> Workflow:
    lookup = LookupConfig(
        lookup_type=LookupType.SELF_GROUP, group_by_field=group_by_field, selector=selector,
        fields=[LookupFieldMap(source_column="pnl", output_field="parent_pnl"),
                LookupFieldMap(source_column="threshold", output_field="parent_threshold")],
        missing_strategy=missing_strategy, default_values=default_values or {},
    )
    nodes = [WorkflowNode(id="in", type=NodeType.INPUT), WorkflowNode(id="lk", type=NodeType.LOOKUP, lookup=lookup)]
    return Workflow(nodes=nodes, edges=[WorkflowEdge(source="in", target="lk")])


def test_self_group_lookup_broadcasts_parent_row_to_siblings():
    # Mirrors the legacy FxoValidator's LN Murex/Margin grouping: rows
    # sharing a deal reference, one flagged as parent, whose PnL/threshold
    # should be visible on every row in the group — not just the parent's
    # own record — so downstream CALCULATE/CONDITION nodes can evaluate
    # against it per row.
    selector = ConditionGroup(operator="AND", children=[
        Condition(field="is_parent", operator=Operator.EQ, value=ValueRef(type="static", value="Y")),
    ])
    wf = _self_group_workflow("dealref", selector=selector)
    rows = [
        {"trade_id": 1, "dealref": "D1", "is_parent": "Y", "pnl": 1000.0, "threshold": 500.0},
        {"trade_id": 2, "dealref": "D1", "is_parent": "N", "pnl": None, "threshold": None},
        {"trade_id": 3, "dealref": "D1", "is_parent": "N", "pnl": None, "threshold": None},
        {"trade_id": 4, "dealref": "D2", "is_parent": "N", "pnl": 10.0, "threshold": 1.0},  # no parent in D2
    ]
    results, diagnostics, summary = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader,
                                                                   record_id_field="trade_id")
    by_id = {r.record_id: r for r in results}
    assert by_id[1].final_record["parent_pnl"] == 1000.0
    assert by_id[2].final_record["parent_pnl"] == 1000.0  # broadcast from D1's parent
    assert by_id[3].final_record["parent_threshold"] == 500.0
    assert by_id[4].final_record["parent_pnl"] is None  # D2 has no row satisfying the selector
    assert diagnostics[0].successful_lookups == 3
    assert diagnostics[0].lookup_failures == 1


def test_self_group_lookup_falls_back_to_first_row_without_a_selector():
    wf = _self_group_workflow("dealref", selector=None)
    rows = [
        {"trade_id": 1, "dealref": "D1", "pnl": 42.0, "threshold": 7.0},
        {"trade_id": 2, "dealref": "D1", "pnl": 99.0, "threshold": 8.0},
    ]
    results, _, _ = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader, record_id_field="trade_id")
    by_id = {r.record_id: r for r in results}
    assert by_id[2].final_record["parent_pnl"] == 42.0  # first row of the group, not its own


def test_self_group_lookup_missing_strategy_default_when_group_key_absent():
    wf = _self_group_workflow("dealref", missing_strategy=MissingLookupStrategy.DEFAULT,
                               default_values={"parent_pnl": 0.0, "parent_threshold": 0.0})
    rows = [{"trade_id": 1, "dealref": "", "pnl": 5.0, "threshold": 1.0}]  # blank group key -> excluded from grouping
    results, _, _ = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader, record_id_field="trade_id")
    assert results[0].final_record["parent_pnl"] == 0.0


def test_self_group_lookup_sum_aggregate_across_group():
    # Mirrors the legacy FxoValidator's TH structure grouping (OAR-FXO-004):
    # sum PnL across every deal sharing a structure id (not just one
    # representative deal's own PnL), while the threshold is broadcast
    # from the group's first row same as any other self_group field.
    lookup = LookupConfig(
        lookup_type=LookupType.SELF_GROUP, group_by_field="structure_id",
        fields=[
            LookupFieldMap(source_column="pnl", output_field="total_pnl", aggregate="sum"),
            LookupFieldMap(source_column="threshold", output_field="structure_threshold"),
        ],
    )
    wf = Workflow(nodes=[WorkflowNode(id="in", type=NodeType.INPUT), WorkflowNode(id="lk", type=NodeType.LOOKUP, lookup=lookup)],
                  edges=[WorkflowEdge(source="in", target="lk")])
    rows = [
        {"trade_id": 1, "structure_id": "S1", "pnl": 100.0, "threshold": 500.0},
        {"trade_id": 2, "structure_id": "S1", "pnl": 250.0, "threshold": None},
        {"trade_id": 3, "structure_id": "S1", "pnl": 200.0, "threshold": None},
        {"trade_id": 4, "structure_id": "S2", "pnl": None, "threshold": 10.0},  # no numeric pnl anywhere in S2
    ]
    results, _, _ = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader, record_id_field="trade_id")
    by_id = {r.record_id: r for r in results}
    assert by_id[1].final_record["total_pnl"] == 550.0  # 100 + 250 + 200, not just row 1's own 100
    assert by_id[2].final_record["total_pnl"] == 550.0  # broadcast to every row in S1
    assert by_id[1].final_record["structure_threshold"] == 500.0  # plain broadcast from the group's first row
    assert by_id[4].final_record["total_pnl"] is None  # sum of zero numeric values -> no data, not 0.0


def test_self_group_lookup_first_aggregate_skips_blank_representative_row():
    # OAR-FXO-004's threshold: the group's first row happens to have a
    # blank threshold, but a later row in the same structure has one —
    # "first" should find that non-null value rather than broadcasting the
    # representative (first) row's own blank one.
    lookup = LookupConfig(
        lookup_type=LookupType.SELF_GROUP, group_by_field="structure_id",
        fields=[LookupFieldMap(source_column="threshold", output_field="structure_threshold", aggregate="first")],
    )
    wf = Workflow(nodes=[WorkflowNode(id="in", type=NodeType.INPUT), WorkflowNode(id="lk", type=NodeType.LOOKUP, lookup=lookup)],
                  edges=[WorkflowEdge(source="in", target="lk")])
    rows = [
        {"trade_id": 1, "structure_id": "S1", "threshold": None},
        {"trade_id": 2, "structure_id": "S1", "threshold": 750.0},
        {"trade_id": 3, "structure_id": "S1", "threshold": 900.0},
    ]
    results, _, _ = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader, record_id_field="trade_id")
    assert results[0].final_record["structure_threshold"] == 750.0  # not row 1's own None, not row 3's 900


def test_self_group_lookup_two_aggregates_on_same_source_column_dont_clobber():
    # Regression: sum and count of the same source_column ("pnl") used to
    # be written into the representative row under that one shared key, so
    # whichever field mapping's aggregate ran last silently overwrote the
    # other's — both output fields would end up equal.
    lookup = LookupConfig(
        lookup_type=LookupType.SELF_GROUP, group_by_field="dealref",
        fields=[
            LookupFieldMap(source_column="pnl", output_field="total_pnl", aggregate="sum"),
            LookupFieldMap(source_column="pnl", output_field="deal_count", aggregate="count"),
        ],
    )
    wf = Workflow(nodes=[WorkflowNode(id="in", type=NodeType.INPUT), WorkflowNode(id="lk", type=NodeType.LOOKUP, lookup=lookup)],
                  edges=[WorkflowEdge(source="in", target="lk")])
    rows = [
        {"trade_id": 1, "dealref": "D1", "pnl": 100.0},
        {"trade_id": 2, "dealref": "D1", "pnl": 50.0},
        {"trade_id": 3, "dealref": "D1", "pnl": 25.0},
    ]
    results, _, _ = workflow_engine.run_workflow(wf, rows, reference_store.reference_loader, record_id_field="trade_id")
    fr = results[0].final_record
    assert fr["total_pnl"] == 175.0
    assert fr["deal_count"] == 3.0


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


def test_dry_run_explainability_keeps_matches_past_the_sample_cap():
    # Regression: matched/not-matched record traces used to be kept only for
    # the first `explain_sample_cap` rows BY RAW DATASET POSITION. On a
    # dataset where every match happens to fall past that position (common
    # once a filter/lookup thins a large dataset down to a handful of real
    # matches), the explainability list would show zero matched records even
    # though the summary correctly counted them — exactly the "3 matched but
    # only 1 shown" symptom reported against a 1,479-row dry run. Matched and
    # not-matched traces must now be capped independently so a match is never
    # crowded out by not-matched rows that merely appear earlier.
    ref = reference_store.upload_version(
        "ccy_ref2", [{"Currency": "USD", "Threshold": 2.0}], "tester")
    rows = (
        [{"trade_id": i, "currency": "USD", "Deviation": 1.0} for i in range(6)]  # 6 non-matches first
        + [{"trade_id": 100 + i, "currency": "USD", "Deviation": 6.0} for i in range(2)]  # 2 matches, past the cap
    )
    wf = _demo_workflow(ref.id)
    results, _, summary = workflow_engine.run_workflow(
        wf, rows, reference_store.reference_loader, record_id_field="trade_id", explain_sample_cap=3,
    )
    assert summary.total_records == 8
    assert summary.matched == 2
    assert summary.matched_shown == 2  # neither dropped, despite both landing past index 3
    matched_ids = {r.record_id for r in results if r.matched}
    assert matched_ids == {100, 101}
    assert summary.not_matched == 6
    assert summary.not_matched_shown == 3  # not-matched bucket capped independently


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


def test_structural_diff_fields_ignores_priority_and_bookkeeping():
    before = _rule(rule_id="FT1", name="FT1", priority=50, notes="old notes")
    after = before.model_copy(deep=True)
    after.priority = 10
    after.notes = "new notes"
    after.version += 1
    assert rule_store.structural_diff_fields(before, after) == set()


def test_structural_diff_fields_catches_a_logic_change():
    before = _rule(rule_id="FT2", name="FT2", priority=50)
    after = before.model_copy(deep=True)
    after.priority = 10  # also changes priority, alongside a real logic edit
    after.conflict_handling = ConflictHandling.ALL_MATCHING
    assert "conflict_handling" in rule_store.structural_diff_fields(before, after)


def test_fast_track_submit_rejected_without_eligibility():
    rule = _rule(rule_id="FT3", name="FT3", status=RuleStatus.DRAFT, fast_track_eligible=False)
    rule_store.upsert_rule(rule, "tester")
    with pytest.raises(ValueError, match="cannot submit directly"):
        rule_store.transition(rule, RuleStatus.PENDING_APPROVAL, "admin", Role.ADMIN)


def test_fast_track_submit_allowed_when_eligible():
    # Simulates what update_rule does: a PUBLISHED rule demoted to DRAFT by
    # a priority-only edit is marked fast_track_eligible, so it can jump
    # straight to PENDING_APPROVAL without a fresh validate/dry-run pass.
    rule = _rule(rule_id="FT4", name="FT4", status=RuleStatus.DRAFT, fast_track_eligible=True)
    rule_store.upsert_rule(rule, "tester")
    rule = rule_store.transition(rule, RuleStatus.PENDING_APPROVAL, "admin", Role.ADMIN)
    assert rule.status == RuleStatus.PENDING_APPROVAL


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


def test_value_map_transform_first_matching_rule_wins():
    node = _transform_node(op="value_map", field="region_desc", output_field="region_code", rules=[
        {"contains": "LONDON", "value": "LN"},
        {"contains": "JAPAN", "value": "JP"},
    ], default="UNKNOWN")
    status, fields = workflow_engine._apply_transform(node, {"region_desc": "London (LN) Branch"})
    assert status == "ok" and fields == {"region_code": "LN"}


def test_value_map_transform_falls_back_to_default_when_no_rule_matches():
    node = _transform_node(op="value_map", field="region_desc", output_field="region_code", rules=[
        {"contains": "LONDON", "value": "LN"},
    ], default="UNKNOWN")
    status, fields = workflow_engine._apply_transform(node, {"region_desc": "Sydney office"})
    assert status == "ok" and fields == {"region_code": "UNKNOWN"}


def test_value_map_transform_without_default_returns_original_value():
    node = _transform_node(op="value_map", field="region_desc", output_field="region_code", rules=[
        {"contains": "LONDON", "value": "LN"},
    ])
    status, fields = workflow_engine._apply_transform(node, {"region_desc": "Sydney office"})
    assert status == "ok" and fields == {"region_code": "Sydney office"}


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


def _pub_self_group_rule(rule_id: str = "SG1") -> Rule:
    # Mirrors OAR-FXO-004's shape: sum a group's pnl and clear when it's
    # within a threshold broadcast from the group.
    lookup = LookupConfig(
        lookup_type=LookupType.SELF_GROUP, group_by_field="structure_id",
        fields=[
            LookupFieldMap(source_column="pnl", output_field="total_pnl", aggregate="sum"),
            LookupFieldMap(source_column="threshold", output_field="structure_threshold"),
        ],
    )
    rule = _rule(rule_id=rule_id, name=rule_id, priority=100, workflow=Workflow(
        nodes=[
            WorkflowNode(id="in", type=NodeType.INPUT),
            WorkflowNode(id="lk", type=NodeType.LOOKUP, lookup=lookup),
            WorkflowNode(id="cond", type=NodeType.CONDITION, condition=ConditionGroup(operator="AND", children=[
                Condition(field="total_pnl", operator=Operator.LTE, value=ValueRef(type="column", name="structure_threshold")),
            ])),
            WorkflowNode(id="out", type=NodeType.OUTCOME, outcomes=[
                OutcomeAction(field="Reason", value=ValueRef(type="static", value=rule_id)),
            ]),
        ],
        edges=[WorkflowEdge(source="in", target="lk"), WorkflowEdge(source="lk", target="cond"),
               WorkflowEdge(source="cond", target="out")],
    ))
    rule_store.upsert_rule(rule, "tester")
    rule.status = RuleStatus.PUBLISHED
    return rule_store.upsert_rule(rule, "tester")


def test_evaluate_record_context_rows_gives_self_group_lookup_sibling_visibility():
    # A lone record is its own entire group — without sibling rows, a
    # self_group LOOKUP's sum would just be that one record's own value,
    # not the whole structure's. `context_rows` is what a per-trade
    # validator wrapper passes to restore group visibility. Alone: 200 <=
    # 250 clears. With a sibling in the same structure: summed 300 > 250
    # breaches, so the rule no longer matches.
    _pub_self_group_rule()
    record = {"structure_id": "S1", "pnl": 200.0, "threshold": 250.0}
    sibling = {"structure_id": "S1", "pnl": 100.0, "threshold": None}

    lone = product_engine.evaluate_record(TEST_PRODUCT, record, "tester")
    assert lone.matched is True and lone.matched_rule_id == "SG1"

    grouped = product_engine.evaluate_record(TEST_PRODUCT, record, "tester", context_rows=[sibling])
    assert grouped.matched is False


def test_evaluate_record_context_rows_only_reflects_the_target_record():
    # `record`'s own result is returned even when siblings are passed in —
    # they're there purely for grouping visibility, not extra output rows.
    _pub_self_group_rule()
    record = {"structure_id": "S1", "pnl": 50.0, "threshold": 250.0}
    # threshold is duplicated across every leg (as in the real data) so the
    # broadcast value doesn't depend on which row is picked as
    # representative — this test is about which record's own result comes
    # back, not the representative-row-selection semantics covered above.
    sibling = {"structure_id": "S1", "pnl": 50.0, "threshold": 250.0}
    result = product_engine.evaluate_record(TEST_PRODUCT, record, "tester", context_rows=[sibling])
    assert result.matched is True and result.matched_rule_id == "SG1"  # 100 summed <= 250


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
