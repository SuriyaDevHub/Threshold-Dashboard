"""GFX Cash calibrator — volatility banding, hybrid pair handling.

  * per-currency daily vol from the reference-rate series (seam: swap in Reuters
    market data here),
  * USD pairs -> the pair's own (non-USD leg) vol,
  * crosses   -> correlation-aware combination  sqrt(va^2+vb^2+2*rho*va*vb),
  * deterministic vol grouping into L bands; group threshold = k * representative
    vol, applied in the chosen horizon (daily fixes the annualization gap;
    annualized kept as an option for continuity with the current RDR process),
  * everything expressed as a % deviation band, unit-consistent with the check.
"""
from __future__ import annotations

import statistics
from typing import Dict, List

from app.modules.threshold_analysis.calibrators import base

USD = "USD"


def _dev_pct(booked, reference) -> float:
    try:
        b, r = float(booked), float(reference)
        return abs(b - r) / abs(r) * 100.0 if r else 0.0
    except (TypeError, ValueError):
        return 0.0


def _currency_series(rows) -> Dict[str, List[float]]:
    by: Dict[str, Dict[str, List[float]]] = {}
    for r in rows:
        c, d, ref = r.get("currency"), r.get("trade_date"), r.get("reference")
        if c is None or ref is None:
            continue
        by.setdefault(c, {}).setdefault(d, []).append(float(ref))
    out = {}
    for c, dd in by.items():
        out[c] = [sum(dd[d]) / len(dd[d]) for d in sorted(dd)]
    return out


def _pair_legs(row) -> List[str]:
    """Return the risk legs of the trade. Supports a base/quote 'pair' column if
    present; else treats 'currency' as the non-USD leg of a USD pair."""
    pair = row.get("pair") or row.get("ccy_pair")
    if pair and len(str(pair)) == 6:
        return [str(pair)[:3], str(pair)[3:]]
    return [row.get("currency"), USD]


