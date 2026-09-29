"""Shared S3 storage primitives for Rule Designer's persistence layer (rules,
versions, reference data, audit log, product registry). Every key passed to
this module is relative to `Settings.RULE_DESIGNER_S3_PREFIX` — callers use
the same relative paths they used against local disk before this module
existed (e.g. "rules/TESTPROD/business_rules.yml"), and this module joins
the common prefix internally.

Mirrors the lazy-import + client-construction pattern already used by
app.core.clients.brv_client for BRV trade data on the same bucket.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from app.core.config import get_settings

# boto3/botocore client construction is documented as NOT safe to do
# concurrently from multiple threads (the underlying service-model loader
# has shared mutable state) — confirmed via a real repro: constructing a
# fresh client per call under this module's own concurrent-writer test
# produced intermittent "I/O operation on closed file" errors. A client's
# actual method calls (get_object/put_object/...) ARE thread-safe once
# constructed, so one cached client per (region, endpoint) is reused
# instead of building a new one on every call; only the cache lookup/build
# itself is guarded.
_client_lock = threading.Lock()
_client_cache: Dict[Tuple[str, str], object] = {}


def _client():
    import boto3  # imported lazily so non-S3 code paths need no AWS deps

    s = get_settings()
    key = (s.S3_REGION, s.S3_ENDPOINT_URL)
    with _client_lock:
        client = _client_cache.get(key)
        if client is None:
            kwargs = {"region_name": s.S3_REGION}
            if s.S3_ENDPOINT_URL:
                kwargs["endpoint_url"] = s.S3_ENDPOINT_URL
            client = boto3.client("s3", **kwargs)
            _client_cache[key] = client
        return client


def _full_key(key: str) -> str:
    prefix = get_settings().RULE_DESIGNER_S3_PREFIX or ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{key}"


_TRANSIENT_RETRIES = 3


def _with_retry(fn):
    """Retries `fn()` on a transient I/O error from the underlying HTTP
    stream (observed under heavy concurrent access to the same key — a
    fresh request on the next attempt gets a fresh connection/stream).
    Every operation here is naturally retry-safe: GET/HEAD are read-only,
    and PUT of a full object is idempotent."""
    last_exc: Optional[Exception] = None
    for _attempt in range(_TRANSIENT_RETRIES):
        try:
            return fn()
        except (ValueError, OSError) as exc:
            last_exc = exc
            continue
    raise last_exc  # pragma: no cover - exhausted retries


def get_text(key: str) -> Optional[str]:
    from botocore.exceptions import ClientError

    s = get_settings()

    def _do():
        try:
            obj = _client().get_object(Bucket=s.S3_BUCKET, Key=_full_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return obj["Body"].read().decode("utf-8")

    return _with_retry(_do)


def put_text(key: str, text: str) -> str:
    """Writes `text` to `key` and returns the response ETag — a single S3
    PUT is already atomic, so callers never need a temp-file+rename dance."""
    s = get_settings()

    def _do():
        resp = _client().put_object(Bucket=s.S3_BUCKET, Key=_full_key(key), Body=text.encode("utf-8"))
        return resp.get("ETag", "")

    return _with_retry(_do)


def head(key: str) -> Optional[str]:
    """The current ETag for `key` without downloading its body, or None if
    it doesn't exist. Used for cheap staleness checks."""
    from botocore.exceptions import ClientError

    s = get_settings()

    def _do():
        try:
            resp = _client().head_object(Bucket=s.S3_BUCKET, Key=_full_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return resp.get("ETag", "")

    return _with_retry(_do)


def exists(key: str) -> bool:
    return head(key) is not None


def delete(key: str) -> None:
    s = get_settings()
    _client().delete_object(Bucket=s.S3_BUCKET, Key=_full_key(key))


def list_keys(prefix: str) -> List[str]:
    """Every key under `prefix`, relative to RULE_DESIGNER_S3_PREFIX (not
    the full S3 key) — paginated."""
    s = get_settings()
    c = _client()
    full_prefix = _full_key(prefix)
    base_prefix_len = len(_full_key(""))
    out: List[str] = []
    token = None
    while True:
        kw = {"Bucket": s.S3_BUCKET, "Prefix": full_prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = c.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            out.append(obj["Key"][base_prefix_len:])
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return out


def list_dirs(prefix: str) -> List[str]:
    """Immediate 'subdirectory' names under `prefix` (S3's CommonPrefixes,
    via Delimiter='/') — the S3 equivalent of os.listdir()+os.path.isdir().
    `prefix` must end with '/'."""
    s = get_settings()
    c = _client()
    full_prefix = _full_key(prefix)
    out: List[str] = []
    token = None
    while True:
        kw = {"Bucket": s.S3_BUCKET, "Prefix": full_prefix, "Delimiter": "/"}
        if token:
            kw["ContinuationToken"] = token
        resp = c.list_objects_v2(**kw)
        for cp in resp.get("CommonPrefixes", []):
            name = cp["Prefix"][len(full_prefix):].rstrip("/")
            if name:
                out.append(name)
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return out


def delete_prefix(prefix: str) -> None:
    """Deletes every key under `prefix` — the S3 equivalent of
    shutil.rmtree(). Batched at up to 1000 keys per delete_objects call."""
    keys = list_keys(prefix)
    if not keys:
        return
    s = get_settings()
    c = _client()
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        c.delete_objects(
            Bucket=s.S3_BUCKET,
            Delete={"Objects": [{"Key": _full_key(k)} for k in batch]},
        )


def copy_prefix(old_prefix: str, new_prefix: str) -> None:
    """Moves every key under `old_prefix` to the equivalent key under
    `new_prefix` (copy then delete-source) — the S3 equivalent of
    os.rename() on a directory. NOT atomic: a crash mid-copy can leave a
    partial result at the destination with the source still present. Used
    only for rare admin actions (product rename/merge), never a hot path."""
    keys = list_keys(old_prefix)
    if not keys:
        return
    s = get_settings()
    c = _client()
    for old_key in keys:
        suffix = old_key[len(old_prefix):]
        new_key = f"{new_prefix}{suffix}"
        c.copy_object(
            Bucket=s.S3_BUCKET,
            CopySource={"Bucket": s.S3_BUCKET, "Key": _full_key(old_key)},
            Key=_full_key(new_key),
        )
    delete_prefix(old_prefix)
