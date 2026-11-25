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
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

from app.modules.rule_designer import lookup_engine, s3_store
from app.modules.rule_designer.models import FieldType, LookupConfig, ReferenceFile, ReferenceFileVersion
from app.modules.rule_designer.schema import infer_schema

BASE_PREFIX = "reference_data/"

# --------------------------------------------------------------------------
# Hot-path caches for get_lookup_index() — the fast path
# workflow_engine._build_indexes() uses instead of reference_loader() +
# lookup_engine.build_index(), since run_workflow() (and therefore these
# lookups) runs fresh on every product_engine.evaluate_record() call, i.e.
# once per trade in live validation. Without this, every trade re-fetches
# the index AND the actual rows from S3 and rebuilds the lookup dict from
# scratch — exactly the per-trade cost the legacy RiverIndex-based
# validators never paid (they cache their built index once per day).
#
# Two tiers, because a reference file's VERSION CONTENT is immutable once
# created — upload_version()/sync_now() always mint a new version number,
# never overwrite an existing one:
#   - which version is "latest" -> bounded-staleness TTL+ETag, same shape
#     as yaml_service._load_plain_rules_cached() (so a newly-uploaded
#     version is picked up within the TTL window, same as a rule edit).
#   - rows/built index for one concrete (file_id, version) -> cached
#     forever once fetched, since that content can never change.
#
# Never used by a write path (upload_version()/configure_source()/
# sync_now()/delete_file() all still call the always-fresh _load_index()
# directly) — a read-modify-write there must never be based on stale data.
_INDEX_CACHE_TTL_SECONDS = 30.0
_index_cache_lock = threading.Lock()
_index_cache_state: Optional[Tuple[Optional[str], float, Dict[str, dict]]] = None  # (etag, cached_at_monotonic, data)

_rows_cache_lock = threading.Lock()
_rows_cache: Dict[Tuple[str, int], List[dict]] = {}  # (file_id, version) -> rows

_lookup_index_cache_lock = threading.Lock()
_lookup_index_cache: Dict[Tuple[str, int, str], "lookup_engine.LookupIndex"] = {}  # (file_id, version, config sig) -> index


def _index_path() -> str:
    return f"{BASE_PREFIX}_index.json"


def _load_index() -> Dict[str, dict]:
    text = s3_store.get_text(_index_path())
    if text is None:
        return {}
    return json.loads(text)


def _save_index(idx: Dict[str, dict]) -> None:
    s3_store.put_text(_index_path(), json.dumps(idx, indent=2))


def _load_index_cached() -> Dict[str, dict]:
    """Bounded-staleness read of the reference-file index — used only by
    get_lookup_index()'s "which version is latest" resolution. Mirrors
    yaml_service._load_plain_rules_cached()'s shape exactly (see this
    module's cache docstring above for why this is safe: never used by a
    write path)."""
    global _index_cache_state
    now = time.monotonic()
    with _index_cache_lock:
        if _index_cache_state is not None and (now - _index_cache_state[1]) < _INDEX_CACHE_TTL_SECONDS:
            return _index_cache_state[2]

    etag = s3_store.head(_index_path())
    with _index_cache_lock:
        if _index_cache_state is not None and _index_cache_state[0] == etag:
            _index_cache_state = (etag, now, _index_cache_state[2])
            return _index_cache_state[2]

    data = _load_index()
    with _index_cache_lock:
        _index_cache_state = (etag, now, data)
    return data


def parse_csv_text(text: str) -> List[dict]:
    """Row keys are whitespace-stripped — an ordinary export shape like a
    space after the comma in the header ("Currency, Threshold") would
    otherwise produce a row keyed ' Threshold', which a LOOKUP node's
    source_column (picked from the UI's column dropdown, itself built
    from these same keys via infer_schema()) would still match AT THE
    TIME it's configured, but silently stop matching the moment this file
    is next uploaded/synced with even slightly different incidental
    whitespace in its header — a real risk for a path-sourced file
    production re-exports on its own schedule (see configure_source()/
    sync_now()). Same fix, same rationale, as
    day_partitioned_source._load_csv()."""
    reader = csv.DictReader(io.StringIO(text))
    return [{(k.strip() if isinstance(k, str) else k): v for k, v in row.items()} for row in reader]


