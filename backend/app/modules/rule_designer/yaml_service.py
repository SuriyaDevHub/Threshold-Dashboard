"""YAML integration (spec §30-31): the YAML file is the authoritative,
machine-readable source of truth. This module is the ONLY place that reads
or writes it — nothing in the API layer or the frontend touches it
directly, and the frontend never sees YAML at all.

Storage is per product, on S3: `rules/<product>/business_rules.yml` (keys
relative to Settings.RULE_DESIGNER_S3_PREFIX — see s3_store.py). A single
monolithic file works for one worked example; it stops working the moment
two products' rule authors touch it in the same window — atomic writes
mean one publish blocks or corrupts the other. Splitting by product also
makes the admin "enable/disable a whole product's rules" action and
per-product version history (versioned independently, rolled back
independently) a natural consequence of the storage layout rather than
something layered on top.

A rule_id is still globally unique, so a small index
(`rules/_index.json`, rule_id -> product) makes `get_rule(rule_id)` and
friends O(1) instead of a scan across every product's file.

Load path:  existing YAML -> raw dict (ruamel round-trip, comments/order
            kept) -> canonical Rule models (best-effort per item; a rule
            that fails to parse is reported, not dropped or crashed on).
Save path:  canonical Rule models -> merged into the raw round-trip
            document (only the `rules` list is touched; every other
            top-level key, comment and ordering is left exactly as read)
            -> a single S3 PUT (already atomic — a GET sees either the
            fully-old or fully-new object, never a partial write) with a
            timestamped backup written first.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from ruamel.yaml import YAML

from app.modules.rule_designer import s3_store
from app.modules.rule_designer.models import Rule

RULES_PREFIX = "rules/"


def _new_yaml() -> YAML:
    """A fresh ruamel YAML() instance per call — deliberately not a shared
    module-level singleton. YAML() holds mutable parser/emitter state
    across a single load()/dump() call, and this module is called
    concurrently from multiple threads (rule_designer's own request
    handlers, plus exception_analysis's in-process product_engine calls,
    each of which re-reads a product's rules on every validation, all
    dispatched through a thread pool). Two threads calling .load()/.dump()
    on the SAME YAML() instance at the same time corrupt each other's
    in-flight state and can write or read genuinely malformed YAML —
    confirmed via a concurrent-writer repro that reliably corrupted output
    when this was a shared instance. Constructing YAML() is cheap (just
    sets a few config attributes), so paying that cost per call is the
    simplest correct fix — no locking needed, since there's no shared
    mutable state left to race on."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _product_dir(product: str) -> str:
    return f"{RULES_PREFIX}{product.upper()}"


def _history_dir(product: str) -> str:
    return f"{_product_dir(product)}/_history"


def _rules_file(product: str) -> str:
    return f"{_product_dir(product)}/business_rules.yml"


def _ensure_dirs(product: str) -> None:
    """No-op under S3 — there's no directory to pre-create, a key simply
    exists or doesn't. Kept so existing call sites (version_service,
    tests) don't need to change."""


# --------------------------------------------------------------------------
# In-memory rules cache — the load_rules() hot path
# --------------------------------------------------------------------------
# load_rules() is called on every single evaluate_record()/evaluate_rows()
# call (rule_store.list_rules() -> active_rules_for_product(), i.e. every
# validation), so with no caching it re-reads and re-parses a product's
# whole business_rules.yml on every call — the old hardcoded
# {product}_validator.py files never paid this cost because they loaded
# rules once via a process-wide singleton (rules_singleton.get_rules())
# and reused it. This cache is that same idea, generalized per product so
# every current and future product's validator gets it for free, with no
# product-specific code anywhere.
#
# Cached at the plain-dict level (post-ruamel, pre-pydantic), not as
# parsed Rule objects: load_rules() still re-validates into fresh Rule
# instances from the cached dicts on every call, which is cheap (no I/O,
# no YAML tokenizing) and means callers never share a mutable Rule object.
#
# Bounded-staleness TTL cache: within _CACHE_TTL_SECONDS of the last check,
# a call returns the cached value with NO S3 call at all — the hot path's
# whole point. Once the TTL expires, a cheap S3 HEAD (ETag only, no body)
# decides whether a re-fetch is actually needed. The write path
# (_atomic_write) updates the cache immediately after a successful PUT
# using the ETag the PUT itself returned — no re-read, no window where a
# just-published rule could read stale.
_CACHE_TTL_SECONDS = 30.0
_rules_cache_lock = threading.Lock()
_rules_cache: Dict[str, Tuple[Optional[str], float, List[Dict[str, Any]]]] = {}


