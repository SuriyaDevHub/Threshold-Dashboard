"""Reference (lookup) file management — upload, versioning (spec §39-40).

Every reference file gets an id; every upload against that id (same name)
creates a new, immutable version. A published rule pins the exact version
it was validated against, so results stay reproducible even if the
reference file is updated later (spec §40).
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import uuid
from typing import Dict, List, Optional

from app.modules.rule_designer.models import FieldType, ReferenceFile, ReferenceFileVersion
from app.modules.rule_designer.schema import infer_schema

BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "reference_data")


def _base_dir() -> str:
    os.makedirs(BASE_DIR, exist_ok=True)
    return BASE_DIR


def _index_path() -> str:
    return os.path.join(_base_dir(), "_index.json")


def _load_index() -> Dict[str, dict]:
    path = _index_path()
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


def _save_index(idx: Dict[str, dict]) -> None:
    path = _index_path()
    tmp = path + f".tmp{uuid.uuid4().hex[:6]}"
    with open(tmp, "w") as fh:
        json.dump(idx, fh, indent=2)
    os.replace(tmp, path)


def parse_csv_text(text: str) -> List[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _rows_path(file_id: str, version: int) -> str:
    d = os.path.join(_base_dir(), file_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"v{version}.json")


def list_files() -> List[ReferenceFile]:
    idx = _load_index()
    out = []
    for file_id, blob in idx.items():
        out.append(ReferenceFile(
            id=file_id, name=blob["name"],
            versions=[ReferenceFileVersion(**v) for v in blob["versions"]],
        ))
    return sorted(out, key=lambda f: f.name)


def get_file(file_id: str) -> Optional[ReferenceFile]:
    idx = _load_index()
    blob = idx.get(file_id)
    if not blob:
        return None
    return ReferenceFile(id=file_id, name=blob["name"],
                          versions=[ReferenceFileVersion(**v) for v in blob["versions"]])


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
    rel_path = os.path.relpath(_rows_path(file_id, version_no), _base_dir())
    with open(_rows_path(file_id, version_no), "w") as fh:
        json.dump(rows, fh)

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


def get_rows(file_id: str, version: Optional[int] = None) -> Optional[List[dict]]:
    f = get_file(file_id)
    if f is None or not f.versions:
        return None
    ver = next((v for v in f.versions if v.version == version), None) if version else f.latest
    if ver is None:
        return None
    path = os.path.join(_base_dir(), ver.file_path)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


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
