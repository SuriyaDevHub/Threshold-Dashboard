"""Generic version of the legacy GfxValidator's RiverIndex: a reference
CSV that lives at a different path per COB date (one file per day under a
date-stamped folder) rather than one static uploaded/path reference file
— see LookupConfig.reference_source="day_partitioned" and
workflow_engine._build_indexes(). This module only ever reads; there is
no admin write path (unlike reference_store.py), since the day's file is
produced by whatever external process owns that export, not by Rule
Designer.

Reuses conventions already established in this codebase for the exact
same kind of extract (River/EPE exports, known to vary in encoding and
COB-date format) rather than inventing new ones — see the matching
rationale in rule_usage_service.py, which already cites RiverIndex:
- Same encoding fallback order, for the same reason (RiverIndex's own
  docstring: "River extracts can vary — UTF-8, CP1252, etc.").
- Same lenient COB-date parse (a bare date or a full ISO-ish timestamp).
"""
from __future__ import annotations

import csv
import os
import threading
from datetime import datetime
from itertools import chain
from typing import Dict, List, Optional, Tuple

from app.modules.rule_designer import lookup_engine
from app.modules.rule_designer.models import LookupConfig

_ENCODINGS_TO_TRY = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_MAX_HEADER_SCAN_LINES = 2000

_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, List[dict]]] = {}  # resolved path -> (mtime, rows)

# Built LookupIndex cache — the actual fix for the per-trade slowdown vs.
# the legacy RiverIndex: that class builds its by_uti dict once per day
# and reuses it for every subsequent trade (O(1) per lookup); without
# this, _build_indexes() would call lookup_engine.build_index() fresh on
# every single evaluate_record() call even though load_day_rows() above
# already has the raw rows cached. Keyed by (resolved path,
# lookup_engine.cache_signature(config)) so two LOOKUP nodes sharing a
# day's file with different join keys never collide.
_index_cache_lock = threading.Lock()
_index_cache: Dict[Tuple[str, tuple], Tuple[float, "lookup_engine.LookupIndex"]] = {}


def parse_day(value: Optional[str]) -> Optional[datetime]:
    """Lenient COB-date parse — a bare date ("2026-09-24") or a full
    ISO-ish timestamp ("2026-09-24T09:28:03", optionally with a trailing
    'Z' or a space instead of 'T'). Anything else -> None rather than
    raising, so one malformed record never breaks the whole lookup."""
    text = (value or "").strip()
    if not text:
        return None
    iso = text.replace("Z", "").replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


def resolve_path(template: str, day_value: Optional[str]) -> Optional[str]:
    """`template` is a strftime pattern applied directly to the parsed
    day — not a single `{day}` placeholder — so year/month/date can each
    be formatted independently wherever they fall in the path, including
    inside the filename itself:
      "/mnt/river/%Y/%m/%d/All trades.csv"       (nested Y/m/d folders)
      "/mnt/river/%Y/%B/All trades_%Y%m%d.csv"    (full month name folder,
                                                     date embedded in the filename)
      "/mnt/river/%Y-%m-%d/All trades.csv"        (one flat dashed folder)
    Returns None if `day_value` doesn't parse as a date at all."""
    parsed = parse_day(day_value)
    if parsed is None:
        return None
    return parsed.strftime(template)


def _find_header_line(fh, key_columns: List[str]) -> Optional[str]:
    """Scans up to `_MAX_HEADER_SCAN_LINES` lines for the header row,
    skipping any preamble garbage lines real extracts are known to carry
    (generalizes RiverIndex._load_csv's "find the row containing UTI").
    With `key_columns` given, the header row is the first line containing
    any of them (case-insensitive). With none given (peek_columns() has
    no join keys configured yet), falls back to "the first line that
    splits into more than one non-empty field" — good enough to find a
    real header past a one-column preamble message."""
    wanted = {c.upper() for c in key_columns}
    for i, line in enumerate(fh):
        if i >= _MAX_HEADER_SCAN_LINES:
            break
        cols = [c.strip().strip('"') for c in line.split(",")]
        if wanted:
            if any(c.upper() in wanted for c in cols):
                return line
        elif len([c for c in cols if c]) > 1:
            return line
    return None


