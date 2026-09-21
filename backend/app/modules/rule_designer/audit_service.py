"""Append-only audit log (spec §36). Every mutation is logged; nothing is
ever rewritten or deleted from this file."""
from __future__ import annotations

import json
import os
from typing import List, Optional

from app.modules.rule_designer.models import AuditEntry

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
AUDIT_DIR = os.path.join(_BACKEND_DIR, "audit")
AUDIT_LOG = os.path.join(AUDIT_DIR, "audit_log.jsonl")


def _ensure_dir() -> None:
    os.makedirs(AUDIT_DIR, exist_ok=True)


def record(entry: AuditEntry) -> AuditEntry:
    _ensure_dir()
    with open(AUDIT_LOG, "a") as fh:
        fh.write(entry.model_dump_json() + "\n")
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
    _ensure_dir()
    if not os.path.exists(AUDIT_LOG):
        return []
    out: List[AuditEntry] = []
    with open(AUDIT_LOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = AuditEntry.model_validate(json.loads(line))
            if rule_id and entry.rule_id != rule_id:
                continue
            if action and entry.action != action:
                continue
            if actor and entry.actor != actor:
                continue
            out.append(entry)
    out.sort(key=lambda e: e.timestamp, reverse=True)
    return out[:limit]
