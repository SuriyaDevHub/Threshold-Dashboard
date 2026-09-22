"""Canonical Rule / Workflow model.

Everything downstream (visual builder, free-text/AI builder, validation,
dry-run, impact analysis, YAML compiler) speaks this model and nothing else.
Both authoring modes (visual and free-text) MUST compile into this same
shape — there is exactly one execution engine, not one per authoring mode.

Design notes:
  * A Rule owns a Workflow: an ordered DAG of typed Nodes (see NodeType)
    connected by Edges, executed in topological order starting from the
    INPUT node.
  * Conditions are a recursive AND/OR/NOT tree (Condition | ConditionGroup)
    so nesting is unlimited, matching the visual builder's group UI.
  * Values referenced anywhere (condition operands, derive operands) are
    typed (`ValueRef`) so a condition can compare a column to a static
    value, another column, a derived column, or a lookup-enriched column
    without special-casing each case downstream.
  * No node, condition or derive spec ever carries a code string that gets
    eval()'d or parsed from free text. A CALCULATE node's `formula` is a
    small operand tree (see calc_ops.py) built entirely by selecting
    fields/constants/operations — the same "pick, don't type" shape as a
    condition or a lookup join key.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class FieldType(str, Enum):
    STRING = "string"
    NUMERIC = "numeric"
    DATE = "date"
    BOOLEAN = "boolean"
    IDENTIFIER = "identifier"


class Operator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    BETWEEN = "between"
    NOT_BETWEEN = "not_between"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IN = "in"
    NOT_IN = "not_in"
    REGEX = "regex"
    MATCHES_PATTERN = "matches_pattern"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    BEFORE = "before"
    AFTER = "after"
    ON = "on"

    @property
    def category(self) -> str:
        return {
            Operator.EQ: "any", Operator.NE: "any",
            Operator.GT: "numeric", Operator.LT: "numeric",
            Operator.GTE: "numeric", Operator.LTE: "numeric",
            Operator.BETWEEN: "numeric_or_date", Operator.NOT_BETWEEN: "numeric_or_date",
            Operator.CONTAINS: "string", Operator.NOT_CONTAINS: "string",
            Operator.STARTS_WITH: "string", Operator.ENDS_WITH: "string",
            Operator.IN: "any", Operator.NOT_IN: "any",
            Operator.REGEX: "string", Operator.MATCHES_PATTERN: "string",
            Operator.IS_NULL: "any", Operator.IS_NOT_NULL: "any",
            Operator.BEFORE: "date", Operator.AFTER: "date", Operator.ON: "date",
        }[self]

    @property
    def arity(self) -> str:
        """none | unary | binary | between | list"""
        if self in (Operator.IS_NULL, Operator.IS_NOT_NULL):
            return "none"
        if self in (Operator.BETWEEN, Operator.NOT_BETWEEN):
            return "between"
        if self in (Operator.IN, Operator.NOT_IN):
            return "list"
        return "binary"


OPERATORS_BY_TYPE: Dict[FieldType, List[Operator]] = {
    FieldType.NUMERIC: [Operator.EQ, Operator.NE, Operator.GT, Operator.LT, Operator.GTE,
                         Operator.LTE, Operator.BETWEEN, Operator.NOT_BETWEEN,
                         Operator.IN, Operator.NOT_IN, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.STRING: [Operator.EQ, Operator.NE, Operator.CONTAINS, Operator.NOT_CONTAINS,
                        Operator.STARTS_WITH, Operator.ENDS_WITH, Operator.IN, Operator.NOT_IN,
                        Operator.REGEX, Operator.MATCHES_PATTERN, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.DATE: [Operator.BEFORE, Operator.AFTER, Operator.ON, Operator.BETWEEN,
                      Operator.NOT_BETWEEN, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.BOOLEAN: [Operator.EQ, Operator.NE, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.IDENTIFIER: [Operator.EQ, Operator.NE, Operator.IN, Operator.NOT_IN,
                            Operator.MATCHES_PATTERN, Operator.IS_NULL, Operator.IS_NOT_NULL],
}


class NodeType(str, Enum):
    INPUT = "input"
    FILTER = "filter"
    LOOKUP = "lookup"
    ENRICHMENT = "enrichment"
    CALCULATE = "calculate"
    CONDITION = "condition"
    GROUP = "group"
    TRANSFORM = "transform"
    OUTCOME = "outcome"
    VALIDATION = "validation"


class JoinType(str, Enum):
    LEFT = "left"
    INNER = "inner"
    RIGHT = "right"
    FULL = "full"


class LookupType(str, Enum):
    EXACT = "exact"
    COMPOSITE = "composite"
    RANGE = "range"
    DATE = "date"


class MissingLookupStrategy(str, Enum):
    REJECT = "reject"
    CONTINUE_NULL = "continue_null"
    DEFAULT = "default"
    FLAG = "flag"
    FALLBACK = "fallback"


class PriorityStrategy(str, Enum):
    FIRST_MATCH = "first_match"
    HIGHEST_PRIORITY = "highest_priority"
    LATEST_EFFECTIVE_DATE = "latest_effective_date"
    LOWEST_THRESHOLD = "lowest_threshold"
    HIGHEST_THRESHOLD = "highest_threshold"


class RuleStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    DRY_RUN_COMPLETED = "DRY_RUN_COMPLETED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


LEGAL_TRANSITIONS: Dict[RuleStatus, List[RuleStatus]] = {
    RuleStatus.DRAFT: [RuleStatus.VALIDATED],
    RuleStatus.VALIDATED: [RuleStatus.DRY_RUN_COMPLETED, RuleStatus.DRAFT],
    RuleStatus.DRY_RUN_COMPLETED: [RuleStatus.PENDING_APPROVAL, RuleStatus.DRAFT],
    RuleStatus.PENDING_APPROVAL: [RuleStatus.APPROVED, RuleStatus.REJECTED],
    RuleStatus.APPROVED: [RuleStatus.PUBLISHED, RuleStatus.DRAFT],
    RuleStatus.PUBLISHED: [RuleStatus.DRAFT],  # amend published -> new draft version
    RuleStatus.REJECTED: [RuleStatus.DRAFT],
}


class Role(str, Enum):
    """Mirrors the production login's two account roles. Kept as an enum
    (rather than a bare bool) so the lifecycle/audit trail always names a
    role, and so a third role could be added later without touching every
    call site — but today it's exactly Admin/User, nothing richer."""
    ADMIN = "ADMIN"
    USER = "USER"


