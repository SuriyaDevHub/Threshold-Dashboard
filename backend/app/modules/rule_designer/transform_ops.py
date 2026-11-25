"""Shared value-transform vocabulary — round/upper/lower/trim/substring/
split/replace/cast_numeric/cast_string/value_map/parse_date/date_shift/
normalize_tenor.

Factored out of workflow_engine.py so it has exactly one implementation,
used by both:
  * TRANSFORM workflow nodes (workflow_engine._apply_transform)
  * LOOKUP field mappings (lookup_engine.apply_lookup), so a value pulled
    from a reference file can be cleaned up with the same operations
    available on any other field, instead of always being written
    through verbatim.
  * A LOOKUP join key's own `transform` (lookup_engine._key_tuple()),
    applied symmetrically to both the source and reference values before
    comparing — this is what lets normalize_tenor turn a dataset's
    numeric Tenor into the same "1M"/"3Y" short code a reference file's
    own Tenor column uses, right at match time, with no separate
    upstream TRANSFORM node needed.

A standalone module (not defined in workflow_engine.py and imported by
lookup_engine.py) specifically to avoid a circular import: workflow_engine
already imports lookup_engine.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re
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
    s = str(val).strip()
    try:
        return _dt.datetime.strptime(s, pattern).date().isoformat()
    except ValueError:
        # A date-only pattern rejects a value carrying a trailing
        # time-of-day component (e.g. "15-01-2026 00:00:00", or an ISO
        # "...T00:00:00Z" suffix) — common when a "date" column is really
        # a midnight timestamp in the source system. Stripping that
        # suffix doesn't change the date's meaning (unlike guessing
        # between two date orderings), so retry once against just the
        # date portion before giving up for good.
        head = s.split("T", 1)[0].split(" ", 1)[0]
        if head == s:
            raise
        return _dt.datetime.strptime(head, pattern).date().isoformat()


def _parse_iso_date(val: Any) -> _dt.date:
    """Parse an already-canonical date value — a raw ISO column, or the
    output of a preceding parse_date TRANSFORM — the same way
    condition_engine._to_date() does (truncate to the first 10 chars,
    strptime as %Y-%m-%d), so date_shift's notion of "a date" matches
    exactly what BEFORE/AFTER/ON will later compare it against. A value
    in some OTHER format needs a parse_date step first — this is
    intentionally not another format-guessing entry point."""
    return _dt.datetime.strptime(str(val)[:10], "%Y-%m-%d").date()


def _shift_months(d: _dt.date, months: int) -> _dt.date:
    """Calendar-month arithmetic, not a fixed day count — "one month
    before" a trade/COB date means the same day of the previous calendar
    month (Mar 15 -> Feb 15), not "30 days earlier", which would silently
    drift across months of different lengths. Clamps the day when the
    target month is shorter (Mar 31 - 1 month -> Feb 28/29)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return _dt.date(year, month, day)


def _shift_date(val: Any, cfg: Dict[str, Any]) -> str:
    """Shift a date by a signed amount of days/weeks/months/years,
    returning a canonical ISO string — e.g. "shift COB date back 1
    month" lets a CONDITION's existing BEFORE operator answer "is the
    trade date more than one month before the COB date?" by comparing
    trade_date against this shifted field, without needing a separate
    date-difference/numeric-threshold mechanism."""
    d = _parse_iso_date(val)
    unit = cfg.get("unit")
    amount = int(cfg.get("amount") or 0)
    if unit == "days":
        return (d + _dt.timedelta(days=amount)).isoformat()
    if unit == "weeks":
        return (d + _dt.timedelta(weeks=amount)).isoformat()
    if unit == "months":
        return _shift_months(d, amount).isoformat()
    if unit == "years":
        return _shift_months(d, amount * 12).isoformat()
    raise ValueError(f"unknown date_shift unit: {unit!r}")


