"""New `fxo_validator.py` — runs the shared rule engine ALONGSIDE the real
legacy FXO validator so the migration can be proven safe before cutover.
Same deliverable shape as `pm_validator.py` in this same directory (read
that file's own docstring for the full rationale); the one difference
worth calling out up front:

**Group context.** OAR-FXO-004/005/006 broadcast a value computed across
every trade sharing a structure id or deal ref (a structure's total PnL,
a parent leg's threshold — see `migrate_fxo_validator.py`'s self-group
LOOKUP nodes) onto every trade in that group. The new engine can only see
that context when it's handed more than one row at a time, so
`validate_trade` below takes an optional `group_rows` — the trade's
siblings, not required for every trade (001/002/003 are per-trade rules
with no grouping) but needed for 004/005/006 to evaluate correctly. If
you already have the day's full trade file in memory when calling this
(the common case for a batch OMRC run), use `validate_batch` instead: it
groups by `omrctradeStructureid`/`omrctradeDealrefid` once, up front, and
calls `validate_trade` with the right siblings for each trade — no manual
grouping needed at your call site.

This file is a DELIVERABLE ONLY, meant to replace the legacy
`fxo_validator.py` in your separate production tool's codebase once
you're ready. It defaults to "shadow" mode: every call computes both the
new engine's result and your existing legacy validator's result, logs
them side by side, and returns the LEGACY result — production behavior
is unchanged while you watch OAR-FXO-* rules in Rule Designer. Only an
explicit switch to "cutover" mode makes the new engine's result the one
actually returned. (FXO is `enabled: false` in the legacy
`omrc_rules.yml` today, per `migrate_fxo_validator.py`'s header — shadow
mode here doesn't change that; it just gives you a way to compare the new
engine's decisions against the legacy code path whenever it does run.)

Preserves the class/method shape confirmed from the legacy file
(`FxoValidator.validate_trade(trade) -> FxoValidationResult`) so any call
site in your prod tool that only ever calls `validate_trade` per-trade
keeps working unchanged — `group_rows` is purely additive.

Wiring in your real legacy logic
---------------------------------
This file never imports your legacy validator directly — your call site
hands it a `legacy_validate` callable, typically just the bound method of
your current `FxoValidator` instance:

    from legacy.fxo_validator import FxoValidator as LegacyFxoValidator
    from fxo_validator import FxoValidator  # this file

    legacy = LegacyFxoValidator()
    validator = FxoValidator(legacy_validate=legacy.validate_trade)

    result = validator.validate_trade(trade)                    # per-trade, no grouping
    result = validator.validate_trade(trade, group_rows=siblings)  # per-trade, with grouping
    results = validator.validate_batch(all_trades)              # whole file, grouping done for you

Two integration options for reaching the new engine itself, same as
before: HTTP (default) or in-process (see the commented alternative below
`_evaluate_new`).
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import requests

RULE_ENGINE_BASE_URL = os.environ.get("RULE_ENGINE_BASE_URL", "http://localhost:8000/api/modules/rule-designer")
RULE_ENGINE_ACTOR = os.environ.get("RULE_ENGINE_ACTOR", "fxo_validator")
RULE_ENGINE_TIMEOUT_S = float(os.environ.get("RULE_ENGINE_TIMEOUT_S", "5"))

# "shadow" (default, safe): compute both, log both, return the LEGACY
# result — production behavior is unchanged.
# "cutover": return the NEW engine's result instead. Flip this only after
# a shadow-test run and/or enough real "shadow" mode traffic shows
# agreement you're comfortable with.
FXO_VALIDATOR_MODE = os.environ.get("FXO_VALIDATOR_MODE", "shadow").strip().lower()

# The two grouping keys OAR-FXO-004/005/006 use — see
# migrate_fxo_validator.py's oar_fxo_004/005/006 for which rule uses which.
GROUP_KEY_FIELDS = ("omrctradeStructureid", "omrctradeDealrefid")

log = logging.getLogger("fxo_validator.parallel")


@dataclass
class FxoValidationResult:
    """Same shape as the legacy dataclass — align field names here to
    your real one if they differ; this is the only part of this file that
    should need adjusting for your exact call sites."""
    status: str                       # "ALERT" | "CLEAR"
    rule_id: Optional[str] = None
    reason_code: Optional[str] = None
    commentary: Optional[str] = None


class FxoValidatorEngineError(RuntimeError):
    """Raised when the NEW rule engine itself is unreachable or errors —
    kept distinct from a normal ALERT/CLEAR result so callers can decide
    whether to fail closed, propagate, or (in shadow mode) simply fall
    back to the legacy result, which is what `validate_trade` below does."""


def _trade_label(trade: dict) -> str:
    for key in ("trade_id", "omrctradeDealrefid", "deal_id", "id"):
        if trade.get(key) is not None:
            return str(trade[key])
    return "<no trade id>"


def _group_rows_by_keys(trades: List[dict]) -> Dict[str, Dict[object, List[dict]]]:
    """One index per grouping field, e.g. {"omrctradeStructureid": {"STR1": [...]}}."""
    index: Dict[str, Dict[object, List[dict]]] = {f: defaultdict(list) for f in GROUP_KEY_FIELDS}
    for trade in trades:
        for field in GROUP_KEY_FIELDS:
            key = trade.get(field)
            if key is not None and key != "":
                index[field][key].append(trade)
    return index


class FxoValidator:
    """Parallel-run wrapper: every OAR-FXO-* rule now lives in the Rule
    Designer's FXO product (authored/versioned/approved there, not
    hardcoded in this file), but nothing about your production output
    changes until `mode` is explicitly "cutover"."""

    def __init__(self, legacy_validate: Optional[Callable[[dict], "FxoValidationResult"]] = None,
                 mode: Optional[str] = None):
        self._legacy_validate = legacy_validate
        self.mode = (mode or FXO_VALIDATOR_MODE)
        if self.mode not in ("shadow", "cutover"):
            raise ValueError(f"FXO_VALIDATOR_MODE must be 'shadow' or 'cutover', got {self.mode!r}")
        if self.mode == "shadow" and legacy_validate is None:
            log.warning(
                "FxoValidator is in 'shadow' mode but no legacy_validate was supplied — "
                "there is nothing to compare against, so this call effectively runs cutover. "
                "Pass legacy_validate=<your legacy FxoValidator instance>.validate_trade to fix this."
            )

    def validate_batch(self, trades: List[dict]) -> List["FxoValidationResult"]:
        """Convenience entry point for a whole day's trade file: groups by
        omrctradeStructureid and omrctradeDealrefid once, then calls
        validate_trade per trade with the right siblings already attached
        — nothing for the caller to pre-group. A trade that shares a key
        with others gets those others (itself excluded) as group_rows for
        whichever key(s) actually matched; a trade with no group key at
        all (or the only member of its group) is validated alone, exactly
        as validate_trade(trade) with no group_rows."""
        by_field = _group_rows_by_keys(trades)
        results = []
        for trade in trades:
            siblings: List[dict] = []
            seen_ids = set()
            for field in GROUP_KEY_FIELDS:
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

    def validate_trade(self, trade: dict, group_rows: Optional[List[dict]] = None) -> "FxoValidationResult":
        new_result: Optional[FxoValidationResult] = None
        new_error: Optional[Exception] = None
        try:
            new_result = self._evaluate_new(trade, group_rows)
        except FxoValidatorEngineError as exc:
            new_error = exc

        legacy_result: Optional[FxoValidationResult] = None
        if self._legacy_validate is not None:
            # The legacy validator's own grouping (_compute_th_structure /
            # _compute_ln_murex_and_margin) reaches its sibling trades
            # however your production code already does that (a preloaded
            # dataset, a DB query, ...) — group_rows is new-engine-only,
            # so the legacy call site is untouched.
            legacy_result = self._legacy_validate(trade)

        self._log_comparison(trade, new_result, new_error, legacy_result)

        if self.mode == "cutover":
            if new_result is not None:
                return new_result
            log.error("cutover mode: new engine call failed for %s, falling back to legacy result", _trade_label(trade))
            if legacy_result is not None:
                return legacy_result
            raise new_error  # no legacy result to fall back to either

        # shadow mode: legacy result is production truth, always.
        if legacy_result is not None:
            return legacy_result
        if new_result is not None:
            return new_result
        raise new_error

    def _log_comparison(self, trade: dict, new_result: Optional[FxoValidationResult],
                         new_error: Optional[Exception], legacy_result: Optional[FxoValidationResult]) -> None:
        label = _trade_label(trade)
        if new_error is not None:
            log.warning("trade %s: new-engine call failed (%s); legacy=%s", label, new_error,
                        legacy_result and legacy_result.status)
            return
        if legacy_result is None:
            log.info("trade %s: new=%s/%s/%s (no legacy result to compare — mode=%s)",
                      label, new_result.status, new_result.rule_id, new_result.reason_code, self.mode)
            return
        agree = (new_result.status == legacy_result.status
                 and new_result.reason_code == legacy_result.reason_code)
        level = log.info if agree else log.warning
        level("trade %s: %s — new=%s/%s/%s legacy=%s/%s/%s", label,
              "AGREE" if agree else "DISAGREE",
              new_result.status, new_result.rule_id, new_result.reason_code,
              legacy_result.status, legacy_result.rule_id, legacy_result.reason_code)

    def _evaluate_new(self, trade: dict, group_rows: Optional[List[dict]]) -> FxoValidationResult:
        try:
            resp = requests.post(
                f"{RULE_ENGINE_BASE_URL}/products/FXO/evaluate-record",
                json={"actor": RULE_ENGINE_ACTOR, "role": "USER", "record": trade, "context_rows": group_rows},
                timeout=RULE_ENGINE_TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FxoValidatorEngineError(f"rule engine call failed: {exc}") from exc

        body = resp.json()
        outcome = body.get("outcome") or {}
        if body.get("matched") and outcome.get("Alert"):
            return FxoValidationResult(status="ALERT", rule_id=body.get("matched_rule_id"),
                                        reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"))
        return FxoValidationResult(status="CLEAR", rule_id=body.get("matched_rule_id"),
                                    reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"))

        # --- in-process alternative (same Python env as Threshold-Dashboard) ---
        # from app.modules.rule_designer import product_engine
        # result = product_engine.evaluate_record("FXO", trade, RULE_ENGINE_ACTOR, context_rows=group_rows)
        # outcome = result.outcome or {}
        # if result.matched and outcome.get("Alert"):
        #     return FxoValidationResult(status="ALERT", rule_id=result.matched_rule_id,
        #                                 reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"))
        # return FxoValidationResult(status="CLEAR", rule_id=result.matched_rule_id,
        #                             reason_code=outcome.get("Reason"), commentary=outcome.get("Commentary"))