ROLE_ALLOWED_ACTIONS: Dict[Role, List[str]] = {
    # Admin: create/edit/test/submit/approve/publish a rule, enable or
    # disable a single rule, and enable or disable a whole product's rules.
    Role.ADMIN: ["view", "create", "edit", "dry_run", "submit", "approve", "reject",
                 "publish", "rollback", "delete", "manage_lookups", "manage_products", "manage_rules"],
    # User: view published configs, and dry-run/shadow-test rules still in
    # draft — never create, edit, or move anything through the lifecycle.
    Role.USER: ["view", "dry_run"],
}


class ConflictHandling(str, Enum):
    FIRST_MATCH = "first_match"
    HIGHEST_PRIORITY = "highest_priority"
    ALL_MATCHING = "all_matching"
    LAST_MATCH = "last_match"


class AuthoringMode(str, Enum):
    VISUAL = "visual"
    FREETEXT = "freetext"


# --------------------------------------------------------------------------
# Value references (operands anywhere in the model)
# --------------------------------------------------------------------------

class ValueRef(BaseModel):
    """A typed operand: a literal, or a reference to a column produced
    anywhere in the pipeline (base, enriched, derived, or another lookup),
    or a `{field}`-interpolated text template (outcome values only — e.g.
    reproducing a legacy `commentary_template.format(**fmt)`)."""
    type: str = "static"  # static | column | derived | lookup | expression | template
    value: Any = None      # for type=static|template (template holds the format string)
    name: Optional[str] = None  # for type=column|derived|lookup: the field name

    @field_validator("type")
    @classmethod
    def _check_type(cls, v):
        if v not in {"static", "column", "derived", "lookup", "expression", "template"}:
            raise ValueError(f"Unknown ValueRef.type '{v}'")
        return v


def literal(value: Any) -> ValueRef:
    return ValueRef(type="static", value=value)


def column(name: str) -> ValueRef:
    return ValueRef(type="column", name=name)


