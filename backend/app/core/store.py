"""Server-side dataset store.

Data Fetch writes pulled rows here under a dataset_id; the analysis modules read
them back by id. This is what makes Data Fetch the source of truth: you pull from
EPE / S3 once, then every module runs against the cached dataset instead of
re-hitting the upstream API.

In-memory primary, with a JSON copy per dataset under DATA_DIR so datasets
survive a backend reload during a working session.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from app.core.config import get_settings


@dataclass
class DatasetMeta:
    id: str
    source: str  # "BRV_S3" | "EPE"
    product_type: str
    legal_entities: List[str]
    source_systems: List[str]
    start_date: str
    end_date: str
    row_count: int
    fetched_at: float = field(default_factory=time.time)
    label: str = ""


_MEM: Dict[str, dict] = {}  # id -> {"meta": DatasetMeta, "rows": [...]}


def _dir() -> str:
    d = get_settings().DATA_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _path(dataset_id: str) -> str:
    return os.path.join(_dir(), f"{dataset_id}.json")


def _load_from_disk() -> None:
    if _MEM:
        return
    try:
        for fn in os.listdir(_dir()):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(_dir(), fn)) as fh:
                blob = json.load(fh)
            meta = DatasetMeta(**blob["meta"])
            _MEM[meta.id] = {"meta": meta, "rows": blob["rows"]}
    except FileNotFoundError:
        pass


def put(source: str, filters: dict, rows: List[dict]) -> DatasetMeta:
    dataset_id = f"ds_{uuid.uuid4().hex[:10]}"
    meta = DatasetMeta(
        id=dataset_id,
        source=source,
        product_type=filters.get("product_type", ""),
        legal_entities=filters.get("legal_entities", []),
        source_systems=filters.get("source_systems", []),
        start_date=filters.get("start_date", ""),
        end_date=filters.get("end_date", ""),
        row_count=len(rows),
        label=f"{source} · {filters.get('product_type','')} · {filters.get('start_date','')}→{filters.get('end_date','')}",
    )
    _MEM[dataset_id] = {"meta": meta, "rows": rows}
    try:
        with open(_path(dataset_id), "w") as fh:
            json.dump({"meta": asdict(meta), "rows": rows}, fh)
    except OSError:
        pass  # disk persistence is best-effort
    return meta


def list_datasets(source: Optional[str] = None) -> List[DatasetMeta]:
    _load_from_disk()
    metas = [v["meta"] for v in _MEM.values()]
    if source:
        metas = [m for m in metas if m.source == source]
    return sorted(metas, key=lambda m: m.fetched_at, reverse=True)


def get_rows(dataset_id: str) -> Optional[List[dict]]:
    _load_from_disk()
    rec = _MEM.get(dataset_id)
    return rec["rows"] if rec else None


def get_meta(dataset_id: str) -> Optional[DatasetMeta]:
    _load_from_disk()
    rec = _MEM.get(dataset_id)
    return rec["meta"] if rec else None


def delete(dataset_id: str) -> bool:
    _MEM.pop(dataset_id, None)
    try:
        os.remove(_path(dataset_id))
    except OSError:
        return False
    return True
