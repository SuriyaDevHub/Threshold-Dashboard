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
from typing import Dict, List, Optional

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


def create_product(code: str, name: str, description: str, actor: str) -> Product:
    data = _load()
    code = code.upper()
    if code in data:
        raise ValueError(f"product '{code}' already exists")
    product = Product(code=code, name=name, description=description, created_by=actor, updated_by=actor)
    data[code] = product.model_dump(mode="json")
    _save(data)
    return product


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
