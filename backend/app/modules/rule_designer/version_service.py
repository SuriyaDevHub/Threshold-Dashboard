"""YAML versioning (spec §32) and rollback (spec §37).

Every publish snapshots the *entire* current rules YAML to
versions/rules_vNNN.yml plus a metadata sidecar — independent of the
live business_rules.yml that yaml_service edits going forward, so a
published version is immutable once written. Rollback restores an old
snapshot's content into the live file and creates a NEW version on top
(history is never deleted or rewritten).
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


def _ensure_dir() -> None:
    os.makedirs(VERSIONS_DIR, exist_ok=True)


def _meta_path(version: int) -> str:
    return os.path.join(VERSIONS_DIR, f"rules_v{version:03d}.json")


def _yaml_path(version: int) -> str:
    return os.path.join(VERSIONS_DIR, f"rules_v{version:03d}.yml")


def list_versions() -> List[RuleSetVersion]:
    _ensure_dir()
    out = []
    for fn in sorted(os.listdir(VERSIONS_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(VERSIONS_DIR, fn)) as fh:
                out.append(RuleSetVersion(**json.load(fh)))
    return sorted(out, key=lambda v: v.version)


def next_version_number() -> int:
    versions = list_versions()
    return (versions[-1].version + 1) if versions else 1


def get_version(version: int) -> Optional[RuleSetVersion]:
    path = _meta_path(version)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return RuleSetVersion(**json.load(fh))


def get_version_yaml_text(version: int) -> Optional[str]:
    path = _yaml_path(version)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return fh.read()


def publish_snapshot(created_by: str, description: str, rule_ids_changed: List[str],
                      dry_run_dataset_id: Optional[str] = None,
                      dry_run_result_id: Optional[str] = None,
                      approved_by: Optional[str] = None) -> RuleSetVersion:
    _ensure_dir()
    version_no = next_version_number()
    yaml_text = yaml_service.rules_yaml_text()
    with open(_yaml_path(version_no), "w") as fh:
        fh.write(yaml_text)
    meta = RuleSetVersion(
        version=version_no, file=f"rules_v{version_no:03d}.yml", created_by=created_by,
        description=description, rule_ids_changed=rule_ids_changed,
        dry_run_dataset_id=dry_run_dataset_id, dry_run_result_id=dry_run_result_id,
        approved_by=approved_by, published_at=time.time(),
    )
    with open(_meta_path(version_no), "w") as fh:
        fh.write(meta.model_dump_json(indent=2))
    return meta


def rollback_to(version: int, actor: str) -> RuleSetVersion:
    """Restore an old snapshot's rules into the live file, then publish
    that as a brand-new version (history is additive, never rewritten)."""
    text = get_version_yaml_text(version)
    if text is None:
        raise ValueError(f"version {version} not found")
    old = get_version(version)
    raw = yaml_service._yaml.load(text)  # noqa: SLF001 (intentional reuse of the same round-trip loader)
    tmp_path = yaml_service.RULES_FILE + f".tmp{os.getpid()}"
    yaml_service._ensure_dirs()  # noqa: SLF001
    if os.path.exists(yaml_service.RULES_FILE):
        backup = os.path.join(yaml_service.HISTORY_DIR, f"business_rules_{int(time.time() * 1000)}.yml")
        with open(yaml_service.RULES_FILE) as src, open(backup, "w") as dst:
            dst.write(src.read())
    with open(tmp_path, "w") as fh:
        yaml_service._yaml.dump(raw, fh)  # noqa: SLF001
    os.replace(tmp_path, yaml_service.RULES_FILE)

    return publish_snapshot(
        created_by=actor,
        description=f"Rollback to v{version}" + (f" ({old.description})" if old else ""),
        rule_ids_changed=old.rule_ids_changed if old else [],
    )
