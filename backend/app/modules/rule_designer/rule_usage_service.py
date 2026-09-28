"""Reads exception_analysis's committed validation-result CSVs (every
validated record it's ever approved, with a RULE_ID column naming which
published rule produced it, and an OMRCTRADECLOSEOFBUSINESSDATE column
naming which close-of-business date it belongs to — see GLOBAL_LIVE_CSV_PATH
in app/core/config.py) and turns them into per-rule/
per-product hit counts for a given date window, so the Dashboard can show
what fraction of a product's live traffic each of its rules actually
accounted for, and flag a published rule nobody's data has matched.

GLOBAL_LIVE_CSV_PATH is a DIRECTORY of per-product files (one per product,
e.g. fxo_validation_results.csv, cashbonds_validation_results.csv,
gfx_validation_results.csv — confirmed live: exception_analysis writes one
result file per product, not a single combined file) — every *.csv file
found directly under it is read and aggregated. A row's product is never
inferred from its filename (the file-naming convention doesn't reliably
match Rule Designer's own product codes, e.g. "gfx_validation_results.csv"
for product GFXCASH) — it's always resolved from the row's own RULE_ID.
GLOBAL_LIVE_CSV_PATH pointing at one CSV file directly (rather than a
directory) is also still supported.

Every file is parsed once per (file, mtime) cache key into a flat list of
per-row records; date-range filtering and rule_hits/product_totals
aggregation both happen per call against that cached list, so narrowing
the date range recomputes hits/hit% from just the rows in range rather
than merely hiding rows from an all-time count.

These files live outside this repo/deployment's own storage and are owned
by exception_analysis, not Rule Designer — this module only ever reads them.
"""
from __future__ import annotations

import csv
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.modules.rule_designer import yaml_service

_RULE_ID_PRODUCT = re.compile(r"^OAR-(.+)-\d+$", re.IGNORECASE)

# exception_analysis's own extracts are known to vary in encoding (the same
# reasoning as the user's RiverIndex._load_csv: "River extracts can vary —
# UTF-8, CP1252, etc."), and opening a file with no explicit encoding picks
# up the platform default — cp1252 on the Windows deployment this actually
# runs on, which raises UnicodeDecodeError on the first non-cp1252 byte
# (confirmed live: a validation-results file failed to load this way).
# Same fallback order and rationale as RiverIndex: BOM-aware UTF-8 first,
# plain UTF-8, then the two single-byte encodings likely to round-trip
# almost any byte without raising.
_ENCODINGS_TO_TRY = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_cache_lock = threading.Lock()
# Keyed on the sorted (file_path, mtime) pairs actually found, not path
# alone — a directory's own mtime doesn't reliably change when a file
# inside it is appended to, and this also invalidates correctly when a
# per-product file is added or removed.
_cache: Dict[str, object] = {"key": None, "parsed": None}


@dataclass
class UsageRecord:
    # rule_id is None for a row exception_analysis didn't attribute to any
    # specific rule (e.g. a fail-safe/unmatched ALERT) — still real product
    # volume, just not a hit for any one rule.
    rule_id: Optional[str]
    product: str
    ts: Optional[datetime]
    # The row's own STATUS column, upper-cased ("ALERT" / "CLEAR"), or None
    # when the file has no STATUS column or the row's value is missing/
    # unrecognized — see exception_category() for how this (plus rule_id)
    # becomes the three-way alerted/cleared_business/cleared_mkt split.
    status: Optional[str] = None


