"""GDM Cash Bonds calibrator — composite-key, per-source thresholds.

Faithful to the manual process:
  * threshold key = Product Sub-Type + Rating Region + Tenor + Credit Rating +
    Issuer Rating + Notional Range (granularity selectable to manage sparsity),
  * one threshold PER price source (BVAL / RDAM / Reuters / EOD): for each
    (key, source) the booked-vs-source deviation distribution -> percentile (or MAD),
  * a trade alerts if ANY source's deviation breaches its threshold (OR),
  * sparse cells (n < min_samples) fall back to the product-level threshold.

Reference prices are read from the data itself for now (the "use market data in
EPE data" step); swap in the separate market-data dump via SOURCE_COLS later.
"""
from __future__ import annotations

from typing import Dict, List

from app.modules.threshold_analysis.calibrators import base

# Source name -> column carrying that source's reference price.
SOURCE_COLS = {
    "BVAL": "bval_price",
    "RDAM": "rdam_price",
    "REUTERS": "reuters_price",
    "EOD": "eod_price",
}
# Composite-key dimensions -> raw column names.
KEY_COLS = {
    "product_sub_type": "product_sub_type",
    "rating_region": "rating_region",
    "tenor_bucket": "tenor_bucket",
    "credit_rating": "credit_rating",
    "issuer_rating": "issuer_rating",
    "notional_range": "notional_range",
}
GRANULARITY = {
    "coarse": ["product_sub_type", "credit_rating"],
    "medium": ["product_sub_type", "credit_rating", "tenor_bucket"],
    "full": ["product_sub_type", "rating_region", "tenor_bucket",
             "credit_rating", "issuer_rating", "notional_range"],
}
ALL = "__ALL__"


def _raw(row):
    return row.get("_raw", row)


def _dev_pct(booked, source_price):
    try:
        b, s = float(booked), float(source_price)
        return abs(b - s) / abs(s) * 100.0 if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def _key(raw, dims):
    return " · ".join(str(raw.get(KEY_COLS[d], "?")) for d in dims)