def template(text: str) -> ValueRef:
    """A `{field}` placeholder string, rendered against the record at
    outcome time — the canonical model's equivalent of a legacy
    `commentary_template.format(**fmt)`. Prefer `literal()` when a rule's
    config carries a static commentary string: legacy validators always
    let a static `commentary` win over `commentary_template` when both are
    present, and that priority is simply which of these two helpers the
    rule's OutcomeAction is built with — the engine needs no extra
    priority logic for it."""
    return ValueRef(type="template", value=text)


# --------------------------------------------------------------------------
# Conditions (recursive AND/OR/NOT tree)
# --------------------------------------------------------------------------

class Condition(BaseModel):
    kind: str = "condition"
    id: str = Field(default_factory=lambda: _id("cond"))
    field: str
    operator: Operator
    value: Optional[ValueRef] = None       # binary operators
    value2: Optional[ValueRef] = None      # BETWEEN upper bound
    values: Optional[List[ValueRef]] = None  # IN / NOT IN


class ConditionGroup(BaseModel):
    kind: str = "group"
    id: str = Field(default_factory=lambda: _id("grp"))
    operator: str = "AND"  # AND | OR | NOT
    children: List["ConditionNode"] = Field(default_factory=list)

    @field_validator("operator")
    @classmethod
    def _check_op(cls, v):
        v = v.upper()
        if v not in {"AND", "OR", "NOT"}:
            raise ValueError("Group operator must be AND, OR or NOT")
        return v


ConditionNode = Union[Condition, ConditionGroup]
ConditionGroup.model_rebuild()


# --------------------------------------------------------------------------
# Derived / calculated fields
# --------------------------------------------------------------------------

class DeriveSpec(BaseModel):
    """A calculated column. `formula` is an operand tree built by
    selecting fields/constants/operations — see calc_ops.py for its shape
    and evaluation (e.g. {"kind": "operation", "op": "divide", "operands":
    [{"kind": "field", "field": "Notional"}, {"kind": "field", "field":
    "Price"}]} for "Notional / Price"). `output_type` is inferred but may
    be pinned."""
    output_field: str
    formula: Dict[str, Any] = Field(default_factory=dict)
    output_type: FieldType = FieldType.NUMERIC
    description: Optional[str] = None


# --------------------------------------------------------------------------
# Lookup / enrichment step
# --------------------------------------------------------------------------

class LookupFieldMap(BaseModel):
    source_column: str
    output_field: str
    # Optional — same {op, ...params} shape as a TRANSFORM node's own
    # config (see transform_ops.apply_transform_op), applied to the raw
    # value pulled from the reference file before it's written to
    # output_field. None/absent means write the looked-up value through
    # unchanged, matching the prior (and still default) behavior.
    transform: Optional[Dict[str, Any]] = None


class LookupConfig(BaseModel):
    lookup_type: LookupType = LookupType.EXACT
    reference_file_id: str
    reference_version: Optional[int] = None  # pinned version; None = latest at publish time

    # exact / composite: list of (source_field, reference_field) join pairs.
    # Each entry may also carry an optional "transform" ({op, ...params},
    # same shape as a TRANSFORM node's config) applied to BOTH the source
    # record's value and the reference file's value before they're
    # compared — e.g. an "upper" transform lets "hkfxo_052" on the record
    # match "HKFXO_052" in the reference file. Dict[str, Any] rather than
    # Dict[str, str] specifically to allow that nested transform object.
    join_keys: List[Dict[str, Any]] = Field(default_factory=list)  # [{source, reference, transform?}]

    # range lookup: source_field falls between reference low/high columns
    range_field: Optional[str] = None
    range_low_column: Optional[str] = None
    range_high_column: Optional[str] = None

    # date lookup: source_date_field falls between reference from/to columns
    date_field: Optional[str] = None
    date_from_column: Optional[str] = None
    date_to_column: Optional[str] = None

    fields: List[LookupFieldMap] = Field(default_factory=list)  # columns to enrich
    join_type: JoinType = JoinType.LEFT
    missing_strategy: MissingLookupStrategy = MissingLookupStrategy.CONTINUE_NULL
    default_values: Dict[str, Any] = Field(default_factory=dict)
    flag_field: Optional[str] = None  # written true/false when missing_strategy=flag
    fallback_reference_file_id: Optional[str] = None
    priority_strategy: PriorityStrategy = PriorityStrategy.FIRST_MATCH
    priority_field: Optional[str] = None  # used by highest/lowest-threshold, highest_priority


