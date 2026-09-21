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
    eval()'d. Expressions (CALCULATE nodes) are parsed into a restricted
    AST by expr_engine.py and only a small whitelist of operators/
    functions is ever executed.
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
            Operator.REGEX: "string",
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
                        Operator.REGEX, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.DATE: [Operator.BEFORE, Operator.AFTER, Operator.ON, Operator.BETWEEN,
                      Operator.NOT_BETWEEN, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.BOOLEAN: [Operator.EQ, Operator.NE, Operator.IS_NULL, Operator.IS_NOT_NULL],
    FieldType.IDENTIFIER: [Operator.EQ, Operator.NE, Operator.IN, Operator.NOT_IN,
                            Operator.IS_NULL, Operator.IS_NOT_NULL],
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
    VIEWER = "VIEWER"
    RULE_CREATOR = "RULE_CREATOR"
    REVIEWER = "REVIEWER"
    APPROVER = "APPROVER"
    ADMIN = "ADMIN"


ROLE_ALLOWED_ACTIONS: Dict[Role, List[str]] = {
    Role.VIEWER: ["view"],
    Role.RULE_CREATOR: ["view", "create", "edit", "dry_run", "submit", "manage_lookups"],
    Role.REVIEWER: ["view", "dry_run", "comment"],
    Role.APPROVER: ["view", "dry_run", "approve", "reject", "publish", "rollback"],
    Role.ADMIN: ["view", "create", "edit", "dry_run", "submit", "approve", "reject",
                 "publish", "rollback", "delete", "manage_lookups"],
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
    anywhere in the pipeline (base, enriched, derived, or another lookup)."""
    type: str = "static"  # static | column | derived | lookup | expression
    value: Any = None      # for type=static
    name: Optional[str] = None  # for type=column|derived|lookup: the field name

    @field_validator("type")
    @classmethod
    def _check_type(cls, v):
        if v not in {"static", "column", "derived", "lookup", "expression"}:
            raise ValueError(f"Unknown ValueRef.type '{v}'")
        return v


def literal(value: Any) -> ValueRef:
    return ValueRef(type="static", value=value)


def column(name: str) -> ValueRef:
    return ValueRef(type="column", name=name)


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
    """A calculated column. `expression` is a small arithmetic expression
    over already-available fields (e.g. "Notional * Price" or
    "Deviation / Threshold"), parsed into a restricted AST by expr_engine —
    never eval()'d. `output_type` is inferred but may be pinned."""
    output_field: str
    expression: str
    output_type: FieldType = FieldType.NUMERIC
    description: Optional[str] = None


# --------------------------------------------------------------------------
# Lookup / enrichment step
# --------------------------------------------------------------------------

class LookupFieldMap(BaseModel):
    source_column: str
    output_field: str


class LookupConfig(BaseModel):
    lookup_type: LookupType = LookupType.EXACT
    reference_file_id: str
    reference_version: Optional[int] = None  # pinned version; None = latest at publish time

    # exact / composite: list of (source_field, reference_field) join pairs
    join_keys: List[Dict[str, str]] = Field(default_factory=list)  # [{source, reference}]

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


class Rule(BaseModel):
    rule_id: str
    name: str
    description: str = ""
    status: RuleStatus = RuleStatus.DRAFT
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
# Versioning + audit
# --------------------------------------------------------------------------

class RuleSetVersion(BaseModel):
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
