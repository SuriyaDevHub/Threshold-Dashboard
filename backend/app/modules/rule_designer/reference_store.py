"""Reference (lookup) file management — upload, versioning (spec §39-40).

Every reference file gets an id; every upload against that id (same name)
creates a new, immutable version. A published rule pins the exact version
it was validated against, so results stay reproducible even if the
reference file is updated later (spec §40). Stored on S3 under
`reference_data/` (keys relative to Settings.RULE_DESIGNER_S3_PREFIX —
see s3_store.py).
"""
from __future__ import annotations

import csv
import io
import json
import time
import uuid
from typing import Dict, List, Optional

from app.modules.rule_designer import s3_store
from app.modules.rule_designer.models import FieldType, ReferenceFile, ReferenceFileVersion
from app.modules.rule_designer.schema import infer_schema

BASE_PREFIX = "reference_data/"


def _index_path() -> str:
    return f"{BASE_PREFIX}_index.json"


def _load_index() -> Dict[str, dict]:
    text = s3_store.get_text(_index_path())
    if text is None:
        return {}
    return json.loads(text)


def _save_index(idx: Dict[str, dict]) -> None:
    s3_store.put_text(_index_path(), json.dumps(idx, indent=2))


def parse_csv_text(text: str) -> List[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _rows_path(file_id: str, version: int) -> str:
    return f"{BASE_PREFIX}{file_id}/v{version}.json"


def _to_model(file_id: str, blob: dict) -> ReferenceFile:
    return ReferenceFile(
        id=file_id, name=blob["name"],
        versions=[ReferenceFileVersion(**v) for v in blob["versions"]],
        # .get(..., default) — entries created before path-based sources
        # existed have none of these keys, and should read as plain
        # "upload" files, exactly as they behave today.
        source_mode=blob.get("source_mode", "upload"),
        source_path=blob.get("source_path"),
        auto_refresh_minutes=blob.get("auto_refresh_minutes"),
        last_synced_at=blob.get("last_synced_at"),
        last_sync_error=blob.get("last_sync_error"),
    )


def list_files() -> List[ReferenceFile]:
    idx = _load_index()
    out = [_to_model(file_id, blob) for file_id, blob in idx.items()]
    return sorted(out, key=lambda f: f.name)


def get_file(file_id: str) -> Optional[ReferenceFile]:
    idx = _load_index()
    blob = idx.get(file_id)
    if not blob:
        return None
    return _to_model(file_id, blob)


def find_by_name(name: str) -> Optional[ReferenceFile]:
    for f in list_files():
        if f.name == name:
            return f
    return None


def upload_version(name: str, rows: List[dict], uploaded_by: str,
                    file_id: Optional[str] = None) -> ReferenceFile:
    idx = _load_index()
    if file_id is None:
        existing = find_by_name(name)
        file_id = existing.id if existing else f"ref_{uuid.uuid4().hex[:10]}"
    blob = idx.get(file_id, {"name": name, "versions": []})
    version_no = (blob["versions"][-1]["version"] + 1) if blob["versions"] else 1

    schema = infer_schema(rows)
    columns = list(schema.keys())
    rel_path = _rows_path(file_id, version_no)
    s3_store.put_text(rel_path, json.dumps(rows))

    for v in blob["versions"]:
        v["status"] = "superseded"
    blob["versions"].append({
        "version": version_no, "uploaded_by": uploaded_by, "uploaded_at": time.time(),
        "records": len(rows), "columns": columns,
        "column_types": {k: v.value for k, v in schema.items()},
        "file_path": rel_path, "status": "active",
    })
    blob["name"] = name
    idx[file_id] = blob
    _save_index(idx)
    return get_file(file_id)


def configure_source(name: str, path: str, auto_refresh_minutes: Optional[int], actor: str,
                      file_id: Optional[str] = None) -> ReferenceFile:
    """Registers (or re-registers) a reference file's source as a local/
    network file this app reads itself, instead of a browser upload — so
    an end user never has to manually re-export/re-upload it when the
    underlying data changes (see sync_now(), and
    reference_sync_scheduler.py for the automatic side of it). Syncs once
    immediately — configuring a path with nothing pulled yet is useless."""
    idx = _load_index()
    if file_id is None:
        existing = find_by_name(name)
        file_id = existing.id if existing else f"ref_{uuid.uuid4().hex[:10]}"
    blob = idx.get(file_id, {"name": name, "versions": []})
    blob["name"] = name
    blob["source_mode"] = "path"
    blob["source_path"] = path
    blob["auto_refresh_minutes"] = auto_refresh_minutes
    idx[file_id] = blob
    _save_index(idx)
    return sync_now(file_id, actor)


def sync_now(file_id: str, actor: str) -> ReferenceFile:
    """Reads this file's configured source_path and feeds it through the
    exact same upload_version() pipeline a browser upload uses — the same
    immutable versioning, the same column inference. Records
    last_synced_at/last_sync_error on the file itself so the UI (and the
    scheduler, on an auto-refresh tick) can show/act on sync health
    without this ever crashing the caller."""
    idx = _load_index()
    blob = idx.get(file_id)
    if blob is None:
        raise ValueError(f"reference file '{file_id}' not found")
    path = blob.get("source_path")
    if not path:
        raise ValueError(f"reference file '{file_id}' has no source path configured")

    try:
        with open(path) as fh:
            text = fh.read()
        rows = parse_csv_text(text)
        if not rows:
            raise ValueError(f"no rows parsed from '{path}'")
    except Exception as exc:
        blob["last_sync_error"] = str(exc)
        idx[file_id] = blob
        _save_index(idx)
        raise ValueError(str(exc)) from exc

    upload_version(blob["name"], rows, actor, file_id=file_id)

    idx = _load_index()
    blob = idx[file_id]
    blob["last_synced_at"] = time.time()
    blob["last_sync_error"] = None
    idx[file_id] = blob
    _save_index(idx)
    return get_file(file_id)


def get_rows(file_id: str, version: Optional[int] = None) -> Optional[List[dict]]:
    f = get_file(file_id)
    if f is None or not f.versions:
        return None
    ver = next((v for v in f.versions if v.version == version), None) if version else f.latest
    if ver is None:
        return None
    text = s3_store.get_text(ver.file_path)
    if text is None:
        return None
    return json.loads(text)


def reference_loader(file_id: str, version: Optional[int] = None) -> Optional[List[dict]]:
    """`workflow_engine.ReferenceLoader`-shaped accessor, shared by every
    caller that executes a workflow (dry-run, impact analysis, …)."""
    return get_rows(file_id, version)


def delete_file(file_id: str) -> bool:
    idx = _load_index()
    if file_id not in idx:
        return False
    del idx[file_id]
    _save_index(idx)
    return True