# --------------------------------------------------------------------------
# Outcome
# --------------------------------------------------------------------------

class OutcomeAction(BaseModel):
    field: str
    value: ValueRef


# --------------------------------------------------------------------------
# Workflow node + edge
# --------------------------------------------------------------------------

class WorkflowNode(BaseModel):
    id: str
    type: NodeType
    label: str = ""
    position: Dict[str, float] = Field(default_factory=lambda: {"x": 0, "y": 0})
    # exactly one of these is populated, matching `type`
    filter: Optional[ConditionGroup] = None
    lookup: Optional[LookupConfig] = None
    calculate: Optional[DeriveSpec] = None
    condition: Optional[ConditionGroup] = None
    outcomes: Optional[List[OutcomeAction]] = None
    transform: Optional[Dict[str, Any]] = None
    validation: Optional[Dict[str, Any]] = None


class WorkflowEdge(BaseModel):
    id: str = Field(default_factory=lambda: _id("edge"))
    source: str
    target: str


class Workflow(BaseModel):
    nodes: List[WorkflowNode] = Field(default_factory=list)
    edges: List[WorkflowEdge] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Rule (top level canonical object)
# --------------------------------------------------------------------------

class ApprovalEvent(BaseModel):
    actor: str
    role: Role
    action: str
    timestamp: float = Field(default_factory=time.time)
    comment: str = ""


class MigrationStatus(str, Enum):
    """Descriptive only — never gates anything. Lets the Products page show
    where each product is in the {product}_validator.py -> rule-engine
    migration without conflating that with the enabled/disabled kill switch."""
    NOT_MIGRATED = "not_migrated"
    IN_PROGRESS = "in_progress"
    MIGRATED = "migrated"


class Product(BaseModel):
    code: str
    name: str
    # The kill switch: when False, the product engine treats this product
    # as having zero active rules and alerts every record (fail-safe,
    # never silently passes) — see product_engine.py.
    enabled: bool = True
    migration_status: MigrationStatus = MigrationStatus.NOT_MIGRATED
    description: str = ""
    # Per-record fail-safe posture when NO published/enabled rule matches a
    # record: "clear" (default — matches this app's own demo products, no
    # outcome emitted) or "alert" (matches every legacy {product}_validator.py
    # seen so far: an unmatched trade is still ALERT with an UNMATCHED
    # reason, never silently clear). Set to "alert" when migrating a legacy
    # validator so its fail-closed contract carries over exactly.
    on_no_match: str = "clear"
    unmatched_reason_code: Optional[str] = None  # e.g. "PM-UNMATCHED"
    disabled_reason_code: Optional[str] = None    # e.g. "PM-DISABLED"
    created_by: str = "system"
    created_at: float = Field(default_factory=time.time)
    updated_by: str = "system"
    updated_at: float = Field(default_factory=time.time)

    @field_validator("on_no_match")
    @classmethod
    def _check_on_no_match(cls, v):
        if v not in {"clear", "alert"}:
            raise ValueError("on_no_match must be 'clear' or 'alert'")
        return v

    @field_validator("code")
    @classmethod
    def _check_code(cls, v):
        if not v or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("product code must be alphanumeric/underscore/hyphen only")
        return v.upper()


class Rule(BaseModel):
    rule_id: str
    product: str  # which product's rule set this belongs to — see product_registry.py
    name: str
    description: str = ""
    status: RuleStatus = RuleStatus.DRAFT
    # Independent of `status`/lifecycle: an admin's instant, reversible
    # on/off switch for a published rule. Disabling doesn't touch version
    # history or approvals — it's the "enable or disable rule" admin
    # action, distinct from the draft->...->published workflow.
    enabled: bool = True
    priority: int = 100
    conflict_handling: ConflictHandling = ConflictHandling.FIRST_MATCH

    authoring_mode: AuthoringMode = AuthoringMode.VISUAL
    source_text: Optional[str] = None
    interpretation_notes: List[str] = Field(default_factory=list)

    workflow: Workflow = Field(default_factory=Workflow)
    dataset_id: Optional[str] = None
    required_columns: List[str] = Field(default_factory=list)

    version: int = 1
    created_by: str = "system"
    created_at: float = Field(default_factory=time.time)
    updated_by: str = "system"
    updated_at: float = Field(default_factory=time.time)
    notes: str = ""

    last_dry_run_id: Optional[str] = None
    approvals: List[ApprovalEvent] = Field(default_factory=list)
    depends_on: List[str] = Field(default_factory=list)  # rule_ids this rule depends on

    @field_validator("rule_id")
    @classmethod
    def _check_rule_id(cls, v):
        if not v or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("rule_id must be alphanumeric/underscore/hyphen only")
        return v

    def can_transition_to(self, target: RuleStatus) -> bool:
        return target in LEGAL_TRANSITIONS.get(self.status, [])


