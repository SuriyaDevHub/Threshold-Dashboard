"""Asset-class registry — the single source of truth for OMRC calibration.

Each asset class declares:
  - metric: how deviation is measured (pct | bps | pips)
  - unit:   display unit
  - buckets: product buckets, each with reference level + typical vol (for mock),
             a per-bucket pip_factor (FX only), and the CURRENT live/policy
             threshold used as the backtest comparison baseline.

Add an asset class or bucket here and it flows through calibration, backtest,
mock data, and the UI selector automatically.
"""
from __future__ import annotations

from typing import Dict, List, Optional

ASSET_CLASSES: Dict[str, dict] = {
    "CASH_BONDS": {
        "code": "CASH_BONDS",
        "name": "Cash Bonds",
        "metric": "pct",
        "unit": "%",
        "k_default": 4.0,
        "floor_default": 0.10,
        "buckets": [
            {"code": "IG_CORP", "name": "IG Corporate", "ccy": "USD", "ref": 100.0, "vol": 0.12, "current": 0.40},
            {"code": "HY_CORP", "name": "HY Corporate", "ccy": "USD", "ref": 98.0, "vol": 0.45, "current": 1.20},
            {"code": "GOVT_OTR", "name": "Govt On-the-run", "ccy": "USD", "ref": 101.0, "vol": 0.06, "current": 0.20},
            {"code": "GOVT_OFR", "name": "Govt Off-the-run", "ccy": "GBP", "ref": 99.5, "vol": 0.18, "current": 0.50},
            {"code": "EM_SOV", "name": "EM Sovereign", "ccy": "EUR", "ref": 96.0, "vol": 0.55, "current": 1.50},
            {"code": "SUPRA", "name": "Supranational", "ccy": "EUR", "ref": 100.5, "vol": 0.10, "current": 0.35},
        ],
    },
    "GFX_CASH": {
        "code": "GFX_CASH",
        "name": "GFX Cash",
        "metric": "pips",
        "unit": "pips",
        "k_default": 4.0,
        "floor_default": 1.0,
        "buckets": [
            {"code": "EURUSD", "name": "EUR/USD", "ccy": "EUR", "ref": 1.0850, "vol": 0.00015, "pip_factor": 10000, "current": 5.0},
            {"code": "GBPUSD", "name": "GBP/USD", "ccy": "GBP", "ref": 1.2700, "vol": 0.00020, "pip_factor": 10000, "current": 6.0},
            {"code": "USDJPY", "name": "USD/JPY", "ccy": "JPY", "ref": 150.20, "vol": 0.020, "pip_factor": 100, "current": 6.0},
            {"code": "USDINR", "name": "USD/INR", "ccy": "INR", "ref": 83.30, "vol": 0.010, "pip_factor": 100, "current": 8.0},
            {"code": "USDBRL", "name": "USD/BRL", "ccy": "BRL", "ref": 5.4200, "vol": 0.0020, "pip_factor": 10000, "current": 20.0},
        ],
    },
    "IRD": {
        "code": "IRD",
        "name": "Rates Derivatives (IRD)",
        "metric": "bps",
        "unit": "bps",
        "k_default": 4.0,
        "floor_default": 0.5,
        "buckets": [
            {"code": "IRS_USD", "name": "USD IRS", "ccy": "USD", "ref": 4.25, "vol": 0.010, "current": 2.0},
            {"code": "IRS_EUR", "name": "EUR IRS", "ccy": "EUR", "ref": 2.80, "vol": 0.012, "current": 2.5},
            {"code": "IRS_GBP", "name": "GBP IRS", "ccy": "GBP", "ref": 4.05, "vol": 0.013, "current": 2.5},
            {"code": "OIS_USD", "name": "USD OIS", "ccy": "USD", "ref": 4.33, "vol": 0.006, "current": 1.5},
            {"code": "BASIS", "name": "Cross-Ccy Basis", "ccy": "USD", "ref": -0.15, "vol": 0.020, "current": 4.0},
        ],
    },
    "MM": {
        "code": "MM",
        "name": "Money Markets",
        "metric": "bps",
        "unit": "bps",
        "k_default": 4.0,
        "floor_default": 0.5,
        "buckets": [
            {"code": "DEPO", "name": "Deposits", "ccy": "USD", "ref": 4.40, "vol": 0.008, "current": 2.0},
            {"code": "CP", "name": "Commercial Paper", "ccy": "USD", "ref": 4.55, "vol": 0.012, "current": 3.0},
            {"code": "CD", "name": "Certificates of Deposit", "ccy": "EUR", "ref": 2.90, "vol": 0.011, "current": 3.0},
            {"code": "REPO", "name": "Repo", "ccy": "USD", "ref": 4.32, "vol": 0.006, "current": 1.5},
        ],
    },
}


def list_asset_classes() -> List[dict]:
    return [
        {
            "code": ac["code"],
            "name": ac["name"],
            "metric": ac["metric"],
            "unit": ac["unit"],
            "k_default": ac["k_default"],
            "floor_default": ac["floor_default"],
            "buckets": [
                {"code": b["code"], "name": b["name"], "ccy": b["ccy"], "current": b["current"]}
                for b in ac["buckets"]
            ],
        }
        for ac in ASSET_CLASSES.values()
    ]


def bucket_index(ac_code: str) -> Dict[str, dict]:
    return {b["code"]: b for b in ASSET_CLASSES[ac_code]["buckets"]}


def deviation(ac_code: str, product_code: str, booked: float, reference: float) -> float:
    """Deviation in the asset class's native unit."""
    ac = ASSET_CLASSES[ac_code]
    metric = ac["metric"]
    if metric == "pct":
        return abs(booked - reference) / abs(reference) * 100.0 if reference else 0.0
    if metric == "bps":
        # rates expressed in percent -> 0.01% == 1bp
        return abs(booked - reference) * 100.0
    if metric == "pips":
        b = bucket_index(ac_code).get(product_code, {})
        return abs(booked - reference) * b.get("pip_factor", 10000)
    return 0.0


def current_thresholds(ac_code: str) -> Dict[str, float]:
    return {b["code"]: b["current"] for b in ASSET_CLASSES[ac_code]["buckets"]}
