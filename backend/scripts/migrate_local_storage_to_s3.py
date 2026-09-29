"""One-time migration: copy Rule Designer's existing local-disk state
(rules, version history, product registry, reference data, audit log) up
to S3 under Settings.RULE_DESIGNER_S3_PREFIX, preserving every relative
path/filename exactly as it is today.

Local files are left in place afterward — this script only reads locally
and writes to S3, it never deletes anything. Once this ships, the backend
no longer reads or writes those local files at all; they stay behind
purely as a backup until someone chooses to remove them manually.

Dry-run results (`_dry_runs/`) are deliberately NOT migrated — Rule
Designer now keeps dry-run results in memory only, cleared on every
restart (see dry_run_service.py), so there is nothing meaningful to
preserve there.

The audit log is the one format change: the local `audit/audit_log.jsonl`
(one JSON object per line, append-only) is split into one S3 object per
line, keyed the same way audit_service.record() now keys new entries
(`audit/{ts_ms:020d}_{uuid8}.json`), so old and new entries interleave
correctly in S3's own lexicographic key order.

Idempotent — safe to re-run; re-uploads simply overwrite with identical
content (except the audit log, which mints a fresh random suffix per
line each run, so re-running it duplicates audit entries — run it once).

Run with:  cd backend && .venv/bin/python -m scripts.migrate_local_storage_to_s3
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.rule_designer import s3_store  # noqa: E402

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# (local dir relative to backend/, S3 key prefix) — every plain-file store
# except the audit log, which needs its own per-line handling below.
_DIR_MAPPINGS = [
    ("rules", "rules/"),
    ("versions", "versions/"),
    ("reference_data", "reference_data/"),
]

_AUDIT_LOG_PATH = _BACKEND_DIR / "audit" / "audit_log.jsonl"


def _upload_dir(local_dir: Path, s3_prefix: str) -> int:
    if not local_dir.is_dir():
        print(f"  (skip) {local_dir} does not exist")
        return 0
    uploaded = 0
    for root, _dirs, files in os.walk(local_dir):
        for fn in files:
            local_path = Path(root) / fn
            rel = local_path.relative_to(local_dir)
            key = f"{s3_prefix}{rel.as_posix()}"
            text = local_path.read_text()
            s3_store.put_text(key, text)
            uploaded += 1
    print(f"  {local_dir} -> {s3_prefix}: {uploaded} file(s) uploaded")
    return uploaded


def _upload_audit_log() -> int:
    if not _AUDIT_LOG_PATH.exists():
        print(f"  (skip) {_AUDIT_LOG_PATH} does not exist")
        return 0
    uploaded = 0
    with open(_AUDIT_LOG_PATH) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            ts_ms = int(float(entry.get("timestamp", 0)) * 1000)
            key = f"audit/{ts_ms:020d}_{uuid.uuid4().hex[:8]}.json"
            s3_store.put_text(key, json.dumps(entry))
            uploaded += 1
    print(f"  {_AUDIT_LOG_PATH} -> audit/: {uploaded} entry object(s) uploaded")
    return uploaded


def main() -> None:
    print(f"Migrating Rule Designer local storage to S3 (prefix reused from RULE_DESIGNER_S3_PREFIX)...")
    total = 0
    for local_dir_name, s3_prefix in _DIR_MAPPINGS:
        total += _upload_dir(_BACKEND_DIR / local_dir_name, s3_prefix)
    total += _upload_audit_log()
    print(f"Skipped: {_BACKEND_DIR / '_dry_runs'} (dry-run results are not migrated — in-memory only going forward)")
    print(f"Done. {total} object(s) uploaded. Local files were left in place, untouched.")


if __name__ == "__main__":
    main()