# --------------------------------------------------------------------------
# Reference (lookup) files
# --------------------------------------------------------------------------

class ReferenceFileVersion(BaseModel):
    version: int
    uploaded_by: str
    uploaded_at: float = Field(default_factory=time.time)
    records: int
    columns: List[str]
    column_types: Dict[str, FieldType]
    file_path: str
    status: str = "active"  # active | superseded


class ReferenceFile(BaseModel):
    id: str
    name: str
    versions: List[ReferenceFileVersion] = Field(default_factory=list)

    @property
    def latest(self) -> Optional[ReferenceFileVersion]:
        return self.versions[-1] if self.versions else None


# --------------------------------------------------------------------------
# Dry run / impact results
# --------------------------------------------------------------------------

class DryRunSummary(BaseModel):
    total_records: int = 0
    matched: int = 0
    not_matched: int = 0
    lookup_failures: int = 0
    errors: int = 0
    execution_time_s: float = 0.0
    match_rate_pct: float = 0.0
    # How many of `matched`/`not_matched` actually got a per-record trace
    # kept in `records` (each capped independently — see workflow_engine's
    # `explain_sample_cap`). Lets the UI say "500 of 1,476 shown" instead of
    # silently displaying a partial list as if it were the complete one.
    matched_shown: int = 0
    not_matched_shown: int = 0


class EnrichmentDiagnostic(BaseModel):
    node_id: str
    label: str
    input_records: int = 0
    successful_lookups: int = 0
    lookup_failures: int = 0
    match_rate_pct: float = 0.0
    duplicate_keys: Dict[str, int] = Field(default_factory=dict)
    unmatched_keys: List[Any] = Field(default_factory=list)


class RecordTraceStep(BaseModel):
    node_id: str
    node_type: NodeType
    label: str
    status: str  # ok | skipped | lookup_miss | error
    detail: str = ""
    fields_added: Dict[str, Any] = Field(default_factory=dict)


class RecordResult(BaseModel):
    record_id: Any
    matched: bool
    trail: List[RecordTraceStep] = Field(default_factory=list)
    outcome: Dict[str, Any] = Field(default_factory=dict)
    final_record: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class DryRunResult(BaseModel):
    id: str = Field(default_factory=lambda: _id("dryrun"))
    rule_id: str
    rule_version: int
    dataset_id: str
    sample_mode: str = "full"
    created_by: str = "system"
    created_at: float = Field(default_factory=time.time)
    summary: DryRunSummary = Field(default_factory=DryRunSummary)
    enrichment: List[EnrichmentDiagnostic] = Field(default_factory=list)
    records: List[RecordResult] = Field(default_factory=list)  # capped sample for UI/explainability
    validation_errors: List[str] = Field(default_factory=list)


class ImpactResult(BaseModel):
    id: str = Field(default_factory=lambda: _id("impact"))
    rule_id: str
    dataset_id: str
    created_at: float = Field(default_factory=time.time)
    current_version: Optional[int] = None
    proposed_version: int = 0
    current_matches: int = 0
    proposed_matches: int = 0
    new_matches: int = 0
    removed_matches: int = 0
    unchanged_matches: int = 0
    outcome_changes: List[Dict[str, Any]] = Field(default_factory=list)
    lookup_changes: List[str] = Field(default_factory=list)
    new_match_ids: List[Any] = Field(default_factory=list)
    removed_match_ids: List[Any] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Shadow / parallel testing — the new engine's decision vs. a legacy
# validator's already-produced output, joined by record id. This is how a
# migration off a hardcoded `{product}_validator.py` gets proven safe
# before cutover: no code from the legacy validator is imported or run
# here, its output is just data like any other upload.
# --------------------------------------------------------------------------

