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

from app.modules.rule_designer import lookup_engine, s3_store, secret_crypto
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


def _resolve_xlsx_password(password_env_var: Optional[str], encrypted_password: Optional[str],
                            password: Optional[str] = None) -> Optional[str]:
    """Three alternative ways to supply an xlsx password. `password` (a
    plaintext value already in hand for this one call — e.g. a typed,
    one-shot preview password that's never persisted) takes precedence,
    since routing it through encrypt-then-immediately-decrypt would gain
    nothing; an encrypted, admin-typed one (see secret_crypto.py) comes
    next; configure_source() rejects setting both of the stored forms at
    once, so in practice at most one of encrypted_password/
    password_env_var is ever set anyway. Neither path ever returns
    anything that gets stored back or logged; the resolved plaintext
    lives only in a local variable, used immediately and then discarded
    once this call returns."""
    if password:
        return password
    if encrypted_password:
        return secret_crypto.decrypt_secret(encrypted_password)
    if password_env_var:
        import os

        password = os.getenv(password_env_var)
        if not password:
            raise ValueError(f"environment variable '{password_env_var}' is not set (or empty)")
        return password
    return None


def _infer_excel_format(path: str) -> str:
    """"csv" unless the extension says this is an Excel file — the one
    rule both sync_now() and parse_xlsx_rows() use for "which reader does
    this file need" when no explicit source_format is configured."""
    lower = path.lower()
    if lower.endswith(".xlsb"):
        return "xlsb"
    if lower.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    return "csv"


def _normalize_excel_rows(header_row, data_rows) -> List[dict]:
    """Shared by both the openpyxl (.xlsx/.xlsm) and pyxlsb (.xlsb)
    readers: header-whitespace-strip (same rationale as
    parse_csv_text()'s own) and blank-cell/blank-row handling. A blank
    cell must become "" rather than stay None — lookup_engine.py's
    _key_tuple() does str(raw) with no None-handling, so an unnormalized
    blank join-key cell would become the literal string "None", a
    different dict key than CSV's own blank-cell convention ("", via
    csv.DictReader) — silently breaking a match against a source record's
    genuinely empty value. A fully-blank row is skipped outright — common
    past a shorter table's real extent when two tables share one sheet
    via differing column_range (the sheet's overall used-row count is
    shared, not per-column-range)."""
    headers = [(h.strip() if isinstance(h, str) else h) for h in header_row]
    rows: List[dict] = []
    for raw_row in data_rows:
        values = ["" if v is None else v for v in raw_row]
        if all(v == "" for v in values):
            continue
        rows.append(dict(zip(headers, values)))
    return rows


def _read_xlsx_rows(source, sheet_name: Optional[str], column_range: Optional[str],
                     path_for_errors: str) -> List[dict]:
    """.xlsx/.xlsm reader via openpyxl. `source` is either the original
    path or a decrypted in-memory io.BytesIO — openpyxl.load_workbook()
    accepts either."""
    import openpyxl

    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                raise ValueError(
                    f"sheet '{sheet_name}' not found in '{path_for_errors}' — available sheets: {wb.sheetnames}"
                )
            ws = wb[sheet_name]
        elif len(wb.sheetnames) == 1:
            ws = wb[wb.sheetnames[0]]
        else:
            raise ValueError(
                f"'{path_for_errors}' has multiple sheets ({wb.sheetnames}) but no sheet_name configured"
            )

        min_col, max_col = _col_letters_to_indices(column_range)
        row_iter = ws.iter_rows(min_col=min_col, max_col=max_col, values_only=True)
        try:
            header = next(row_iter)
        except StopIteration:
            raise ValueError(f"sheet '{ws.title}' in '{path_for_errors}' has no rows in range '{column_range}'")
        return _normalize_excel_rows(header, row_iter)
    finally:
        wb.close()


