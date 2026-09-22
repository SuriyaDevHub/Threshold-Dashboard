import { useEffect } from "react";
import { Plus, Trash2, X } from "lucide-react";
import ConditionBuilder, { newGroup } from "./ConditionBuilder.jsx";
import LookupConfigForm from "./LookupConfigForm.jsx";

const NODE_TYPE_LABEL = {
  input: "Input dataset", filter: "Filter", lookup: "Lookup / Enrichment",
  enrichment: "Lookup / Enrichment", calculate: "Calculated field", condition: "Business condition",
  group: "Condition group", transform: "Transform", outcome: "Outcome", validation: "Validation",
};

// Every OUTCOME node reads/writes these three fields through dedicated
// controls (spec: the rule author decides "if this rule passes, what
// commentary/reason code gets populated and whether it's alert/clear" —
// not a generic key=value list). Any other outcome field (e.g. a legacy
// rule's custom Risk_Level) still goes through the generic editor below.
const STATUS_FIELD = "Alert";
const REASON_FIELD = "Reason";
const COMMENTARY_FIELD = "Commentary";
const RESERVED_OUTCOME_FIELDS = [STATUS_FIELD, REASON_FIELD, COMMENTARY_FIELD];

function OutcomeEditor({ outcomes, onChange, fields }) {
  const actions = outcomes || [];
  const statusAction = actions.find((a) => a.field === STATUS_FIELD);
  const reasonAction = actions.find((a) => a.field === REASON_FIELD);
  const commentaryAction = actions.find((a) => a.field === COMMENTARY_FIELD);
  // No Alert action yet (a brand-new outcome node) defaults to "alert" —
  // a rule whose conditions matched is an alert unless the author
  // explicitly chooses Clear (e.g. an allow-list/exception rule).
  const status = !statusAction || statusAction.value?.value !== false ? "alert" : "clear";

  function upsertReserved(field, value) {
    const rest = actions.filter((a) => a.field !== field);
    onChange(value === null ? rest : [...rest, { field, value }]);
  }
  function setStatus(next) { upsertReserved(STATUS_FIELD, { type: "static", value: next === "alert" }); }
  function setReason(text) { upsertReserved(REASON_FIELD, text ? { type: "template", value: text } : null); }
  function setCommentary(text) { upsertReserved(COMMENTARY_FIELD, text ? { type: "template", value: text } : null); }

  function update(i, patch) { const a = [...actions]; a[i] = { ...a[i], ...patch }; onChange(a); }
  function add() { onChange([...actions, { field: "", value: { type: "static", value: "" } }]); }
  function remove(i) { onChange(actions.filter((_, idx) => idx !== i)); }

  return (
    <div className="lk-block">
      <div className="lk-block-title">Outcome when this rule matches</div>
      <label className="control">
        <span>Status</span>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="alert">Alert</option>
          <option value="clear">Clear</option>
        </select>
      </label>
      <label className="control">
        <span>Reason code</span>
        <input className="mono" placeholder="e.g. OAR-PM-001" value={reasonAction?.value?.value ?? ""}
               onChange={(e) => setReason(e.target.value)} />
      </label>
      <label className="control">
        <span>Commentary</span>
        <textarea rows={3} placeholder="e.g. Deviation for {currency} exceeds the applicable threshold."
                   value={commentaryAction?.value?.value ?? ""}
                   onChange={(e) => setCommentary(e.target.value)} />
      </label>
      <p className="empty-hint">
        Reason code and commentary support {"{field}"} placeholders resolved against the record when the rule
        matches (e.g. "Region {"{region}"} breached."). Plain text with no placeholders works unchanged.
        Available fields at this stage: {fields.map((f) => f.field).join(", ") || "(none yet)"}.
      </p>

      {actions.some((a) => !RESERVED_OUTCOME_FIELDS.includes(a.field)) && (
        <div className="lk-block-title" style={{ marginTop: 12 }}>Additional outcome fields</div>
      )}
      {actions.map((a, i) => RESERVED_OUTCOME_FIELDS.includes(a.field) ? null : (
        <div className="lk-row" key={i}>
          <input placeholder="field (e.g. Risk_Level)" value={a.field}
                 onChange={(e) => update(i, { field: e.target.value })} />
          <span className="mono">=</span>
          <select value={a.value?.type === "column" ? "field" : "static"}
                  onChange={(e) => update(i, { value: e.target.value === "field"
                    ? { type: "column", name: fields[0]?.field || "" } : { type: "static", value: "" } })}>
            <option value="static">value</option>
            <option value="field">field</option>
          </select>
          {a.value?.type === "column" ? (
            <select value={a.value?.name || ""} onChange={(e) => update(i, { value: { type: "column", name: e.target.value } })}>
              {fields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
            </select>
          ) : (
            <input value={a.value?.value ?? ""} onChange={(e) => {
              const raw = e.target.value; const num = Number(raw);
              update(i, { value: { type: "static", value: raw !== "" && !Number.isNaN(num) && /^-?[\d.]+$/.test(raw) ? num : raw } });
            }} />
          )}
          <button className="icon-btn" onClick={() => remove(i)}><Trash2 size={13} /></button>
        </div>
      ))}
      <button className="btn btn--ghost btn--xs" onClick={add}><Plus size={13} /> Add custom outcome field</button>
    </div>
  );
}

export default function NodeConfigPanel({ node, fields, meta, onChange, onClose, onDelete }) {
  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!node) return null;

  function set(patch) { onChange({ ...node, ...patch }); }

  return (
    <div className="rd-panel">
      <div className="rd-panel-head">
        <div>
          <div className="rd-panel-type">{NODE_TYPE_LABEL[node.type] || node.type}</div>
          <input className="rd-panel-label" value={node.label} onChange={(e) => set({ label: e.target.value })} />
        </div>
        <div className="rd-panel-head-actions">
          {/* Every node type is optional and deletable, including INPUT —
              a workflow needs no fixed shape; the engine treats a missing
              INPUT node as an implicit start (see validation_service.py). */}
          <button className="icon-btn" title="Delete node" onClick={onDelete}><Trash2 size={15} /></button>
          <button className="icon-btn" title="Close" onClick={onClose}><X size={16} /></button>
        </div>
      </div>

      <div className="rd-panel-body">
        {node.type === "input" && (
          <p className="empty-hint">The dataset selected in the Dry Run / Impact tabs flows in here.</p>
        )}

        {(node.type === "filter" || node.type === "group") && (
          <ConditionBuilder group={node.filter || newGroup()} onChange={(g) => set({ filter: g })} fields={fields} meta={meta} />
        )}

        {(node.type === "lookup" || node.type === "enrichment") && (
          <LookupConfigForm lookup={node.lookup} onChange={(l) => set({ lookup: l })} sourceFields={fields} />
        )}

        {node.type === "calculate" && (
          <div className="lk-block">
            <label className="control"><span>Output field name</span>
              <input value={node.calculate?.output_field || ""}
                     onChange={(e) => set({ calculate: { ...node.calculate, output_field: e.target.value } })} />
            </label>
            <label className="control"><span>Expression</span>
              <input className="mono" placeholder="e.g. Notional * Price, or abs(a - b) / b * 100"
                     value={node.calculate?.expression || ""}
                     onChange={(e) => set({ calculate: { ...node.calculate, expression: e.target.value } })} />
            </label>
            <p className="empty-hint">
              Available fields at this stage: {fields.map((f) => f.field).join(", ") || "(none yet)"}.
              Only +, -, *, /, %, **, comparisons and abs/round/min/max/sqrt/len are allowed — never arbitrary code.
            </p>
          </div>
        )}

        {node.type === "condition" && (
          <ConditionBuilder group={node.condition || newGroup()} onChange={(g) => set({ condition: g })} fields={fields} meta={meta} />
        )}

        {node.type === "outcome" && (
          <OutcomeEditor outcomes={node.outcomes} onChange={(o) => set({ outcomes: o })} fields={fields} />
        )}

        {node.type === "transform" && (
          <div className="lk-block">
            <div className="lk-row">
              <select value={node.transform?.field || ""} onChange={(e) => set({ transform: { ...node.transform, field: e.target.value } })}>
                <option value="" disabled>field…</option>
                {fields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
              </select>
              <select value={node.transform?.op || "round"} onChange={(e) => set({ transform: { ...node.transform, op: e.target.value } })}>
                <option value="round">round</option>
                <option value="upper">upper</option>
                <option value="lower">lower</option>
                <option value="cast_numeric">cast to number</option>
                <option value="cast_string">cast to text</option>
              </select>
              <input placeholder="output field (optional)" value={node.transform?.output_field || ""}
                     onChange={(e) => set({ transform: { ...node.transform, output_field: e.target.value } })} />
            </div>
          </div>
        )}

        {node.type === "validation" && (
          <div className="lk-block">
            <label className="control"><span>Required columns (comma-separated)</span>
              <input value={(node.validation?.required_columns || []).join(", ")}
                     onChange={(e) => set({ validation: { required_columns: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} />
            </label>
          </div>
        )}
      </div>
    </div>
  );
}
