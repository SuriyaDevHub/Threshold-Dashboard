"""YAML integration (spec §30-31): the YAML file is the authoritative,
machine-readable source of truth. This module is the ONLY place that reads
or writes it — nothing in the API layer or the frontend touches it
directly, and the frontend never sees YAML at all.

Load path:  existing YAML -> raw dict (ruamel round-trip, comments/order
            kept) -> canonical Rule models (best-effort per item; a rule
            that fails to parse is reported, not dropped or crashed on).
Save path:  canonical Rule models -> merged into the raw round-trip
            document (only the `rules` list is touched; every other
            top-level key, comment and ordering is left exactly as read)
            -> atomic write (temp file + os.replace) with a timestamped
            backup, never a partial write.

We first INSPECT whatever is on disk rather than assume a shape (spec
§2/§55): `inspect_yaml()` reports the top-level keys and, if a `rules` key
exists, how many entries parsed cleanly vs. not, before anything else in
the app relies on it.
"""
from __future__ import annotations

import io
import os
import time
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError
from ruamel.yaml import YAML

from app.modules.rule_designer.models import Rule

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
RULES_DIR = os.path.join(_BACKEND_DIR, "rules")
HISTORY_DIR = os.path.join(RULES_DIR, "_history")
RULES_FILE = os.path.join(RULES_DIR, "business_rules.yml")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)


def _ensure_dirs() -> None:
    os.makedirs(RULES_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)


def load_raw() -> Dict[str, Any]:
    """The existing YAML, parsed as-is. Never assumes `rules` exists."""
    _ensure_dirs()
    if not os.path.exists(RULES_FILE):
        return {"schema_version": 1, "rules": []}
    with open(RULES_FILE) as fh:
        data = _yaml.load(fh)
    return data if data is not None else {"schema_version": 1, "rules": []}


def inspect_yaml() -> Dict[str, Any]:
    """Phase-1 inspection report: what shape is actually on disk right now."""
    raw = load_raw()
    report: Dict[str, Any] = {
        "path": RULES_FILE,
        "exists": os.path.exists(RULES_FILE),
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


def load_rules() -> Tuple[List[Rule], List[Dict[str, Any]]]:
    """Returns (rules that parsed, [{rule_id, errors}] for ones that didn't)."""
    raw = load_raw()
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


def save_rules(rules: List[Rule], actor: str = "system") -> None:
    """Merge the given rules into the on-disk YAML, touching only the
    `rules` list and only the entries whose rule_id is in `rules` — every
    other top-level key and every other rule entry is preserved verbatim.
    Atomic: written to a temp file in the same directory, then renamed.
    """
    _ensure_dirs()
    raw = load_raw()
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
    raw["last_updated_at"] = time.time()
    raw["last_updated_by"] = actor

    if os.path.exists(RULES_FILE):
        backup = os.path.join(HISTORY_DIR, f"business_rules_{int(time.time() * 1000)}.yml")
        with open(RULES_FILE) as src, open(backup, "w") as dst:
            dst.write(src.read())

    tmp_path = RULES_FILE + f".tmp{os.getpid()}"
    try:
        with open(tmp_path, "w") as fh:
            _yaml.dump(raw, fh)
        os.replace(tmp_path, RULES_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def delete_rule(rule_id: str, actor: str = "system") -> bool:
    raw = load_raw()
    before = len(raw.get("rules", []))
    raw["rules"] = [r for r in raw.get("rules", []) if _plain(r).get("rule_id") != rule_id]
    if len(raw["rules"]) == before:
        return False
    raw["last_updated_at"] = time.time()
    raw["last_updated_by"] = actor
    _ensure_dirs()
    if os.path.exists(RULES_FILE):
        backup = os.path.join(HISTORY_DIR, f"business_rules_{int(time.time() * 1000)}.yml")
        with open(RULES_FILE) as src, open(backup, "w") as dst:
            dst.write(src.read())
    tmp_path = RULES_FILE + f".tmp{os.getpid()}"
    with open(tmp_path, "w") as fh:
        _yaml.dump(raw, fh)
    os.replace(tmp_path, RULES_FILE)
    return True


def dump_to_text(raw: Dict[str, Any]) -> str:
    buf = io.StringIO()
    _yaml.dump(raw, buf)
    return buf.getvalue()


def rules_yaml_text() -> str:
    return dump_to_text(load_raw())
