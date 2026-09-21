"""Audit Sampling plugin — samples from a fetched EPE exception dataset."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import store
from app.core.column_map import normalize_exceptions
from app.modules.audit_sampling.service import coverage, random_sample, stratified_sample

META = {
    "id": "audit-sampling",
    "name": "Audit Sampling",
    "description": "Draw a reproducible, audit-ready sample from a fetched EPE dataset.",
    "icon": "list-checks",
}

router = APIRouter()
STRATIFY_KEY = "product_type"


class SampleBody(BaseModel):
    dataset_id: str
    size: int = 20
    method: str = "stratified"  # stratified | random
    seed: int = 42


@router.get("/exception-datasets")
async def exception_datasets():
    metas = store.list_datasets("EPE")
    return {
        "datasets": [
            {"id": m.id, "label": m.label, "product_type": m.product_type,
             "row_count": m.row_count, "start_date": m.start_date, "end_date": m.end_date}
            for m in metas
        ]
    }


@router.post("/sample")
async def run_sample(body: SampleBody):
    meta = store.get_meta(body.dataset_id)
    raw = store.get_rows(body.dataset_id)
    if meta is None or raw is None:
        raise HTTPException(404, "Dataset not found")
    if meta.source != "EPE":
        raise HTTPException(400, "Audit Sampling needs an EPE exception dataset")

    population = normalize_exceptions(raw)
    if body.method == "random":
        sample = random_sample(population, body.size, seed=body.seed)
    else:
        sample = stratified_sample(population, body.size, key=STRATIFY_KEY, seed=body.seed)

    return {
        "dataset_id": meta.id, "method": body.method, "seed": body.seed,
        "stats": coverage(sample, population, key=STRATIFY_KEY),
        "sample": [s.get("_raw", s) for s in sample],
    }
