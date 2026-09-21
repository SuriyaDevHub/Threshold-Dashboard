"""YAML integration (spec §30-31): the YAML file is the authoritative,
machine-readable source of truth. This module is the ONLY place that reads
or writes it — nothing in the API layer or the frontend touches it
directly, and the frontend never sees YAML at all.

Storage is per product: `rules/<product>/business_rules.yml`. A single
monolithic file works for one worked example; it stops working the moment
two products' rule authors touch it in the same window — atomic file
writes mean one publish blocks or corrupts the other. Splitting by product
also makes the admin "enable/disable a whole product's rules" action and
per-product version history (versioned independently, rolled back
independently) a natural consequence of the storage layout rather than
something layered on top.

A rule_id is still globally unique, so a small index
(`rules/_index.json`, rule_id -> product) makes `get_rule(rule_id)` and
friends O(1) instead of a scan across every product's file.

Load path:  existing YAML -> raw dict (ruamel round-trip, comments/order
            kept) -> canonical Rule models (best-effort per item; a rule
            that fails to parse is reported, not dropped or crashed on).
Save path:  canonical Rule models -> merged into the raw round-trip
            document (only the `rules` list is touched; every other
            top-level key, comment and ordering is left exactly as read)
            -> atomic write (temp file + os.replace) with a timestamped
            backup, never a partial write.
"""
from __future__ import annotations

import io
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from ruamel.yaml import YAML

from app.modules.rule_designer.models import Rule

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
RULES_DIR = os.path.join(_BACKEND_DIR, "rules")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)


def _product_dir(product: str) -> str:
    return os.path.join(RULES_DIR, product.upper())


def _history_dir(product: str) -> str:
    return os.path.join(_product_dir(product), "_history")


def _rules_file(product: str) -> str:
    return os.path.join(_product_dir(product), "business_rules.yml")


def _ensure_dirs(product: str) -> None:
    os.makedirs(_product_dir(product), exist_ok=True)
    os.makedirs(_history_dir(product), exist_ok=True)


# --------------------------------------------------------------------------
# rule_id -> product index (rule_id stays globally unique)
# --------------------------------------------------------------------------

def _index_path() -> str:
    return os.path.join(RULES_DIR, "_index.json")


def _load_index() -> Dict[str, str]:
    path = _index_path()
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return json.load(fh)


def _save_index(idx: Dict[str, str]) -> None:
    os.makedirs(RULES_DIR, exist_ok=True)
    path = _index_path()
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(idx, fh, indent=2)
    os.replace(tmp, path)


def resolve_product(rule_id: str) -> Optional[str]:
    return _load_index().get(rule_id)


def _index_upsert(rule_ids_by_product: Dict[str, str]) -> None:
    idx = _load_index()
    idx.update(rule_ids_by_product)
    _save_index(idx)


def _index_remove(rule_id: str) -> None:
    idx = _load_index()
    if rule_id in idx:
        del idx[rule_id]
        _save_index(idx)


# --------------------------------------------------------------------------
# Per-product read/write
# --------------------------------------------------------------------------

def load_raw(product: str) -> Dict[str, Any]:
    """The existing YAML for one product, parsed as-is. Never assumes
    `rules` exists."""
    _ensure_dirs(product)
    path = _rules_file(product)
    if not os.path.exists(path):
        return {"schema_version": 1, "product": product.upper(), "rules": []}
    with open(path) as fh:
        data = _yaml.load(fh)
    return data if data is not None else {"schema_version": 1, "product": product.upper(), "rules": []}


def inspect_yaml(product: str) -> Dict[str, Any]:
    """Phase-1 inspection report: what shape is actually on disk right now."""
    raw = load_raw(product)
    report: Dict[str, Any] = {
        "product": product.upper(),
        "path": _rules_file(product),
        "exists": os.path.exists(_rules_file(product)),
        "top_level_keys": list(raw.keys()) if isinstance(raw, dict) else [],
        "rule_count": 0,
        "parsed_ok": 0,
        "parse_errors": [],
    }
    rules_blob = raw.get("rules", []) if isinstance(raw, dict) else []
    report["rule_count"] = len(rules_blob)
    for item in rules_blob:
        try:
            Rule.model_validate(_plain(item))
            report["parsed_ok"] += 1
        except ValidationError as exc:
            report["parse_errors"].append({
                "rule_id": item.get("rule_id", "?") if isinstance(item, dict) else "?",
                "errors": exc.errors(),
            })
    return report


