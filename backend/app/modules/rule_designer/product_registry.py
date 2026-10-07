"""Product registry — the unit that Rule Designer rules, YAML files, and
version history are all scoped by (spec discussion: "rules will be created
based on the product"), and the home of the admin "enable or disable the
complete product's rules" kill switch.

Seeded from `app.core.asset_classes.ASSET_CLASSES` so product codes stay
consistent with the rest of this app (Threshold Analysis, Data Fetch) —
this is not a second, drifting list of product names. Field vocabulary
per product is deliberately NOT hardcoded here: it's inferred from
whichever dataset a rule is bound to (schema.py), per the standing
requirement to inspect data rather than assume a fixed schema.

Stored on S3 (keys relative to Settings.RULE_DESIGNER_S3_PREFIX — see
s3_store.py).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from app.core.asset_classes import ASSET_CLASSES
from app.modules.rule_designer import s3_store
from app.modules.rule_designer.models import MigrationStatus, Product

REGISTRY_KEY = "rules/products.json"
# Tombstone for a code that was deliberately renamed/merged away (see
# rename_product()) — without this, _load()'s own "reseed any missing
# ASSET_CLASSES code" reconciliation below has no way to tell "this code
# was never created yet" apart from "this code was just deleted on
# purpose", and will silently resurrect it as a fresh blank duplicate on
# the very next call (confirmed: a rename away from an ASSET_CLASSES-
# seeded code, e.g. CASH_BONDS, reappeared within one subsequent
# list_products() call). A separate key, not a key inside products.json
# itself, so every existing `for v in data.values()` /
# `Product.model_validate(v)` call site keeps working unchanged.
REMOVED_CODES_KEY = "rules/_removed_product_codes.json"

# Guards the read-modify-write sequence every mutating function below does
# — S3 PUT prevents a torn object, but doesn't prevent two concurrent
# callers both reading the same pre-mutation state and one silently
# losing the other's change (this module runs inside the same
# multi-threaded process as data_fetch/exception_analysis, same as
# yaml_service.py — see that module's own concurrency-fix history).
_write_lock = threading.Lock()


def _seed_defaults() -> Dict[str, dict]:
    seeded = {}
    for code, ac in ASSET_CLASSES.items():
        seeded[code] = Product(
            code=code, name=ac["name"], enabled=True,
            migration_status=MigrationStatus.NOT_MIGRATED,
            description=f"Seeded from asset_classes — {ac.get('metric', '')} deviation metric.",
        ).model_dump(mode="json")
    return seeded


def _load_removed_codes() -> Set[str]:
    text = s3_store.get_text(REMOVED_CODES_KEY)
    if text is None:
        return set()
    return set(json.loads(text))


def _save_removed_codes(codes: Set[str]) -> None:
    s3_store.put_text(REMOVED_CODES_KEY, json.dumps(sorted(codes), indent=2))


def _mark_removed(code: str) -> None:
    codes = _load_removed_codes()
    codes.add(code.upper())
    _save_removed_codes(codes)


def _unmark_removed(code: str) -> None:
    codes = _load_removed_codes()
    if code.upper() in codes:
        codes.discard(code.upper())
        _save_removed_codes(codes)


def _load() -> Dict[str, dict]:
    text = s3_store.get_text(REGISTRY_KEY)
    if text is None:
        data = _seed_defaults()
        _save(data)
        return data
    data = json.loads(text)
    # products.json is additive-only for known asset classes: a code added to
    # ASSET_CLASSES later shows up here automatically without clobbering any
    # admin-set enabled/migration_status on existing entries — EXCEPT a code
    # an admin explicitly renamed/merged away (see REMOVED_CODES_KEY above),
    # which stays gone until deliberately re-created via create_product().
    removed = _load_removed_codes()
    changed = False
    for code, ac in ASSET_CLASSES.items():
        if code not in data and code not in removed:
            data[code] = Product(code=code, name=ac["name"]).model_dump(mode="json")
            changed = True
    if changed:
        _save(data)
    return data


def _save(data: Dict[str, dict]) -> None:
    s3_store.put_text(REGISTRY_KEY, json.dumps(data, indent=2))


# Bounded-staleness read cache — product_engine.evaluate_record() (the
# live per-trade entry point) calls get_product() on every single trade;
# without this it was the last uncached S3 round trip left in that path
# after the LOOKUP-index caching fix (yaml_service's rules cache and
# reference_store's reference-index cache already had this shape — see
# yaml_service._load_plain_rules_cached() for the pattern this mirrors).
# Every write path (create_product()/configure_fail_safe()/set_enabled()/
# set_migration_status()/rename_product()) still calls the always-fresh
# _load() directly under _write_lock for its own read-modify-write, so
# that part is never based on stale data — then calls
# _refresh_load_cache() right after _save() so a read immediately after
# a write sees it too, instead of up to 30s of stale cached data.
_LOAD_CACHE_TTL_SECONDS = 30.0
_load_cache_lock = threading.Lock()
_load_cache_state: Optional[Tuple[Optional[str], float, Dict[str, dict]]] = None  # (etag, cached_at_monotonic, data)


def _load_cached() -> Dict[str, dict]:
    global _load_cache_state
    now = time.monotonic()
    with _load_cache_lock:
        if _load_cache_state is not None and (now - _load_cache_state[1]) < _LOAD_CACHE_TTL_SECONDS:
            return _load_cache_state[2]

    etag = s3_store.head(REGISTRY_KEY)
    with _load_cache_lock:
        if _load_cache_state is not None and _load_cache_state[0] == etag:
            _load_cache_state = (etag, now, _load_cache_state[2])
            return _load_cache_state[2]

    data = _load()  # may itself write once (auto-seeding a newly-added ASSET_CLASSES code)
    etag = s3_store.head(REGISTRY_KEY)  # re-check: _load() above may have just changed it
    with _load_cache_lock:
        _load_cache_state = (etag, now, data)
    return data


def _refresh_load_cache(data: Dict[str, dict]) -> None:
    """Called by every write function right after its own _save(data) —
    without this, a write followed immediately by a get_product()/
    list_products() read could return up-to-30s-stale pre-write data
    (unlike yaml_service's rules cache, which this mirrors: its
    _atomic_write() refreshes the cache the same way, right after its own
    write, using the write's own fresh ETag)."""
    global _load_cache_state
    etag = s3_store.head(REGISTRY_KEY)
    with _load_cache_lock:
        _load_cache_state = (etag, time.monotonic(), data)


def list_products() -> List[Product]:
    return sorted((Product.model_validate(v) for v in _load_cached().values()), key=lambda p: p.code)


def get_product(code: str) -> Optional[Product]:
    data = _load_cached()
    blob = data.get(code.upper())
    return Product.model_validate(blob) if blob else None


def is_known_product(code: str) -> bool:
    return code.upper() in _load_cached()


def create_product(code: str, name: str, description: str, actor: str,
                    on_no_match: str = "clear", unmatched_reason_code: Optional[str] = None,
                    disabled_reason_code: Optional[str] = None) -> Product:
    with _write_lock:
        data = _load()
        code = code.upper()
        if code in data:
            raise ValueError(f"product '{code}' already exists")
        product = Product(code=code, name=name, description=description, created_by=actor, updated_by=actor,
                           on_no_match=on_no_match, unmatched_reason_code=unmatched_reason_code,
                           disabled_reason_code=disabled_reason_code)
        data[code] = product.model_dump(mode="json")
        _save(data)
        _refresh_load_cache(data)
        # A deliberate (re-)registration of this code overrides any earlier
        # rename that tombstoned it — otherwise it would just get deleted
        # again by the next reconciliation pass.
        _unmark_removed(code)
        return product


def configure_fail_safe(code: str, on_no_match: str, unmatched_reason_code: Optional[str],
                         disabled_reason_code: Optional[str], actor: str) -> Product:
    """Sets the per-record fail-safe posture (spec: preserve a migrated
    legacy validator's exact ALERT/UNMATCHED/DISABLED reason-code
    contract) without touching the enabled kill switch or migration
    status."""
    with _write_lock:
        data = _load()
        code = code.upper()
        if code not in data:
            raise ValueError(f"product '{code}' not found")
        data[code]["on_no_match"] = on_no_match
        data[code]["unmatched_reason_code"] = unmatched_reason_code
        data[code]["disabled_reason_code"] = disabled_reason_code
        data[code]["updated_by"] = actor
        data[code]["updated_at"] = time.time()
        _save(data)
        _refresh_load_cache(data)
        return Product.model_validate(data[code])


def set_enabled(code: str, enabled: bool, actor: str) -> Product:
    with _write_lock:
        data = _load()
        code = code.upper()
        if code not in data:
            raise ValueError(f"product '{code}' not found")
        data[code]["enabled"] = enabled
        data[code]["updated_by"] = actor
        data[code]["updated_at"] = time.time()
        _save(data)
        _refresh_load_cache(data)
        return Product.model_validate(data[code])


def set_migration_status(code: str, status: MigrationStatus, actor: str) -> Product:
    with _write_lock:
        data = _load()
        code = code.upper()
        if code not in data:
            raise ValueError(f"product '{code}' not found")
        data[code]["migration_status"] = status.value
        data[code]["updated_by"] = actor
        data[code]["updated_at"] = time.time()
        _save(data)
        _refresh_load_cache(data)
        return Product.model_validate(data[code])


def rename_product(old_code: str, new_code: str, actor: str) -> Tuple[Product, bool]:
    """Fixes a product registered under the wrong code (spec: the
    GenericValidator-integration mismatch, e.g. registry has CASHBONDS
    but the validator calls it CASH_BONDS) — GenericValidator resolves
    everything by this code, so this isn't cosmetic. Callers should move
    the product's rules/version history (yaml_service.rename_product_rules)
    BEFORE calling this, so a failed rule migration never leaves the
    registry pointing at a code with no rules moved to it yet.

    old_code does NOT have to already be a registered product — the other
    real-world shape this fixes is a rules prefix that was never
    registered at all (an orphan, e.g. GFXCASH sitting next to the
    registered GFX_CASH; see product_registry.consistency_report()). In
    that case there's no old config to carry over, so new_code is
    registered fresh (using old_code's rules already moved by the caller)
    rather than refusing just because there was no registry entry.

    Returns (the resulting Product, whether this merged into an already-
    registered new_code rather than a pure rename/fresh-register). On a
    merge, the existing target's own config (enabled, migration_status,
    fail-safe settings) is authoritative — it's the real product; the
    mismatched one was the mistake — so only the old entry is discarded,
    nothing about the target is overwritten.

    old_code is tombstoned (see REMOVED_CODES_KEY) so it doesn't get
    silently re-created — confirmed via reproduction: without this, if
    old_code is also one of ASSET_CLASSES's seeded defaults (e.g.
    CASH_BONDS), _load()'s own reconciliation resurrects it as a fresh
    blank product on the very next call anywhere in the app, including
    the frontend's own post-rename reload() — which is exactly what
    looked like "renaming created a duplicate."
    """
    with _write_lock:
        data = _load()
        old_code, new_code = old_code.upper(), new_code.upper()
        if old_code == new_code:
            raise ValueError(f"'{old_code}' is already using that code")
        old_exists = old_code in data

        merged = new_code in data
        if not merged:
            if old_exists:
                entry = dict(data[old_code])
                entry["code"] = new_code
                entry["updated_by"] = actor
                entry["updated_at"] = time.time()
            else:
                entry = Product(code=new_code, name=new_code.replace("_", " ").title(),
                                 created_by=actor, updated_by=actor).model_dump(mode="json")
            data[new_code] = entry
        if old_exists:
            del data[old_code]
            _mark_removed(old_code)
        _save(data)
        _refresh_load_cache(data)
        return Product.model_validate(data[new_code]), merged


def consistency_report() -> Dict[str, list]:
    """Backend validation for exactly the mismatch this module's rename
    action fixes: a rules prefix on S3 whose code isn't registered
    (GenericValidator would find nothing there, or the reverse — the
    registered product has no rules yet). Deferred yaml_service import:
    not a circular dependency today, but this module owns product
    identity and shouldn't assume it always will be yaml_service's turn
    to import product_registry instead.

    Returns:
      orphaned_with_rules: on-S3 code, not registered, WITH real rule
        content — the actual problem (e.g. GFXCASH sitting next to the
        registered GFX_CASH). Needs a rename to fix.
      empty_scaffold_dirs: on-S3 code, not registered, no rule content
        — harmless leftover (e.g. a stray history backup with no live
        business_rules.yml), not a rename candidate (there's nothing in
        it to move).
      registered_without_rules: registered product with no rules file yet
        — informational only (normal for a newly-created product).
    """
    from app.modules.rule_designer import yaml_service

    registered = {p.code for p in list_products()}
    on_disk = yaml_service.list_product_codes()

    orphaned_with_rules = []
    empty_scaffold_dirs = []
    for code in on_disk:
        if code in registered:
            continue
        rules, _ = yaml_service.load_rules(code)
        if rules:
            orphaned_with_rules.append({"code": code, "rule_count": len(rules)})
        else:
            empty_scaffold_dirs.append(code)

    registered_without_rules = [
        code for code in registered
        if code not in on_disk
    ]

    return {
        "orphaned_with_rules": sorted(orphaned_with_rules, key=lambda r: r["code"]),
        "empty_scaffold_dirs": sorted(empty_scaffold_dirs),
        "registered_without_rules": sorted(registered_without_rules),
    }
