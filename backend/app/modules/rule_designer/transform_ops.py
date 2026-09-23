"""Shared value-transform vocabulary — round/upper/lower/trim/substring/
split/replace/cast_numeric/cast_string/value_map.

Factored out of workflow_engine.py so it has exactly one implementation,
used by both:
  * TRANSFORM workflow nodes (workflow_engine._apply_transform)
  * LOOKUP field mappings (lookup_engine.apply_lookup), so a value pulled
    from a reference file can be cleaned up with the same operations
    available on any other field, instead of always being written
    through verbatim.

A standalone module (not defined in workflow_engine.py and imported by
lookup_engine.py) specifically to avoid a circular import: workflow_engine
already imports lookup_engine.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def apply_transform_op(op: Optional[str], val: Any, cfg: Dict[str, Any]) -> Any:
    """Pure value transform — never touches a record or field name, just
    the value itself. Returns `val` unchanged for a None value or an
    unrecognized/absent op, matching the "no-op passthrough" behavior
    every call site already relies on."""
    if val is None or not op:
        return val
    if op == "round":
        return round(float(val), int(cfg.get("precision") or 2))
    if op == "upper":
        return str(val).upper()
    if op == "lower":
        return str(val).lower()
    if op == "trim":
        return str(val).strip()
    if op == "substring":
        s = str(val)
        start = int(cfg.get("start") or 0)
        end_raw = cfg.get("end")
        end = int(end_raw) if end_raw not in (None, "") else None
        return s[start:end]
    if op == "split":
        s = str(val)
        delimiter = cfg.get("delimiter") or ","
        idx = int(cfg.get("index") or 0)
        parts = s.split(delimiter)
        return parts[idx] if -len(parts) <= idx < len(parts) else None
    if op == "replace":
        s = str(val)
        find = cfg.get("find") or ""
        return s.replace(find, cfg.get("replace_with") or "") if find else s
    if op == "cast_numeric":
        return float(str(val).replace(",", ""))
    if op == "cast_string":
        return str(val)
    if op == "value_map":
        # Free-text -> code normalization: "LONDON (LN)" -> "LN". First
        # matching rule wins (checked in configured order), falling back to
        # `default` when given, else the original value — never raises, so
        # an unmapped value degrades to a no-op rather than breaking the
        # pipeline. cfg["rules"] is [{"contains": str, "value": str}, ...].
        s = str(val).upper()
        for rule in cfg.get("rules") or []:
            needle = str(rule.get("contains") or "").upper()
            if needle and needle in s:
                return rule.get("value")
        return cfg.get("default") or val
    return val
