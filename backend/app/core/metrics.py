"""Deviation metric per product type. Cash/equity-like -> %, rates -> bps, FX -> pips."""
from __future__ import annotations

PRODUCT_METRIC = {
    "CASHBONDS": "pct",
    "CASHEQUITIES": "pct",
    "SBL": "pct",
    "PM": "pct",
    "TRS": "pct",
    "GFXCASH": "pips",
    "FXO": "pips",
    "IRD": "bps",
    "MM": "bps",
    "CDS": "bps",
}

UNIT = {"pct": "%", "bps": "bps", "pips": "pips"}

# Default calibration knobs per metric.
K_DEFAULT = {"pct": 4.0, "bps": 4.0, "pips": 4.0}
FLOOR_DEFAULT = {"pct": 0.10, "bps": 0.5, "pips": 1.0}


def metric_for(product_type: str) -> str:
    # Lazy import: app/core sits below app/modules architecturally, so this
    # stays function-local to avoid a module-load-time coupling — same
    # pattern already established by data_filters.live_product_types().
    # A product created dynamically via Rule Designer has no entry in the
    # static PRODUCT_METRIC dict below (which predates product_registry),
    # and would otherwise silently default to "pct" even if it's really an
    # FX-like product that should read in pips. An explicit metric set on
    # the product (Product.metric) wins; everything else keeps today's
    # exact behavior.
    from app.modules.rule_designer import product_registry
    p = product_registry.get_product(product_type)
    if p is not None and p.metric:
        return p.metric
    return PRODUCT_METRIC.get(product_type, "pct")


def deviation(metric: str, booked, reference, currency: str = "") -> float:
    """Deviation in the metric's native unit. Returns 0 on bad input."""
    try:
        b = float(booked)
        r = float(reference)
    except (TypeError, ValueError):
        return 0.0
    if metric == "pct":
        return abs(b - r) / abs(r) * 100.0 if r else 0.0
    if metric == "bps":
        # rates expressed in percent -> 0.01% == 1bp
        return abs(b - r) * 100.0
    if metric == "pips":
        pip_factor = 100 if "JPY" in (currency or "").upper() else 10000
        return abs(b - r) * pip_factor
    return 0.0
