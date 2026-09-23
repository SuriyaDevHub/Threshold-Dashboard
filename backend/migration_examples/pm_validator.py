"""PM's validator wrapper — a thin alias over generic_validator.py, kept
under this name/class only so any production call site already doing
`from pm_validator import PmValidator, PmValidationResult` keeps working
unchanged. All the actual shadow/cutover logic and HTTP calls live in
generic_validator.py now. See generic_validator.py's own docstring for
the full rationale and for how a brand-new product's validator wrapper
is written (spoiler: no new file, just `GenericValidator(product="WHATEVER")`).

PM has no self-group LOOKUP rules, so GET /products/PM/group-keys
returns an empty list and validate_batch() just calls validate_trade()
per trade with no grouping — this file doesn't need to know that; it's
discovered the same way for every product.

Usage — identical to before:

    from legacy.pm_validator import PmValidator as LegacyPmValidator
    from pm_validator import PmValidator

    legacy = LegacyPmValidator()
    validator = PmValidator(legacy_validate=legacy.validate_trade)

    result = validator.validate_trade(trade)

Set PM_VALIDATOR_MODE=cutover (same env var name as before) to switch
from shadow to cutover; see generic_validator.py for the fallback chain
if that specific var isn't set.
"""
from __future__ import annotations

from typing import Callable, Optional

from generic_validator import GenericValidator, ValidationResult as PmValidationResult

__all__ = ["PmValidator", "PmValidationResult"]


class PmValidator(GenericValidator):
    def __init__(self, legacy_validate: Optional[Callable[[dict], "PmValidationResult"]] = None,
                 mode: Optional[str] = None):
        super().__init__(product="PM", legacy_validate=legacy_validate, mode=mode)
