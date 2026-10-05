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

_ENCODINGS_TO_TRY = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_MAX_HEADER_SCAN_LINES = 2000

_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[float, List[dict]]] = {}  # resolved path -> (mtime, rows)


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


def resolve_path(template: str, day_value: Optional[str], day_format: str) -> Optional[str]:
    """`template` with its "{day}" placeholder filled in from `day_value`,
    or None if `day_value` doesn't parse as a date at all."""
    parsed = parse_day(day_value)
    if parsed is None:
        return None
    return template.format(day=parsed.strftime(day_format))


def _load_csv(path: str, key_columns: List[str]) -> List[dict]:
    """Scans for the header row containing any of `key_columns`
    (generalizes RiverIndex._load_csv's "find the row containing UTI" —
    real extracts carry a few preamble lines before the real header),
    then DictReader's the rest. Tries each encoding in turn, exactly like
    RiverIndex._load_csv."""
    wanted = {c.upper() for c in key_columns}
    last_err: Optional[Exception] = None
    for enc in _ENCODINGS_TO_TRY:
        try:
            with open(path, encoding=enc, newline="") as fh:
                header_line = None
                for i, line in enumerate(fh):
                    if i >= _MAX_HEADER_SCAN_LINES:
                        break
                    cols = [c.strip().strip('"') for c in line.split(",")]
                    if any(c.strip().upper() in wanted for c in cols):
                        header_line = line
                        break
                if header_line is None:
                    raise ValueError(f"no header row found containing any of {sorted(wanted)}")
                return list(csv.DictReader(chain([header_line], fh)))
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    raise last_err or ValueError(f"could not read {path}")


def load_day_rows(template: str, day_value: Optional[str], day_format: str,
                   key_columns: List[str]) -> List[dict]:
    """Rows for the file `day_value` resolves to, cached per (resolved
    path, mtime) so repeated records for the same day — in one batch, or
    across dry runs — never re-read the file from disk. Returns []
    (never raises) when the day doesn't parse or the file doesn't exist —
    that record's lookup simply misses, handled the same as any other
    unmatched record by lookup_engine.apply_lookup()'s missing_strategy."""
    path = resolve_path(template, day_value, day_format)
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
