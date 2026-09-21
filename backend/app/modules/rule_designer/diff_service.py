"""Review diffs (spec §35): business-logic diff, enrichment diff and a
developer-friendly YAML diff between two rule versions."""
from __future__ import annotations

import difflib
from typing import List, Optional

from app.modules.rule_designer.models import NodeType, Rule


def yaml_diff(before_text: str, after_text: str) -> List[str]:
    return list(difflib.unified_diff(
        before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
        fromfile="before", tofile="after", lineterm="",
    ))


def _node_signatures(rule: Optional[Rule], node_type: NodeType) -> List[str]:
    if rule is None:
        return []
    out = []
    for n in rule.workflow.nodes:
        if n.type == node_type:
            out.append(n.model_dump_json())
    return out


def enrichment_diff(before: Optional[Rule], after: Optional[Rule]) -> dict:
    before_nodes = {n.id: n for n in before.workflow.nodes} if before else {}
    after_nodes = {n.id: n for n in after.workflow.nodes} if after else {}
    before_lookups = {n.lookup.reference_file_id for n in before_nodes.values()
                       if n.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and n.lookup} if before else set()
    after_lookups = {n.lookup.reference_file_id for n in after_nodes.values()
                      if n.type in (NodeType.LOOKUP, NodeType.ENRICHMENT) and n.lookup} if after else set()
    return {
        "added": sorted(after_lookups - before_lookups),
        "removed": sorted(before_lookups - after_lookups),
        "unchanged": sorted(before_lookups & after_lookups),
    }


def business_logic_diff(before: Optional[Rule], after: Optional[Rule]) -> dict:
    from app.modules.rule_designer.explain_service import generate_explanation
    return {
        "before": generate_explanation(before) if before else "(no prior version)",
        "after": generate_explanation(after) if after else "(none)",
    }
