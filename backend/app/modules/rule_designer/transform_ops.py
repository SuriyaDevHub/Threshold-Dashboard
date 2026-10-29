"""Shared value-transform vocabulary — round/upper/lower/trim/substring/
split/replace/cast_numeric/cast_string/value_map/parse_date.

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

import datetime as _dt
from typing import Any, Dict, Optional

# Preset strptime patterns a `parse_date` TRANSFORM can select by name —
# kept here (not just typed free-hand in the UI) so a wrong/typo'd pattern
# can't silently reach strptime from client input; "custom" is the escape
# hatch for anything not in this list via cfg["custom_pattern"].
DATE_INPUT_FORMATS: Dict[str, str] = {
    "mdy": "%m/%d/%Y",
    "dmy": "%d/%m/%Y",
    "dmy_dash": "%d-%m-%Y",
    "d_mon_y": "%d-%b-%Y",
    "ymd_slash": "%Y/%m/%d",
}


def _parse_date(val: Any, cfg: Dict[str, Any]) -> str:
    """Normalize `val` to a canonical ISO `YYYY-MM-DD` string, matching
    schema.py's _DATE_RE and condition_engine.py's _to_date() exactly, so
    the result is immediately comparable via BEFORE/AFTER/ON. Raises
    ValueError on anything unparseable — never silently guesses a format,
    since e.g. "01/02/2026" is genuinely ambiguous (Jan 2 vs Feb 1) and a
    wrong guess would silently misdate a real trade."""
    fmt = cfg.get("input_format")
    if fmt == "epoch_seconds":
        return _dt.datetime.fromtimestamp(float(val), tz=_dt.timezone.utc).date().isoformat()
    if fmt == "epoch_millis":
        return _dt.datetime.fromtimestamp(float(val) / 1000, tz=_dt.timezone.utc).date().isoformat()
    pattern = cfg.get("custom_pattern") if fmt == "custom" else DATE_INPUT_FORMATS.get(fmt)
    if not pattern:
        raise ValueError(f"unknown date input_format: {fmt!r}")
    return _dt.datetime.strptime(str(val).strip(), pattern).date().isoformat()


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
    if op == "parse_date":
        return _parse_date(val, cfg)
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
