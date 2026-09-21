import { Plus, Trash2, Copy, ChevronUp, ChevronDown } from "lucide-react";

// Recursive AND/OR/NOT condition-group editor (spec §5, §9). `group` is a
// ConditionGroup { kind:"group", operator, children:[Condition|Group] }.
// `fields` is [{field,type}] scoped to what's available at this workflow
// stage; `meta` is the /meta payload (operators per type + arity).

function uid(prefix) {
  return `${prefix}_${Math.random().toString(16).slice(2, 10)}`;
}

function newCondition(field) {
  return { kind: "condition", id: uid("cond"), field: field || "", operator: "eq",
           value: { type: "static", value: "" }, value2: null, values: null };
}

function newGroup(operator = "AND") {
  return { kind: "group", id: uid("grp"), operator, children: [] };
}

function fieldType(fields, name) {
  return fields.find((f) => f.field === name)?.type;
}

function opsForField(meta, fields, name) {
  const t = fieldType(fields, name);
  if (t && meta?.operators?.[t]) return meta.operators[t];
  return Object.values(meta?.operators || {}).flat().filter((v, i, a) => a.indexOf(v) === i);
}

function ValueEditor({ value, onChange, fields, placeholder }) {
  const kind = value?.type === "column" ? "field" : "static";
  return (
    <span className="cb-value">
      <select
        className="cb-value-kind"
        value={kind}
        onChange={(e) => onChange(e.target.value === "field"
          ? { type: "column", name: fields[0]?.field || "" }
          : { type: "static", value: "" })}
      >
        <option value="static">Value</option>
        <option value="field">Field</option>
      </select>
      {kind === "field" ? (
        <select value={value?.name || ""} onChange={(e) => onChange({ type: "column", name: e.target.value })}>
          {fields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
        </select>
      ) : (
        <input
          placeholder={placeholder}
          value={value?.value ?? ""}
          onChange={(e) => {
            const raw = e.target.value;
            const num = Number(raw);
            onChange({ type: "static", value: raw !== "" && !Number.isNaN(num) && /^-?[\d.]+$/.test(raw) ? num : raw });
          }}
        />
      )}
    </span>
  );
}

function ConditionRow({ cond, fields, meta, onChange, onDelete, onDuplicate }) {
  const arity = meta?.operator_arity?.[cond.operator] || "binary";
  const ops = opsForField(meta, fields, cond.field);

  function set(patch) { onChange({ ...cond, ...patch }); }

  return (
    <div className="cb-row">
      <select value={cond.field} onChange={(e) => set({ field: e.target.value })}>
        <option value="" disabled>field…</option>
        {fields.map((f) => <option key={f.field} value={f.field}>{f.field} ({f.type})</option>)}
      </select>
      <select value={cond.operator} onChange={(e) => set({ operator: e.target.value })}>
        {ops.map((op) => <option key={op} value={op}>{op.replace(/_/g, " ")}</option>)}
      </select>

      {arity === "binary" && (
        <ValueEditor value={cond.value} onChange={(v) => set({ value: v })} fields={fields} placeholder="value" />
      )}
      {arity === "between" && (
        <>
          <ValueEditor value={cond.value} onChange={(v) => set({ value: v })} fields={fields} placeholder="from" />
          <span className="cb-and">and</span>
          <ValueEditor value={cond.value2} onChange={(v) => set({ value2: v })} fields={fields} placeholder="to" />
        </>
      )}
      {arity === "list" && (
        <input
          placeholder="comma,separated,values"
          value={(cond.values || []).map((v) => v.value).join(", ")}
          onChange={(e) => set({ values: e.target.value.split(",").map((s) => s.trim()).filter(Boolean)
            .map((s) => ({ type: "static", value: s })) })}
        />
      )}

      <span className="cb-row-actions">
        <button className="icon-btn" title="Duplicate" onClick={onDuplicate}><Copy size={14} /></button>
        <button className="icon-btn" title="Delete" onClick={onDelete}><Trash2 size={14} /></button>
      </span>
    </div>
  );
}

export default function ConditionBuilder({ group, onChange, fields, meta, depth = 0 }) {
  const children = group?.children || [];

  function updateChild(i, next) {
    const c = [...children]; c[i] = next; onChange({ ...group, children: c });
  }
  function removeChild(i) {
    onChange({ ...group, children: children.filter((_, idx) => idx !== i) });
  }
  function duplicateChild(i) {
    const copy = JSON.parse(JSON.stringify(children[i]));
    copy.id = uid(copy.kind === "group" ? "grp" : "cond");
    onChange({ ...group, children: [...children.slice(0, i + 1), copy, ...children.slice(i + 1)] });
  }
  function move(i, dir) {
    const j = i + dir;
    if (j < 0 || j >= children.length) return;
    const c = [...children]; [c[i], c[j]] = [c[j], c[i]];
    onChange({ ...group, children: c });
  }
  function addCondition() {
    onChange({ ...group, children: [...children, newCondition(fields[0]?.field)] });
  }
  function addGroup() {
    onChange({ ...group, children: [...children, newGroup("AND")] });
  }

  return (
    <div className={`cb-group depth-${depth % 4}`}>
      <div className="cb-group-head">
        <select
          className="cb-op"
          value={group.operator}
          onChange={(e) => onChange({ ...group, operator: e.target.value })}
        >
          <option value="AND">ALL match (AND)</option>
          <option value="OR">ANY match (OR)</option>
          <option value="NOT">NOT</option>
        </select>
        <button className="btn btn--ghost btn--xs" onClick={addCondition}><Plus size={13} /> Condition</button>
        <button className="btn btn--ghost btn--xs" onClick={addGroup}><Plus size={13} /> Group</button>
      </div>
      <div className="cb-children">
        {children.length === 0 && <div className="cb-empty">No conditions yet — add one above.</div>}
        {children.map((child, i) => (
          <div className="cb-child" key={child.id}>
            {child.kind === "group" ? (
              <ConditionBuilder group={child} onChange={(g) => updateChild(i, g)} fields={fields} meta={meta} depth={depth + 1} />
            ) : (
              <ConditionRow cond={child} fields={fields} meta={meta} onChange={(c) => updateChild(i, c)}
                            onDelete={() => removeChild(i)} onDuplicate={() => duplicateChild(i)} />
            )}
            <span className="cb-child-actions">
              <button className="icon-btn" title="Move up" onClick={() => move(i, -1)}><ChevronUp size={13} /></button>
              <button className="icon-btn" title="Move down" onClick={() => move(i, 1)}><ChevronDown size={13} /></button>
              {child.kind === "group" && (
                <>
                  <button className="icon-btn" title="Duplicate group" onClick={() => duplicateChild(i)}><Copy size={13} /></button>
                  <button className="icon-btn" title="Delete group" onClick={() => removeChild(i)}><Trash2 size={13} /></button>
                </>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export { newCondition, newGroup };