class LegacyResultRow(BaseModel):
    record_id: str
    legacy_status: str
    legacy_reason_code: Optional[str] = None
    legacy_commentary: Optional[str] = None


class ShadowComparisonCategory(str, Enum):
    AGREE_ALERT = "agree_alert"
    AGREE_CLEAR = "agree_clear"
    NEW_ONLY = "new_only"        # new rule alerts, legacy did not — review for over-alerting
    LEGACY_ONLY = "legacy_only"  # legacy alerted, new rule did not — the regression risk


class ShadowRecordComparison(BaseModel):
    record_id: Any
    category: ShadowComparisonCategory
    legacy_status: Optional[str] = None
    legacy_reason_code: Optional[str] = None
    new_matched: bool = False
    new_outcome: Dict[str, Any] = Field(default_factory=dict)
    trail: List[RecordTraceStep] = Field(default_factory=list)


class ShadowTestSummary(BaseModel):
    total_compared: int = 0
    dataset_records_without_legacy_result: int = 0
    legacy_results_without_dataset_record: int = 0
    agree_alert: int = 0
    agree_clear: int = 0
    new_only: int = 0
    legacy_only: int = 0
    agreement_rate_pct: float = 0.0


class ShadowTestResult(BaseModel):
    id: str = Field(default_factory=lambda: _id("shadow"))
    rule_id: str
    rule_version: int
    dataset_id: str
    legacy_alert_values: List[str] = Field(default_factory=list)
    created_by: str = "system"
    created_at: float = Field(default_factory=time.time)
    summary: ShadowTestSummary = Field(default_factory=ShadowTestSummary)
    mismatches: List[ShadowRecordComparison] = Field(default_factory=list)  # new_only + legacy_only, capped


# --------------------------------------------------------------------------
# Product-level evaluation — this IS the generic_validator from the
# migration plan: every PUBLISHED, enabled rule for a product, evaluated
# in priority order per that product's conflict_handling. A disabled
# product or a product with zero active rules alerts every record
# (fail-safe default — never silently passes, per the non-negotiable
# requirement), it does not silently no-op.
# --------------------------------------------------------------------------

class ProductRecordResult(BaseModel):
    record_id: Any
    matched: bool
    matched_rule_id: Optional[str] = None
    matched_rule_ids: List[str] = Field(default_factory=list)  # when conflict_handling=all_matching
    outcome: Dict[str, Any] = Field(default_factory=dict)
    trail: List[RecordTraceStep] = Field(default_factory=list)


class ProductEvaluationSummary(BaseModel):
    product: str
    product_enabled: bool = True
    active_rule_count: int = 0
    total_records: int = 0
    matched: int = 0
    not_matched: int = 0
    match_rate_pct: float = 0.0
    fail_safe_triggered: bool = False
    fail_safe_reason: str = ""


class ProductEvaluationResult(BaseModel):
    id: str = Field(default_factory=lambda: _id("producteval"))
    product: str
    dataset_id: str
    created_by: str = "system"
    created_at: float = Field(default_factory=time.time)
    summary: ProductEvaluationSummary = Field(default_factory=lambda: ProductEvaluationSummary(product=""))
    records: List[ProductRecordResult] = Field(default_factory=list)  # capped sample


# --------------------------------------------------------------------------
# Versioning + audit
# --------------------------------------------------------------------------

class RuleSetVersion(BaseModel):
    product: str
    version: int
    file: str
    created_by: str
    created_at: float = Field(default_factory=time.time)
    description: str = ""
    rule_ids_changed: List[str] = Field(default_factory=list)
    dry_run_dataset_id: Optional[str] = None
    dry_run_result_id: Optional[str] = None
    approved_by: Optional[str] = None
    published_at: Optional[float] = None


class AuditEntry(BaseModel):
    id: str = Field(default_factory=lambda: _id("audit"))
    timestamp: float = Field(default_factory=time.time)
    actor: str
    role: Optional[Role] = None
    action: str  # CREATE|EDIT|DELETE|DRY_RUN|SUBMIT|APPROVE|REJECT|PUBLISH|ROLLBACK
    rule_id: Optional[str] = None
    rule_name: Optional[str] = None
    previous_version: Optional[int] = None
    new_version: Optional[int] = None
    dataset_id: Optional[str] = None
    lookup_files: List[str] = Field(default_factory=list)
    dry_run_result_id: Optional[str] = None
    detail: str = ""