def _load_plain_rules_cached(product: str) -> List[Dict[str, Any]]:
    product = product.upper()
    now = time.monotonic()
    with _rules_cache_lock:
        cached = _rules_cache.get(product)
        if cached is not None and (now - cached[1]) < _CACHE_TTL_SECONDS:
            return cached[2]

    etag = s3_store.head(_rules_file(product))
    with _rules_cache_lock:
        cached = _rules_cache.get(product)
        if cached is not None and cached[0] == etag:
            _rules_cache[product] = (etag, now, cached[2])
            return cached[2]

    raw = load_raw(product)
    plain_rules = [_plain(item) for item in (raw.get("rules") or [])]
    with _rules_cache_lock:
        _rules_cache[product] = (etag, now, plain_rules)
    return plain_rules


def _update_rules_cache(product: str, raw: Dict[str, Any], etag: Optional[str]) -> None:
    product = product.upper()
    plain_rules = [_plain(item) for item in (raw.get("rules") or [])]
    with _rules_cache_lock:
        _rules_cache[product] = (etag, time.monotonic(), plain_rules)


# --------------------------------------------------------------------------
# rule_id -> product index (rule_id stays globally unique)
# --------------------------------------------------------------------------

def _index_path() -> str:
    return f"{RULES_PREFIX}_index.json"


def _load_index() -> Dict[str, str]:
    text = s3_store.get_text(_index_path())
    if text is None:
        return {}
    return json.loads(text)


def _save_index(idx: Dict[str, str]) -> None:
    s3_store.put_text(_index_path(), json.dumps(idx, indent=2))


def resolve_product(rule_id: str) -> Optional[str]:
    return _load_index().get(rule_id)


def _index_upsert(rule_ids_by_product: Dict[str, str]) -> None:
    idx = _load_index()
    idx.update(rule_ids_by_product)
    _save_index(idx)


def _index_remove(rule_id: str) -> None:
    idx = _load_index()
    if rule_id in idx:
        del idx[rule_id]
        _save_index(idx)


# --------------------------------------------------------------------------
# Per-product read/write
# --------------------------------------------------------------------------

def load_raw(product: str) -> Dict[str, Any]:
    """The existing YAML for one product, parsed as-is. Never assumes
    `rules` exists."""
    text = s3_store.get_text(_rules_file(product))
    if text is None:
        return {"schema_version": 1, "product": product.upper(), "rules": []}
    data = _new_yaml().load(text)
    return data if data is not None else {"schema_version": 1, "product": product.upper(), "rules": []}


def inspect_yaml(product: str) -> Dict[str, Any]:
    """Phase-1 inspection report: what shape is actually on disk right now."""
    raw = load_raw(product)
    report: Dict[str, Any] = {
        "product": product.upper(),
        "path": _rules_file(product),
        "exists": s3_store.exists(_rules_file(product)),
        "top_level_keys": list(raw.keys()) if isinstance(raw, dict) else [],
        "rule_count": 0,
        "parsed_ok": 0,
        "parse_errors": [],
    }
    rules_blob = raw.get("rules", []) if isinstance(raw, dict) else []
    report["rule_count"] = len(rules_blob)
    for item in rules_blob:
        try:
            Rule.model_validate(_plain(item))
            report["parsed_ok"] += 1
        except ValidationError as exc:
            report["parse_errors"].append({
                "rule_id": item.get("rule_id", "?") if isinstance(item, dict) else "?",
                "errors": exc.errors(),
            })
    return report


def _plain(obj: Any) -> Any:
    """Strip ruamel's CommentedMap/Seq wrappers down to plain dict/list so
    pydantic can validate them."""
    if hasattr(obj, "items"):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def load_rules(product: str) -> Tuple[List[Rule], List[Dict[str, Any]]]:
    """Returns (rules that parsed, [{rule_id, errors}] for ones that didn't).
    This is the hot path (every evaluate_record()/evaluate_rows() call goes
    through it via rule_store.list_rules()) — see _load_plain_rules_cached()
    for why it doesn't hit S3/re-parse YAML on every call."""
    plain_rules = _load_plain_rules_cached(product)
    rules: List[Rule] = []
    failures: List[Dict[str, Any]] = []
    for plain in plain_rules:
        try:
            rules.append(Rule.model_validate(plain))
        except ValidationError as exc:
            failures.append({"rule_id": plain.get("rule_id", "?"), "errors": exc.errors()})
    return rules, failures


