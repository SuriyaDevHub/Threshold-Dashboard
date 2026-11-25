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


def cache_signature(config: LookupConfig) -> str:
    """A hashable key for "is this LookupIndex, AS CACHED, safe to hand to
    a different LOOKUP node pointing at the same file/day" — used by the
    index caches in day_partitioned_source.py and reference_store.py.

    MUST cover the config's full content, not just join_keys: a cached
    LookupIndex stores its own built-against `config`
    (`LookupIndex.config`), and `apply_lookup()`/`lookup_one()` read
    `idx.config.fields`/`.missing_strategy`/`.priority_strategy`/
    `.default_values`/`.flag_field`/`.range_*`/`.date_*` straight off
    THAT stored config, not off whatever config the current caller
    passed in. An earlier version of this function only hashed
    lookup_type + join_keys — the exact-match SHAPE — reasoning that was
    enough to keep the *index build* correct. It missed that the cached
    config is reused wholesale for enrichment too: two LOOKUP nodes
    sharing a reference file/day and join keys but configured with
    different field mappings (or missing-match/priority handling)
    collided on the same cache key, and whichever one built the entry
    first silently had its field mappings served to every other node
    sharing that key — reproduced live: rule B's lookup returned rule A's
    enriched field name/value, never its own, even though rule B's own
    data was genuinely present in the file. Hashing the model's own full
    serialized content (rather than a hand-picked field list, which is
    exactly what went stale last time) means a future LookupConfig field
    is covered automatically without this function needing to be
    remembered and updated in lockstep."""
    return config.model_dump_json()


def _key_tuple(row: dict, join_keys: List[Dict[str, Any]], side: str) -> Tuple[Any, ...]:
    """side: "source" or "reference" — which column name(s) each join key
    entry supplies to pull the raw value out of `row`. `jk[side]` is
    normally a single column name, but MAY be a list of column names —
    a composite reference key, for a reference file that splits what the
    other side treats as one logical field across two (or more) of its
    own columns (e.g. the dataset's single "Curve" value has to match
    reference columns A and B concatenated together, because the
    external file has no single column holding that combined value).
    Concatenation is plain (no separator) and blank/missing columns
    contribute "" rather than the literal string "None".

    A join key's optional `transform` is applied on BOTH sides (building
    the reference index and matching an incoming record) so the two stay
    comparable however the source/reference values are normalized — e.g.
    an "upper" transform makes "hkfxo_052" match "HKFXO_052" regardless
    of which side is actually in mixed case. For a composite side,
    transform runs once against the already-concatenated value, not
    separately per column."""
    vals = []
    for jk in join_keys:
        col = jk[side]
        if isinstance(col, list):
            raw = "".join("" if (v := row.get(c)) is None else str(v) for c in col)
        else:
            raw = row.get(col)
        transform = jk.get("transform")
        if transform:
            try:
                raw = apply_transform_op(transform.get("op"), raw, transform)
            except (TypeError, ValueError, IndexError):
                pass  # keep the raw value rather than fail the whole lookup
        vals.append(str(raw))
    return tuple(vals)


def reference_columns(join_keys: List[Dict[str, Any]]) -> List[str]:
    """Every individual reference-side column name a lookup's join keys
    touch, flattened — a composite (list) reference like
    ["ColumnA", "ColumnB"] contributes both names, alongside every plain
    single-column entry. For code that needs the flat set of real
    reference-file columns a lookup depends on (CSV preamble-scanning
    header detection, validation's column-existence checks), never for
    the actual per-row key value itself — that's _key_tuple()'s job."""
    cols: List[str] = []
    for jk in join_keys:
        ref = jk.get("reference")
        if isinstance(ref, list):
            cols.extend(ref)
        elif ref:
            cols.append(ref)
    return cols


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
        return self.config.lookup_type in (
            LookupType.EXACT, LookupType.COMPOSITE, LookupType.SELF_GROUP, LookupType.INTERPOLATED,
        )


def build_index(reference_rows: List[dict], config: LookupConfig) -> LookupIndex:
    idx = LookupIndex(config=config, reference_rows=reference_rows)
    if idx.is_keyed:
        for row in reference_rows:
            k = _key_tuple(row, config.join_keys, "reference")
            idx.exact_index.setdefault(k, []).append(row)
        # Duplicate-key detection (spec §29) flags a genuine ambiguous-match
        # problem for EXACT/COMPOSITE — two reference rows sharing one key
        # with no stated priority. For INTERPOLATED, many rows sharing one
        # key is the entire point (a curve's tenor ladder), not a conflict,
        # so it's never computed for that lookup_type.
        if config.lookup_type != LookupType.INTERPOLATED:
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


