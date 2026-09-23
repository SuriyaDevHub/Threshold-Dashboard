"""Lookup / enrichment engine.

Supports the four lookup shapes from spec §12: exact, composite (multi-key
exact), range (value falls between two reference columns) and date-based
(date falls between two reference date columns) — plus join-type semantics
(§13), explicit missing-match handling (§14) and duplicate-key detection
with a configurable resolution strategy (§29). Pure functions over
list-of-dict rows, consistent with the rest of this codebase (no pandas).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.modules.rule_designer.models import (
    LookupConfig, LookupType, MissingLookupStrategy, PriorityStrategy,
)
from app.modules.rule_designer.transform_ops import apply_transform_op

# self_group aggregate values (see workflow_engine._self_group_reference_rows)
# are stashed on the representative row under this synthetic per-field-mapping
# key rather than overwriting `source_column` directly — two field mappings
# commonly share the same source_column with different aggregate ops (e.g.
# sum AND count of the same PnL field), and writing both into one shared key
# would let the second silently clobber the first.
def self_group_agg_key(output_field: str) -> str:
    return f"__self_group_agg::{output_field}"


def _key_tuple(row: dict, join_keys: List[Dict[str, Any]], side: str) -> Tuple[Any, ...]:
    """side: "source" or "reference" — which column name each join key
    entry supplies to pull the raw value out of `row`. A join key's
    optional `transform` is applied on BOTH sides (building the
    reference index and matching an incoming record) so the two stay
    comparable however the source/reference values are normalized —
    e.g. an "upper" transform makes "hkfxo_052" match "HKFXO_052"
    regardless of which side is actually in mixed case."""
    vals = []
    for jk in join_keys:
        raw = row.get(jk[side])
        transform = jk.get("transform")
        if transform:
            try:
                raw = apply_transform_op(transform.get("op"), raw, transform)
            except (TypeError, ValueError, IndexError):
                pass  # keep the raw value rather than fail the whole lookup
        vals.append(str(raw))
    return tuple(vals)


def _to_num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_date(v: Any) -> Optional[_dt.date]:
    if v is None or v == "":
        return None
    try:
        return _dt.datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass
class LookupIndex:
    config: LookupConfig
    reference_rows: List[dict]
    exact_index: Dict[Tuple[Any, ...], List[dict]] = field(default_factory=dict)
    duplicate_keys: Dict[str, int] = field(default_factory=dict)

    @property
    def is_keyed(self) -> bool:
        return self.config.lookup_type in (LookupType.EXACT, LookupType.COMPOSITE, LookupType.SELF_GROUP)


def build_index(reference_rows: List[dict], config: LookupConfig) -> LookupIndex:
    idx = LookupIndex(config=config, reference_rows=reference_rows)
    if idx.is_keyed:
        for row in reference_rows:
            k = _key_tuple(row, config.join_keys, "reference")
            idx.exact_index.setdefault(k, []).append(row)
        idx.duplicate_keys = {
            " · ".join(k): len(v) for k, v in idx.exact_index.items() if len(v) > 1
        }
    return idx


def _select_by_priority(candidates: List[dict], config: LookupConfig) -> dict:
    if len(candidates) == 1 or config.priority_strategy == PriorityStrategy.FIRST_MATCH:
        return candidates[0]
    pf = config.priority_field
    if config.priority_strategy == PriorityStrategy.HIGHEST_PRIORITY and pf:
        return max(candidates, key=lambda r: _to_num(r.get(pf)) or float("-inf"))
    if config.priority_strategy == PriorityStrategy.LOWEST_THRESHOLD and pf:
        return min(candidates, key=lambda r: _to_num(r.get(pf)) if _to_num(r.get(pf)) is not None else float("inf"))
    if config.priority_strategy == PriorityStrategy.HIGHEST_THRESHOLD and pf:
        return max(candidates, key=lambda r: _to_num(r.get(pf)) or float("-inf"))
    if config.priority_strategy == PriorityStrategy.LATEST_EFFECTIVE_DATE and pf:
        dated = [(_to_date(r.get(pf)), r) for r in candidates]
        dated = [(d, r) for d, r in dated if d is not None]
        if dated:
            return max(dated, key=lambda t: t[0])[1]
    return candidates[0]


def lookup_one(record: dict, idx: LookupIndex) -> Tuple[bool, List[dict], str]:
    """Return (matched, candidate_rows, reason_if_no_match)."""
    config = idx.config
    if config.lookup_type in (LookupType.EXACT, LookupType.COMPOSITE, LookupType.SELF_GROUP):
        src_cols = [jk["source"] for jk in config.join_keys]
        k = _key_tuple(record, config.join_keys, "source")
        candidates = idx.exact_index.get(k, [])
        if not candidates:
            return False, [], f"no reference row for key {dict(zip(src_cols, [record.get(c) for c in src_cols]))}"
        return True, candidates, ""

    if config.lookup_type == LookupType.RANGE:
        v = _to_num(record.get(config.range_field))
        if v is None:
            return False, [], f"{config.range_field} is not numeric"
        candidates = [
            r for r in idx.reference_rows
            if (_to_num(r.get(config.range_low_column)) is not None
                and _to_num(r.get(config.range_high_column)) is not None
                and _to_num(r.get(config.range_low_column)) <= v <= _to_num(r.get(config.range_high_column)))
        ]
        if not candidates:
            return False, [], f"{config.range_field}={v} matched no range"
        return True, candidates, ""

    if config.lookup_type == LookupType.DATE:
        v = _to_date(record.get(config.date_field))
        if v is None:
            return False, [], f"{config.date_field} is not a valid date"
        candidates = [
            r for r in idx.reference_rows
            if (_to_date(r.get(config.date_from_column)) or _dt.date.min) <= v <=
               (_to_date(r.get(config.date_to_column)) or _dt.date.max)
        ]
        if not candidates:
            return False, [], f"{config.date_field}={v} matched no effective-dated row"
        return True, candidates, ""

    return False, [], "unknown lookup type"


@dataclass
class LookupOutcome:
    status: str  # matched | missing_rejected | missing_null | missing_default | missing_flagged | missing_fallback
    fields_added: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    reject: bool = False


def apply_lookup(record: dict, idx: LookupIndex,
                  fallback_idx: Optional[LookupIndex] = None) -> LookupOutcome:
    config = idx.config
    matched, candidates, reason = lookup_one(record, idx)

    if matched:
        chosen = _select_by_priority(candidates, config)
        fields = {}
        for fm in config.fields:
            if fm.aggregate and config.lookup_type == LookupType.SELF_GROUP:
                raw = chosen.get(self_group_agg_key(fm.output_field))
            else:
                raw = chosen.get(fm.source_column)
            if fm.transform:
                try:
                    raw = apply_transform_op(fm.transform.get("op"), raw, fm.transform)
                except (TypeError, ValueError, IndexError):
                    pass  # keep the raw looked-up value rather than fail the whole lookup
            fields[fm.output_field] = raw
        return LookupOutcome("matched", fields, f"matched {len(candidates)} candidate(s)")

    strategy = config.missing_strategy
    if strategy == MissingLookupStrategy.REJECT:
        return LookupOutcome("missing_rejected", {}, reason, reject=True)
    if strategy == MissingLookupStrategy.DEFAULT:
        fields = {fm.output_field: config.default_values.get(fm.output_field) for fm in config.fields}
        return LookupOutcome("missing_default", fields, reason)
    if strategy == MissingLookupStrategy.FLAG:
        fields = {fm.output_field: None for fm in config.fields}
        if config.flag_field:
            fields[config.flag_field] = True
        return LookupOutcome("missing_flagged", fields, reason)
    if strategy == MissingLookupStrategy.FALLBACK and fallback_idx is not None:
        fb = apply_lookup(record, fallback_idx, None)
        if fb.status == "matched":
            return LookupOutcome("missing_fallback", fb.fields_added, f"{reason}; fallback matched")
        fields = {fm.output_field: None for fm in config.fields}
        return LookupOutcome("missing_null", fields, f"{reason}; fallback also missed")
    # continue_null (default)
    fields = {fm.output_field: None for fm in config.fields}
    return LookupOutcome("missing_null", fields, reason)