def _col_letters_to_indices(column_range: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """"A:I" -> (1, 9); None -> (None, None) meaning "whole sheet". Resolved
    with openpyxl.utils.column_index_from_string rather than relying on
    openpyxl's native bracket-slicing ("ws['A:I']"), which isn't supported
    in the read_only=True mode parse_xlsx_rows() uses — a read-only
    worksheet only supports iter_rows()/iter_cols()."""
    if not column_range:
        return None, None
    from openpyxl.utils.cell import column_index_from_string
    try:
        left, right = column_range.split(":")
        return column_index_from_string(left.strip()), column_index_from_string(right.strip())
    except (ValueError, KeyError) as exc:
        raise ValueError(f"invalid column_range '{column_range}' (expected e.g. 'A:I')") from exc


def parse_xlsx_rows(path: str, sheet_name: Optional[str], column_range: Optional[str],
                     password_env_var: Optional[str]) -> List[dict]:
    """xlsx counterpart to parse_csv_text() — produces the exact same
    List[dict] shape (keys = header strings, values = cell values) so
    everything downstream of this function (upload_version(),
    infer_schema(), lookup_engine.build_index(), the whole LOOKUP/
    CONDITION/dry-run pipeline) needs zero awareness the data came from
    Excel.

    An optional password is resolved from an env var NAME at read time —
    the actual secret value is never returned, stored, or logged; only
    the name lives in the file's stored config (see ReferenceFile.
    password_env_var). column_range lets two unrelated tables on the same
    sheet each be read as an independent reference file."""
    import openpyxl

    if password_env_var:
        import os
        import msoffcrypto

        password = os.getenv(password_env_var)
        if not password:
            raise ValueError(
                f"environment variable '{password_env_var}' is not set (or empty) — "
                f"cannot decrypt xlsx source_path '{path}'"
            )
        with open(path, "rb") as fh:
            office_file = msoffcrypto.OfficeFile(fh)
            try:
                office_file.load_key(password=password, verify_password=True)
                decrypted = io.BytesIO()
                office_file.decrypt(decrypted)
            except Exception as exc:
                raise ValueError(
                    f"could not decrypt '{path}' with the password from env var "
                    f"'{password_env_var}' (wrong password, or file is not actually "
                    f"encrypted): {exc}"
                ) from exc
        wb = openpyxl.load_workbook(decrypted, read_only=True, data_only=True)
    else:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    try:
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                raise ValueError(
                    f"sheet '{sheet_name}' not found in '{path}' — available sheets: {wb.sheetnames}"
                )
            ws = wb[sheet_name]
        elif len(wb.sheetnames) == 1:
            ws = wb[wb.sheetnames[0]]
        else:
            raise ValueError(
                f"'{path}' has multiple sheets ({wb.sheetnames}) but no sheet_name configured"
            )

        min_col, max_col = _col_letters_to_indices(column_range)
        row_iter = ws.iter_rows(min_col=min_col, max_col=max_col, values_only=True)
        try:
            header = next(row_iter)
        except StopIteration:
            raise ValueError(f"sheet '{ws.title}' in '{path}' has no rows in range '{column_range}'")

        # Same whitespace-strip as parse_csv_text(), same reason: an
        # incidental header-whitespace change in the next re-export
        # shouldn't silently stop a LOOKUP's join key from matching.
        headers = [(h.strip() if isinstance(h, str) else h) for h in header]
        rows: List[dict] = []
        for raw_row in row_iter:
            # openpyxl returns None for a blank cell; lookup_engine.py's
            # _key_tuple() does str(raw) with no None-handling, so an
            # unnormalized blank join-key cell would become the literal
            # string "None" — a different dict key than CSV's own blank-
            # cell convention ("", via csv.DictReader) — silently
            # breaking a match against a source record's genuinely empty
            # value. Normalize here, at the source, instead of teaching
            # lookup_engine.py a second blank-value convention.
            values = ["" if v is None else v for v in raw_row]
            if all(v == "" for v in values):
                continue  # fully-blank row — common past a shorter table's
                          # real extent when two tables share one sheet and
                          # have different row counts (ws.max_row is sheet-
                          # wide, not per-column-range)
            rows.append(dict(zip(headers, values)))
        return rows
    finally:
        wb.close()


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
        source_format=blob.get("source_format"),
        sheet_name=blob.get("sheet_name"),
        column_range=blob.get("column_range"),
        password_env_var=blob.get("password_env_var"),
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
                      file_id: Optional[str] = None, source_format: Optional[str] = None,
                      sheet_name: Optional[str] = None, column_range: Optional[str] = None,
                      password_env_var: Optional[str] = None) -> ReferenceFile:
    """Registers (or re-registers) a reference file's source as a local/
    network file this app reads itself, instead of a browser upload — so
    an end user never has to manually re-export/re-upload it when the
    underlying data changes (see sync_now(), and
    reference_sync_scheduler.py for the automatic side of it). Syncs once
    immediately — configuring a path with nothing pulled yet is useless.

    source_format/sheet_name/column_range/password_env_var are xlsx-only
    (see parse_xlsx_rows()) and all optional — leaving them unset behaves
    exactly like a plain CSV path source, today's only behavior.
    password_env_var stores only the NAME of a server-side environment
    variable; the actual password is never passed to this function, never
    stored here, and never reaches the audit log (see the
    /reference-files/configure-source route)."""
    idx = _load_index()
    if file_id is None:
        existing = find_by_name(name)
        file_id = existing.id if existing else f"ref_{uuid.uuid4().hex[:10]}"
    blob = idx.get(file_id, {"name": name, "versions": []})
    blob["name"] = name
    blob["source_mode"] = "path"
    blob["source_path"] = path
    blob["auto_refresh_minutes"] = auto_refresh_minutes
    blob["source_format"] = source_format
    blob["sheet_name"] = sheet_name
    blob["column_range"] = column_range
    blob["password_env_var"] = password_env_var
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

    source_format = blob.get("source_format") or ("xlsx" if path.lower().endswith((".xlsx", ".xlsm")) else "csv")

    try:
        if source_format == "xlsx":
            rows = parse_xlsx_rows(
                path, blob.get("sheet_name"), blob.get("column_range"), blob.get("password_env_var"),
            )
        else:
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


