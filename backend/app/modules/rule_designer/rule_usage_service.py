"""Reads exception_analysis's committed validation-result CSVs (every
validated record it's ever approved, with a RULE_ID column naming which
published rule produced it — see GLOBAL_LIVE_CSV_PATH in app/core/config.py)
and turns them into per-rule/per-product hit counts, so the Dashboard can
show what fraction of a product's live traffic each of its rules actually
accounts for, and flag a published rule nobody's data has ever matched.

GLOBAL_LIVE_CSV_PATH is a DIRECTORY of per-product files (one per product,
e.g. fxo_validation_results.csv, cashbonds_validation_results.csv,
gfx_validation_results.csv — confirmed live: exception_analysis writes one
result file per product, not a single combined file) — every *.csv file
found directly under it is read and aggregated. A row's product is never
inferred from its filename (the file-naming convention doesn't reliably
match Rule Designer's own product codes, e.g. "gfx_validation_results.csv"
for product GFXCASH) — it's always resolved from the row's own RULE_ID,
exactly like the single-file case. GLOBAL_LIVE_CSV_PATH pointing at one
CSV file directly (rather than a directory) is also still supported.

These files live outside this repo/deployment's own storage and are owned
by exception_analysis, not Rule Designer — this module only ever reads them.
"""
from __future__ import annotations

import csv
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.modules.rule_designer import yaml_service

_RULE_ID_PRODUCT = re.compile(r"^OAR-(.+)-\d+$", re.IGNORECASE)

_cache_lock = threading.Lock()
# Keyed on the sorted (file_path, mtime) pairs actually found, not path
# alone — a directory's own mtime doesn't reliably change when a file
# inside it is appended to, and this also invalidates correctly when a
# per-product file is added or removed.
_cache: Dict[str, object] = {"key": None, "result": None}


@dataclass
class UsageSnapshot:
    available: bool
    csv_path: str
    as_of: Optional[float] = None
    total_rows: int = 0
    rule_hits: Dict[str, int] = field(default_factory=dict)
    product_totals: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


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
    return m.group(1).upper() if m else None


def _csv_files(path: str) -> List[str]:
    if os.path.isdir(path):
        return sorted(
            os.path.join(path, fn) for fn in os.listdir(path)
            if fn.lower().endswith(".csv")
        )
    return [path]


def _cache_key(files: List[str]) -> Tuple[Tuple[str, float], ...]:
    return tuple(sorted((f, os.path.getmtime(f)) for f in files))


def _parse_row(row: dict, rule_id_col: Optional[str], rule_hits: Dict[str, int],
                product_totals: Dict[str, int]) -> None:
    rule_id = (row.get(rule_id_col) or "").strip() if rule_id_col else ""
    if not rule_id:
        return
    product = _product_for_rule_id(rule_id)
    if not product:
        return
    rule_hits[rule_id] = rule_hits.get(rule_id, 0) + 1
    product_totals[product] = product_totals.get(product, 0) + 1


def _parse_all(csv_path: str, files: List[str]) -> UsageSnapshot:
    rule_hits: Dict[str, int] = {}
    product_totals: Dict[str, int] = {}
    total_rows = 0
    as_of = 0.0
    for file_path in files:
        as_of = max(as_of, os.path.getmtime(file_path))
        with open(file_path, newline="") as fh:
            reader = csv.DictReader(fh)
            header_map = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
            rule_id_col = header_map.get("rule_id")
            for row in reader:
                total_rows += 1
                _parse_row(row, rule_id_col, rule_hits, product_totals)
    return UsageSnapshot(available=True, csv_path=csv_path, as_of=as_of, total_rows=total_rows,
                          rule_hits=rule_hits, product_totals=product_totals)


def compute_usage() -> UsageSnapshot:
    path = get_settings().GLOBAL_LIVE_CSV_PATH
    if not path:
        return UsageSnapshot(available=False, csv_path="", error="GLOBAL_LIVE_CSV_PATH is not set")
    if not os.path.exists(path):
        return UsageSnapshot(available=False, csv_path=path, error=f"'{path}' does not exist")

    with _cache_lock:
        try:
            files = _csv_files(path)
            if not files:
                return UsageSnapshot(available=False, csv_path=path, error=f"no .csv files found under '{path}'")
            key = _cache_key(files)
        except OSError as exc:
            return UsageSnapshot(available=False, csv_path=path, error=str(exc))
        if _cache["key"] == key and _cache["result"] is not None:
            return _cache["result"]
        try:
            result = _parse_all(path, files)
        except (OSError, csv.Error) as exc:
            result = UsageSnapshot(available=False, csv_path=path, error=f"could not read '{path}': {exc}")
        _cache["key"] = key
        _cache["result"] = result
        return result
