"""Reads exception_analysis's committed "Global Live CSV" (every validated
record it's ever approved, across every product, with a RULE_ID column
naming which published rule produced it — see GLOBAL_LIVE_CSV_PATH in
app/core/config.py) and turns it into per-rule/per-product hit counts, so
the Dashboard can show what fraction of a product's live traffic each of
its rules actually accounts for, and flag a published rule nobody's data
has ever matched.

The file lives outside this repo/deployment's own storage and is owned by
exception_analysis, not Rule Designer — this module only ever reads it.
"""
from __future__ import annotations

import csv
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

from app.core.config import get_settings
from app.modules.rule_designer import yaml_service

_RULE_ID_PRODUCT = re.compile(r"^OAR-(.+)-\d+$", re.IGNORECASE)

_cache_lock = threading.Lock()
# Keyed on (path, mtime), not mtime alone — GLOBAL_LIVE_CSV_PATH can change
# between calls (tests monkeypatch it; an admin could repoint it), and two
# different paths landing on the same mtime would otherwise silently serve
# one path's cached result for the other.
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


def _parse(path: str) -> UsageSnapshot:
    rule_hits: Dict[str, int] = {}
    product_totals: Dict[str, int] = {}
    total_rows = 0
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        header_map = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
        rule_id_col = header_map.get("rule_id")
        for row in reader:
            total_rows += 1
            rule_id = (row.get(rule_id_col) or "").strip() if rule_id_col else ""
            if not rule_id:
                continue
            product = _product_for_rule_id(rule_id)
            if not product:
                continue
            rule_hits[rule_id] = rule_hits.get(rule_id, 0) + 1
            product_totals[product] = product_totals.get(product, 0) + 1
    return UsageSnapshot(available=True, csv_path=path, as_of=os.path.getmtime(path),
                          total_rows=total_rows, rule_hits=rule_hits, product_totals=product_totals)


def compute_usage() -> UsageSnapshot:
    path = get_settings().GLOBAL_LIVE_CSV_PATH
    if not path:
        return UsageSnapshot(available=False, csv_path="", error="GLOBAL_LIVE_CSV_PATH is not set")
    if not os.path.exists(path):
        return UsageSnapshot(available=False, csv_path=path, error=f"'{path}' does not exist")

    with _cache_lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError as exc:
            return UsageSnapshot(available=False, csv_path=path, error=str(exc))
        key = (path, mtime)
        if _cache["key"] == key and _cache["result"] is not None:
            return _cache["result"]
        try:
            result = _parse(path)
        except (OSError, csv.Error) as exc:
            result = UsageSnapshot(available=False, csv_path=path, error=f"could not read '{path}': {exc}")
        _cache["key"] = key
        _cache["result"] = result
        return result
