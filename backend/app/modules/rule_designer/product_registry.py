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
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from app.core.asset_classes import ASSET_CLASSES
from app.modules.rule_designer.models import MigrationStatus, Product

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
REGISTRY_PATH = os.path.join(_BACKEND_DIR, "rules", "products.json")


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)


def _seed_defaults() -> Dict[str, dict]:
    seeded = {}
    for code, ac in ASSET_CLASSES.items():
        seeded[code] = Product(
            code=code, name=ac["name"], enabled=True,
            migration_status=MigrationStatus.NOT_MIGRATED,
            description=f"Seeded from asset_classes — {ac.get('metric', '')} deviation metric.",
        ).model_dump(mode="json")
    return seeded


def _load() -> Dict[str, dict]:
    _ensure_dir()
    if not os.path.exists(REGISTRY_PATH):
        data = _seed_defaults()
        _save(data)
        return data
    with open(REGISTRY_PATH) as fh:
        data = json.load(fh)
    # products.json is additive-only for known asset classes: a code added to
    # ASSET_CLASSES later shows up here automatically without clobbering any
    # admin-set enabled/migration_status on existing entries.
    changed = False
    for code, ac in ASSET_CLASSES.items():
        if code not in data:
            data[code] = Product(code=code, name=ac["name"]).model_dump(mode="json")
            changed = True
    if changed:
        _save(data)
    return data


def _save(data: Dict[str, dict]) -> None:
    _ensure_dir()
    tmp = REGISTRY_PATH + f".tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, REGISTRY_PATH)


def list_products() -> List[Product]:
    return sorted((Product.model_validate(v) for v in _load().values()), key=lambda p: p.code)


def get_product(code: str) -> Optional[Product]:
    data = _load()
    blob = data.get(code.upper())
    return Product.model_validate(blob) if blob else None


def is_known_product(code: str) -> bool:
    return code.upper() in _load()


def create_product(code: str, name: str, description: str, actor: str,
                    on_no_match: str = "clear", unmatched_reason_code: Optional[str] = None,
                    disabled_reason_code: Optional[str] = None) -> Product:
    data = _load()
    code = code.upper()
    if code in data:
        raise ValueError(f"product '{code}' already exists")
    product = Product(code=code, name=name, description=description, created_by=actor, updated_by=actor,
                       on_no_match=on_no_match, unmatched_reason_code=unmatched_reason_code,
                       disabled_reason_code=disabled_reason_code)
    data[code] = product.model_dump(mode="json")
    _save(data)
    return product


def configure_fail_safe(code: str, on_no_match: str, unmatched_reason_code: Optional[str],
                         disabled_reason_code: Optional[str], actor: str) -> Product:
    """Sets the per-record fail-safe posture (spec: preserve a migrated
    legacy validator's exact ALERT/UNMATCHED/DISABLED reason-code
    contract) without touching the enabled kill switch or migration
    status."""
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
    return Product.model_validate(data[code])


def set_enabled(code: str, enabled: bool, actor: str) -> Product:
    data = _load()
    code = code.upper()
    if code not in data:
        raise ValueError(f"product '{code}' not found")
    data[code]["enabled"] = enabled
    data[code]["updated_by"] = actor
    data[code]["updated_at"] = time.time()
    _save(data)
    return Product.model_validate(data[code])


def set_migration_status(code: str, status: MigrationStatus, actor: str) -> Product:
    data = _load()
    code = code.upper()
    if code not in data:
        raise ValueError(f"product '{code}' not found")
    data[code]["migration_status"] = status.value
    data[code]["updated_by"] = actor
    data[code]["updated_at"] = time.time()
    _save(data)
    return Product.model_validate(data[code])


def rename_product(old_code: str, new_code: str, actor: str) -> Tuple[Product, bool]:
    """Fixes a product registered under the wrong code (spec: the
    GenericValidator-integration mismatch, e.g. registry has CASHBONDS
    but the validator calls it CASH_BONDS) — GenericValidator resolves
    everything by this code, so this isn't cosmetic. Callers should move
    the product's rules/version history (yaml_service.rename_product_rules)
    BEFORE calling this, so a failed rule migration never leaves the
    registry pointing at a code with no rules moved to it yet.

    Returns (the resulting Product, whether this merged into an already-
    registered new_code rather than a pure rename). On a merge, the
    existing target's own config (enabled, migration_status, fail-safe
    settings) is authoritative — it's the real product; the mismatched
    one was the mistake — so only the old entry is discarded, nothing
    about the target is overwritten."""
    data = _load()
    old_code, new_code = old_code.upper(), new_code.upper()
    if old_code not in data:
        raise ValueError(f"product '{old_code}' not found")
    if old_code == new_code:
        raise ValueError(f"'{old_code}' is already using that code")

    merged = new_code in data
    if not merged:
        entry = dict(data[old_code])
        entry["code"] = new_code
        entry["updated_by"] = actor
        entry["updated_at"] = time.time()
        data[new_code] = entry
    del data[old_code]
    _save(data)
    return Product.model_validate(data[new_code]), merged
