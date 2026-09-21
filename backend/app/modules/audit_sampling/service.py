"""Audit Sampling — pull an audit-ready sample from the EPE exception
population. Supports random and stratified-by-product sampling with a fixed
seed so a given run is reproducible (important for audit defensibility)."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Dict, List


def random_sample(rows: List[Dict], size: int, seed: int = 42) -> List[Dict]:
    rnd = random.Random(seed)
    if size >= len(rows):
        return list(rows)
    return rnd.sample(rows, size)


def stratified_sample(
    rows: List[Dict], size: int, key: str = "product_code", seed: int = 42
) -> List[Dict]:
    """Proportional allocation across strata, with at least 1 per non-empty stratum."""
    rnd = random.Random(seed)
    strata: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        strata[str(r.get(key, "UNKNOWN"))].append(r)

    total = len(rows)
    sample: List[Dict] = []
    for _, items in strata.items():
        alloc = max(1, math.floor(size * len(items) / total)) if total else 0
        alloc = min(alloc, len(items))
        sample.extend(rnd.sample(items, alloc))

    # Trim or top-up to hit the requested size as closely as possible.
    if len(sample) > size:
        sample = rnd.sample(sample, size)
    return sample


def coverage(sample: List[Dict], population: List[Dict], key: str = "product_code") -> Dict:
    pop_keys = {str(r.get(key)) for r in population}
    smp_keys = {str(r.get(key)) for r in sample}
    return {
        "population_size": len(population),
        "sample_size": len(sample),
        "coverage_pct": round(len(sample) / len(population) * 100, 2) if population else 0,
        "strata_covered": len(smp_keys),
        "strata_total": len(pop_keys),
    }
