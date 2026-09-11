# Requirement Prompt — Phase 2: Enrichment/Pivot Extension

Use this prompt as-is with your dev team or an AI coding assistant, against
your EXISTING Phase 1 codebase. It describes the extension generically (by
data shape and behavior, not by specific file names) so it applies
regardless of your current backend language/framework or React project
structure.

---

## Context — what Phase 1 already does

A rule-configuration tool exists. Per product, a user can create, edit,
validate, promote, disable, and deprecate a rule. A rule currently holds:
`rule_id, name, product, status, owner, condition` (a field/operator/value
tree, possibly AND/OR-nested) `, reason_code_id, commentary`. At runtime,
a validator applies each active rule's condition **directly** against an
exception record and, if satisfied, closes it with the rule's reason code
and commentary.

## Phase 2 objective

Extend both the rule config model and the React UI so a rule can
optionally run an ordered set of **pre-steps** — each either an
**enrichment** (pull fields in from an external source) or a **pivot**
(aggregate the exception dataset itself) — before its business condition
is evaluated. Each pre-step is independently, conditionally applicable.
The business condition, once pre-steps have run, may use any field the
pre-steps added, on top of the original product fields.

This is additive: a rule with no pre-steps must behave exactly as it does
today (backward compatible with every existing Phase 1 rule).

---

## Part 1 — Data model extension

Add to the rule config:

