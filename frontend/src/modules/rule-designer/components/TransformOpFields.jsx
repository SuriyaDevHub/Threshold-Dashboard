import { Plus, Trash2 } from "lucide-react";

// Shared transform-op selector + per-op parameter fields — one
// implementation used both by the Transform workflow node (NodeConfigPanel)
// and by LOOKUP field mappings (LookupConfigForm), so a value looked up
// from a reference file can be cleaned up with exactly the same operations
// available on any other field.
//
// `transform` is the caller's whole transform config object — this
// component only reads/writes `op` and the op-specific param keys below,
// leaving any other keys the caller stores on it (e.g. `field`,
// `output_field`) untouched. `onChange(next)` always receives the
// complete replacement object (or null when "no transform" is chosen,
// only reachable with `allowNone`) — never a partial patch — so the
// caller can set it directly without re-merging.

const OP_PARAM_KEYS = ["precision", "start", "end", "delimiter", "index", "find", "replace_with", "rules", "default"];

const TRANSFORM_OPS = [
  { value: "round", label: "round" },
  { value: "upper", label: "upper" },
  { value: "lower", label: "lower" },
  { value: "trim", label: "trim" },
  { value: "substring", label: "substring" },
  { value: "split", label: "split" },
  { value: "replace", label: "find & replace" },
  { value: "cast_numeric", label: "cast to number" },
  { value: "cast_string", label: "cast to text" },
  { value: "value_map", label: "map text to a value" },
];

export default function TransformOpFields({ transform, onChange, allowNone }) {
  const op = transform?.op || (allowNone ? "" : "round");

  function set(patch) { onChange({ ...transform, ...patch }); }

  function setOp(nextOp) {
    if (!nextOp) { onChange(null); return; }
    // Reset the op-specific params on switch — leftover start/end/
    // delimiter/etc. from a previous op would otherwise silently carry
    // over and confuse the next one — while preserving every other key
    // the caller keeps on this object (field, output_field, ...).
    const next = { ...transform, op: nextOp };
    for (const key of OP_PARAM_KEYS) delete next[key];
    onChange(next);
  }

  return (
    <>
      <select value={op} onChange={(e) => setOp(e.target.value)}>
        {allowNone && <option value="">no transform</option>}
        {TRANSFORM_OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>

      {op === "round" && (
        <label className="control"><span>Decimal places</span>
          <input type="number" min="0" value={transform?.precision ?? 2}
                 onChange={(e) => set({ precision: e.target.value })} />
        </label>
      )}

      {op === "substring" && (
        <>
          <label className="control"><span>Start index</span>
            <input type="number" value={transform?.start ?? 0}
                   onChange={(e) => set({ start: e.target.value })} />
          </label>
          <label className="control"><span>End index (optional)</span>
            <input type="number" value={transform?.end ?? ""}
                   onChange={(e) => set({ end: e.target.value })} />
          </label>
        </>
      )}

      {op === "split" && (
        <>
          <label className="control"><span>Delimiter</span>
            <input value={transform?.delimiter ?? ","} onChange={(e) => set({ delimiter: e.target.value })} />
          </label>
          <label className="control"><span>Segment index</span>
            <input type="number" value={transform?.index ?? 0}
                   onChange={(e) => set({ index: e.target.value })} />
          </label>
        </>
      )}

      {op === "replace" && (
        <>
          <label className="control"><span>Find</span>
            <input value={transform?.find ?? ""} onChange={(e) => set({ find: e.target.value })} />
          </label>
          <label className="control"><span>Replace with</span>
            <input value={transform?.replace_with ?? ""} onChange={(e) => set({ replace_with: e.target.value })} />
          </label>
        </>
      )}

      {op === "value_map" && (
        <ValueMapFields transform={transform} onChange={set} />
      )}
    </>
  );
}

function ValueMapFields({ transform, onChange }) {
  const rules = transform?.rules || [];

  function updateRule(i, patch) {
    const next = [...rules]; next[i] = { ...next[i], ...patch }; onChange({ rules: next });
  }
  function addRule() { onChange({ rules: [...rules, { contains: "", value: "" }] }); }
  function removeRule(i) { onChange({ rules: rules.filter((_, idx) => idx !== i) }); }

  return (
    <div style={{ width: "100%" }}>
      {rules.map((r, i) => (
        <div className="lk-row" key={i} style={{ marginTop: i === 0 ? 0 : 6 }}>
          <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>if contains</span>
          <input placeholder="e.g. LONDON" value={r.contains ?? ""}
                 onChange={(e) => updateRule(i, { contains: e.target.value })} />
          <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>→</span>
          <input placeholder="e.g. LN" value={r.value ?? ""}
                 onChange={(e) => updateRule(i, { value: e.target.value })} />
          <button className="icon-btn" onClick={() => removeRule(i)}><Trash2 size={13} /></button>
        </div>
      ))}
      <button className="btn btn--ghost btn--xs" style={{ marginTop: 6 }} onClick={addRule}>
        <Plus size={13} /> Add rule
      </button>
      <label className="control" style={{ marginTop: 6 }}><span>Default (no rule matches)</span>
        <input placeholder="leave blank to keep the original value" value={transform?.default ?? ""}
               onChange={(e) => onChange({ default: e.target.value })} />
      </label>
    </div>
  );
}

export function transformHint(op) {
  if (op === "substring") return 'e.g. start 0, end 3 keeps the first 3 characters. Leave end blank to go to the end of the text.';
  if (op === "split") return 'e.g. splitting "HKFXO_052" on "_" at index 1 gives "052".';
  if (op === "value_map") return 'Checked top to bottom — the first rule whose text is found (case-insensitive) anywhere in the value wins, e.g. "London (LN) Branch" containing "LONDON" maps to "LN".';
  return null;
}