def _read_xlsb_rows(source, sheet_name: Optional[str], column_range: Optional[str],
                     path_for_errors: str) -> List[dict]:
    """.xlsb (Excel Binary Workbook) reader via pyxlsb — a different
    binary (BIFF12-in-zip) format openpyxl cannot open at all, read here
    with the same sheet-resolution and column_range/blank/header handling
    as _read_xlsx_rows() above. `source` is either the original path or a
    decrypted in-memory io.BytesIO — pyxlsb.open_workbook() accepts
    either (it opens whatever it's given with zipfile.ZipFile, which
    takes a path or a file-like object).

    Unlike openpyxl's iter_rows(min_col=, max_col=), pyxlsb's
    ws.rows() has no column-range option — each row comes back as a
    plain list already indexed by absolute 0-based column number (one
    Cell(r, c, v) namedtuple per column, v is None for a blank cell), so
    column_range is applied as an ordinary list slice instead."""
    import pyxlsb

    wb = pyxlsb.open_workbook(source)
    try:
        if sheet_name:
            if sheet_name.lower() not in [s.lower() for s in wb.sheets]:
                raise ValueError(
                    f"sheet '{sheet_name}' not found in '{path_for_errors}' — available sheets: {wb.sheets}"
                )
            ws = wb.get_sheet(sheet_name)
        elif len(wb.sheets) == 1:
            ws = wb.get_sheet(wb.sheets[0])
        else:
            raise ValueError(
                f"'{path_for_errors}' has multiple sheets ({wb.sheets}) but no sheet_name configured"
            )
        try:
            min_col, max_col = _col_letters_to_indices(column_range)
            lo = (min_col - 1) if min_col else None
            hi = max_col if max_col else None

            def sliced_values(row):
                return [cell.v for cell in row][lo:hi]

            row_iter = ws.rows()
            try:
                header = sliced_values(next(row_iter))
            except StopIteration:
                raise ValueError(f"sheet '{ws.name}' in '{path_for_errors}' has no rows in range '{column_range}'")
            return _normalize_excel_rows(header, (sliced_values(r) for r in row_iter))
        finally:
            ws.close()
    finally:
        wb.close()


