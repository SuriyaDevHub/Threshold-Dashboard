"""The generic validator: ONE shadow-mode wrapper for every product, not
one hand-written file per product. This is the file to point at when
someone asks "what does a NEW product's validator wrapper look like" —
the answer is `GenericValidator(product="WHATEVER")`, with no new Python
written, because everything product-specific already lives in Rule
Designer (the rules themselves) rather than in this file.

Why this exists (read this if you're wondering "didn't we already build
pm_validator.py and fxo_validator.py?"): those two files are real and
still work, but they were written as separate, nearly-identical Python
files — their own class name, their own result dataclass, their own env
var, and (fxo_validator.py only) a hardcoded list of which fields to
group sibling trades by. That shape doesn't scale: every future product
migration would mean copy-pasting a new file and re-deciding the same
few design choices each time. This file is that logic collapsed into
one product-agnostic implementation; pm_validator.py and fxo_validator.py
now just alias it (see their own files) so any code already importing
PmValidator/FxoValidator by name keeps working unchanged.

What "generic" means concretely:
- product_engine.evaluate_record()/evaluate_product() (the actual rule
  engine this calls) already dispatch purely by product code — no
  per-product code there either. This file is the last mile: the same
  property, extended to the shadow/cutover wrapper a production call
  site actually imports.
- Grouping keys for self-group LOOKUP rules (see fxo_validator.py's
  history — OAR-FXO-004/005/006 need to see sibling trades) are NOT
  hardcoded here. They're discovered from GET /products/{code}/group-keys,
  which inspects the product's own published rules for self-group LOOKUP
  nodes. A brand-new product that needs grouping just needs its rules
  authored with a self-group LOOKUP node in Rule Designer — this file
  picks that up automatically, with zero code changes.
- The shadow/cutover mode env var is generic too: GENERIC_VALIDATOR_MODE
  falls back to VALIDATOR_MODE, but a `{PRODUCT}_VALIDATOR_MODE` env var
  (e.g. PM_VALIDATOR_MODE, FXO_VALIDATOR_MODE — the exact names the two
  existing per-product files already used) always wins if set, so
  existing deployments' env config keeps working unchanged.

Usage for any product, existing or new:

    from legacy.newproduct_validator import NewProductValidator as LegacyValidator
    from generic_validator import GenericValidator

    legacy = LegacyValidator()
    validator = GenericValidator(product="NEWPRODUCT", legacy_validate=legacy.validate_trade)

    result = validator.validate_trade(trade)                      # per-trade, no grouping
    result = validator.validate_trade(trade, group_rows=siblings)  # per-trade, with grouping
    results = validator.validate_batch(all_trades)                # whole file, auto-grouped

Defaults to "shadow" mode (computes both, logs the comparison, returns
the LEGACY result — production behavior unchanged) exactly like
pm_validator.py/fxo_validator.py did. Flip to "cutover" only after
you're comfortable with what shadow-mode logging shows.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import requests

RULE_ENGINE_BASE_URL = os.environ.get("RULE_ENGINE_BASE_URL", "http://localhost:8000/api/modules/rule-designer")
RULE_ENGINE_ACTOR = os.environ.get("RULE_ENGINE_ACTOR", "generic_validator")
RULE_ENGINE_TIMEOUT_S = float(os.environ.get("RULE_ENGINE_TIMEOUT_S", "5"))
# A whole-dataset pass (validate_dataset) reads and evaluates far more rows
# than a single validate_trade() call — separate, longer timeout so a big
# batch doesn't get cut off at the per-trade default.
RULE_ENGINE_DATASET_TIMEOUT_S = float(os.environ.get("RULE_ENGINE_DATASET_TIMEOUT_S", "120"))

log = logging.getLogger("generic_validator.parallel")


@dataclass
class ValidationResult:
    """Same shape pm_validator.py/fxo_validator.py already used (status/
    reason_code/commentary, plus rule_id — PM's legacy result never had
    one, but an unused default field doesn't break anything that only
    ever read status/reason_code/commentary from it)."""
    status: str                       # "ALERT" | "CLEAR"
    rule_id: Optional[str] = None
    reason_code: Optional[str] = None
    commentary: Optional[str] = None


class GenericValidatorEngineError(RuntimeError):
    """Raised when the NEW rule engine itself is unreachable or errors —
    kept distinct from a normal ALERT/CLEAR result so callers can decide
    whether to fail closed, propagate, or (in shadow mode) simply fall
    back to the legacy result, which is what validate_trade below does."""


def _trade_label(trade: dict) -> str:
    for key in ("trade_id", "trade_ref", "deal_id", "dealref", "id"):
        if trade.get(key) is not None:
            return str(trade[key])
    return "<no trade id>"


def _group_rows_by_keys(trades: List[dict], key_fields: List[str]) -> Dict[str, Dict[object, List[dict]]]:
    index: Dict[str, Dict[object, List[dict]]] = {f: defaultdict(list) for f in key_fields}
    for trade in trades:
        for field in key_fields:
            key = trade.get(field)
            if key is not None and key != "":
                index[field][key].append(trade)
    return index


class GenericValidator:
    """Parallel-run wrapper for ANY product's rules — every OAR-* rule
    lives in Rule Designer (authored/versioned/approved there, not
    hardcoded in this file, or in any product-specific Python file at
    all), and nothing about production output changes until `mode` is
    explicitly "cutover"."""

    def __init__(self, product: str, legacy_validate: Optional[Callable[[dict], "ValidationResult"]] = None,
                 mode: Optional[str] = None, group_key_fields: Optional[List[str]] = None):
        self.product = product.upper()
        self._legacy_validate = legacy_validate
        self.mode = (mode or os.environ.get(f"{self.product}_VALIDATOR_MODE")
                     or os.environ.get("GENERIC_VALIDATOR_MODE") or os.environ.get("VALIDATOR_MODE", "shadow")).strip().lower()
        if self.mode not in ("shadow", "cutover"):
            raise ValueError(f"validator mode for {self.product} must be 'shadow' or 'cutover', got {self.mode!r}")
        if self.mode == "shadow" and legacy_validate is None:
            log.warning(
                "GenericValidator(%s) is in 'shadow' mode but no legacy_validate was supplied — "
                "there is nothing to compare against, so this call effectively runs cutover. "
                "Pass legacy_validate=<your legacy validator instance>.validate_trade to fix this.",
                self.product,
            )
        # None (not yet discovered) vs. [] (discovered, product needs no grouping) vs. an
        # explicit caller-supplied override — all distinct, so discovery only ever runs once.
        self._group_key_fields = group_key_fields

    def _group_key_fields_or_discover(self) -> List[str]:
        if self._group_key_fields is not None:
            return self._group_key_fields
        try:
            resp = requests.get(f"{RULE_ENGINE_BASE_URL}/products/{self.product}/group-keys",
                                 timeout=RULE_ENGINE_TIMEOUT_S)
            resp.raise_for_status()
            self._group_key_fields = resp.json().get("group_key_fields") or []
        except requests.RequestException as exc:
            log.warning("could not discover group keys for %s (%s) — grouping disabled for this run",
                        self.product, exc)
            self._group_key_fields = []
        return self._group_key_fields

    def validate_dataset(self, dataset_id: str, record_id_field: Optional[str] = None) -> Dict[str, "ValidationResult"]:
        """The fast path for a batch that's already a dataset — e.g. one
        pulled via Data Fetch — instead of looping validate_trade() (one
        HTTP call per row) over it. Makes ONE call to
        product_engine.evaluate_product(), which evaluates every active
        rule once across the whole dataset in-process, rather than N calls
        each re-running every rule for a single row; for self-group LOOKUP
        rules (grouping by structure/dealref, etc.) this also groups
        correctly over the entire dataset in one pass, with none of
        validate_trade()'s per-call context_rows plumbing needed at all.

        Returns {record_id: ValidationResult}, one entry per dataset row
        (keys are strings — the record_id_field's value, or the row index
        when record_id_field is omitted, matching how the dataset's own
        rows are ordered).

        Shadow mode still applies, just computed differently: if
        legacy_validate was supplied, it's called once per row — a local
        Python call, not a network round trip, so batching it doesn't cost
        what batching calls to the NEW engine did — purely to log AGREE/
        DISAGREE and to decide which result each row actually returns,
        the same shadow-vs-cutover rule validate_trade() uses."""
        rows = self._fetch_dataset_rows(dataset_id)
        try:
            resp = requests.post(
                f"{RULE_ENGINE_BASE_URL}/products/{self.product}/evaluate",
                json={"actor": RULE_ENGINE_ACTOR, "role": "USER", "dataset_id": dataset_id,
                      "record_id_field": record_id_field, "record_sample_cap": len(rows)},
                timeout=RULE_ENGINE_DATASET_TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise GenericValidatorEngineError(f"rule engine dataset evaluation failed for {self.product}: {exc}") from exc

        # --- in-process alternative (preferred where available — see
        # _fetch_dataset_rows's own in-process note below) ---
        # from app.modules.rule_designer import product_engine
        # result = product_engine.evaluate_product(self.product, dataset_id, RULE_ENGINE_ACTOR,
        #                                           record_id_field, record_sample_cap=len(rows))
        # records_json = [r.model_dump(mode="json") for r in result.records]  # then map same as below

        new_by_id: Dict[str, ValidationResult] = {}
        for rec in resp.json().get("records", []):
            outcome = rec.get("outcome") or {}
            status = "ALERT" if (rec.get("matched") and outcome.get("Alert")) else "CLEAR"
            new_by_id[str(rec.get("record_id"))] = ValidationResult(
                status=status, rule_id=rec.get("matched_rule_id"),
                reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"),
            )

        if self._legacy_validate is None:
            if self.mode == "shadow":
                log.warning(
                    "GenericValidator(%s).validate_dataset: shadow mode with no legacy_validate — "
                    "nothing to compare against, returning the new engine's results.", self.product,
                )
            return new_by_id

        results: Dict[str, ValidationResult] = {}
        for i, row in enumerate(rows):
            rid = str(row.get(record_id_field)) if record_id_field else str(i)
            new_result = new_by_id.get(rid)
            legacy_result = self._legacy_validate(row)
            self._log_comparison(row, new_result, None, legacy_result)
            results[rid] = new_result if (self.mode == "cutover" and new_result is not None) else legacy_result
        return results

    def _fetch_dataset_rows(self, dataset_id: str) -> List[dict]:
        try:
            resp = requests.get(f"{RULE_ENGINE_BASE_URL}/datasets/{dataset_id}/preview",
                                 params={"limit": 10_000_000}, timeout=RULE_ENGINE_DATASET_TIMEOUT_S)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise GenericValidatorEngineError(f"could not read dataset {dataset_id}: {exc}") from exc
        return resp.json().get("rows", [])

        # --- in-process alternative (same Python env as Threshold-Dashboard —
        # the natural fit, since tcfc_omrc's exception_analysis/data_fetch/
        # rule_designer all run inside the same PyInstaller-packaged process) ---
        # from app.core import store as dataset_store
        # return dataset_store.get_rows(dataset_id) or []

    def validate_batch(self, trades: List[dict]) -> List["ValidationResult"]:
        """Convenience entry point for a whole day's trade file: groups by
        whatever field(s) this product's own rules declare (auto-discovered,
        not hardcoded here — see GET /products/{code}/group-keys), then
        calls validate_trade per trade with the right siblings already
        attached. A product with no grouping rules (most products) just
        calls validate_trade(trade) with no group_rows for every trade."""
        key_fields = self._group_key_fields_or_discover()
        if not key_fields:
            return [self.validate_trade(trade) for trade in trades]

        by_field = _group_rows_by_keys(trades, key_fields)
        results = []
        for trade in trades:
            siblings: List[dict] = []
            seen_ids = set()
            for field in key_fields:
                key = trade.get(field)
                if key is None or key == "":
                    continue
                for sibling in by_field[field].get(key, []):
                    if sibling is trade or id(sibling) in seen_ids:
                        continue
                    seen_ids.add(id(sibling))
                    siblings.append(sibling)
            results.append(self.validate_trade(trade, group_rows=siblings or None))
        return results

    def validate_trade(self, trade: dict, group_rows: Optional[List[dict]] = None) -> "ValidationResult":
        new_result: Optional[ValidationResult] = None
        new_error: Optional[Exception] = None
        try:
            new_result = self._evaluate_new(trade, group_rows)
        except GenericValidatorEngineError as exc:
            new_error = exc

        legacy_result: Optional[ValidationResult] = None
        if self._legacy_validate is not None:
            # The legacy validator's own grouping (if any) reaches its
            # sibling trades however your production code already does
            # that — group_rows is new-engine-only, so the legacy call
            # site is untouched.
            legacy_result = self._legacy_validate(trade)

        self._log_comparison(trade, new_result, new_error, legacy_result)

        if self.mode == "cutover":
            if new_result is not None:
                return new_result
            log.error("cutover mode: new engine call failed for %s (%s), falling back to legacy result",
                      self.product, _trade_label(trade))
            if legacy_result is not None:
                return legacy_result
            raise new_error  # no legacy result to fall back to either

        # shadow mode: legacy result is production truth, always.
        if legacy_result is not None:
            return legacy_result
        if new_result is not None:
            return new_result
        raise new_error

    def _log_comparison(self, trade: dict, new_result: Optional[ValidationResult],
                         new_error: Optional[Exception], legacy_result: Optional[ValidationResult]) -> None:
        label = _trade_label(trade)
        if new_error is not None:
            log.warning("%s trade %s: new-engine call failed (%s); legacy=%s", self.product, label, new_error,
                        legacy_result and legacy_result.status)
            return
        if legacy_result is None:
            log.info("%s trade %s: new=%s/%s/%s (no legacy result to compare — mode=%s)",
                      self.product, label, new_result.status, new_result.rule_id, new_result.reason_code, self.mode)
            return
        agree = (new_result.status == legacy_result.status
                 and new_result.reason_code == legacy_result.reason_code)
        level = log.info if agree else log.warning
        level("%s trade %s: %s — new=%s/%s/%s legacy=%s/%s/%s", self.product, label,
              "AGREE" if agree else "DISAGREE",
              new_result.status, new_result.rule_id, new_result.reason_code,
              legacy_result.status, legacy_result.rule_id, legacy_result.reason_code)

    def _evaluate_new(self, trade: dict, group_rows: Optional[List[dict]]) -> ValidationResult:
        try:
            resp = requests.post(
                f"{RULE_ENGINE_BASE_URL}/products/{self.product}/evaluate-record",
                json={"actor": RULE_ENGINE_ACTOR, "role": "USER", "record": trade, "context_rows": group_rows},
                timeout=RULE_ENGINE_TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise GenericValidatorEngineError(f"rule engine call failed for {self.product}: {exc}") from exc

        body = resp.json()
        outcome = body.get("outcome") or {}
        status = "ALERT" if (body.get("matched") and outcome.get("Alert")) else "CLEAR"
        return ValidationResult(status=status, rule_id=body.get("matched_rule_id"),
                                 reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"))

        # --- in-process alternative (same Python env as Threshold-Dashboard) ---
        # from app.modules.rule_designer import product_engine
        # result = product_engine.evaluate_record(self.product, trade, RULE_ENGINE_ACTOR, context_rows=group_rows)
        # outcome = result.outcome or {}
        # status = "ALERT" if (result.matched and outcome.get("Alert")) else "CLEAR"
        # return ValidationResult(status=status, rule_id=result.matched_rule_id,
        #                          reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"))
