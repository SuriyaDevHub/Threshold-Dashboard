"""In-process view over the rule set — S3 (via yaml_service) for anything
that's been submitted or further, plus a local-disk working copy
(draft_store) for a rule that's still pre-submission (DRAFT / VALIDATED /
DRY_RUN_COMPLETED), so the normal author-edit-validate-dry_run loop never
round-trips to S3. A rule's local copy is written through to S3 (and then
removed locally) the moment it's submitted for approval — see
upsert_rule() — at which point S3 is authoritative again, exactly as
before this split existed. Every read (list_rules/get_rule) transparently
merges both stores; every other module and the API layer are unaware the
split exists."""
from __future__ import annotations

import re
import time
from typing import List, Optional

from app.modules.rule_designer import draft_store, product_registry, yaml_service
from app.modules.rule_designer.models import LEGAL_TRANSITIONS, Rule, RuleStatus

# Statuses whose working copy lives only on local disk — production
# evaluation (product_engine.py) only ever reads PUBLISHED rules, so
# nothing here needs to be durable/S3-visible until it's actually
# submitted for approval.
_LOCAL_STATUSES = {RuleStatus.DRAFT, RuleStatus.VALIDATED, RuleStatus.DRY_RUN_COMPLETED}

# A rule counts toward the next auto-generated id only once it has passed
# approval — a draft nobody ever finishes (or a rejected one) shouldn't
# permanently burn a number, matching the product's real approved rule
# count rather than every scratch attempt.
_COUNTED_ID_STATUSES = {RuleStatus.APPROVED, RuleStatus.PUBLISHED}

# Fields a priority-only edit is allowed to change without invalidating a
# rule's prior validate/dry-run pass. Everything else on the model (the
# workflow itself, its dataset binding, conflict_handling, ...) affects what
# the rule actually does and must go through VALIDATED -> DRY_RUN_COMPLETED
# again before it can be resubmitted.
_IGNORED_FOR_STRUCTURAL_DIFF = {
    "priority", "status", "fast_track_eligible", "version",
    "updated_by", "updated_at", "last_dry_run_id", "approvals", "notes",
    # `dataset_id` only picks which dataset Validate/Dry Run default to —
    # it's never read by evaluate_record()/evaluate_product() (both take
    # dataset_id as an explicit call argument, independent of this stored
    # value). Found via end-to-end testing: the workspace UI's "Bound
    # dataset" selector submits whatever it's currently showing on every
    # Save, including a priority-only one, so without this a rule could
    # silently pick up a dataset_id (e.g. left over from browsing another
    # rule earlier in the session) and lose fast-track eligibility for a
    # change that never touched its actual logic.
    "dataset_id",
}


def structural_diff_fields(before: Rule, after: Rule) -> set:
    """Every top-level field that differs between two Rule versions, other
    than bookkeeping fields and the one this whole mechanism exists to
    exempt (`priority`). Used to decide whether a DRAFT demoted from
    APPROVED/PUBLISHED can fast-track straight back to PENDING_APPROVAL."""
    b = before.model_dump(mode="json")
    a = after.model_dump(mode="json")
    return {
        key for key in (set(b) | set(a)) - _IGNORED_FOR_STRUCTURAL_DIFF
        if b.get(key) != a.get(key)
    }


def next_rule_id(product: str) -> str:
    """OAR-{PRODUCT}-NNN, NNN = 1 + the highest existing suffix among this
    product's APPROVED/PUBLISHED rules matching that pattern (draft-only
    rules and other naming conventions don't affect the count)."""
    product = product.upper()
    pattern = re.compile(rf"^OAR-{re.escape(product)}-(\d+)$", re.IGNORECASE)
    highest = 0
    for r in list_rules(product):
        if r.status not in _COUNTED_ID_STATUSES:
            continue
        m = pattern.match(r.rule_id)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"OAR-{product}-{highest + 1:03d}"


def list_rules(product: Optional[str] = None) -> List[Rule]:
    if product:
        s3_rules, _ = yaml_service.load_rules(product)
        drafts = draft_store.list_for_product(product)
    else:
        s3_rules = []
        for p in product_registry.list_products():
            r, _ = yaml_service.load_rules(p.code)
            s3_rules.extend(r)
        drafts = draft_store.list_all()
    merged = {r.rule_id: r for r in s3_rules}
    merged.update({r.rule_id: r for r in drafts})  # a local draft is always the newer state
    return sorted(merged.values(), key=lambda r: (r.product, r.priority, r.rule_id))


def get_rule(rule_id: str, product: Optional[str] = None) -> Optional[Rule]:
    draft = draft_store.get(rule_id)
    if draft is not None:
        return draft
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
    if rule.status in _LOCAL_STATUSES:
        draft_store.save(rule)
    else:
        yaml_service.save_rules([rule], actor=actor)
        draft_store.delete(rule.rule_id)  # promoted — S3 is authoritative from here
    return rule


def delete_rule(rule_id: str, actor: str, product: Optional[str] = None) -> bool:
    if draft_store.get(rule_id) is not None:
        return draft_store.delete(rule_id)
    product = product or yaml_service.resolve_product(rule_id)
    if not product:
        return False
    return yaml_service.delete_rule(rule_id, product, actor=actor)


def build_product_yaml_text(product: str) -> str:
    """Like yaml_service.rules_yaml_text(), but overlays this product's
    local drafts into the rules list first — so a diff/preview reflects a
    rule's actual current content even while it's still local-only, not
    stale S3 content. Same by-rule_id merge yaml_service.save_rules() uses."""
    drafts = draft_store.list_for_product(product)
    raw = yaml_service.load_raw(product)
    if drafts:
        by_id = {d.rule_id: d for d in drafts}
        new_list = []
        seen = set()
        for item in raw.get("rules") or []:
            plain = yaml_service._plain(item)  # noqa: SLF001
            rid = plain.get("rule_id")
            seen.add(rid)
            draft = by_id.get(rid)
            new_list.append(draft.model_dump(mode="json") if draft else plain)
        for rid, d in by_id.items():
            if rid not in seen:
                new_list.append(d.model_dump(mode="json"))
        raw = dict(raw)
        raw["rules"] = new_list
    return yaml_service.dump_to_text(raw)


def transition(rule: Rule, target: RuleStatus, actor: str, role, comment: str = "") -> Rule:
    from app.modules.rule_designer.models import ApprovalEvent
    legal = LEGAL_TRANSITIONS.get(rule.status, [])
    if target not in legal:
        raise ValueError(
            f"illegal transition {rule.status.value} -> {target.value} "
            f"(legal targets from {rule.status.value}: {[t.value for t in legal]})"
        )
    if rule.status == RuleStatus.DRAFT and target == RuleStatus.PENDING_APPROVAL and not rule.fast_track_eligible:
        raise ValueError(
            "cannot submit directly from DRAFT — this rule has changes beyond its priority; "
            "validate and dry-run it first, then submit from DRY_RUN_COMPLETED"
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
