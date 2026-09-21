"""Shared helpers for product calibrators + the response envelope contract.

A calibrator is any object exposing:
    product_types : list[str]        # which product types it handles ([] = default)
    method, method_label : str
    config()                         -> {"params": [param specs]}
    calibrate(rows, params)          -> envelope (see build_calibration)
    backtest(rows, confirmed_ids, params) -> envelope (see build_backtest)

The envelope is generic (columns + rows + chart + summary) so ONE UI renders any
product. Add a calibrator module -> new product works with no UI change.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List

ANNUALIZE = math.sqrt(252.0)


# ---------- estimators ----------
def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
    return s[idx]


def mad_threshold(values: List[float], k: float, floor: float = 0.0) -> float:
    if not values:
        return floor
    med = statistics.median(values)
    mad = 1.4826 * statistics.median([abs(v - med) for v in values])
    return max(floor, med + k * mad)


def daily_log_returns(series: List[float]) -> List[float]:
    out = []
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        if a and b and a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def daily_vol(series: List[float]) -> float:
    rets = daily_log_returns(series)
    return statistics.pstdev(rets) if len(rets) >= 2 else 0.0


def quantile_groups(keys_sorted: List[str], n: int) -> Dict[str, int]:
    """Assign group 1..n by position (deterministic, reproducible)."""
    L = len(keys_sorted)
    return {k: (min(n, int(i / L * n) + 1) if L else 1) for i, k in enumerate(keys_sorted)}


# ---------- param spec helpers ----------
def slider(key, label, mn, mx, step, default):
    return {"key": key, "label": label, "type": "slider", "min": mn, "max": mx, "step": step, "default": default}


def select(key, label, options, default):
    return {"key": key, "label": label, "type": "select", "options": options, "default": default}


# ---------- envelope builders ----------
def build_calibration(product_type, method, method_label, unit, columns, rows, chart, summary, trade_count):
    return {
        "kind": "calibration", "product_type": product_type,
        "method": method, "method_label": method_label, "unit": unit,
        "trade_count": trade_count, "summary": summary,
        "columns": columns, "rows": rows, "chart": chart,
    }


def build_backtest(product_type, method, has_ground_truth, summary, series, accuracy, columns, rows):
    return {
        "kind": "backtest", "product_type": product_type, "method": method,
        "has_ground_truth": has_ground_truth, "summary": summary,
        "series": series, "accuracy": accuracy, "columns": columns, "rows": rows,
    }


def pr_f1(tp, fp, fn):
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 3), round(r, 3), round(f, 3)
