"""Cash Bonds calibrator — per-check percentile / MAD with OR/AND/COMBO logic.
Also the DEFAULT calibrator for any product without its own module.
"""
from __future__ import annotations

from app.core.metrics import UNIT, metric_for
from app.modules.threshold_analysis.calibrators import base
from app.modules.threshold_analysis.service import (
    candidate_flags_checks,
    CHECK_LABEL,
    DEFAULT_CHECKS,
    backtest_checks,
    calibrate_checks,
)

LOGIC_OPTS = [
    {"value": "OR", "label": "OR — any check"},
    {"value": "AND", "label": "AND — all checks"},
    {"value": "COMBO", "label": "Combination — Price OR (PnL AND PnL/Notional)"},
]
METHOD_OPTS = [
    {"value": "percentile", "label": "Percentile of distribution"},
    {"value": "mad", "label": "Median + k·MAD"},
]


class DefaultCalibrator:
    product_types = []          # default fallback for unregistered products
    is_default = True
    method = "per-check-distribution"
    method_label = "Per-check percentile / MAD"
    unit = ""

    def config(self):
        return {"params": [
            base.select("method", "Method", METHOD_OPTS, "percentile"),
            base.slider("percentile", "Percentile", 80, 99.9, 0.1, 95),
            base.slider("k", "MAD multiplier k", 1, 8, 0.5, 4),
            base.select("logic", "Combination logic", LOGIC_OPTS, "COMBO"),
        ]}

    def _params(self, product_type, params, looser=False):
        method = params.get("method", "percentile")
        checks = list(DEFAULT_CHECKS)
        if method == "percentile":
            p = float(params.get("percentile", 95))
            if looser:
                p = min(99.9, p + 4)
            pbc = {c: {"percentile": p} for c in checks}
        else:
            k = float(params.get("k", 4))
            if looser:
                k = k + 2
            pbc = {c: {"k": k, "floor": 0.0} for c in checks}
        return method, checks, pbc

    def candidate_flags(self, product_type, rows, params):
        method, checks, pbc = self._params(product_type, params)
        logic = params.get("logic", "COMBO")
        return candidate_flags_checks(rows, product_type, checks, method, pbc, logic)

    def calibrate(self, product_type, rows, params):
        method, checks, pbc = self._params(product_type, params)
        logic = params.get("logic", "COMBO")
        buckets = calibrate_checks(rows, product_type, checks, method, pbc, logic)

        columns = [
            {"key": "bucket", "label": "Bucket", "mono": True},
            {"key": "n", "label": "n", "align": "right", "mono": True},
        ]
        for c in checks:
            unit = buckets[0]["checks"][c]["unit"] if buckets else ""
            columns.append({"key": c, "label": CHECK_LABEL[c], "align": "right", "mono": True, "unit": unit})
        columns.append({"key": "alert_rate_pct", "label": "Alert rate", "align": "right",
                        "mono": True, "unit": "%", "status": "alert"})

        rows_out = []
        for b in buckets:
            row = {"bucket": b["bucket"], "n": b["n"], "alert_rate_pct": b["alert_rate_pct"]}
            for c in checks:
                row[c] = b["checks"][c]["threshold"]
            rows_out.append(row)

        avg = round(sum(b["alert_rate_pct"] for b in buckets) / len(buckets), 2) if buckets else 0.0
        summary = [
            {"label": "Trades", "value": len(rows)},
            {"label": "Buckets", "value": len(buckets)},
            {"label": "Logic", "value": logic},
            {"label": "Avg alert rate", "value": f"{avg}%"},
        ]
        chart = {"type": "bar", "x": "bucket", "y": "alert_rate_pct", "unit": "%"}
        return base.build_calibration(product_type, method, self.method_label,
                                      UNIT[metric_for(product_type)], columns, rows_out, chart, summary, len(rows))

    def backtest(self, product_type, rows, confirmed_ids, params):
        method, checks, pbc = self._params(product_type, params)
        _, _, baseline = self._params(product_type, params, looser=True)
        logic = params.get("logic", "COMBO")
        res = backtest_checks(rows, product_type, checks, method, pbc, logic,
                              confirmed_ids=confirmed_ids, baseline_params=baseline)
        a, d = res["accuracy"], res["delta"]
        summary = [
            {"label": "Candidate alerts", "value": d["candidate_total"]},
            {"label": "Baseline alerts", "value": d["baseline_total"]},
            {"label": "Delta", "value": d["delta"]},
            {"label": "Confirmed off-market", "value": a["confirmed_total"]},
        ]
        columns = [
            {"key": "bucket", "label": "Bucket", "mono": True},
            {"key": "n", "label": "n", "align": "right", "mono": True},
            {"key": "tp", "label": "TP", "align": "right", "mono": True},
            {"key": "fp", "label": "FP", "align": "right", "mono": True},
            {"key": "fn", "label": "FN", "align": "right", "mono": True},
            {"key": "precision", "label": "Precision", "align": "right", "mono": True},
            {"key": "recall", "label": "Recall", "align": "right", "mono": True},
            {"key": "f1", "label": "F1", "align": "right", "mono": True},
        ]
        return base.build_backtest(product_type, method, bool(confirmed_ids),
                                   summary, res["series"], a, columns, res["per_bucket"])


CALIBRATOR = DefaultCalibrator()