- `mode`: `"direct"` (today's behavior — no pre-steps) or `"enriched"`
  (runs `pre_steps` first). A rule with `pre_steps` must be `"enriched"`;
  a rule with none must be `"direct"`.
- `pre_steps`: an ordered list, each one of two shapes:
  - **Enrich step**: `{ step_id, apply_when?, source_name, join_on,
    fields: {source_column: output_field}, field_types?, derive? }`.
    `join_on` is a field on the exception record; the step looks up a
    matching row in the named external source and copies the mapped
    columns onto the record under the given output names.
  - **Pivot step**: `{ step_id, apply_when?, group_by: [fields], 
    agg_function: sum|count|avg|min|max, agg_field, output_field, 
    derive? }`. Aggregates the exception dataset by `group_by`, joining
    the aggregate value back onto every record in that group.
  - Both support `apply_when` — an optional condition (same shape as the
    business condition) that gates whether the step runs at all for a
    given record. Absent = always runs.
  - Both support `derive` — a list of `{output_field, op, a, b}` computed
    fields, `op` restricted to a small whitelist (e.g. `abs_diff, diff,
    sum, ratio, pct_diff`) over fields already present at that point.
    Never evaluate arbitrary user-supplied expressions.
- Add `priority` (integer, lower runs first) if not already present —
  needed once multiple active rules could both apply to one exception;
  first satisfied rule (in priority order) closes it.

## Part 2 — Validation extension (the critical part)

Fields become available **progressively** through the pipeline:
stage 0 = the product's base fields; after `pre_steps[i]` = stage i's
fields plus whatever that step declares it outputs. Enforce, at save
time, before any dry-run or promotion:

1. A step's `apply_when` may reference ONLY fields available **before**
   that step runs (base fields + prior steps' outputs). It must not
   reference that same step's own output — reject with a clear message
   naming the field and the pipeline stage if it does.
2. The business `condition` may reference base fields **plus every**
   pre-step's declared outputs, regardless of order.
3. An enrich step's `source_name` must be a registered/known source; its
   mapped source columns must exist in that source's schema; `join_on`
   must be an available field at that stage.
4. A pivot step's `group_by` fields and `agg_field` must be available at
   that stage; a non-`count` aggregation requires a numeric `agg_field`.
5. Operator-vs-field-type checks (numeric fields take
   `gt/gte/lt/lte/between`; categorical fields take `eq/ne/in/not_in`)
   apply to `apply_when` conditions exactly as they already do to the
   business condition.
6. `mode="direct"` with any `pre_steps` present is rejected;
   `mode="enriched"` with zero `pre_steps` is rejected.
7. Collect and return every validation error found, not just the first —
   the UI needs the full list to show the user at once.

## Part 3 — Execution engine extension

For a rule with `mode="enriched"`, before evaluating the business
condition on each record:

1. Precompute any pivot aggregates **once** per rule execution (group the
   whole dataset, don't recompute per record).
2. Run each pre-step in declared order:
   - If `apply_when` is present and not satisfied for this record → skip
     this step for this record. **This is not an error** — record it (for
     the trail/audit) and move to the next step.
   - Enrich: look up the join key in the source; if no match, skip (not
     an error) and record why.
   - Pivot: look up this record's aggregate group; if none, skip and
     record why.
   - Apply any `derive` computations using fields present at this point.
3. Evaluate the business condition against the fully prepared record
   (exactly as Phase 1 already does for direct rules).
4. Return, per record: the prepared field values, a trail of which steps
   ran/were skipped and why, and the closure decision (unchanged from
   Phase 1: reason code + rendered commentary on satisfy, else open).

## Part 4 — React UI extension

Add to the existing rule editor (do not replace the direct-mode editing
experience — it must still work unchanged for existing rules):

1. **Mode toggle** (Direct / Enriched) at the top of the condition
   section. Selecting Direct clears any pre-steps. Selecting Enriched
   reveals the pre-step builder below.
2. **Pre-step builder**: an ordered, reorderable list of step cards.
   Each card:
   - A step-type badge (Enrich / Pivot) and an editable `step_id`.
   - An **"Apply when"** control: collapsed by default showing "always
     runs"; expanding reveals a condition row (field/operator/value)
     whose **field dropdown is scoped to fields available before this
     step** — recompute this scope live as steps above are added, removed,
     or reordered.
   - Enrich card: source dropdown, join-field dropdown (scoped to
     available fields), and a repeatable "source column → output field
     name" mapping row list.
   - Pivot card: multi-select group-by fields (scoped to available
     fields), aggregation function dropdown, aggregation field dropdown
     (numeric fields only, unless function is `count`), output field name
     input.
   - A derived-fields sub-section per card: repeatable rows of output
     name / operation dropdown / two operand dropdowns, **operand
     dropdowns scoped to fields available after this step's own
     enrich/pivot output is added**.
   - Each card displays a small "Adds to the record: field1, field2…"
     summary so the author can see what downstream steps and the business
     condition will have access to.
   - Remove-step and add-step ("+ Enrich from source" / "+ Pivot /
     aggregate") controls.
3. **Business condition field dropdown**: extend its options to include
   every pre-step's declared output fields, recomputed live as the
   pipeline is edited — this is the piece that makes the enrichment
   useful, so don't let it lag behind pipeline edits.
4. **Dry-run / test panel**: extend the existing results table with a
   per-record **step trail** — which steps ran, which were skipped and
   why, what each one added — alongside the existing outcome/reason-code/
   commentary columns.
5. **Validation error display**: unchanged mechanism, just needs to
   surface the new error types from Part 2 (unknown field at a given
   stage, step referencing its own output, unknown source/column, etc.)
   in the same place existing validation errors already show.

## Part 5 — Non-functional / compatibility requirements

- **Backward compatible**: every existing Phase 1 rule (implicitly
  `mode="direct"`, no `pre_steps`) must load, edit, validate, dry-run, and
  execute identically to before this extension.
- **No eval() of user input** anywhere in the derive/condition evaluation
  path.
- Pre-step misses (no match, gate not satisfied) are **skips, not
  errors** — the pipeline continues and the exception simply doesn't get
  those fields, which typically (but not necessarily) means the business
  condition won't be satisfied.

## Part 6 — Testing requirements

1. Unit tests (backend): a rule with no pre-steps behaves identically
   before/after this change (regression); an enrich step with a
   conditional `apply_when` correctly skips for non-matching records and
   runs for matching ones; a pivot step's aggregate is computed once and
   correctly joined back per group; a derived field computes correctly
   from fields added earlier in the same pipeline; validation correctly
   rejects a step's `apply_when` referencing its own output, a business
   condition referencing a field no step produces, `direct` mode with
   pre-steps present, and `enriched` mode with none.
2. React UI tests: mode toggle correctly shows/hides the pre-step
   builder and clears pre-steps when switching to Direct; adding a step
   updates the business-condition field dropdown options live; removing
   or reordering a step correctly narrows a later step's or the business
   condition's available-fields scope; an existing Phase 1 rule (direct
   mode) opens and dry-runs with no visual or behavioral change.
3. Prefer a real browser-based test (not just component unit tests) for
   at least one full "build an enriched rule from scratch, dry-run it,
   see the step trail, save it" flow end to end.

## Acceptance criteria

- [ ] A rule can be authored with zero, one, or multiple pre-steps of
      either type, each independently conditional.
- [ ] Field availability is enforced by pipeline stage, both in backend
      validation and in what the React UI offers as selectable options.
- [ ] Every existing Phase 1 rule continues to work unchanged.
- [ ] The dry-run/test view shows a per-step trail, not just the final
      outcome.
- [ ] All new validation error types produce specific, actionable
      messages naming the field and the pipeline stage involved.
