import { Plus, Trash2 } from "lucide-react";
import { withCurrent } from "./ConditionBuilder.jsx";

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

const OP_PARAM_KEYS = ["precision", "start", "end", "delimiter", "index", "find", "replace_with", "rules", "default",
                       "input_format", "custom_pattern", "unit", "amount", "fields", "separator",
                       "spt_days", "snx_days", "key", "pair_delimiter", "kv_delimiter"];

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
  { value: "parse_date", label: "parse date" },
  { value: "date_shift", label: "shift date" },
  { value: "normalize_tenor", label: "normalize tenor" },
  { value: "extract_kv", label: "extract key:value" },
];

// Must match transform_ops.py's _shift_date() units exactly.
const DATE_SHIFT_UNITS = [
  { value: "days", label: "days" },
  { value: "weeks", label: "weeks" },
  { value: "months", label: "months" },
  { value: "years", label: "years" },
];

// Must match transform_ops.py's DATE_INPUT_FORMATS keys exactly, plus the
// two epoch literals and "custom" that _parse_date() also recognizes.
const DATE_INPUT_FORMATS = [
  { value: "mdy", label: "MM/DD/YYYY" },
  { value: "dmy", label: "DD/MM/YYYY" },
  { value: "dmy_dash", label: "DD-MM-YYYY" },
  { value: "d_mon_y", label: "DD-Mon-YYYY (15-Jan-2026)" },
  { value: "ymd_slash", label: "YYYY/MM/DD" },
  { value: "epoch_seconds", label: "Epoch seconds" },
  { value: "epoch_millis", label: "Epoch milliseconds" },
  { value: "custom", label: "Custom pattern…" },
];