def _rule_to_plain(rule: Rule) -> dict:
    return rule.model_dump(mode="json")


def _backup(product: str) -> None:
    text = s3_store.get_text(_rules_file(product))
    if text is not None:
        backup_key = f"{_history_dir(product)}/business_rules_{int(time.time() * 1000)}.yml"
        s3_store.put_text(backup_key, text)


def _atomic_write(product: str, raw: Dict[str, Any]) -> None:
    product = product.upper()
    etag = s3_store.put_text(_rules_file(product), dump_to_text(raw))
    _update_rules_cache(product, raw, etag)


def save_rules(rules: List[Rule], actor: str = "system") -> None:
    """Merge the given rules into their product's YAML, touching only the
    `rules` list and only the entries whose rule_id is in `rules` — every
    other top-level key and every other rule entry is preserved verbatim.
    Every rule in `rules` must share one product — mixing products in one
    call is almost always a bug at the caller.
    """
    if not rules:
        return
    products = {r.product for r in rules}
    if len(products) > 1:
        raise ValueError(f"save_rules() received rules from multiple products: {sorted(products)}")
    product = next(iter(products))

    raw = load_raw(product)
    if "rules" not in raw or raw.get("rules") is None:
        raw["rules"] = []

    by_id = {r.rule_id: r for r in rules}
    existing_ids = set()
    new_list = []
    for item in raw["rules"]:
        plain = _plain(item)
        rid = plain.get("rule_id")
        existing_ids.add(rid)
        if rid in by_id:
            new_list.append(_rule_to_plain(by_id[rid]))
        else:
            new_list.append(plain)  # untouched rule, preserved as-is
    for rid, r in by_id.items():
        if rid not in existing_ids:
            new_list.append(_rule_to_plain(r))
    raw["rules"] = new_list
    raw["schema_version"] = raw.get("schema_version", 1)
    raw["product"] = product.upper()
    raw["last_updated_at"] = time.time()
    raw["last_updated_by"] = actor

    _backup(product)
    _atomic_write(product, raw)
    _index_upsert({rid: product for rid in by_id})


def delete_rule(rule_id: str, product: str, actor: str = "system") -> bool:
    raw = load_raw(product)
    before = len(raw.get("rules", []))
    raw["rules"] = [r for r in raw.get("rules", []) if _plain(r).get("rule_id") != rule_id]
    if len(raw["rules"]) == before:
        return False
    raw["last_updated_at"] = time.time()
    raw["last_updated_by"] = actor
    _backup(product)
    _atomic_write(product, raw)
    _index_remove(rule_id)
    return True


def _rule_id_for_new_product(rule_id: str, old_code: str, new_code: str) -> str:
    """OAR-{OLD_CODE}-NNN -> OAR-{NEW_CODE}-NNN, preserving the sequence
    number — matches rule_store.next_rule_id()'s own OAR-{PRODUCT}-NNN
    convention exactly, so a renamed rule's id stays consistent with
    every other rule under its new product (and with what a fresh
    next_rule_id(new_code) call would produce next). A rule_id that
    doesn't follow this convention (a hand-picked id like
    FX_DEVIATION_HIGH_RISK) is left exactly as-is — there's no product
    code embedded in it to fix."""
    m = re.match(rf"^OAR-{re.escape(old_code)}-(\d+)$", rule_id, re.IGNORECASE)
    if not m:
        return rule_id
    return f"OAR-{new_code}-{m.group(1)}"