@dataclass
class _ParsedData:
    available: bool
    csv_path: str
    as_of: Optional[float] = None
    records: List[UsageRecord] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class UsageSnapshot:
    available: bool
    csv_path: str
    as_of: Optional[float] = None
    total_rows: int = 0
    rule_hits: Dict[str, int] = field(default_factory=dict)
    product_totals: Dict[str, int] = field(default_factory=dict)
    # One entry per (day, product) with any volume in the requested window —
    # a flat list rather than a day->product->count nesting so the frontend
    # can pivot it into whatever chart-library row shape it needs. A record
    # with no parseable timestamp has no day to bucket into, so it's not
    # represented here even though it's still in total_rows/product_totals.
    daily_product_counts: List[Dict[str, object]] = field(default_factory=list)
    # The business-benefit split of every row that carried a recognized
    # STATUS: "alerted" (STATUS=ALERT — a real exception, nothing cleared
    # it), "cleared_business" (STATUS=CLEAR with a RULE_ID — a business
    # rule evaluated the record and its own logic cleared it) and
    # "cleared_mkt" (STATUS=CLEAR with no RULE_ID — cleared by market-data
    # validation, not attributable to any one rule). A row with no STATUS
    # column, or a value that isn't ALERT/CLEAR, contributes to neither —
    # still real volume (counted in total_rows/product_totals) but with no
    # signal for this breakdown.
    status_totals: Dict[str, int] = field(default_factory=dict)
    # status_totals, split by product — {product: {category: count}} — so
    # the Dashboard's KPI band can show an accurate alerted/cleared count
    # for one selected product without falling back to summing
    # daily_status_counts (which, like daily_product_counts, silently
    # excludes undated rows).
    product_status_totals: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # Same (day, product) shape as daily_product_counts, with an added
    # category dimension — one entry per (day, product, category) — so the
    # frontend can build the alerted/cleared_business/cleared_mkt trend
    # either as an all-products total or narrowed to one product.
    daily_status_counts: List[Dict[str, object]] = field(default_factory=list)
    earliest_date: Optional[str] = None
    latest_date: Optional[str] = None
    error: Optional[str] = None


# One legacy naming quirk, not a general synonym system: exception_analysis
# rows committed before GFX was renamed to GFXCASH still carry rule_ids
# shaped "OAR-GFX-NNN" (current/live rules are all "OAR-GFXCASH-NNN"). The
# regex fallback below would otherwise bucket those historical rows under a
# phantom "GFX" product that doesn't match the registered "GFXCASH" code,
# splitting one product's volume into two totals. Map that one old code
# forward so old and new data land in the same product.
_LEGACY_PRODUCT_ALIASES = {"GFX": "GFXCASH"}


def _product_for_rule_id(rule_id: str) -> Optional[str]:
    """The product a CSV row's RULE_ID belongs to — the existing rule_id
    index first (correct for both OAR-{PRODUCT}-NNN and hand-picked ids),
    falling back to parsing the OAR-{PRODUCT}-NNN convention itself for a
    rule_id the index no longer knows (renamed or deleted since this row
    was committed) so historical rows still count toward the right
    product's total even when they can't be attributed to a live rule."""
    product = yaml_service.resolve_product(rule_id)
    if product:
        return product
    m = _RULE_ID_PRODUCT.match(rule_id)
    if not m:
        return None
    code = m.group(1).upper()
    return _LEGACY_PRODUCT_ALIASES.get(code, code)


def _parse_ts(value: str) -> Optional[datetime]:
    """Lenient OMRCTRADECLOSEOFBUSINESSDATE parse — a bare date
    ("2026-09-24") or a full ISO-ish timestamp ("2026-09-24T09:28:03",
    optionally with a trailing 'Z' or a space instead of 'T'). Anything
    else -> None rather than raising, so one malformed row never breaks
    the whole file."""
    value = (value or "").strip()
    if not value:
        return None
    text = value.replace("Z", "").replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _csv_files(path: str) -> List[str]:
    if os.path.isdir(path):
        return sorted(
            os.path.join(path, fn) for fn in os.listdir(path)
            if fn.lower().endswith(".csv")
        )
    return [path]


def _cache_key(files: List[str]) -> Tuple[Tuple[str, float], ...]:
    return tuple(sorted((f, os.path.getmtime(f)) for f in files))


