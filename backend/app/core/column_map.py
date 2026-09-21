"""Column mapping — the bridge between raw EPE / S3 columns and the canonical
fields the analysis modules expect.

When you go live, edit the right-hand values to match your real column names.
Nothing else in the analysis code needs to change.
"""
from __future__ import annotations

from typing import Dict, List

# canonical_field -> your raw column name (trade data from S3 / BRV)
TRADE_MAP: Dict[str, str] = {
    "trade_id": "trade_id",
    "trade_date": "trade_date",
    "product_type": "product_type",
    # column to GROUP ON for calibration buckets. Point this at a rating /
    # sub-product / tenor column if you have one; else it groups by product_type.
    "bucket": "product_type",
    "legal_entity": "legal_entity",
    "source_system": "source_system",
    "currency": "currency",
    "booked": "booked_price",        # booked price / rate
    "reference": "reference_price",  # market reference price / rate
    "pnl": "pnl",                    # EOD day-1 PnL (for the PnL check)
    "notional": "notional",          # for the PnL/Notional check
}

# canonical_field -> your raw column name (exception data from EPE)
EXCEPTION_MAP: Dict[str, str] = {
    "exception_id": "exception_id",
    "trade_id": "trade_id",
    "raised_date": "raised_date",
    "product_type": "product_type",
    "legal_entity": "legal_entity",
    "source_system": "source_system",
    "currency": "currency",
    "status": "status",
    "resolution_level": "resolution_level",
    "outcome": "outcome",
    "deviation": "deviation",
}

# Exception resolution -> ground-truth label:
#   GENUINE  (True Positive)  = confirmed off-market at L2 / escalated / upheld
#   L1_FALSE_POSITIVE         = closed at L1 (alert fired, found to be nothing)
# Edit these to match your real EPE status / resolution values.
GENUINE_STATUSES = {"L2_CONFIRMED", "ESCALATED", "UPHELD", "APPROVED"}
L1_FP_STATUSES = {"L1_CLOSED", "CLOSED_L1", "CLOSED"}
# Back-compat alias used elsewhere as "confirmed off-market".
CONFIRMED_STATUSES = GENUINE_STATUSES


def _normalize(row: Dict, mapping: Dict[str, str]) -> Dict:
    out = {canon: row.get(raw) for canon, raw in mapping.items()}
    out["_raw"] = row  # keep original for display / export
    return out


def normalize_trades(rows: List[Dict]) -> List[Dict]:
    return [_normalize(r, TRADE_MAP) for r in rows]


def normalize_exceptions(rows: List[Dict]) -> List[Dict]:
    return [_normalize(r, EXCEPTION_MAP) for r in rows]
