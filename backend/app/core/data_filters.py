"""Canonical filter vocabularies for the Data Fetch module — mirrors the lists
from the original Streamlit layout.py so the UI dropdowns are backend-driven."""
from __future__ import annotations

from typing import List

# Fallback only, used if the product registry can't be read at all — normal
# callers get `live_product_types()` below instead. This static list predates
# the Rule Designer's product registry and uses a different code convention
# for some products (GFXCASH/CASHBONDS here vs. the registry's GFX_CASH/
# CASH_BONDS) plus four codes (CASHEQUITIES, CDS, SBL, TRS) the registry has
# no entry for — register those via Rule Designer's "+ New product" if they
# need to appear in Data Fetch again.
PRODUCT_TYPES = [
    "GFXCASH", "IRD", "CASHBONDS", "CASHEQUITIES", "CDS",
    "SBL", "PM", "MM", "TRS", "FXO",
]

LEGAL_ENTITIES = [
    "HBEU", "HBAP", "HBUS", "HBFR", "HBUK",
    "HBIE", "HBSG", "HBME", "HBAU", "HBJP",
]

SOURCE_SYSTEMS = [
    "FLEXRATE", "RIVER", "SUMMIT", "TREATS", "NFOS",
    "DSL", "BLOOMBERG", "GLOBAL_CALYPSO", "XFOS", "FIDESSA",
]

DATASETS = ["OMRC Trade Data", "EPE_Data", "Both"]


def live_product_types() -> List[str]:
    """The product codes Data Fetch's dropdown should actually offer —
    read from the Rule Designer's product registry (the real source of
    truth for what products exist) instead of the static PRODUCT_TYPES
    list above, so a product created via "+ New product" shows up here
    with no code change. Lazy-imports product_registry to keep app.core
    from depending on app.modules at import time; falls back to
    PRODUCT_TYPES only if the registry genuinely can't be read."""
    try:
        from app.modules.rule_designer import product_registry
        codes = [p.code for p in product_registry.list_products()]
        return codes or PRODUCT_TYPES
    except Exception:
        return PRODUCT_TYPES


def filter_config() -> dict:
    return {
        "product_types": live_product_types(),
        "legal_entities": LEGAL_ENTITIES,
        "source_systems": SOURCE_SYSTEMS,
        "datasets": DATASETS,
    }
