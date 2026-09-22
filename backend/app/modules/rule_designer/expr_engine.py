"""Text-template rendering for outcome commentary/reason fields.

CALCULATE nodes no longer parse free-text arithmetic expressions — that
moved to calc_ops.py, which evaluates a structured operand tree built
entirely by selecting fields/constants/operations instead. This module now
only renders `{field}` placeholders in Reason/Commentary text (ValueRef
type "template"), a plain str.format substitution that never executes
anything, so it stays separate from calc_ops rather than folded into it.
"""
from __future__ import annotations

from typing import Any, Dict


def render_template(template: str, record: Dict[str, Any]) -> str:
    """Render `{field}` placeholders in commentary/reason text. Never
    executes anything — pure str.format against the prepared record."""
    class _Default(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    try:
        return template.format_map(_Default(record))
    except (ValueError, IndexError):
        return template
