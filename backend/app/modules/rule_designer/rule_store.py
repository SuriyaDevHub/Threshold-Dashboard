"""In-process view over the YAML-backed rule set (spec §30): every read
goes through `yaml_service.load_rules()`, every write through
`yaml_service.save_rules()` — there is no separate database, the YAML
file (plus its versions/) is the store. Product-scoped: `list_rules()`
with no product aggregates across every known product; everything else
either takes a product explicitly or resolves it via yaml_service's
rule_id index."""
from __future__ import annotations

import time
from typing import List, Optional

from app.modules.rule_designer import product_registry, yaml_service
from app.modules.rule_designer.models import LEGAL_TRANSITIONS, Rule, RuleStatus


def list_rules(product: Optional[str] = None) -> List[Rule]:
    if product:
        rules, _ = yaml_service.load_rules(product)
    else:
        rules = []
        for p in product_registry.list_products():
            r, _ = yaml_service.load_rules(p.code)
            rules.extend(r)
    return sorted(rules, key=lambda r: (r.product, r.priority, r.rule_id))


def get_rule(rule_id: str, product: Optional[str] = None) -> Optional[Rule]:
    product = product or yaml_service.resolve_product(rule_id)
    if not product:
        return None
    rules, _ = yaml_service.load_rules(product)
    for r in rules:
        if r.rule_id == rule_id:
            return r
    return None


def upsert_rule(rule: Rule, actor: str) -> Rule:
    rule.updated_by = actor
    rule.updated_at = time.time()
    yaml_service.save_rules([rule], actor=actor)
    return rule


def delete_rule(rule_id: str, actor: str, product: Optional[str] = None) -> bool:
    product = product or yaml_service.resolve_product(rule_id)
    if not product:
        return False
    return yaml_service.delete_rule(rule_id, product, actor=actor)


def transition(rule: Rule, target: RuleStatus, actor: str, role, comment: str = "") -> Rule:
    from app.modules.rule_designer.models import ApprovalEvent
    legal = LEGAL_TRANSITIONS.get(rule.status, [])
    if target not in legal:
        raise ValueError(
            f"illegal transition {rule.status.value} -> {target.value} "
            f"(legal targets from {rule.status.value}: {[t.value for t in legal]})"
        )
    came_from = rule.status
    rule.status = target
    if target == RuleStatus.DRAFT and came_from == RuleStatus.PUBLISHED:
        rule.version += 1  # amending a published rule starts a new draft version
    rule.approvals.append(ApprovalEvent(actor=actor, role=role, action=target.value, comment=comment))
    return upsert_rule(rule, actor)


def set_enabled(rule: Rule, enabled: bool, actor: str) -> Rule:
    rule.enabled = enabled
    return upsert_rule(rule, actor)
