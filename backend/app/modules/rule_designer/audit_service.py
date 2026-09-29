"""Append-only audit log (spec §36). Every mutation is logged; nothing is
ever rewritten or deleted.

Stored on S3 as one object per entry under `audit/` (keys relative to
Settings.RULE_DESIGNER_S3_PREFIX — see s3_store.py), since S3 has no native
append. Each key is prefixed with the entry's own millisecond timestamp
(zero-padded, so it sorts lexicographically the same as numerically),
which gives chronological order for free from list_objects_v2 without
needing to read every entry just to sort them.
"""
from __future__ import annotations

import json
import uuid
from typing import List, Optional

from app.modules.rule_designer import s3_store
from app.modules.rule_designer.models import AuditEntry

AUDIT_PREFIX = "audit/"


def _entry_key(entry: AuditEntry) -> str:
    ts_ms = int(entry.timestamp * 1000)
    return f"{AUDIT_PREFIX}{ts_ms:020d}_{uuid.uuid4().hex[:8]}.json"


def record(entry: AuditEntry) -> AuditEntry:
    s3_store.put_text(_entry_key(entry), entry.model_dump_json())
    return entry


def log(actor: str, action: str, role: Optional[str] = None, rule_id: Optional[str] = None,
        rule_name: Optional[str] = None, previous_version: Optional[int] = None,
        new_version: Optional[int] = None, dataset_id: Optional[str] = None,
        lookup_files: Optional[List[str]] = None, dry_run_result_id: Optional[str] = None,
        detail: str = "") -> AuditEntry:
    entry = AuditEntry(
        actor=actor, role=role, action=action, rule_id=rule_id, rule_name=rule_name,
        previous_version=previous_version, new_version=new_version, dataset_id=dataset_id,
        lookup_files=lookup_files or [], dry_run_result_id=dry_run_result_id, detail=detail,
    )
    return record(entry)


def query(rule_id: Optional[str] = None, action: Optional[str] = None,
          actor: Optional[str] = None, limit: int = 500) -> List[AuditEntry]:
    out: List[AuditEntry] = []
    for key in sorted(s3_store.list_keys(AUDIT_PREFIX), reverse=True):  # newest first
        text = s3_store.get_text(key)
        if text is None:
            continue
        entry = AuditEntry.model_validate(json.loads(text))
        if rule_id and entry.rule_id != rule_id:
            continue
        if action and entry.action != action:
            continue
        if actor and entry.actor != actor:
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    return out