# Simple calendar-day approximations (NOT a real holiday calendar/business-
# day convention — those vary by currency and are a materially bigger,
# separate feature). D/W/M/Y are deliberately exact ×1/×7/×30/×365, matching
# the same "estimate, not a settlement calculation" convention this was
# specified with.
_TENOR_UNIT_DAYS: Dict[str, int] = {"D": 1, "W": 7, "M": 30, "Y": 365}
_TENOR_ALIASES: Dict[str, str] = {
    "D": "D", "DY": "D",
    "W": "W", "WK": "W",
    "M": "M", "MO": "M",
    "Y": "Y", "YR": "Y", "YRS": "Y",
}
# SPT/SNX's real spot-lag varies by currency/market — overridable per
# transform via cfg["spt_days"]/cfg["snx_days"]; these are just the
# defaults when neither is configured.
_TENOR_SPECIAL_DAYS: Dict[str, int] = {"TDY": 0, "TOM": 1, "O/N": 1, "ON": 1, "SPT": 2, "SNX": 3}

_TENOR_COMPONENT_RE = re.compile(r"(\d+(?:\.\d+)?)([A-Z]+)")


def _encode_tenor_days(days: float) -> str:
    """The reverse of parsing: pick the single largest unit that evenly
    divides `days`, in Y -> M -> W -> D priority, so a round value comes
    back out as the same short code a reference file's own Tenor column
    would use ("1M", "3Y", ...) rather than an always-in-days value.
    Falls back to raw days when nothing divides evenly — deterministic,
    not a fuzzy "nearest tenor bucket" guess."""
    if days != int(days):
        return f"{days:g}D"
    days_int = int(days)
    for unit in ("Y", "M", "W"):
        unit_days = _TENOR_UNIT_DAYS[unit]
        if days_int != 0 and days_int % unit_days == 0:
            return f"{days_int // unit_days}{unit}"
    return f"{days_int}D"


def _normalize_tenor(val: Any, cfg: Dict[str, Any]) -> str:
    """Normalizes a tenor value — however it's represented on either side
    of a LOOKUP join key — to one canonical short code ("1M", "3Y", "6M",
    ...), so a dataset's numeric day-count Tenor can match a reference
    file's own normalized Tenor column. Handles, in order: a plain
    number (read as a day count, the dataset's own convention); the
    special codes TDY/TOM/SPT/SNX/O_N (simple calendar-day
    approximations, not a real holiday calendar — see _TENOR_SPECIAL_DAYS);
    and one or more <number><unit> components (D/DY, W/WK, M/MO,
    Y/YR/YRS, case-insensitive, decimals allowed) concatenated together,
    e.g. "3Y6M" — summed to a total day count using the same simple
    ×1/×7/×30/×365 conversion. Raises ValueError for anything that
    doesn't fully parse as one of these — surfaced the same way every
    other transform op's bad input already is, via
    workflow_engine._apply_single_transform()'s existing try/except."""
    raw = str(val).strip()
    s = raw.upper().replace(" ", "")
    if not s:
        raise ValueError("empty tenor value")

    try:
        return _encode_tenor_days(float(s))
    except ValueError:
        pass

    special_days = dict(_TENOR_SPECIAL_DAYS)
    if cfg.get("spt_days") not in (None, ""):
        special_days["SPT"] = float(cfg["spt_days"])
    if cfg.get("snx_days") not in (None, ""):
        special_days["SNX"] = float(cfg["snx_days"])
    if s in special_days:
        return _encode_tenor_days(special_days[s])

    matches = list(_TENOR_COMPONENT_RE.finditer(s))
    if not matches or "".join(m.group(0) for m in matches) != s:
        raise ValueError(f"unrecognized tenor '{raw}'")
    total_days = 0.0
    for m in matches:
        qty = float(m.group(1))
        alias = m.group(2)
        unit = _TENOR_ALIASES.get(alias)
        if unit is None:
            raise ValueError(f"unrecognized tenor unit '{alias}' in '{raw}'")
        total_days += qty * _TENOR_UNIT_DAYS[unit]
    return _encode_tenor_days(total_days)


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
    if op == "date_shift":
        return _shift_date(val, cfg)
    if op == "normalize_tenor":
        return _normalize_tenor(val, cfg)
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