class CashBondsCalibrator:
    product_types = ["CASHBONDS"]
    method = "composite-key-per-source"
    method_label = "GDM Cash Bonds — composite key, per-source"
    unit = "%"

    def config(self):
        return {"params": [
            base.select("method", "Method",
                        [{"value": "percentile", "label": "Percentile of distribution"},
                         {"value": "mad", "label": "Median + k·MAD"}], "percentile"),
            base.slider("percentile", "Percentile", 80, 99.9, 0.1, 99),
            base.slider("k", "MAD multiplier k", 1, 8, 0.5, 4),
            base.select("granularity", "Key granularity",
                        [{"value": "coarse", "label": "Coarse (sub-type + rating)"},
                         {"value": "medium", "label": "Medium (+ tenor)"},
                         {"value": "full", "label": "Full (all dimensions)"}], "coarse"),
            base.slider("min_samples", "Min samples / cell", 5, 100, 5, 20),
        ]}

    def _sources(self, rows):
        present = [s for s, col in SOURCE_COLS.items()
                   if any(_raw(r).get(col) is not None for r in rows[:50])]
        return present or ["BVAL"]

    def _thr(self, values, params):
        if params.get("method", "percentile") == "percentile":
            return round(base.percentile(values, float(params.get("percentile", 99))), 4)
        return round(base.mad_threshold(values, float(params.get("k", 4)), 0.0), 4)

    def _calibrate_keyed(self, rows, dims, sources, params):
        """Return thresholds[key][source] and the per-(key) sample count."""
        devs: Dict[str, Dict[str, List[float]]] = {}
        counts: Dict[str, int] = {}
        for r in rows:
            raw = _raw(r)
            k = _key(raw, dims)
            counts[k] = counts.get(k, 0) + 1
            counts[ALL] = counts.get(ALL, 0) + 1
            for s in sources:
                d = _dev_pct(r.get("booked"), raw.get(SOURCE_COLS[s]))
                devs.setdefault(k, {}).setdefault(s, []).append(d)
                devs.setdefault(ALL, {}).setdefault(s, []).append(d)
        thr = {k: {s: self._thr(sv, params) for s, sv in smap.items()} for k, smap in devs.items()}
        return thr, counts

    def _effective(self, thr, counts, k, min_samples):
        return thr[k] if counts.get(k, 0) >= min_samples and k in thr else thr.get(ALL, {})

    def _alerts(self, rows, dims, sources, thr, counts, min_samples):
        """Return per-key {n, alerts} using OR across sources, with fallback."""
        agg: Dict[str, Dict] = {}
        for r in rows:
            raw = _raw(r)
            k = _key(raw, dims)
            eff = self._effective(thr, counts, k, min_samples)
            hit = any(_dev_pct(r.get("booked"), raw.get(SOURCE_COLS[s])) > eff.get(s, float("inf"))
                      for s in sources)
            a = agg.setdefault(k, {"n": 0, "alerts": 0})
            a["n"] += 1
            if hit:
                a["alerts"] += 1
        return agg

    def candidate_flags(self, product_type, rows, params):
        dims = GRANULARITY[params.get("granularity", "coarse")]
        min_samples = int(params.get("min_samples", 20))
        sources = self._sources(rows)
        thr, counts = self._calibrate_keyed(rows, dims, sources, params)
        out = {}
        for r in rows:
            raw = _raw(r)
            eff = self._effective(thr, counts, _key(raw, dims), min_samples)
            out[r.get("trade_id")] = any(
                _dev_pct(r.get("booked"), raw.get(SOURCE_COLS[s])) > eff.get(s, float("inf"))
                for s in sources)
        return out

    def calibrate(self, product_type, rows, params):
        dims = GRANULARITY[params.get("granularity", "coarse")]
        min_samples = int(params.get("min_samples", 20))
        sources = self._sources(rows)
        thr, counts = self._calibrate_keyed(rows, dims, sources, params)
        agg = self._alerts(rows, dims, sources, thr, counts, min_samples)

        rows_out = []
        for k, a in agg.items():
            eff = self._effective(thr, counts, k, min_samples)
            row = {"key": k, "n": a["n"],
                   "alert_rate_pct": round(a["alerts"] / a["n"] * 100, 2) if a["n"] else 0.0,
                   "fallback": "yes" if counts.get(k, 0) < min_samples else ""}
            for s in sources:
                row[s] = eff.get(s)
            rows_out.append(row)
        rows_out.sort(key=lambda x: (-x["n"], x["key"]))

        columns = [
            {"key": "key", "label": "Composite key", "mono": True},
            {"key": "n", "label": "n", "align": "right", "mono": True},
        ]
        for s in sources:
            columns.append({"key": s, "label": f"{s} thr", "align": "right", "mono": True, "unit": "%"})
        columns += [
            {"key": "fallback", "label": "Fallback", "mono": True},
            {"key": "alert_rate_pct", "label": "Alert rate", "align": "right", "mono": True, "unit": "%", "status": "alert"},
        ]
        avg = round(sum(r["alert_rate_pct"] for r in rows_out) / len(rows_out), 2) if rows_out else 0.0
        summary = [
            {"label": "Trades", "value": len(rows)},
            {"label": "Key cells", "value": len([r for r in rows_out if not r["fallback"]])},
            {"label": "Sources", "value": ", ".join(sources)},
            {"label": "Avg alert rate", "value": f"{avg}%"},
        ]
        chart = {"type": "bar", "x": "key", "y": "alert_rate_pct", "unit": "%"}
        return base.build_calibration("CASHBONDS", self.method, self.method_label, "%",
                                      columns, rows_out, chart, summary, len(rows))

    def backtest(self, product_type, rows, confirmed_ids, params):
        dims = GRANULARITY[params.get("granularity", "coarse")]
        min_samples = int(params.get("min_samples", 20))
        sources = self._sources(rows)
        thr, counts = self._calibrate_keyed(rows, dims, sources, params)
        # looser baseline
        bp = dict(params)
        if params.get("method", "percentile") == "percentile":
            bp["percentile"] = min(99.9, float(params.get("percentile", 99)) + 0.8)
        else:
            bp["k"] = float(params.get("k", 4)) + 2
        bthr, _ = self._calibrate_keyed(rows, dims, sources, bp)

        by_date: Dict[str, Dict] = {}
        tp = fp = fn = tn = 0
        cand_total = base_total = 0
        per_key: Dict[str, Dict] = {}
        for r in rows:
            raw = _raw(r)
            k = _key(raw, dims)
            eff = self._effective(thr, counts, k, min_samples)
            beff = self._effective(bthr, counts, k, min_samples)
            cand = any(_dev_pct(r.get("booked"), raw.get(SOURCE_COLS[s])) > eff.get(s, float("inf")) for s in sources)
            basef = any(_dev_pct(r.get("booked"), raw.get(SOURCE_COLS[s])) > beff.get(s, float("inf")) for s in sources)
            actual = r.get("trade_id") in confirmed_ids

            d = r.get("trade_date") or "n/a"
            row = by_date.setdefault(d, {"date": d, "trades": 0, "candidate_flags": 0, "baseline_flags": 0})
            row["trades"] += 1
            if cand:
                row["candidate_flags"] += 1; cand_total += 1
            if basef:
                row["baseline_flags"] += 1; base_total += 1

            pk = per_key.setdefault(k, {"key": k, "tp": 0, "fp": 0, "fn": 0, "tn": 0, "n": 0})
            pk["n"] += 1
            if cand and actual:
                tp += 1; pk["tp"] += 1
            elif cand and not actual:
                fp += 1; pk["fp"] += 1
            elif not cand and actual:
                fn += 1; pk["fn"] += 1
            else:
                tn += 1; pk["tn"] += 1

        p, r_, f1 = base.pr_f1(tp, fp, fn)
        for pk in per_key.values():
            pk["precision"], pk["recall"], pk["f1"] = base.pr_f1(pk["tp"], pk["fp"], pk["fn"])
        summary = [
            {"label": "Candidate alerts", "value": cand_total},
            {"label": "Baseline alerts", "value": base_total},
            {"label": "Delta", "value": cand_total - base_total},
            {"label": "Confirmed off-market", "value": tp + fn},
        ]
        columns = [
            {"key": "key", "label": "Composite key", "mono": True},
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
        rows_out = sorted(per_key.values(), key=lambda x: -x["n"])
        return base.build_backtest("CASHBONDS", self.method, bool(confirmed_ids),
                                   summary, series, accuracy, columns, rows_out)


CALIBRATOR = CashBondsCalibrator()