def get_lookup_index(file_id: Optional[str], version: Optional[int],
                      config: LookupConfig) -> Optional["lookup_engine.LookupIndex"]:
    """The built LookupIndex for one reference file's (file_id, version) —
    version=None resolves to "latest" through _load_index_cached(); the
    concrete version's rows and built index are then cached indefinitely
    once fetched (see this module's cache docstring above). None if
    file_id/version doesn't resolve to anything — the internal fast path
    workflow_engine._build_indexes() uses instead of
    reference_loader()+lookup_engine.build_index()."""
    if not file_id:
        return None
    idx = _load_index_cached()
    blob = idx.get(file_id)
    if not blob or not blob.get("versions"):
        return None
    versions = blob["versions"]
    ver_blob = next((v for v in versions if v["version"] == version), None) if version else versions[-1]
    if ver_blob is None:
        return None
    concrete_version = ver_blob["version"]

    sig = lookup_engine.cache_signature(config)
    index_cache_key = (file_id, concrete_version, sig)
    with _lookup_index_cache_lock:
        cached = _lookup_index_cache.get(index_cache_key)
        if cached is not None:
            return cached

    rows_cache_key = (file_id, concrete_version)
    with _rows_cache_lock:
        rows = _rows_cache.get(rows_cache_key)
    if rows is None:
        text = s3_store.get_text(ver_blob["file_path"])
        rows = json.loads(text) if text is not None else []
        with _rows_cache_lock:
            _rows_cache[rows_cache_key] = rows

    index = lookup_engine.build_index(rows, config)
    with _lookup_index_cache_lock:
        _lookup_index_cache[index_cache_key] = index
    return index


def delete_file(file_id: str) -> bool:
    idx = _load_index()
    if file_id not in idx:
        return False
    del idx[file_id]
    _save_index(idx)
    return True