def _interpolate_curve(record: dict, idx: LookupIndex, curve_rows: List[dict]) -> Tuple[bool, List[dict], str]:
    """The INTERPOLATED branch of lookup_one(): `curve_rows` is every
    reference row sharing the matched curve key (e.g. one curve's full
    tenor ladder). Brackets the record's own numeric tenor between the
    nearest lower/equal and next-higher node, by `interp_node_field`, and
    returns a SINGLE synthetic candidate row whose keys are already the
    `fields[].source_column` names the normal apply_lookup() matched-path
    reads — so that path needs no awareness interpolation ever happened:
    an exact node hit or a clamped edge returns that real row as-is (its
    own column values ARE the answer, no arithmetic needed); only a true
    in-between case computes a synthetic row at all."""
    config = idx.config
    target = _to_num(record.get(config.interp_source_field))
    if target is None:
        return False, [], f"{config.interp_source_field} is not numeric"

    nodes = [(_to_num(r.get(config.interp_node_field)), r) for r in curve_rows]
    nodes = [(n, r) for n, r in nodes if n is not None]
    if not nodes:
        return False, [], f"curve has no numeric {config.interp_node_field} values"

    exact = next((r for n, r in nodes if n == target), None)
    if exact is not None:
        return True, [exact], ""

    below = [t for t in nodes if t[0] < target]
    above = [t for t in nodes if t[0] > target]
    lo = max(below, key=lambda t: t[0]) if below else None
    hi = min(above, key=lambda t: t[0]) if above else None

    if lo is None or hi is None:
        edge = lo or hi
        node_range = f"[{nodes[0][0]}, {nodes[-1][0]}]" if len(nodes) > 1 else f"[{nodes[0][0]}]"
        if config.interp_out_of_range == "reject":
            return False, [], f"{config.interp_source_field}={target} is outside the curve's range {node_range}"
        return True, [edge[1]], f"{target} outside the curve's range {node_range} — clamped to nearest node"

    lo_n, lo_row = lo
    hi_n, hi_row = hi
    frac = (target - lo_n) / (hi_n - lo_n)
    synthetic: Dict[str, Any] = {}
    for fm in config.fields:
        lo_v = _to_num(lo_row.get(fm.source_column))
        hi_v = _to_num(hi_row.get(fm.source_column))
        synthetic[fm.source_column] = (lo_v + (hi_v - lo_v) * frac) if (lo_v is not None and hi_v is not None) else None
    return True, [synthetic], f"interpolated between {lo_n} and {hi_n}"


def _apply_source_defaults(record: dict, join_keys: List[Dict[str, Any]]) -> Optional[dict]:
    """A shallow copy of `record` with each join key's configured
    `default_on_miss` value(s) written into the corresponding SOURCE
    FIELD NAME(s) — not a key-tuple value, so the caller can re-run the
    unchanged `_key_tuple()` over the result afterward (same transform/
    composite-concatenation logic, no second implementation to keep in
    sync). Only ever called as a RETRY after the record's own real
    values have already failed to match anything (see lookup_one()) —
    never overrides a value that already produced a match.

    For a plain-string source, `default_on_miss` is a single fallback
    value. For a composite (list) source — e.g. `["Site", "CCY"]` — it's
    a PARALLEL list of the same length; a blank/None entry at a given
    position leaves THAT column at its own real value, so a fallback can
    be configured for just one component (e.g. Site) while the others
    (CCY) keep varying per record, which a single fixed fallback string
    for the whole composite key could never express.

    Returns None when no join key has any default configured at all, so
    the caller can skip a redundant identical retry entirely."""
    patched: Dict[str, Any] = {}
    any_default = False
    for jk in join_keys:
        col = jk["source"]
        default = jk.get("default_on_miss")
        if isinstance(col, list):
            defaults = default if isinstance(default, list) else []
            for i, c in enumerate(col):
                d = defaults[i] if i < len(defaults) else None
                if d not in (None, ""):
                    patched[c] = d
                    any_default = True
        elif default not in (None, ""):
            patched[col] = default
            any_default = True
    if not any_default:
        return None
    return {**record, **patched}


def _default_on_miss_columns(join_keys: List[Dict[str, Any]]) -> List[str]:
    """Source column name(s) that have a `default_on_miss` configured —
    used only to name them in a default-fallback match's detail message,
    never for the actual lookup."""
    cols: List[str] = []
    for jk in join_keys:
        col = jk["source"]
        default = jk.get("default_on_miss")
        if isinstance(col, list):
            defaults = default if isinstance(default, list) else []
            cols.extend(c for i, c in enumerate(col) if i < len(defaults) and defaults[i] not in (None, ""))
        elif default not in (None, ""):
            cols.append(col)
    return cols


def lookup_one(record: dict, idx: LookupIndex) -> Tuple[bool, List[dict], str]:
    """Return (matched, candidate_rows, reason_if_no_match)."""
    config = idx.config
    if config.lookup_type in (LookupType.EXACT, LookupType.COMPOSITE, LookupType.SELF_GROUP, LookupType.INTERPOLATED):
        src_cols = [jk["source"] for jk in config.join_keys]
        k = _key_tuple(record, config.join_keys, "source")
        candidates = idx.exact_index.get(k, [])
        default_detail = ""
        if not candidates:
            defaulted_record = _apply_source_defaults(record, config.join_keys)
            if defaulted_record is not None:
                k2 = _key_tuple(defaulted_record, config.join_keys, "source")
                if k2 != k:
                    default_candidates = idx.exact_index.get(k2, [])
                    if default_candidates:
                        candidates = default_candidates
                        default_detail = "matched using configured default value(s) for: " + ", ".join(
                            str(c) for c in _default_on_miss_columns(config.join_keys)
                        )
        if not candidates:
            return False, [], f"no reference row for key {dict(zip(src_cols, [record.get(c) for c in src_cols]))}"
        if config.lookup_type == LookupType.INTERPOLATED:
            interp_matched, interp_candidates, interp_detail = _interpolate_curve(record, idx, candidates)
            if interp_matched and default_detail:
                interp_detail = f"{default_detail}; {interp_detail}" if interp_detail else default_detail
            return interp_matched, interp_candidates, interp_detail
        return True, candidates, default_detail

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
        # lookup_one() can return a more specific reason even on a match
        # (a default-fallback note, or — for INTERPOLATED — an
        # interpolation/clamp note) — surface that instead of the generic
        # count whenever it has one, so a record that only matched via a
        # configured default is never indistinguishable from an ordinary
        # exact match in the dry-run/live trail.
        detail = reason or f"matched {len(candidates)} candidate(s)"
        return LookupOutcome("matched", fields, detail)

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
