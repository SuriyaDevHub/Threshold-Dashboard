"""Canonical filter vocabularies for the Data Fetch module — mirrors the lists
from the original Streamlit layout.py so the UI dropdowns are backend-driven."""
from __future__ import annotations

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


def filter_config() -> dict:
    return {
        "product_types": PRODUCT_TYPES,
        "legal_entities": LEGAL_ENTITIES,
        "source_systems": SOURCE_SYSTEMS,
        "datasets": DATASETS,
    }
