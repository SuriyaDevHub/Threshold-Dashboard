"""Threshold Analysis plugin — per-product calibrators, dispatched by product type.

Calibration and backtesting both dispatch on the dataset's product type.
Backtesting uses EPE data (confirmed off-market) as ground truth.
"""
from __future__ import annotations

from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import store
from app.core.column_map import (
    GENUINE_STATUSES, L1_FP_STATUSES, normalize_exceptions, normalize_trades,
)
from app.core.metrics import UNIT, metric_for
from app.modules.threshold_analysis.calibrators import get_calibrator, list_supported

META = {
    "id": "threshold-analysis",
    "name": "Threshold Analysis",
    "description": "Per-product threshold calibration & backtesting (dispatched by product type).",
    "icon": "sliders",
}

router = APIRouter()


def _trade_dataset(dataset_id: str):
    meta = store.get_meta(dataset_id)
    rows = store.get_rows(dataset_id)
    if meta is None or rows is None:
        raise HTTPException(404, "Dataset not found")
    if meta.source != "BRV_S3":
        raise HTTPException(400, "Threshold Analysis needs a BRV S3 trade dataset")
    return meta, normalize_trades(rows)


def _labels(epe_dataset_id: Optional[str]):
    """Return (genuine_ids, l1fp_ids, meta_id) from an EPE dataset's resolution."""
    if not epe_dataset_id:
        return set(), set(), None
    emeta = store.get_meta(epe_dataset_id)
    erows = store.get_rows(epe_dataset_id)
    if not emeta or not erows or emeta.source != "EPE":
        return set(), set(), None
    genuine, l1fp = set(), set()
    for e in normalize_exceptions(erows):
        st = e.get("status")
        if st in GENUINE_STATUSES:
            genuine.add(e.get("trade_id"))
        elif st in L1_FP_STATUSES:
            l1fp.add(e.get("trade_id"))
    return genuine, l1fp, emeta.id


def _business_benefit(flags, genuine, l1fp, minutes_per_l1):
    """Quantify FP removed / TP retained on the EPE alert population."""
    present = set(flags)
    g_in = genuine & present     # genuine exceptions whose trade is in this dataset
    f_in = l1fp & present        # L1 false-positive exceptions present
    tp_retained = sum(1 for t in g_in if flags[t])
    fp_eliminated = sum(1 for t in f_in if not flags[t])
    total_exc = len(genuine) + len(l1fp)
    matched = len(g_in) + len(f_in)
    hours = round(fp_eliminated * minutes_per_l1 / 60.0, 1)
    return {
        "current_exceptions": total_exc,
        "current_false_positives": len(l1fp),
        "current_fp_rate_pct": round(len(l1fp) / total_exc * 100, 1) if total_exc else 0.0,
        "current_genuine": len(genuine),
        "fp_eliminated": fp_eliminated,
        "fp_eliminated_pct": round(fp_eliminated / len(f_in) * 100, 1) if f_in else 0.0,
        "tp_retained": tp_retained,
        "tp_retained_pct": round(tp_retained / len(g_in) * 100, 1) if g_in else 0.0,
        "tp_dropped": len(g_in) - tp_retained,
        "hours_saved": hours,
        "minutes_per_l1": minutes_per_l1,
        "join_matched": matched,
        "join_total": total_exc,
        "join_match_rate_pct": round(matched / total_exc * 100, 1) if total_exc else 0.0,
    }


class CalibrateBody(BaseModel):
    dataset_id: str
    params: Dict = Field(default_factory=dict)


class BacktestBody(BaseModel):
    dataset_id: str
    epe_dataset_id: Optional[str] = None
    params: Dict = Field(default_factory=dict)
    minutes_per_l1: int = 20


@router.get("/trade-datasets")
async def trade_datasets():
    metas = store.list_datasets("BRV_S3")
    return {"datasets": [
        {"id": m.id, "label": m.label, "product_type": m.product_type,
         "metric": metric_for(m.product_type), "unit": UNIT[metric_for(m.product_type)],
         "row_count": m.row_count, "start_date": m.start_date, "end_date": m.end_date,
         "supported": m.product_type in list_supported()}
        for m in metas
    ]}


@router.get("/config")
async def config(product_type: str):
    cal = get_calibrator(product_type)
    cfg = cal.config()
    return {"product_type": product_type, "method": cal.method,
            "method_label": cal.method_label, **cfg}


@router.post("/calibrate")
async def run_calibrate(body: CalibrateBody):
    meta, rows = _trade_dataset(body.dataset_id)
    cal = get_calibrator(meta.product_type)
    result = cal.calibrate(meta.product_type, rows, body.params)
    return {"dataset_id": meta.id, "product_type": meta.product_type, **result}


@router.post("/backtest")
async def run_backtest(body: BacktestBody):
    meta, rows = _trade_dataset(body.dataset_id)
    cal = get_calibrator(meta.product_type)
    genuine, l1fp, epe_used = _labels(body.epe_dataset_id)
    # Confusion is scored against the GENUINE (L2/confirmed) set as positive class.
    result = cal.backtest(meta.product_type, rows, genuine, body.params)
    benefit = None
    if epe_used:
        flags = cal.candidate_flags(meta.product_type, rows, body.params)
        benefit = _business_benefit(flags, genuine, l1fp, body.minutes_per_l1)
    return {"dataset_id": meta.id, "epe_dataset_id": epe_used,
            "product_type": meta.product_type, "business_benefit": benefit, **result}
