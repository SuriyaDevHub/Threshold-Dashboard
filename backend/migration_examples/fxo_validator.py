"""FXO's validator wrapper — a thin alias over generic_validator.py, kept
under this name/class only so any production call site already doing
`from fxo_validator import FxoValidator, FxoValidationResult` keeps
working unchanged. All the actual shadow/cutover logic, HTTP calls, and
group-key discovery live in generic_validator.py now; nothing here is
FXO-specific anymore (it wasn't the rules that were the problem — those
correctly live in Rule Designer — it was this wrapper file existing as
its own copy-pasted implementation per product). See generic_validator.py's
own docstring for the full rationale and for how a brand-new product's
validator wrapper is written (spoiler: no new file, just
`GenericValidator(product="WHATEVER")`).

Grouping (OAR-FXO-004/005/006 need to see sibling trades sharing a
structure id or deal ref) is no longer hardcoded here either — it's
discovered from GET /products/FXO/group-keys, which reads FXO's own
published rules' self-group LOOKUP nodes. If those rules ever change
which field(s) they group by, this file needs no edit.

Usage — identical to before:

    from legacy.fxo_validator import FxoValidator as LegacyFxoValidator
    from fxo_validator import FxoValidator

    legacy = LegacyFxoValidator()
    validator = FxoValidator(legacy_validate=legacy.validate_trade)

    result = validator.validate_trade(trade)
    result = validator.validate_trade(trade, group_rows=siblings)
    results = validator.validate_batch(all_trades)

Set FXO_VALIDATOR_MODE=cutover (same env var name as before) to switch
from shadow to cutover; see generic_validator.py for the fallback chain
if that specific var isn't set.
"""
from __future__ import annotations

from typing import Callable, Optional

from generic_validator import GenericValidator, ValidationResult as FxoValidationResult

__all__ = ["FxoValidator", "FxoValidationResult"]


class FxoValidator(GenericValidator):
    def __init__(self, legacy_validate: Optional[Callable[[dict], "FxoValidationResult"]] = None,
                 mode: Optional[str] = None):
        super().__init__(product="FXO", legacy_validate=legacy_validate, mode=mode)
