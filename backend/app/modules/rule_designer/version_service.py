"""YAML versioning (spec §32) and rollback (spec §37) — per product.

Every publish snapshots the *entire* current rules YAML for one product to
versions/<product>/rules_vNNN.yml plus a metadata sidecar (on S3, keys
relative to Settings.RULE_DESIGNER_S3_PREFIX — see s3_store.py) —
independent of the live business_rules.yml that yaml_service edits going
forward, so a published version is immutable once written. Rollback
restores an old snapshot's content into that product's live file and
creates a NEW version on top (history is never deleted or rewritten).
Version numbers are independent per product — CASHBONDS v7 and FX v2 are
unrelated.
"""
from __future__ import annotations

import json
import time
from typing import List, Optional

from app.modules.rule_designer import s3_store, yaml_service
from app.modules.rule_designer.models import RuleSetVersion

VERSIONS_PREFIX = "versions/"


def _product_dir(product: str) -> str:
    return f"{VERSIONS_PREFIX}{product.upper()}"


def _meta_path(product: str, version: int) -> str:
    return f"{_product_dir(product)}/rules_v{version:03d}.json"


def _yaml_path(product: str, version: int) -> str:
    return f"{_product_dir(product)}/rules_v{version:03d}.yml"


def list_versions(product: str) -> List[RuleSetVersion]:
    out = []
    for key in s3_store.list_keys(_product_dir(product) + "/"):
        if key.endswith(".json"):
            text = s3_store.get_text(key)
            if text is not None:
                out.append(RuleSetVersion(**json.loads(text)))
    return sorted(out, key=lambda v: v.version)


def list_all_versions() -> List[RuleSetVersion]:
    """Across every product that has published at least once — for a
    cross-product versions view."""
    out: List[RuleSetVersion] = []
    for product in s3_store.list_dirs(VERSIONS_PREFIX):
        out.extend(list_versions(product))
    return sorted(out, key=lambda v: v.published_at or 0, reverse=True)


def next_version_number(product: str) -> int:
    versions = list_versions(product)
    return (versions[-1].version + 1) if versions else 1


def get_version(product: str, version: int) -> Optional[RuleSetVersion]:
    text = s3_store.get_text(_meta_path(product, version))
    if text is None:
        return None
    return RuleSetVersion(**json.loads(text))


def get_version_yaml_text(product: str, version: int) -> Optional[str]:
    return s3_store.get_text(_yaml_path(product, version))


def publish_snapshot(product: str, created_by: str, description: str, rule_ids_changed: List[str],
                      dry_run_dataset_id: Optional[str] = None,
                      dry_run_result_id: Optional[str] = None,
                      approved_by: Optional[str] = None) -> RuleSetVersion:
    version_no = next_version_number(product)
    yaml_text = yaml_service.rules_yaml_text(product)
    s3_store.put_text(_yaml_path(product, version_no), yaml_text)
    meta = RuleSetVersion(
        product=product.upper(), version=version_no, file=f"rules_v{version_no:03d}.yml",
        created_by=created_by, description=description, rule_ids_changed=rule_ids_changed,
        dry_run_dataset_id=dry_run_dataset_id, dry_run_result_id=dry_run_result_id,
        approved_by=approved_by, published_at=time.time(),
    )
    s3_store.put_text(_meta_path(product, version_no), meta.model_dump_json(indent=2))
    return meta


def rollback_to(product: str, version: int, actor: str) -> RuleSetVersion:
    """Restore an old snapshot's rules into that product's live file, then
    publish that as a brand-new version (history is additive, never
    rewritten)."""
    text = get_version_yaml_text(product, version)
    if text is None:
        raise ValueError(f"{product} version {version} not found")
    old = get_version(product, version)
    raw = yaml_service.load_text(text)

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