def rename_product_rules(old_code: str, new_code: str, actor: str = "system") -> int:
    """Moves every rule (and, where unambiguous, version history) from
    `old_code` to `new_code` — the fix for a product registered under the
    wrong code (e.g. CASHBONDS when the validator integration actually
    calls it CASH_BONDS): GenericValidator resolves everything by this
    code, so a mismatch here means validation fails outright, not just
    cosmetically. Returns the number of rules moved.

    Reuses save_rules() for the actual merge rather than writing new
    rules-file logic: retagging each rule's `product` and calling
    save_rules() gets the target's current file backed up, the rules
    merged into its `rules` list by rule_id, the atomic write, and
    rules/_index.json updated — all for free, exactly like a normal
    multi-rule save would.

    Each rule's rule_id is renamed alongside its product when it follows
    the OAR-{PRODUCT}-NNN convention (see _rule_id_for_new_product) —
    confirmed as a real gap from a live screenshot: rules moved from
    CASH_BONDS to CASHBONDS kept ids like OAR-CASH_BONDS-001, so the id
    and the Product column disagreed about which product the rule
    actually belonged to. The stale old id -> product index entry is
    removed (save_rules() only ever adds/overwrites, never removes).

    If `new_code` is already a separate registered product (the
    CASHBONDS/CASH_BONDS shape), this merges old_code's rules into it —
    a rule_id (after renaming) that exists under BOTH is refused up
    front, before any write, rather than letting the merge silently
    pick one.
    """
    old_code, new_code = old_code.upper(), new_code.upper()
    old_rules, _ = load_rules(old_code)
    if not old_rules:
        # No rules to retag — clear out anything that may have accumulated
        # under old_code (e.g. a stray history backup) so it doesn't linger
        # as clutter a later consistency check would flag.
        s3_store.delete_prefix(_product_dir(old_code) + "/")
        with _rules_cache_lock:
            _rules_cache.pop(old_code, None)
        _move_or_merge_version_history(old_code, new_code)
        return 0

    renamed_ids = [_rule_id_for_new_product(r.rule_id, old_code, new_code) for r in old_rules]
    if len(set(renamed_ids)) != len(renamed_ids):
        # two old rule_ids collapsed onto the same new id — shouldn't
        # happen given unique NNN suffixes, but never silently merge them
        raise ValueError(f"cannot rename '{old_code}' to '{new_code}': renaming would collide rule ids")

    new_rules, _ = load_rules(new_code)
    existing_target_ids = {r.rule_id for r in new_rules}
    collisions = set(renamed_ids) & existing_target_ids
    if collisions:
        raise ValueError(
            f"cannot rename '{old_code}' to '{new_code}': rule_id(s) "
            f"{sorted(collisions)} already exist under '{new_code}'"
        )

    id_changes = {r.rule_id: new_id for r, new_id in zip(old_rules, renamed_ids) if new_id != r.rule_id}
    retagged = [
        r.model_copy(update={"product": new_code, "rule_id": new_id})
        for r, new_id in zip(old_rules, renamed_ids)
    ]
    save_rules(retagged, actor=actor)
    for old_id in id_changes:
        _index_remove(old_id)

    s3_store.delete_prefix(_product_dir(old_code) + "/")
    with _rules_cache_lock:
        _rules_cache.pop(old_code, None)
    _move_or_merge_version_history(old_code, new_code)
    return len(retagged)


def list_product_codes() -> List[str]:
    """Every product code with at least one key under rules/ — the S3
    equivalent of os.listdir(RULES_DIR)+os.path.isdir(). Used by
    product_registry.consistency_report() to find on-disk codes."""
    return s3_store.list_dirs(RULES_PREFIX)


def _move_or_merge_version_history(old_code: str, new_code: str) -> None:
    """versions/{code}/ isn't owned by this module (version_service.py is)
    — imported here, not at module scope, since version_service.py
    already imports yaml_service (a real, load-time circular import, not
    just a style preference)."""
    from app.modules.rule_designer import version_service

    old_prefix = version_service._product_dir(old_code) + "/"  # noqa: SLF001
    if not s3_store.list_keys(old_prefix):
        return
    new_prefix = version_service._product_dir(new_code) + "/"  # noqa: SLF001
    if s3_store.list_keys(new_prefix):
        # Target already has its own version history — splicing two
        # independently-numbered sequences together is more likely to
        # confuse than help, so old_code's history is left in place,
        # still on S3 and inspectable, just not merged into new_code's
        # next_version_number() sequence.
        return
    s3_store.copy_prefix(old_prefix, new_prefix)


def dump_to_text(raw: Dict[str, Any]) -> str:
    buf = io.StringIO()
    _new_yaml().dump(raw, buf)
    return buf.getvalue()


def load_text(text: str) -> Any:
    """Parse a YAML string with the same round-trip loader load_raw() uses
    (a fresh instance per call — see _new_yaml()'s own docstring for why).
    For callers that already have YAML text in hand (e.g. a version
    snapshot) rather than a product's live file."""
    return _new_yaml().load(text)


def rules_yaml_text(product: str) -> str:
    return dump_to_text(load_raw(product))
