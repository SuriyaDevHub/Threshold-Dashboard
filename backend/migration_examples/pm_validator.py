"""New `pm_validator.py` — runs the shared rule engine ALONGSIDE the real
legacy PM validator so the migration can be proven safe before cutover.

This file lives in the Threshold-Dashboard repo as a DELIVERABLE ONLY: it
is meant to replace the legacy `pm_validator.py` in your separate
production tool's codebase, once you're ready — but its whole point is
that you are NOT ready on day one. It defaults to "shadow" mode: every
call computes both the new engine's result and your existing legacy
validator's result, logs them side by side, and returns the LEGACY
result — production behavior is completely unchanged while you configure
and watch OAR-PM-* rules in Rule Designer. Only an explicit switch to
"cutover" mode makes the new engine's result the one that's actually
returned.

Preserves the class/method shape confirmed from the legacy file
(`PmValidator.validate_trade(trade) -> PmValidationResult`) so every call
site in your prod tool keeps working unchanged.

Wiring in your real legacy logic
---------------------------------
This file never imports your legacy validator directly (that would
couple two codebases together for no reason) — instead your call site
hands it a `legacy_validate` callable, typically just the bound method of
your current `PmValidator` instance, however you deploy this:

    from legacy.pm_validator import PmValidator as LegacyPmValidator
    from pm_validator import PmValidator  # this file

    legacy = LegacyPmValidator()
    validator = PmValidator(legacy_validate=legacy.validate_trade)

    result = validator.validate_trade(trade)   # legacy result, both logged

Two integration options for reaching the new engine itself, same as
before: HTTP (default, works whenever the two services are separate
processes) or in-process (see the commented alternative below `_evaluate_new`).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

import requests

RULE_ENGINE_BASE_URL = os.environ.get("RULE_ENGINE_BASE_URL", "http://localhost:8000/api/modules/rule-designer")
RULE_ENGINE_ACTOR = os.environ.get("RULE_ENGINE_ACTOR", "pm_validator")
RULE_ENGINE_TIMEOUT_S = float(os.environ.get("RULE_ENGINE_TIMEOUT_S", "5"))

# "shadow" (default, safe): compute both, log both, return the LEGACY
# result — production behavior is unchanged.
# "cutover": return the NEW engine's result instead. Flip this only after
# a shadow-test run (see shadow_test_service.py) and/or enough real
# "shadow" mode traffic shows agreement you're comfortable with.
PM_VALIDATOR_MODE = os.environ.get("PM_VALIDATOR_MODE", "shadow").strip().lower()

log = logging.getLogger("pm_validator.parallel")


@dataclass
class PmValidationResult:
    """Same shape as the legacy dataclass — align field names here to your
    real one if they differ; this is the only part of this file that
    should need adjusting for your exact call sites."""
    status: str                       # "ALERT" | "CLEAR"
    reason_code: Optional[str] = None
    commentary: Optional[str] = None


class PmValidatorEngineError(RuntimeError):
    """Raised when the NEW rule engine itself is unreachable or errors —
    kept distinct from a normal ALERT/CLEAR result so callers can decide
    whether to fail closed, propagate, or (in shadow mode) simply fall
    back to the legacy result, which is what `validate_trade` below does."""


def _trade_label(trade: dict) -> str:
    for key in ("trade_id", "trade_ref", "deal_id", "id"):
        if trade.get(key) is not None:
            return str(trade[key])
    return "<no trade id>"


class PmValidator:
    """Parallel-run wrapper: every OAR-PM-* rule now lives in the Rule
    Designer's PM product (authored/versioned/approved there, not
    hardcoded in this file), but nothing about your production output
    changes until `mode` is explicitly "cutover"."""

    def __init__(self, legacy_validate: Optional[Callable[[dict], "PmValidationResult"]] = None,
                 mode: Optional[str] = None):
        self._legacy_validate = legacy_validate
        self.mode = (mode or PM_VALIDATOR_MODE)
        if self.mode not in ("shadow", "cutover"):
            raise ValueError(f"PM_VALIDATOR_MODE must be 'shadow' or 'cutover', got {self.mode!r}")
        if self.mode == "shadow" and legacy_validate is None:
            log.warning(
                "PmValidator is in 'shadow' mode but no legacy_validate was supplied — "
                "there is nothing to compare against, so this call effectively runs cutover. "
                "Pass legacy_validate=<your legacy PmValidator instance>.validate_trade to fix this."
            )

    def validate_trade(self, trade: dict) -> "PmValidationResult":
        new_result: Optional[PmValidationResult] = None
        new_error: Optional[Exception] = None
        try:
            new_result = self._evaluate_new(trade)
        except PmValidatorEngineError as exc:
            new_error = exc

        legacy_result: Optional[PmValidationResult] = None
        if self._legacy_validate is not None:
            legacy_result = self._legacy_validate(trade)

        self._log_comparison(trade, new_result, new_error, legacy_result)

        if self.mode == "cutover":
            if new_result is not None:
                return new_result
            # The new engine is down mid-cutover — fail back to legacy
            # rather than silently clearing a trade that should alert.
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

    def _log_comparison(self, trade: dict, new_result: Optional[PmValidationResult],
                         new_error: Optional[Exception], legacy_result: Optional[PmValidationResult]) -> None:
        label = _trade_label(trade)
        if new_error is not None:
            log.warning("trade %s: new-engine call failed (%s); legacy=%s", label, new_error,
                        legacy_result and legacy_result.status)
            return
        if legacy_result is None:
            log.info("trade %s: new=%s/%s (no legacy result to compare — mode=%s)",
                      label, new_result.status, new_result.reason_code, self.mode)
            return
        agree = (new_result.status == legacy_result.status
                 and new_result.reason_code == legacy_result.reason_code)
        level = log.info if agree else log.warning
        level("trade %s: %s — new=%s/%s legacy=%s/%s", label,
              "AGREE" if agree else "DISAGREE",
              new_result.status, new_result.reason_code,
              legacy_result.status, legacy_result.reason_code)

    def _evaluate_new(self, trade: dict) -> PmValidationResult:
        try:
            resp = requests.post(
                f"{RULE_ENGINE_BASE_URL}/products/PM/evaluate-record",
                json={"actor": RULE_ENGINE_ACTOR, "role": "USER", "record": trade},
                timeout=RULE_ENGINE_TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PmValidatorEngineError(f"rule engine call failed: {exc}") from exc

        body = resp.json()
        outcome = body.get("outcome") or {}
        if body.get("matched") and outcome.get("Alert"):
            return PmValidationResult(status="ALERT", reason_code=outcome.get("Reason"),
                                       commentary=outcome.get("Commentary"))
        return PmValidationResult(status="CLEAR")

        # --- in-process alternative (same Python env as Threshold-Dashboard) ---
        # from app.modules.rule_designer import product_engine
        # result = product_engine.evaluate_record("PM", trade, RULE_ENGINE_ACTOR)
        # if result.matched and result.outcome.get("Alert"):
        #     return PmValidationResult(status="ALERT", reason_code=result.outcome.get("Reason"),
        #                                commentary=result.outcome.get("Commentary"))
        # return PmValidationResult(status="CLEAR")
