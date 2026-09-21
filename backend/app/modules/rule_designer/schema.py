"""Datatype inference for datasets and reference files.

The UI needs to know each column's type to pick operators and input
widgets (spec §6-8). We infer from a sample of rows rather than trust a
declared schema, since uploaded CSVs and pulled datasets carry none.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, List

from app.modules.rule_designer.models import FieldType

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_BOOL_VALUES = {"true", "false", "yes", "no", "y", "n"}


def _infer_value_type(v: Any) -> FieldType:
    if v is None or v == "":
        return FieldType.STRING
    if isinstance(v, bool):
        return FieldType.BOOLEAN
    if isinstance(v, (int, float)):
        return FieldType.NUMERIC
    s = str(v).strip()
    if s.lower() in _BOOL_VALUES:
        return FieldType.BOOLEAN
    if _DATE_RE.match(s):
        return FieldType.DATE
    try:
        float(s.replace(",", ""))
        return FieldType.NUMERIC
    except ValueError:
        pass
    return FieldType.STRING


IDENTIFIER_HINTS = ("_id", "id", "ref", "code", "key")


def infer_schema(rows: List[Dict[str, Any]], sample_size: int = 200) -> Dict[str, FieldType]:
    """Majority-vote column type over a sample of rows."""
    if not rows:
        return {}
    columns: List[str] = []
    seen = set()
    for r in rows[:sample_size]:
        for k in r.keys():
            if k not in seen and not k.startswith("_"):
                seen.add(k)
                columns.append(k)

    schema: Dict[str, FieldType] = {}
    for col in columns:
        votes: Dict[FieldType, int] = {}
        non_null = 0
        for r in rows[:sample_size]:
            v = r.get(col)
            if v is None or v == "":
                continue
            non_null += 1
            t = _infer_value_type(v)
            votes[t] = votes.get(t, 0) + 1
        if not votes:
            schema[col] = FieldType.STRING
            continue
        best = max(votes.items(), key=lambda kv: kv[1])[0]
        lower = col.lower()
        if best in (FieldType.STRING, FieldType.NUMERIC) and any(
            lower.endswith(h) or lower == h for h in IDENTIFIER_HINTS
        ):
            best = FieldType.IDENTIFIER
        schema[col] = best
    return schema


def schema_to_wire(schema: Dict[str, FieldType]) -> List[Dict[str, str]]:
    return [{"field": k, "type": v.value} for k, v in schema.items()]