export default function TransformOpFields({ transform, onChange, allowNone, fields }) {
  const op = transform?.op || (allowNone ? "" : "round");
  // "concatenate fields" needs a list of this node's available source
  // fields to pick from — only the TRANSFORM node's own editor
  // (NodeConfigPanel.jsx) passes `fields`; LOOKUP's two callers (field
  // mappings, join-key transforms) operate on one already-pulled value
  // and have no use for a field picker, so the op simply never appears
  // there.
  const ops = fields ? [{ value: "concat", label: "concatenate fields" }, ...TRANSFORM_OPS] : TRANSFORM_OPS;

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
        {ops.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>

      {op === "concat" && (
        <ConcatFields transform={transform} onChange={set} fields={fields} />
      )}

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

      {op === "parse_date" && (
        <>
          <label className="control"><span>Input format</span>
            <select value={transform?.input_format || ""} onChange={(e) => set({ input_format: e.target.value })}>
              <option value="" disabled>format…</option>
              {DATE_INPUT_FORMATS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </select>
          </label>
          {transform?.input_format === "custom" && (
            <label className="control"><span>strptime pattern</span>
              <input placeholder="e.g. %d.%m.%Y" value={transform?.custom_pattern ?? ""}
                     onChange={(e) => set({ custom_pattern: e.target.value })} />
            </label>
          )}
        </>
      )}

      {op === "date_shift" && (
        <>
          <label className="control"><span>Amount</span>
            <input type="number" placeholder="e.g. -1" value={transform?.amount ?? ""}
                   onChange={(e) => set({ amount: e.target.value })} />
          </label>
          <select value={transform?.unit || ""} onChange={(e) => set({ unit: e.target.value })}>
            <option value="" disabled>unit…</option>
            {DATE_SHIFT_UNITS.map((u) => <option key={u.value} value={u.value}>{u.label}</option>)}
          </select>
        </>
      )}

      {op === "normalize_tenor" && (
        <>
          <label className="control"><span>SPT lag (days, optional)</span>
            <input type="number" min="0" placeholder="2" value={transform?.spt_days ?? ""}
                   onChange={(e) => set({ spt_days: e.target.value })} />
          </label>
          <label className="control"><span>SNX lag (days, optional)</span>
            <input type="number" min="0" placeholder="3" value={transform?.snx_days ?? ""}
                   onChange={(e) => set({ snx_days: e.target.value })} />
          </label>
        </>
      )}

      {op === "extract_kv" && (
        <>
          <label className="control"><span>Key</span>
            <input placeholder="e.g. BENCHMARK_DEVIATION" value={transform?.key ?? ""}
                   onChange={(e) => set({ key: e.target.value })} />
          </label>
          <label className="control"><span>Pair delimiter (optional)</span>
            <input placeholder="|" value={transform?.pair_delimiter ?? ""}
                   onChange={(e) => set({ pair_delimiter: e.target.value })} />
          </label>
          <label className="control"><span>Key:value delimiter (optional)</span>
            <input placeholder=":" value={transform?.kv_delimiter ?? ""}
                   onChange={(e) => set({ kv_delimiter: e.target.value })} />
          </label>
        </>
      )}
    </>
  );
}

function ConcatFields({ transform, onChange, fields }) {
  // Same "+ combine column" multi-picker idiom already shipped in
  // LookupConfigForm.jsx's join-key reference side, for a consistent UX
  // wherever this app lets you build a value from several columns.
  const cols = transform?.fields || [];

  function updateCol(i, value) {
    const next = [...cols]; next[i] = value; onChange({ fields: next });
  }
  function addCol() { onChange({ fields: [...cols, fields[0]?.field || ""] }); }
  function removeCol(i) { onChange({ fields: cols.filter((_, idx) => idx !== i) }); }

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
      {cols.map((c, i) => (
        <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          {i > 0 && <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>+</span>}
          <select value={c} onChange={(e) => updateCol(i, e.target.value)}>
            <option value="" disabled>field…</option>
            {withCurrent(fields, c).map((f) => (
              <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
            ))}
          </select>
          {cols.length > 1 && (
            <button className="icon-btn" onClick={() => removeCol(i)}><Trash2 size={11} /></button>
          )}
        </span>
      ))}
      <button className="btn btn--ghost btn--xs" onClick={addCol}><Plus size={13} /> Add field</button>
      <label className="control" style={{ minWidth: 120 }}><span>Separator (optional)</span>
        <input value={transform?.separator ?? ""} onChange={(e) => onChange({ separator: e.target.value })} placeholder="none" />
      </label>
    </span>
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
  if (op === "concat") return 'Builds the output field by joining the selected fields\' values together, in order — leave the separator blank to join them directly, or set one (e.g. "-" or "_") to insert between each. Handy for materializing a composite key (e.g. TradingLocation+Currency+Curve+Tenor) as an actual field that other rules or a LOOKUP\'s join key can then match against directly.';
  if (op === "substring") return 'e.g. start 0, end 3 keeps the first 3 characters. Leave end blank to go to the end of the text.';
  if (op === "split") return 'e.g. splitting "HKFXO_052" on "_" at index 1 gives "052".';
  if (op === "value_map") return 'Checked top to bottom — the first rule whose text is found (case-insensitive) anywhere in the value wins, e.g. "London (LN) Branch" containing "LONDON" maps to "LN".';
  if (op === "parse_date") return "Output is always stored as YYYY-MM-DD, so it can be compared with before/after/on against any other date field — including one that never needed a transform.";
  if (op === "date_shift") return 'Negative amount shifts earlier, positive shifts later (e.g. months, -1 = "one month before"). Months/years shift by calendar month, not a fixed day count (Mar 31 minus 1 month lands on Feb 28). Combine with a "before"/"after" condition against this field to build thresholds like "more than one month before COB date": shift COB date back 1 month, then check trade date is before the result.';
  if (op === "normalize_tenor") return 'Normalizes a tenor to one short code ("1M", "3Y", "6M", ...) regardless of how it\'s represented on input — a plain number is read as a day count, strings like "1WK"/"18M"/"3Y6M" are parsed (D/W/M/Y units, decimals and mixed components allowed), and TDY/TOM/SPT/SNX/O_N are simple calendar-day approximations (not a real holiday calendar) — SPT/SNX default to 2/3 days unless overridden below. Put this directly on a LOOKUP join key\'s own transform to make a numeric dataset Tenor match a reference file\'s normalized Tenor column, with no separate TRANSFORM node needed.';
  if (op === "extract_kv") return 'Pulls one named value out of a packed "KEY:VALUE|KEY:VALUE|..." column (e.g. a threshold-config blob) by key, regardless of where that key falls in the string. A missing key becomes blank, not an error. The result is always text (e.g. "0.05") — chain a second "cast to number" step afterward if you need it numeric for a condition or calculation.';
  return null;
}