def _read_rows(file_path: str) -> Tuple[List[str], List[dict]]:
    """(fieldnames, rows) for one file, trying each of _ENCODINGS_TO_TRY in
    turn. Read fully (not lazily) inside the try — a wrong encoding can
    decode the header fine and only raise UnicodeDecodeError partway
    through the data rows, so the whole file has to be consumed before an
    attempt can be trusted. fieldnames comes from the reader directly
    (not row.keys()) so a header-only file with zero data rows still
    reports its columns correctly."""
    last_err: Optional[UnicodeDecodeError] = None
    for enc in _ENCODINGS_TO_TRY:
        try:
            with open(file_path, encoding=enc, newline="") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
                return reader.fieldnames or [], rows
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    raise last_err  # every encoding failed — surface the last attempt's error


def _parse_file(
    rows: List[dict], rule_id_col: Optional[str], ts_col: Optional[str], status_col: Optional[str] = None,
) -> List[UsageRecord]:
    """One file's rows -> records. A validation-results row is real product
    volume whether or not a specific rule claimed it (a fail-safe/unmatched
    ALERT has no RULE_ID at all) — dropping those un-attributed rows would
    shrink a product's total down to just its rule-tagged rows and make a
    single active rule's hit% look like ~100% of "everything" when it's
    really a small slice of real traffic (confirmed live: IRD showed
    OAR-IRD-001 at 100%/21 hits while the real tool showed 121 total rows
    for IRD — only the 21 CLEAR rows carried a RULE_ID). So attribution
    happens PER FILE, not per row: which product owns every row in this
    file is decided from whichever rule_ids in it actually resolve (never
    from the filename — see module docstring), and every row — tagged or
    not — then counts toward that product's total.

    The one exception is a file whose resolvable rule_ids point at MORE
    THAN ONE product — the "one big combined CSV" shape this module also
    supports (GLOBAL_LIVE_CSV_PATH pointing at a single multi-product
    file) rather than one-file-per-product. There, per-file attribution
    would be a guess, so it falls back to the old per-row behavior: each
    row counts only when its own rule_id resolves, un-attributed rows are
    dropped (same limitation as before — there's no product signal at all
    on a row with no rule_id in a file that mixes products)."""
    tagged: List[Tuple[dict, str, str]] = []  # (row, rule_id, product)
    for row in rows:
        rule_id = (row.get(rule_id_col) or "").strip() if rule_id_col else ""
        if not rule_id:
            continue
        product = _product_for_rule_id(rule_id)
        if product:
            tagged.append((row, rule_id, product))

    def _ts(row: dict) -> Optional[datetime]:
        return _parse_ts(row.get(ts_col)) if ts_col else None

    def _status(row: dict) -> Optional[str]:
        if not status_col:
            return None
        return (row.get(status_col) or "").strip().upper() or None

    distinct_products = {p for _, _, p in tagged}
    if len(distinct_products) > 1:
        # mixed-product file — no safe file-level attribution, row-level only
        return [UsageRecord(rule_id=rid, product=p, ts=_ts(row), status=_status(row)) for row, rid, p in tagged]

    if not distinct_products:
        return []  # nothing in this file resolves to any product at all

    file_product = Counter(p for _, _, p in tagged).most_common(1)[0][0]
    records: List[UsageRecord] = []
    for row in rows:
        rule_id = (row.get(rule_id_col) or "").strip() if rule_id_col else ""
        records.append(UsageRecord(rule_id=rule_id or None, product=file_product, ts=_ts(row), status=_status(row)))
    return records


def _parse_all(csv_path: str, files: List[str]) -> _ParsedData:
    records: List[UsageRecord] = []
    as_of = 0.0
    for file_path in files:
        as_of = max(as_of, os.path.getmtime(file_path))
        fieldnames, rows = _read_rows(file_path)
        header_map = {(h or "").strip().lower(): h for h in fieldnames}
        rule_id_col = header_map.get("rule_id")
        # The close-of-business date a row belongs to — not when
        # exception_analysis happened to raise/commit it — is what the
        # Dashboard's date window and daily trend are built from.
        ts_col = header_map.get("omrctradecloseofbusinessdate")
        status_col = header_map.get("status")
        records.extend(_parse_file(rows, rule_id_col, ts_col, status_col))
    return _ParsedData(available=True, csv_path=csv_path, as_of=as_of, records=records)


