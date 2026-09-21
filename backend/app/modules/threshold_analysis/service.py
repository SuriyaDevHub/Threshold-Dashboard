"""Threshold Analysis engine — faithful to the OMRC methodology.

Per-check calibration (Price deviation, PnL, PnL/Notional), each from its own
distribution, by PERCENTILE (the documented method) or Median+k*MAD (robust
alternative). Checks are combined into an alert via OR / AND / COMBINATION logic
(COMBINATION = Price OR (PnL AND PnL/Notional), per the methodology doc).

Pure stdlib -> runs on StatPy / Py3.9.
"""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Set

from app.core.metrics import UNIT, deviation, metric_for

# Default check set and the order COMBINATION treats as primary-first.
DEFAULT_CHECKS = ["price", "pnl", "pnl_notional"]
CHECK_LABEL = {"price": "Price deviation", "pnl": "PnL", "pnl_notional": "PnL / Notional"}


# ---------------------------------------------------------------- check values
def check_value(check: str, product_type: str, row: Dict) -> float:
    if check == "price":
        return deviation(metric_for(product_type), row.get("booked"), row.get("reference"), row.get("currency", ""))
    if check == "pnl":
        try:
            return abs(float(row.get("pnl") or 0.0))
        except (TypeError, ValueError):
            return 0.0
    if check == "pnl_notional":
        try:
            pnl = abs(float(row.get("pnl") or 0.0))
            notl = abs(float(row.get("notional") or 0.0))
            return pnl / notl * 1e4 if notl else 0.0  # bps of notional
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def check_unit(check: str, product_type: str) -> str:
    if check == "price":
        return UNIT[metric_for(product_type)]
    if check == "pnl":
        return "ccy"
    if check == "pnl_notional":
        return "bps"
    return ""


# ------------------------------------------------------------- estimators
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


def calibrate_one(values: List[float], method: str, params: Dict) -> float:
    if method == "percentile":
        return round(percentile(values, params.get("percentile", 95)), 4)
    return round(mad_threshold(values, params.get("k", 4.0), params.get("floor", 0.0)), 4)


# ------------------------------------------------------------- alert logic
def is_alert(hits: Dict[str, bool], logic: str, checks: List[str]) -> bool:
    if logic == "OR":
        return any(hits.get(c, False) for c in checks)
    if logic == "AND":
        return all(hits.get(c, False) for c in checks)
    if logic == "COMBO":
        primary, rest = checks[0], checks[1:]
        return hits.get(primary, False) or (bool(rest) and all(hits.get(c, False) for c in rest))
    return False


# ------------------------------------------------------------- calibration
def calibrate_checks(rows, product_type, checks, method, params_by_check, logic):
    """Per bucket: a threshold + stats per check, plus combined alert rate."""
    buckets: Dict[str, Dict] = {}
    for t in rows:
        key = t.get("bucket") or product_type
        b = buckets.setdefault(key, {"bucket": key, "currency": t.get("currency", ""),
                                     "n": 0, "_vals": {c: [] for c in checks}, "_rows": []})
        b["n"] += 1
        b["_rows"].append(t)
        for c in checks:
            b["_vals"][c].append(check_value(c, product_type, t))

    out: List[Dict] = []
    for b in buckets.values():
        thr = {}
        checks_out = {}
        for c in checks:
            vals = b["_vals"][c]
            t = calibrate_one(vals, method, params_by_check.get(c, {}))
            thr[c] = t
            br = sum(1 for v in vals if v > t)
            checks_out[c] = {
                "threshold": t, "unit": check_unit(c, product_type), "label": CHECK_LABEL[c],
                "median": round(statistics.median(vals), 4) if vals else 0.0,
                "p95": round(percentile(vals, 95), 4) if vals else 0.0,
                "breaches": br, "breach_rate_pct": round(br / len(vals) * 100, 2) if vals else 0.0,
            }
        # combined alert rate under the logic
        alerts = 0
        for t in b["_rows"]:
            hits = {c: check_value(c, product_type, t) > thr[c] for c in checks}
            if is_alert(hits, logic, checks):
                alerts += 1
        out.append({
            "bucket": b["bucket"], "currency": b["currency"], "n": b["n"],
            "checks": checks_out,
            "alerts": alerts, "alert_rate_pct": round(alerts / b["n"] * 100, 2) if b["n"] else 0.0,
        })
    return sorted(out, key=lambda r: str(r["bucket"]))


