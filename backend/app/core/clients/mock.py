"""Synthetic data, asset-class aware. Deterministic per (asset_class, date).

Trades carry a hidden `confirmed_off_market` label so the backtester can
compute real precision/recall. In live mode that label comes from joining BRV
trades to EPE exceptions with a confirmed status — never from BRV itself.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Dict, List

from app.core.asset_classes import ASSET_CLASSES

DESKS = ["Flow", "Structured", "EM", "Treasury"]
TRADERS = ["TRD_014", "TRD_022", "TRD_031", "TRD_045", "TRD_058"]


def _rng(seed: str) -> random.Random:
    return random.Random(seed)


def _booked_value(rnd: random.Random, ref: float, vol: float):
    """Return (booked_value, is_off_market). ~5% are genuinely off-market with
    a large move; a small share of normal trades also breach (-> false positives)
    and some off-market moves are subtle (-> false negatives)."""
    roll = rnd.random()
    if roll < 0.05:  # genuinely off-market
        move = rnd.uniform(4, 12) * vol * (1 if rnd.random() < 0.5 else -1)
        # ~20% of these are subtle (will be missed by a tight threshold)
        if rnd.random() < 0.2:
            move *= 0.35
        return ref + move, True
    move = rnd.gauss(0, vol)
    return ref + move, False


def mock_trades(as_of: str, ac_code: str, n: int = 60) -> List[Dict]:
    ac = ASSET_CLASSES[ac_code]
    rnd = _rng(f"{ac_code}-{as_of}")
    rows: List[Dict] = []
    for i in range(n):
        b = rnd.choice(ac["buckets"])
        booked, off = _booked_value(rnd, b["ref"], b["vol"])
        digits = 4 if ac["metric"] != "pct" else 4
        rows.append(
            {
                "trade_id": f"{ac_code[:3]}{as_of.replace('-', '')}{i:04d}",
                "asset_class": ac_code,
                "product_code": b["code"],
                "product_name": b["name"],
                "currency": b["ccy"],
                "desk": rnd.choice(DESKS),
                "trader_id": rnd.choice(TRADERS),
                "notional": rnd.choice([1, 2, 5, 10, 25]) * 1_000_000,
                "booked_value": round(booked, digits),
                "reference_value": round(b["ref"], digits),
                "trade_date": as_of,
                "confirmed_off_market": off,
            }
        )
    return rows


def mock_trade_window(ac_code: str, days: int, as_of: str = "") -> Dict[str, List[Dict]]:
    end = date.fromisoformat(as_of) if as_of else date.today()
    out: Dict[str, List[Dict]] = {}
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        out[d] = mock_trades(d, ac_code)
    return out


def mock_exceptions(as_of: str, n: int = 64) -> List[Dict]:
    """Exception population from EPE (asset-class mixed)."""
    rnd = _rng(f"exc-{as_of}")
    statuses = ["OPEN", "UNDER_REVIEW", "APPROVED", "ESCALATED", "CLOSED"]
    reasons = [
        "Price deviation > threshold",
        "Stale reference price",
        "Manual override flag",
        "Off-market rate suspected",
        "Counterparty mismatch",
    ]
    ac_codes = list(ASSET_CLASSES.keys())
    rows: List[Dict] = []
    for i in range(n):
        ac_code = rnd.choice(ac_codes)
        b = rnd.choice(ASSET_CLASSES[ac_code]["buckets"])
        rows.append(
            {
                "exception_id": f"EPE{as_of.replace('-', '')}{i:04d}",
                "trade_id": f"{ac_code[:3]}{as_of.replace('-', '')}{rnd.randint(0, 59):04d}",
                "asset_class": ac_code,
                "product_code": b["code"],
                "product_name": b["name"],
                "currency": b["ccy"],
                "desk": rnd.choice(DESKS),
                "deviation": round(abs(rnd.gauss(0, 1.4)) + 0.3, 3),
                "status": rnd.choice(statuses),
                "reason": rnd.choice(reasons),
                "raised_date": as_of,
                "ageing_days": rnd.randint(0, 21),
            }
        )
    return rows


# ----------------------------------------------- Data Fetch module records
# Raw, filterable rows carrying legal_entity / source_system dimensions, used
# by the Data Fetch module (distinct from the calibration buckets above).
from datetime import date as _date, timedelta as _td  # noqa: E402

_CCY_BY_LE = {
    "HBEU": "EUR", "HBAP": "USD", "HBUS": "USD", "HBFR": "EUR", "HBUK": "GBP",
    "HBIE": "EUR", "HBSG": "SGD", "HBME": "USD", "HBAU": "AUD", "HBJP": "JPY",
}


def _date_range(start: str, end: str):
    s = _date.fromisoformat(start)
    e = _date.fromisoformat(end)
    days = (e - s).days
    return [(s + _td(days=i)).isoformat() for i in range(max(0, days) + 1)]


def _apply_common(rnd, les, sss):
    le = rnd.choice(les) if les else "HBEU"
    ss = rnd.choice(sss) if sss else "FLEXRATE"
    return le, ss, _CCY_BY_LE.get(le, "USD")


def _add_bond_dims(rnd, row, ref, booked, notional):
    """GDM Cash Bonds dimensions + per-source reference prices (BVAL/RDAM/Reuters/EOD).
    Sources cluster around a 'true' price; booked sits near them with an off-market tail."""
    sub_type = rnd.choice(["ABS", "CBO", "GBO"])
    region = rnd.choice(["EMEA", "AMER", "APAC"])
    credit_rating = rnd.choices(["Investment", "Non-Investment", "Default"], weights=[6, 3, 1])[0]
    issuer_rating = rnd.choice(["AAA", "AA", "A", "BBB", "BB", "B"])
    maturity_years = round(rnd.uniform(0.5, 20), 1)
    if maturity_years <= 2:
        tenor_bucket = "<=2y"
    elif maturity_years <= 10:
        tenor_bucket = "2-10y"
    else:
        tenor_bucket = ">10y"
    n_m = notional / 1_000_000
    notional_range = "<5M" if n_m < 5 else ("5-10M" if n_m <= 10 else ">10M")
    # per-source prices: small source-specific noise around the reference
    row["bval_price"] = round(ref * (1 + rnd.gauss(0, 0.0008)), 4)
    row["rdam_price"] = round(ref * (1 + rnd.gauss(0, 0.0012)), 4)
    row["reuters_price"] = round(ref * (1 + rnd.gauss(0, 0.0010)), 4)
    row["eod_price"] = round(ref * (1 + rnd.gauss(0, 0.0015)), 4)
    row.update({
        "product_sub_type": sub_type, "rating_region": region,
        "credit_rating": credit_rating, "issuer_rating": issuer_rating,
        "maturity_years": maturity_years, "tenor_bucket": tenor_bucket,
        "notional_range": notional_range,
    })


def mock_trade_records(product_type, legal_entities, source_systems,
                       start_date, end_date, per_day=6):
    rnd = _rng(f"trd-{product_type}-{start_date}-{end_date}")
    dates = _date_range(start_date, end_date)

    # GFX: build realistic per-currency rate paths (random walk at FX-like daily
    # vol) so volatility calibration produces sensible numbers.
    fx_paths = None
    if product_type == "GFXCASH":
        fx = {  # ccy: (base_rate, daily_vol_fraction)
            "EUR": (1.08, 0.005), "GBP": (1.27, 0.006), "JPY": (150.0, 0.006),
            "AUD": (0.66, 0.007), "CAD": (1.36, 0.005), "CHF": (0.88, 0.005),
            "INR": (83.3, 0.003), "BRL": (5.42, 0.012), "MXN": (17.1, 0.010),
            "ZAR": (18.4, 0.013),
        }
        fx_paths = {}
        for c, (base, dv) in fx.items():
            prng = _rng(f"fx-{c}-{start_date}")
            level = base
            path = {}
            for d in dates:
                level *= (1 + prng.gauss(0, dv))
                path[d] = level
            fx_paths[c] = path

    rows = []
    for d in dates:
        for k in range(per_day):
            le, ss, ccy = _apply_common(rnd, legal_entities, source_systems)
            if fx_paths is not None:
                ccy = rnd.choice(list(fx_paths))
                ref = round(fx_paths[ccy][d], 6)
                move = rnd.uniform(0.004, 0.02) * (1 if rnd.random() < 0.5 else -1) \
                    if rnd.random() < 0.05 else rnd.gauss(0, 0.0004)
                booked = round(ref * (1 + move), 6)
            else:
                ref = round(rnd.uniform(88, 112), 4)
                booked = round(ref * (1 + rnd.gauss(0, 0.4) / 100), 4)
            notional = rnd.choice([1, 2, 5, 10, 25]) * 1_000_000
            # Make the first trade each day a genuine off-market trade (large
            # deviation); EPE routes its confirmed/L2 exceptions to these.
            if k == 0:
                sign = 1 if rnd.random() < 0.5 else -1
                booked = round(ref * (1 + sign * rnd.uniform(0.012, 0.03)), 6 if fx_paths else 4)
            pnl = round((booked - ref) / ref * notional * 0.5 + rnd.gauss(0, notional * 1e-4), 2)
            row = {
                "trade_id": f"BRV{d.replace('-', '')}{k:04d}",
                "trade_date": d,
                "product_type": product_type,
                "legal_entity": le,
                "source_system": ss,
                "currency": ccy,
                "counterparty": f"CPTY_{rnd.randint(100, 999)}",
                "notional": notional,
                "booked_price": booked,
                "reference_price": ref,
                "pnl": pnl,
                "trader_id": rnd.choice(TRADERS),
            }
            if product_type == "CASHBONDS":
                _add_bond_dims(rnd, row, ref, booked, notional)
            rows.append(row)
    return rows


def mock_exception_records(product_type, legal_entities, source_systems,
                           start_date, end_date, per_day=3, brv_per_day=6):
    rnd = _rng(f"exc-{product_type}-{start_date}-{end_date}")
    reasons = [
        "Price deviation > threshold", "Stale reference price",
        "Manual override flag", "Off-market rate suspected", "Counterparty mismatch",
    ]
    # Resolution mix: most alerts close at L1 (false positives); a minority are
    # confirmed genuine off-market at L2. A few remain open.
    resolutions = [
        ("L1_CLOSED", "L1", "false_positive", 0.60),
        ("L2_CONFIRMED", "L2", "genuine", 0.18),
        ("ESCALATED", "L2", "genuine", 0.07),
        ("OPEN", "", "unresolved", 0.08),
        ("UNDER_REVIEW", "", "unresolved", 0.07),
    ]
    statuses = [r[0] for r in resolutions]
    weights = [r[3] for r in resolutions]
    res_by_status = {r[0]: (r[1], r[2]) for r in resolutions}

    dates = _date_range(start_date, end_date)
    rows = []
    i = 0
    for d in dates:
        for _ in range(per_day):
            le, ss, ccy = _apply_common(rnd, legal_entities, source_systems)
            status = rnd.choices(statuses, weights=weights)[0]
            level, outcome = res_by_status[status]
            # Route resolution to the matching trade: genuine -> the off-market
            # trade (k=0); L1 false positives -> a normal trade (k>=1).
            if outcome == "genuine":
                k = 0
            elif outcome == "false_positive":
                k = rnd.randint(1, max(1, brv_per_day - 1))
            else:
                k = rnd.randint(0, max(1, brv_per_day - 1))
            rows.append({
                "exception_id": f"EPE{d.replace('-', '')}{i:05d}",
                # references a real BRV trade for this date (EPE is a subset of BRV)
                "trade_id": f"BRV{d.replace('-', '')}{k:04d}",
                "raised_date": d,
                "product_type": product_type,
                "legal_entity": le,
                "source_system": ss,
                "currency": ccy,
                "deviation": round(abs(rnd.gauss(0, 1.4)) + 0.3, 3),
                "status": status,
                "resolution_level": level,     # L1 / L2 / ""
                "outcome": outcome,            # false_positive / genuine / unresolved
                "reason": rnd.choice(reasons),
                "ageing_days": rnd.randint(0, 21),
            })
            i += 1
    return rows