def _load_csv(path: str, key_columns: List[str]) -> List[dict]:
    """Finds the header row (see `_find_header_line`), then DictReader's
    the rest. Tries each encoding in turn, exactly like RiverIndex.
    Row keys are whitespace-stripped — an ordinary export shape like a
    space after the comma in the header ("UTI, Notional") would otherwise
    produce a row keyed ' Notional', silently never matching a
    hand-configured source_column of "Notional" (the actual reported
    bug: lookup matches, but one enrichment field comes back blank even
    though the column has real data)."""
    last_err: Optional[Exception] = None
    for enc in _ENCODINGS_TO_TRY:
        try:
            with open(path, encoding=enc, newline="") as fh:
                header_line = _find_header_line(fh, key_columns)
                if header_line is None:
                    wanted = {c.upper() for c in key_columns}
                    raise ValueError(f"no header row found containing any of {sorted(wanted)}")
                rows = list(csv.DictReader(chain([header_line], fh)))
                return [{(k.strip() if isinstance(k, str) else k): v for k, v in row.items()} for row in rows]
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    raise last_err or ValueError(f"could not read {path}")


def load_day_rows(template: str, day_value: Optional[str], key_columns: List[str]) -> List[dict]:
    """Rows for the file `day_value` resolves to, cached per (resolved
    path, mtime) so repeated records for the same day — in one batch, or
    across dry runs — never re-read the file from disk. Returns []
    (never raises) when the day doesn't parse or the file doesn't exist —
    that record's lookup simply misses, handled the same as any other
    unmatched record by lookup_engine.apply_lookup()'s missing_strategy."""
    path = resolve_path(template, day_value)
    if path is None or not os.path.exists(path):
        return []
    mtime = os.path.getmtime(path)
    with _cache_lock:
        cached = _cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    rows = _load_csv(path, key_columns)
    with _cache_lock:
        _cache[path] = (mtime, rows)
    return rows


def peek_columns(template: str, day_value: Optional[str]) -> Tuple[List[str], Optional[str]]:
    """(columns, error) for the file `day_value` resolves to — reads just
    enough to find the header row and return its (whitespace-stripped)
    column names, so LookupConfigForm.jsx can populate join-key/enrich-
    field pickers instead of making the admin hand-type a column name
    blind (the free-text entry that caused the mismatched-header bug
    `_load_csv` now guards against). Never raises — a bad path/day/file
    is reported back as an error string, not an exception, since this is
    a best-effort UX helper and the UI keeps its free-text fallback."""
    path = resolve_path(template, day_value)
    if path is None:
        return [], f"'{day_value}' is not a recognizable date"
    if not os.path.exists(path):
        return [], f"no file at '{path}'"
    last_err: Optional[Exception] = None
    for enc in _ENCODINGS_TO_TRY:
        try:
            with open(path, encoding=enc, newline="") as fh:
                header_line = _find_header_line(fh, [])
                if header_line is None:
                    return [], f"no header row found in '{path}'"
                reader = csv.reader([header_line])
                return [c.strip() for c in next(reader)], None
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    return [], str(last_err) if last_err else f"could not read '{path}'"


def get_day_index(template: str, day_value: Optional[str], config: LookupConfig) -> "lookup_engine.LookupIndex":
    """The built LookupIndex for the file `day_value` resolves to, cached
    per (resolved path, mtime, join signature) — so repeated
    run_workflow() calls for the same day (e.g. every trade in
    evaluate_record()'s per-trade loop) reuse the same index object
    instead of rebuilding it from scratch each time, the same O(1)-after-
    first-build shape RiverIndex already has. Never raises — a day that
    doesn't parse or has no file yields an index built from []."""
    path = resolve_path(template, day_value)
    key_columns = [jk["reference"] for jk in config.join_keys]
    if path is None or not os.path.exists(path):
        return lookup_engine.build_index([], config)
    mtime = os.path.getmtime(path)
    sig = lookup_engine.cache_signature(config)
    cache_key = (path, sig)
    with _index_cache_lock:
        cached = _index_cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    rows = load_day_rows(template, day_value, key_columns)
    index = lookup_engine.build_index(rows, config)
    with _index_cache_lock:
        _index_cache[cache_key] = (mtime, index)
    return index