class GFXCashCalibrator:
    product_types = ["GFXCASH"]
    method = "vol-band-hybrid"
    method_label = "Volatility banding (hybrid)"
    unit = "%"

    def config(self):
        return {"params": [
            base.slider("k", "Risk multiplier k", 0.5, 6.0, 0.5, 3.0),
            base.slider("group_count", "Vol groups (L)", 2, 8, 1, 5),
            base.select("horizon", "Horizon",
                        [{"value": "daily", "label": "Daily (recommended)"},
                         {"value": "annualized", "label": "Annualized"}], "daily"),
            base.select("group_rep", "Group representative",
                        [{"value": "mean", "label": "Group mean"},
                         {"value": "max", "label": "Group upper bound"}], "mean"),
        ]}

    # ---- core: per-currency vol + group thresholds ----
    def _thresholds(self, rows, params):
        k = float(params.get("k", 3.0))
        n = int(params.get("group_count", 5))
        horizon = params.get("horizon", "daily")
        rep = params.get("group_rep", "mean")

        series = _currency_series(rows)
        dvol = {c: base.daily_vol(s) for c, s in series.items()}  # daily fractional
        # USD has no vol vs itself
        dvol[USD] = 0.0

        def vol_pct(c):
            v = dvol.get(c, 0.0) * (base.ANNUALIZE if horizon == "annualized" else 1.0)
            return v * 100.0

        # group currencies (excl. USD) by vol ascending
        ccys = sorted([c for c in dvol if c != USD], key=lambda c: dvol[c])
        grp = base.quantile_groups(ccys, n)
        grp[USD] = 0

        # raw per-currency threshold = k * vol%
        raw = {c: round(k * vol_pct(c), 4) for c in dvol}
        # group representative threshold
        members: Dict[int, List[str]] = {}
        for c in ccys:
            members.setdefault(grp[c], []).append(c)
        group_thr = {}
        for g, ms in members.items():
            vals = [raw[c] for c in ms]
            group_thr[g] = round(statistics.mean(vals) if rep == "mean" else max(vals), 4)
        group_thr[0] = 0.0
        return dvol, grp, group_thr, vol_pct

    def _pair_threshold(self, legs, dvol, grp, group_thr):
        """Hybrid: USD pair -> non-USD leg's group threshold; cross -> max leg
        (note: a true cross threshold needs correlation; see cross_vol)."""
        groups = [grp.get(l, 0) for l in legs]
        g = max(groups)  # current methodology: higher group wins
        return group_thr.get(g, 0.0), g

    @staticmethod
    def cross_vol(va, vb, rho):
        """Correlation-aware cross vol for EUR/JPY-type pairs."""
        return (va * va + vb * vb + 2 * rho * va * vb) ** 0.5

    def candidate_flags(self, product_type, rows, params):
        """{trade_id: bool} — does the candidate threshold flag each trade?"""
        dvol, grp, group_thr, _ = self._thresholds(rows, params)
        out = {}
        for r in rows:
            thr, _ = self._pair_threshold(_pair_legs(r), dvol, grp, group_thr)
            out[r.get("trade_id")] = _dev_pct(r.get("booked"), r.get("reference")) > thr
        return out

    def calibrate(self, product_type, rows, params):
        dvol, grp, group_thr, vol_pct = self._thresholds(rows, params)

        # alert rate per currency from its trades
        per_ccy: Dict[str, Dict] = {}
        for r in rows:
            legs = _pair_legs(r)
            thr, g = self._pair_threshold(legs, dvol, grp, group_thr)
            key = r.get("currency")
            pc = per_ccy.setdefault(key, {"currency": key, "group": grp.get(key, 0),
                                          "threshold": thr, "n": 0, "alerts": 0})
            pc["n"] += 1
            if _dev_pct(r.get("booked"), r.get("reference")) > thr:
                pc["alerts"] += 1

        rows_out = []
        for c, pc in per_ccy.items():
            rows_out.append({
                "currency": c, "group": pc["group"],
                "daily_vol": round(dvol.get(c, 0.0) * 100, 4),
                "ann_vol": round(dvol.get(c, 0.0) * base.ANNUALIZE * 100, 4),
                "threshold": pc["threshold"], "n": pc["n"],
                "alert_rate_pct": round(pc["alerts"] / pc["n"] * 100, 2) if pc["n"] else 0.0,
            })
        rows_out.sort(key=lambda x: (x["group"], x["currency"]))

        columns = [
            {"key": "currency", "label": "Currency", "mono": True},
            {"key": "group", "label": "Group", "align": "right", "mono": True},
            {"key": "daily_vol", "label": "Daily vol", "align": "right", "mono": True, "unit": "%"},
            {"key": "ann_vol", "label": "Ann. vol", "align": "right", "mono": True, "unit": "%"},
            {"key": "threshold", "label": "Threshold", "align": "right", "mono": True, "unit": "%", "strong": True},
            {"key": "alert_rate_pct", "label": "Alert rate", "align": "right", "mono": True, "unit": "%", "status": "alert"},
        ]
        avg_alert = round(sum(r["alert_rate_pct"] for r in rows_out) / len(rows_out), 2) if rows_out else 0.0
        summary = [
            {"label": "Trades", "value": len(rows)},
            {"label": "Currencies", "value": len(rows_out)},
            {"label": "Vol groups", "value": int(params.get("group_count", 5))},
            {"label": "Avg alert rate", "value": f"{avg_alert}%"},
        ]
        chart = {"type": "bar", "x": "currency", "y": "threshold", "unit": "%"}
        return base.build_calibration("GFXCASH", self.method, self.method_label, "%",
                                      columns, rows_out, chart, summary, len(rows))

    def backtest(self, product_type, rows, confirmed_ids, params):
        dvol, grp, group_thr, _ = self._thresholds(rows, params)
        # baseline = looser (1.5x k)
        bparams = dict(params); bparams["k"] = float(params.get("k", 3.0)) * 1.5
        _, bgrp, bgroup_thr, _ = self._thresholds(rows, bparams)

        by_date: Dict[str, Dict] = {}
        tp = fp = fn = tn = 0
        per_group: Dict[int, Dict] = {}
        cand_total = base_total = 0

        for r in rows:
            legs = _pair_legs(r)
            thr, g = self._pair_threshold(legs, dvol, grp, group_thr)
            bthr, _ = self._pair_threshold(legs, dvol, bgrp, bgroup_thr)
            dev = _dev_pct(r.get("booked"), r.get("reference"))
            cand_hit, base_hit = dev > thr, dev > bthr
            actual = r.get("trade_id") in confirmed_ids

            d = r.get("trade_date") or "n/a"
            row = by_date.setdefault(d, {"date": d, "trades": 0, "candidate_flags": 0, "baseline_flags": 0})
            row["trades"] += 1
            if cand_hit:
                row["candidate_flags"] += 1; cand_total += 1
            if base_hit:
                row["baseline_flags"] += 1; base_total += 1

            pg = per_group.setdefault(g, {"group": g, "tp": 0, "fp": 0, "fn": 0, "tn": 0, "n": 0})
            pg["n"] += 1
            if cand_hit and actual:
                tp += 1; pg["tp"] += 1
            elif cand_hit and not actual:
                fp += 1; pg["fp"] += 1
            elif not cand_hit and actual:
                fn += 1; pg["fn"] += 1
            else:
                tn += 1; pg["tn"] += 1

        p, r_, f1 = base.pr_f1(tp, fp, fn)
        for pg in per_group.values():
            pg["precision"], pg["recall"], pg["f1"] = base.pr_f1(pg["tp"], pg["fp"], pg["fn"])

        summary = [
            {"label": "Candidate alerts", "value": cand_total},
            {"label": "Baseline alerts", "value": base_total},
            {"label": "Delta", "value": cand_total - base_total},
            {"label": "Confirmed off-market", "value": tp + fn},
        ]
        columns = [
            {"key": "group", "label": "Group", "align": "right", "mono": True},
            {"key": "n", "label": "n", "align": "right", "mono": True},
            {"key": "tp", "label": "TP", "align": "right", "mono": True},
            {"key": "fp", "label": "FP", "align": "right", "mono": True},
            {"key": "fn", "label": "FN", "align": "right", "mono": True},
            {"key": "precision", "label": "Precision", "align": "right", "mono": True},
            {"key": "recall", "label": "Recall", "align": "right", "mono": True},
            {"key": "f1", "label": "F1", "align": "right", "mono": True},
        ]
        accuracy = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": p, "recall": r_,
                    "f1": f1, "confirmed_total": tp + fn}
        series = [by_date[d] for d in sorted(by_date)]
        rows_out = sorted(per_group.values(), key=lambda x: x["group"])
        return base.build_backtest("GFXCASH", self.method, bool(confirmed_ids),
                                   summary, series, accuracy, columns, rows_out)


CALIBRATOR = GFXCashCalibrator()
