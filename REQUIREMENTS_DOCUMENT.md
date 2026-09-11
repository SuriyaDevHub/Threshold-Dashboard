# Requirement Document — Generic Exception Closure & Validation System

**Requested by:** Suriya — TCFC OMRC AI/ML, CIB Middle Office
**Prepared:** 11 Sep 2026
**Status:** Prototype built and tested against every requirement below (see Traceability). Production wiring (persistence location, execution trigger, closure write-back) remains TBD — see §8.

---

## 1. Background / Problem Statement

Today, business logic that decides whether a flagged exception can be auto-closed lives in **per-product hardcoded YAML files**, read by **per-product Python validators** (e.g. `cashbond_validator.py`) that contain **rule-ID-specific branches** (`if rule_id == "CB_WITHIN_TOLERANCE": ...`). Consequences:

- A new rule, or a change to an existing threshold, requires a code change and redeploy.
- Each product needs its own validator file, duplicating logic.
- There is no validation, versioning, audit trail, or approval step around a rule change — it's a direct file/code edit.
- There is no way to prepare a record (pull in external reference data, or aggregate the dataset itself) before applying business logic, except by hand-coding it per rule.

## 2. Objective

Replace this with a **generic, configuration-driven rule engine**: rules are authored, validated, and versioned through a console; a single, product-agnostic validator function applies whatever rules are currently active; a config change (new rule, amendment, promotion, disablement) takes effect on the **next validation run**, with no code change and no redeploy.

## 3. Actors

| Actor | Role |
|---|---|
| Ops / Rule Author | Creates, amends, tests, and promotes rules via the Rule Console |
| Exception Analyst | Uploads/selects an exception dataset per product and runs validation |
| Approver (maker-checker, TBD) | Promotes a rule to active — see §8 |
| System | RuleStore, RuleRegistry, generic validator, pipeline engine |

## 4. Functional Requirements

| # | Requirement | Status |
|---|---|---|
| FR1 | A rule may apply its business condition **directly** to the exception record. | Built & tested |
| FR2 | A rule may instead run an ordered set of **pre-steps** first, each either **enriching** from an external source or **pivoting** (aggregating) the exception dataset itself, before the business condition runs. | Built & tested |
| FR3 | Each pre-step may be **conditionally applied** (`apply_when`), gated on fields available before that step. | Built & tested |
| FR4 | A rule's business condition, and each step's gate, may only reference fields **actually available at that point in the pipeline** — validated statically before save. | Built & tested |
| FR5 | Rules are **scoped to a product**; each product has its own field vocabulary. A rule cannot reference a field its product doesn't have. | Built & tested |
| FR6 | Every rule that closes an exception must carry a **reason code ID** and **commentary** (supports `{field}` placeholders), both mandatory. | Built & tested |
| FR7 | Rules have a lifecycle: `draft → active ⇄ disabled → deprecated`. Illegal transitions are rejected. | Built & tested |
| FR8 | **Amending an already-active rule does not require re-promotion** — the amendment is live as soon as saved. | Built & tested |
| FR9 | Multiple active rules may exist for one product; they evaluate in **priority order**, first satisfied rule wins. | Built & tested |
| FR10 | A user can **dry-run** a rule against sample data before promoting it, seeing per-record outcome, reason code, rendered commentary, and a step trail. | Built & tested |
| FR11 | Every rule mutation (create/update/promote/disable/deprecate) is **versioned, backed up, and audit-logged**. | Built & tested |
| FR12 | A single **generic validator function** applies rules to a dataset for **any product**, with no product- or rule-ID-specific code. | Built & tested |
| FR13 | The validator automatically reflects the **current** rule configuration on every call — detected via a cheap fingerprint of the active rule set, not a fixed cache TTL and not a restart. | Built & tested |
| FR14 | Exception Analysis: user selects a product and an uploaded dataset, clicks Run Validation, and sees CLEARED (with reason code + commentary) or ALERTED per exception. | Built & tested |

## 5. Non-Functional Requirements

- **No code change for a new rule or product-vocabulary-compatible amendment.**
- **No eval() of user input anywhere** — derived-field computation is a fixed, whitelisted operation set.
- **Auditability** — every mutation traceable to an actor and timestamp; nothing is hard-deleted by default.
- **Fail-safe default** — a product with no active rules alerts every exception rather than silently passing them.

## 6. Workflow

See the diagram rendered alongside this document (and reproduced in outline below). Three stages:

1. **Authoring** — Ops creates/amends a rule for any product → `validate_rule()` gate (schema + stage-aware field checks) → on pass, atomic write with version bump and audit entry → lifecycle action (promote/disable/deprecate) as a separate, audited step.
2. **Persistence** — `RuleStore`: one YAML file per rule, scoped by product; exposes `fingerprint(product)` — a hash of `{rule_id: version}` for currently-active rules.
3. **Runtime** — Exception Analysis: user selects product + dataset → Run Validation → `generic_validator.validate_exceptions()` → asks `RuleRegistry.get_active_rules(product)` → registry checks the fingerprint; reloads from the store only if it changed, else serves cached rules → for each active rule in priority order, `pipeline_engine.run_rule()` executes pre-steps then the business condition → first satisfied rule closes the exception (reason code + commentary); if none do, it's alerted.

## 7. Data Model Summary

- **Rule**: `rule_id, name, product, version, status, severity, owner, mode (direct|enriched), pre_steps[], priority, condition, reason_code_id, commentary, created_at, updated_at, notes`
- **Pre-step (enrich)**: `step_id, apply_when?, source_name, join_on, fields{}, field_types{}, derive[]`
- **Pre-step (pivot)**: `step_id, apply_when?, group_by[], agg_function, agg_field, output_field, derive[]`
- **Condition** / **CompositeCondition**: recursive `{field, operator, value}` / `{logic: AND|OR, conditions[]}`

## 8. Assumptions, Dependencies, and TBDs

- Reference source data (`issuer_credit_events`, `manual_override_log`, `counterparty_master`, `fx_benchmark_rates`) — production system-of-record is **TBD**.
- Exception dataset ingestion (the "upload" step) — format and storage location **TBD**.
- Execution trigger for batch/scheduled validation (vs. this document's user-initiated Run Validation) — **TBD**.
- Closure write-back target (case management system) — **TBD**.
- Maker-checker / segregation of duties on `promote` — **TBD**, not currently enforced.
- Reason-code master list validation — **TBD**, currently free text.
- **Known limitation surfaced in testing:** commentary is static text with `{field}` substitution; it does not automatically reflect a later amendment to the condition's threshold value (e.g. changing 10bps→14bps doesn't update wording that says "10bps"). Author must update commentary text manually alongside any such amendment.

## 9. Traceability (requirement → proof)

Every FR above was exercised by an automated test: `test_pipeline.py` (21 checks — FR1–FR6), `test_dynamic_adaptation.py` / `test_generic_validator.py` (16 + 6 checks — FR8, FR9, FR12, FR13), and two real headless-browser runs (`test_unified_console.js`, `test_exception_analysis.js` — 19 + 17 checks) covering FR7, FR10, FR11, FR14 end-to-end through the actual UI, not just the underlying functions.