def parse_xlsx_rows(path: str, sheet_name: Optional[str], column_range: Optional[str],
                     password_env_var: Optional[str] = None,
                     encrypted_password: Optional[str] = None,
                     password: Optional[str] = None,
                     source_format: Optional[str] = None) -> List[dict]:
    """Excel reader (.xlsx/.xlsm via openpyxl, .xlsb via pyxlsb) —
    produces the exact same List[dict] shape as parse_csv_text() so
    everything downstream of this function (upload_version(),
    infer_schema(), lookup_engine.build_index(), the whole LOOKUP/
    CONDITION/dry-run pipeline) needs zero awareness the data came from
    Excel, or which Excel format.

    `source_format` ("xlsx" or "xlsb") wins when given; otherwise it's
    inferred from `path`'s extension (see _infer_excel_format()) — same
    "explicit wins, else infer" rule used everywhere else a format is
    configurable in this module.

    An optional password is resolved from either a server-side env var
    NAME or an admin-typed, encrypted-at-rest value (see
    _resolve_xlsx_password()) — the actual secret value is never
    returned, stored, or logged by this function; only its source (an env
    var name, or ciphertext) lives in the file's stored config. The
    decrypt step itself is format-agnostic: msoffcrypto decrypts the
    outer OLE2/CFB container the same way regardless of what's inside it,
    so the same decrypted bytes feed whichever of the two readers below
    actually runs.
    column_range lets two unrelated tables on the same sheet each be read
    as an independent reference file."""
    fmt = source_format or _infer_excel_format(path)

    resolved_password = _resolve_xlsx_password(password_env_var, encrypted_password, password)
    if resolved_password:
        import msoffcrypto

        with open(path, "rb") as fh:
            office_file = msoffcrypto.OfficeFile(fh)
            try:
                office_file.load_key(password=resolved_password, verify_password=True)
                decrypted = io.BytesIO()
                office_file.decrypt(decrypted)
            except Exception as exc:
                raise ValueError(
                    f"could not decrypt '{path}' with the configured password "
                    f"(wrong password, or file is not actually encrypted): {exc}"
                ) from exc
        source = decrypted
    else:
        source = path

    if fmt == "xlsb":
        return _read_xlsb_rows(source, sheet_name, column_range, path)
    return _read_xlsx_rows(source, sheet_name, column_range, path)


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
        encrypted_password=blob.get("encrypted_password"),
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
                      password_env_var: Optional[str] = None, password: Optional[str] = None) -> ReferenceFile:
    """Registers (or re-registers) a reference file's source as a local/
    network file this app reads itself, instead of a browser upload — so
    an end user never has to manually re-export/re-upload it when the
    underlying data changes (see sync_now(), and
    reference_sync_scheduler.py for the automatic side of it). Syncs once
    immediately — configuring a path with nothing pulled yet is useless.

    source_format/sheet_name/column_range are Excel-only (xlsx/xlsm/xlsb — see
    parse_xlsx_rows()) and all optional — leaving them unset behaves
    exactly like a plain CSV path source, today's only behavior.

    Two alternative, mutually exclusive ways to supply an xlsx password:
    password_env_var stores only the NAME of a server-side environment
    variable (the actual value is never passed to this function); password
    is a plaintext an admin typed into the config form, which this
    function encrypts immediately via secret_crypto.encrypt_secret() and
    stores only as ciphertext — the plaintext parameter is never itself
    stored or logged (see the /reference-files/configure-source route for
    the audit-log call, which never interpolates either)."""
    if password_env_var and password:
        raise ValueError("set either a password env var name or a typed password, not both")
    encrypted_password = secret_crypto.encrypt_secret(password) if password else None

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
    blob["encrypted_password"] = encrypted_password
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

    source_format = blob.get("source_format") or _infer_excel_format(path)

    try:
        if source_format in ("xlsx", "xlsb"):
            rows = parse_xlsx_rows(
                path, blob.get("sheet_name"), blob.get("column_range"),
                blob.get("password_env_var"), blob.get("encrypted_password"),
                source_format=source_format,
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


def find_rules_using(file_id: str) -> List[Tuple[str, str]]:
    """[(product_code, rule_id), ...] for every rule with a LOOKUP/
    ENRICHMENT node pointing at file_id (as either its primary
    reference_file_id or fallback_reference_file_id) — used to warn an
    admin before deleting a reference file whose versions a published
    rule may still be pinned to (delete_file() itself doesn't check this;
    nothing here blocks the delete, it's a warning the caller surfaces).

    Uses rule_store.list_rules() (not yaml_service directly) because a
    freshly-created or edited rule lives in draft_store until it's
    published — yaml_service.load_rules() alone would miss it, making a
    reference file look unused when a draft rule's LOOKUP node already
    points at it. rule_store.list_rules() already merges both, preferring
    a draft over its published counterpart, same as the rest of the admin
    UI does. Bounded, admin-triggered (the delete-confirmation flow), not
    a hot path — no caching, same as any other one-off admin lookup in
    this module."""
    from app.modules.rule_designer import rule_store
    from app.modules.rule_designer.models import NodeType

    hits: List[Tuple[str, str]] = []
    for rule in rule_store.list_rules():
        for node in rule.workflow.nodes:
            if node.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and node.lookup and (
                node.lookup.reference_file_id == file_id or node.lookup.fallback_reference_file_id == file_id
            ):
                hits.append((rule.product, rule.rule_id))
                break
    return hits


def delete_file(file_id: str) -> bool:
    idx = _load_index()
    if file_id not in idx:
        return False
    del idx[file_id]
    _save_index(idx)
    return True
