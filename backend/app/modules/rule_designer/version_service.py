"""YAML versioning (spec §32) and rollback (spec §37) — per product.

Every publish snapshots the *entire* current rules YAML for one product to
versions/<product>/rules_vNNN.yml plus a metadata sidecar — independent of
the live business_rules.yml that yaml_service edits going forward, so a
published version is immutable once written. Rollback restores an old
snapshot's content into that product's live file and creates a NEW
version on top (history is never deleted or rewritten). Version numbers
are independent per product — CASHBONDS v7 and FX v2 are unrelated.
"""
from __future__ import annotations

import json
import os
import time
from typing import List, Optional

from app.modules.rule_designer import yaml_service
from app.modules.rule_designer.models import RuleSetVersion

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
VERSIONS_DIR = os.path.join(_BACKEND_DIR, "versions")


def _product_dir(product: str) -> str:
    return os.path.join(VERSIONS_DIR, product.upper())


def _ensure_dir(product: str) -> None:
    os.makedirs(_product_dir(product), exist_ok=True)


def _meta_path(product: str, version: int) -> str:
    return os.path.join(_product_dir(product), f"rules_v{version:03d}.json")


def _yaml_path(product: str, version: int) -> str:
    return os.path.join(_product_dir(product), f"rules_v{version:03d}.yml")


def list_versions(product: str) -> List[RuleSetVersion]:
    _ensure_dir(product)
    out = []
    for fn in sorted(os.listdir(_product_dir(product))):
        if fn.endswith(".json"):
            with open(os.path.join(_product_dir(product), fn)) as fh:
                out.append(RuleSetVersion(**json.load(fh)))
    return sorted(out, key=lambda v: v.version)


def list_all_versions() -> List[RuleSetVersion]:
    """Across every product that has published at least once — for a
    cross-product versions view."""
    os.makedirs(VERSIONS_DIR, exist_ok=True)
    out: List[RuleSetVersion] = []
    for product in os.listdir(VERSIONS_DIR):
        if os.path.isdir(os.path.join(VERSIONS_DIR, product)):
            out.extend(list_versions(product))
    return sorted(out, key=lambda v: v.published_at or 0, reverse=True)


def next_version_number(product: str) -> int:
    versions = list_versions(product)
    return (versions[-1].version + 1) if versions else 1


def get_version(product: str, version: int) -> Optional[RuleSetVersion]:
    path = _meta_path(product, version)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return RuleSetVersion(**json.load(fh))


def get_version_yaml_text(product: str, version: int) -> Optional[str]:
    path = _yaml_path(product, version)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return fh.read()


def publish_snapshot(product: str, created_by: str, description: str, rule_ids_changed: List[str],
                      dry_run_dataset_id: Optional[str] = None,
                      dry_run_result_id: Optional[str] = None,
                      approved_by: Optional[str] = None) -> RuleSetVersion:
    _ensure_dir(product)
    version_no = next_version_number(product)
    yaml_text = yaml_service.rules_yaml_text(product)
    with open(_yaml_path(product, version_no), "w") as fh:
        fh.write(yaml_text)
    meta = RuleSetVersion(
        product=product.upper(), version=version_no, file=f"rules_v{version_no:03d}.yml",
        created_by=created_by, description=description, rule_ids_changed=rule_ids_changed,
        dry_run_dataset_id=dry_run_dataset_id, dry_run_result_id=dry_run_result_id,
        approved_by=approved_by, published_at=time.time(),
    )
    with open(_meta_path(product, version_no), "w") as fh:
        fh.write(meta.model_dump_json(indent=2))
    return meta


def rollback_to(product: str, version: int, actor: str) -> RuleSetVersion:
    """Restore an old snapshot's rules into that product's live file, then
    publish that as a brand-new version (history is additive, never
    rewritten)."""
    text = get_version_yaml_text(product, version)
    if text is None:
        raise ValueError(f"{product} version {version} not found")
    old = get_version(product, version)
    raw = yaml_service._yaml.load(text)  # noqa: SLF001 (intentional reuse of the same round-trip loader)

    yaml_service._ensure_dirs(product)  # noqa: SLF001
    yaml_service._backup(product)  # noqa: SLF001
    yaml_service._atomic_write(product, raw)  # noqa: SLF001
    rule_ids = [r.get("rule_id") for r in (raw.get("rules") or []) if r.get("rule_id")]
    if rule_ids:
        yaml_service._index_upsert({rid: product for rid in rule_ids})  # noqa: SLF001

    return publish_snapshot(
        product=product, created_by=actor,
        description=f"Rollback to v{version}" + (f" ({old.description})" if old else ""),
        rule_ids_changed=old.rule_ids_changed if old else rule_ids,
    )