def _load_parsed() -> _ParsedData:
    path = get_settings().GLOBAL_LIVE_CSV_PATH
    if not path:
        return _ParsedData(available=False, csv_path="", error="GLOBAL_LIVE_CSV_PATH is not set")
    if not os.path.exists(path):
        return _ParsedData(available=False, csv_path=path, error=f"'{path}' does not exist")

    with _cache_lock:
        try:
            files = _csv_files(path)
            if not files:
                return _ParsedData(available=False, csv_path=path, error=f"no .csv files found under '{path}'")
            key = _cache_key(files)
        except OSError as exc:
            return _ParsedData(available=False, csv_path=path, error=str(exc))
        if _cache["key"] == key and _cache["parsed"] is not None:
            return _cache["parsed"]
        try:
            parsed = _parse_all(path, files)
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            parsed = _ParsedData(available=False, csv_path=path, error=f"could not read '{path}': {exc}")
        _cache["key"] = key
        _cache["parsed"] = parsed
        return parsed


def _exception_category(r: UsageRecord) -> Optional[str]:
    """The business-benefit bucket a row's own STATUS (plus whether a rule
    claimed it) puts it in — see UsageSnapshot.status_totals. STATUS values
    other than ALERT/CLEAR (or no STATUS column at all) categorize as
    None — real volume, just no signal for this particular breakdown."""
    if r.status == "ALERT":
        return "alerted"
    if r.status == "CLEAR":
        return "cleared_business" if r.rule_id else "cleared_mkt"
    return None


def compute_usage(date_from: Optional[date] = None, date_to: Optional[date] = None) -> UsageSnapshot:
    parsed = _load_parsed()
    if not parsed.available:
        return UsageSnapshot(available=False, csv_path=parsed.csv_path, error=parsed.error)

    all_ts = [r.ts for r in parsed.records if r.ts is not None]
    earliest = min(all_ts).date().isoformat() if all_ts else None
    latest = max(all_ts).date().isoformat() if all_ts else None

    in_range = parsed.records
    if date_from is not None or date_to is not None:
        def _matches(r: UsageRecord) -> bool:
            if r.ts is None:
                return False  # can't confirm it's in range — exclude, don't guess
            d = r.ts.date()
            if date_from is not None and d < date_from:
                return False
            if date_to is not None and d > date_to:
                return False
            return True
        in_range = [r for r in parsed.records if _matches(r)]

    rule_hits: Dict[str, int] = {}
    product_totals: Dict[str, int] = {}
    daily_product: Dict[Tuple[str, str], int] = {}
    status_totals: Dict[str, int] = {}
    product_status_totals: Dict[str, Dict[str, int]] = {}
    daily_status: Dict[Tuple[str, str, str], int] = {}
    for r in in_range:
        if r.rule_id:
            rule_hits[r.rule_id] = rule_hits.get(r.rule_id, 0) + 1
        product_totals[r.product] = product_totals.get(r.product, 0) + 1
        if r.ts is not None:
            key = (r.ts.date().isoformat(), r.product)
            daily_product[key] = daily_product.get(key, 0) + 1
        category = _exception_category(r)
        if category:
            status_totals[category] = status_totals.get(category, 0) + 1
            product_cats = product_status_totals.setdefault(r.product, {})
            product_cats[category] = product_cats.get(category, 0) + 1
            if r.ts is not None:
                skey = (r.ts.date().isoformat(), r.product, category)
                daily_status[skey] = daily_status.get(skey, 0) + 1

    daily_product_counts = [
        {"date": d, "product": p, "count": c} for (d, p), c in sorted(daily_product.items())
    ]
    daily_status_counts = [
        {"date": d, "product": p, "category": cat, "count": c}
        for (d, p, cat), c in sorted(daily_status.items())
    ]

    return UsageSnapshot(available=True, csv_path=parsed.csv_path, as_of=parsed.as_of,
                          total_rows=len(in_range), rule_hits=rule_hits, product_totals=product_totals,
                          daily_product_counts=daily_product_counts,
                          status_totals=status_totals, product_status_totals=product_status_totals,
                          daily_status_counts=daily_status_counts,
                          earliest_date=earliest, latest_date=latest)
