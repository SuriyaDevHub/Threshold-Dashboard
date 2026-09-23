"""Data Fetch plugin — the source of truth.

Pulls from EPE (exceptions) or BRV S3 (trades) with shared filters, then STORES
the result server-side under a dataset_id. Other modules read those datasets
back by id, so upstream APIs are hit once per pull, not once per slider move.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import store
from app.core.clients.brv_client import get_brv_client
from app.core.clients.epe_client import get_epe_client
from app.core.data_filters import (
    LEGAL_ENTITIES,
    PRODUCT_TYPES,
    SOURCE_SYSTEMS,
    filter_config,
    live_product_types,
)

META = {
    "id": "data-fetch",
    "name": "Data Fetch",
    "description": "Pull EPE exception data and BRV S3 trade data; cache as reusable datasets.",
    "icon": "boxes",
}

router = APIRouter()

PREVIEW = 200  # rows returned inline; full set stays in the store


class FetchFilters(BaseModel):
    product_type: str = Field(default_factory=lambda: (live_product_types() or PRODUCT_TYPES)[0])
    legal_entities: List[str] = Field(default_factory=lambda: [LEGAL_ENTITIES[0]])
    source_systems: List[str] = Field(default_factory=lambda: [SOURCE_SYSTEMS[0]])
    start_date: str = Field(default="2024-01-01")
    end_date: str = Field(default_factory=lambda: date.today().isoformat())


def _meta_dict(m) -> dict:
    return {
        "id": m.id, "source": m.source, "product_type": m.product_type,
        "legal_entities": m.legal_entities, "source_systems": m.source_systems,
        "start_date": m.start_date, "end_date": m.end_date,
        "row_count": m.row_count, "fetched_at": m.fetched_at, "label": m.label,
    }


@router.get("/filters")
async def get_filters():
    return filter_config()


@router.post("/trades")
async def fetch_trades(f: FetchFilters):
    rows = await get_brv_client().fetch_trades_s3(
        f.product_type, f.legal_entities, f.source_systems, f.start_date, f.end_date
    )
    meta = store.put("BRV_S3", f.model_dump(), rows)
    return {"dataset": _meta_dict(meta), "preview": rows[:PREVIEW]}


@router.post("/exceptions")
async def fetch_exceptions(f: FetchFilters):
    rows = await get_epe_client().fetch_exceptions(
        f.product_type, f.legal_entities, f.source_systems, f.start_date, f.end_date
    )
    meta = store.put("EPE", f.model_dump(), rows)
    return {"dataset": _meta_dict(meta), "preview": rows[:PREVIEW]}


@router.get("/datasets")
async def list_datasets(source: Optional[str] = None):
    return {"datasets": [_meta_dict(m) for m in store.list_datasets(source)]}


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, limit: int = 500):
    meta = store.get_meta(dataset_id)
    rows = store.get_rows(dataset_id)
    if meta is None or rows is None:
        raise HTTPException(404, "Dataset not found")
    return {"dataset": _meta_dict(meta), "rows": rows[:limit]}


@router.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str):
    store.delete(dataset_id)
    return {"deleted": dataset_id}
