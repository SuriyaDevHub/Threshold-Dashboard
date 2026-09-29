"""Local (non-S3) working copy for a rule while it's still pre-submission
(DRAFT / VALIDATED / DRY_RUN_COMPLETED). Production evaluation only ever
reads PUBLISHED rules (see product_engine.py), so nothing about these
statuses needs to touch S3 on every edit/validate/dry-run — that's exactly
what made the normal authoring loop slow. A rule's file here is removed
the moment it's synced to S3 (Submit for Approval and beyond), at which
point S3 is authoritative again — see rule_store.upsert_rule().

One JSON file per rule_id, so there's no shared index to race on the way
yaml_service.py's rules/_index.json or product_registry.py's registry
file need a write lock for.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from typing import List, Optional

from app.modules.rule_designer.models import Rule

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
DRAFTS_DIR = os.path.join(_BACKEND_DIR, "_local_drafts")


def _path(rule_id: str) -> str:
    return os.path.join(DRAFTS_DIR, f"{rule_id}.json")


def save(rule: Rule) -> None:
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    path = _path(rule.rule_id)
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w") as fh:
            fh.write(rule.model_dump_json())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def get(rule_id: str) -> Optional[Rule]:
    path = _path(rule_id)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return Rule.model_validate(json.load(fh))


def delete(rule_id: str) -> bool:
    path = _path(rule_id)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def list_all() -> List[Rule]:
    if not os.path.isdir(DRAFTS_DIR):
        return []
    out = []
    for fn in os.listdir(DRAFTS_DIR):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(DRAFTS_DIR, fn)) as fh:
            out.append(Rule.model_validate(json.load(fh)))
    return out


def list_for_product(product: str) -> List[Rule]:
    product = product.upper()
    return [r for r in list_all() if r.product == product]