def _plain(obj: Any) -> Any:
    """Strip ruamel's CommentedMap/Seq wrappers down to plain dict/list so
    pydantic can validate them."""
    if hasattr(obj, "items"):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


def load_rules(product: str) -> Tuple[List[Rule], List[Dict[str, Any]]]:
    """Returns (rules that parsed, [{rule_id, errors}] for ones that didn't)."""
    raw = load_raw(product)
    rules: List[Rule] = []
    failures: List[Dict[str, Any]] = []
    for item in raw.get("rules", []) or []:
        plain = _plain(item)
        try:
            rules.append(Rule.model_validate(plain))
        except ValidationError as exc:
            failures.append({"rule_id": plain.get("rule_id", "?"), "errors": exc.errors()})
    return rules, failures


def _rule_to_plain(rule: Rule) -> dict:
    return rule.model_dump(mode="json")


def _backup(product: str) -> None:
    path = _rules_file(product)
    if os.path.exists(path):
        backup = os.path.join(_history_dir(product), f"business_rules_{int(time.time() * 1000)}.yml")
        with open(path) as src, open(backup, "w") as dst:
            dst.write(src.read())


def _atomic_write(product: str, raw: Dict[str, Any]) -> None:
    path = _rules_file(product)
    tmp_path = path + f".tmp{os.getpid()}"
    try:
        with open(tmp_path, "w") as fh:
            _yaml.dump(raw, fh)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def save_rules(rules: List[Rule], actor: str = "system") -> None:
    """Merge the given rules into their product's on-disk YAML, touching
    only the `rules` list and only the entries whose rule_id is in `rules`
    — every other top-level key and every other rule entry is preserved
    verbatim. Atomic: written to a temp file in the same directory, then
    renamed. Every rule in `rules` must share one product — mixing
    products in one call is almost always a bug at the caller.
    """
    if not rules:
        return
    products = {r.product for r in rules}
    if len(products) > 1:
        raise ValueError(f"save_rules() received rules from multiple products: {sorted(products)}")
    product = next(iter(products))

    _ensure_dirs(product)
    raw = load_raw(product)
    if "rules" not in raw or raw.get("rules") is None:
        raw["rules"] = []

    by_id = {r.rule_id: r for r in rules}
    existing_ids = set()
    new_list = []
    for item in raw["rules"]:
        plain = _plain(item)
        rid = plain.get("rule_id")
        existing_ids.add(rid)
        if rid in by_id:
            new_list.append(_rule_to_plain(by_id[rid]))
        else:
            new_list.append(plain)  # untouched rule, preserved as-is
    for rid, r in by_id.items():
        if rid not in existing_ids:
            new_list.append(_rule_to_plain(r))
    raw["rules"] = new_list
    raw["schema_version"] = raw.get("schema_version", 1)
    raw["product"] = product.upper()
    raw["last_updated_at"] = time.time()
    raw["last_updated_by"] = actor

    _backup(product)
    _atomic_write(product, raw)
    _index_upsert({rid: product for rid in by_id})


def delete_rule(rule_id: str, product: str, actor: str = "system") -> bool:
    raw = load_raw(product)
    before = len(raw.get("rules", []))
    raw["rules"] = [r for r in raw.get("rules", []) if _plain(r).get("rule_id") != rule_id]
    if len(raw["rules"]) == before:
        return False
    raw["last_updated_at"] = time.time()
    raw["last_updated_by"] = actor
    _ensure_dirs(product)
    _backup(product)
    _atomic_write(product, raw)
    _index_remove(rule_id)
    return True


def dump_to_text(raw: Dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(raw, buf)
    return buf.getvalue()


def rules_yaml_text(product: str) -> str:
    return dump_to_text(load_raw(product))
