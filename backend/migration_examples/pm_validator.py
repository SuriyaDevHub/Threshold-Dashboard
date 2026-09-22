"""New thin `pm_validator.py` — replaces the hardcoded per-rule Python in
the legacy PM validator with a single call into the shared rule engine
(Threshold-Dashboard's Rule Designer / `product_engine.evaluate_record`).

This file lives in the Threshold-Dashboard repo as a DELIVERABLE ONLY: the
legacy `pm_validator.py` it replaces lives in your separate production
tool's codebase, not here. Copy this file over that one once PM's shadow
test results look right (see migrate_pm_validator.py's own warning about
its rules being structural templates, not byte-exact yet).

Preserves the class/method shape confirmed from the legacy file
(`PmValidator.validate_trade(trade) -> PmValidationResult`) so every call
site in your prod tool keeps working unchanged — only the body changes,
from hardcoded per-rule `if` branches to one HTTP call.

Two integration options are shown; pick the one matching your deployment:

  1. HTTP (default below) — your prod tool and Threshold-Dashboard's
     backend are separate services/processes. Safest default: no shared
     Python environment or import path required.
  2. In-process — only if your prod tool literally runs inside the same
     Python process/venv as this repo's backend (e.g. a shared monorepo
     deploy). See the commented-out alternative at the bottom of
     `validate_trade()`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

RULE_ENGINE_BASE_URL = os.environ.get("RULE_ENGINE_BASE_URL", "http://localhost:8000/api/rule-designer")
RULE_ENGINE_ACTOR = os.environ.get("RULE_ENGINE_ACTOR", "pm_validator")
RULE_ENGINE_TIMEOUT_S = float(os.environ.get("RULE_ENGINE_TIMEOUT_S", "5"))


@dataclass
class PmValidationResult:
    """Same shape as the legacy dataclass — align field names here to your
    real one if they differ; this is the only part of this file that
    should need adjusting for your exact call sites."""
    status: str                       # "ALERT" | "CLEAR"
    reason_code: Optional[str] = None
    commentary: Optional[str] = None


class PmValidatorEngineError(RuntimeError):
    """Raised when the rule engine itself is unreachable or errors — kept
    distinct from a normal ALERT/CLEAR result so callers can decide
    whether to fail closed (treat as ALERT) or propagate, rather than
    that choice being silently made inside this wrapper."""


class PmValidator:
    """Drop-in replacement for the legacy PmValidator. No rule-ID-specific
    code lives here any more — every OAR-PM-* rule now lives in the Rule
    Designer's PM product, authored/versioned/approved there instead of
    hardcoded in this file."""

    def validate_trade(self, trade: dict) -> PmValidationResult:
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
            return PmValidationResult(
                status="ALERT",
                reason_code=outcome.get("Reason"),
                commentary=outcome.get("Commentary"),
            )
        return PmValidationResult(status="CLEAR")

        # --- in-process alternative (same Python env as Threshold-Dashboard) ---
        # from app.modules.rule_designer import product_engine
        # result = product_engine.evaluate_record("PM", trade, RULE_ENGINE_ACTOR)
        # if result.matched and result.outcome.get("Alert"):
        #     return PmValidationResult(status="ALERT", reason_code=result.outcome.get("Reason"),
        #                                commentary=result.outcome.get("Commentary"))
        # return PmValidationResult(status="CLEAR")