def thresholds_table(buckets) -> Dict[str, Dict[str, float]]:
    return {b["bucket"]: {c: b["checks"][c]["threshold"] for c in b["checks"]} for b in buckets}


# ------------------------------------------------------------- backtest
def backtest_checks(rows, product_type, checks, method, params_by_check, logic,
                    confirmed_ids: Optional[Set[str]] = None, baseline_params=None):
    """Replay combined alerts under `logic` over the dataset's date span, candidate
    vs a looser baseline, and score precision/recall vs confirmed off-market."""
    confirmed_ids = confirmed_ids or set()
    cand = thresholds_table(calibrate_checks(rows, product_type, checks, method, params_by_check, logic))
    base = thresholds_table(calibrate_checks(rows, product_type, checks, method, baseline_params or params_by_check, logic))

    by_date: Dict[str, Dict] = {}
    tp = fp = fn = tn = 0
    cand_total = base_total = 0
    per_bucket: Dict[str, Dict] = {}

    for t in rows:
        bucket = t.get("bucket") or product_type
        vals = {c: check_value(c, product_type, t) for c in checks}
        cand_hit = is_alert({c: vals[c] > cand.get(bucket, {}).get(c, float("inf")) for c in checks}, logic, checks)
        base_hit = is_alert({c: vals[c] > base.get(bucket, {}).get(c, float("inf")) for c in checks}, logic, checks)
        actual = t.get("trade_id") in confirmed_ids

        d = t.get("trade_date") or "n/a"
        row = by_date.setdefault(d, {"date": d, "trades": 0, "candidate_flags": 0, "baseline_flags": 0})
        row["trades"] += 1
        if cand_hit:
            row["candidate_flags"] += 1; cand_total += 1
        if base_hit:
            row["baseline_flags"] += 1; base_total += 1

        pb = per_bucket.setdefault(bucket, {"bucket": bucket, "tp": 0, "fp": 0, "fn": 0, "tn": 0, "n": 0})
        pb["n"] += 1
        if cand_hit and actual:
            tp += 1; pb["tp"] += 1
        elif cand_hit and not actual:
            fp += 1; pb["fp"] += 1
        elif not cand_hit and actual:
            fn += 1; pb["fn"] += 1
        else:
            tn += 1; pb["tn"] += 1

    def _pr(tp_, fp_, fn_):
        p = tp_ / (tp_ + fp_) if (tp_ + fp_) else 0.0
        r = tp_ / (tp_ + fn_) if (tp_ + fn_) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return round(p, 3), round(r, 3), round(f, 3)

    p, r, f1 = _pr(tp, fp, fn)
    for pb in per_bucket.values():
        pb["precision"], pb["recall"], pb["f1"] = _pr(pb["tp"], pb["fp"], pb["fn"])

    return {
        "series": [by_date[d] for d in sorted(by_date)],
        "accuracy": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                     "precision": p, "recall": r, "f1": f1, "confirmed_total": tp + fn},
        "delta": {"candidate_total": cand_total, "baseline_total": base_total,
                  "delta": cand_total - base_total},
        "per_bucket": sorted(per_bucket.values(), key=lambda x: str(x["bucket"])),
        "candidate_thresholds": cand,
    }


def candidate_flags_checks(rows, product_type, checks, method, params_by_check, logic):
    """{trade_id: bool} for the per-check method — does the candidate alert?"""
    buckets = calibrate_checks(rows, product_type, checks, method, params_by_check, logic)
    cand = {b["bucket"]: {c: b["checks"][c]["threshold"] for c in checks} for b in buckets}
    out = {}
    for t in rows:
        bucket = t.get("bucket") or product_type
        hits = {c: check_value(c, product_type, t) > cand.get(bucket, {}).get(c, float("inf")) for c in checks}
        out[t.get("trade_id")] = is_alert(hits, logic, checks)
    return out
