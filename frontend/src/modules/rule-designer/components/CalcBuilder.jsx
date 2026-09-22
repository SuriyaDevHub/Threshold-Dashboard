import { Plus, Trash2 } from "lucide-react";

// CALCULATE-node formula editor — a recursive operand tree built entirely
// by picking a field, a constant, or a nested operation, never by typing.
// Mirrors ConditionBuilder's recursive group/child shape (cb-group,
// cb-row, cb-child) so it looks and behaves the same as every other
// "select the columns based on the operation" builder in this app
// (conditions, lookup join keys, transforms). `formula` is the operation
// tree calc_ops.py evaluates server-side: {kind:"operation", op,
// operands:[...], precision?} where each operand is itself {kind:"field",
// field} | {kind:"constant", value} | {kind:"operation", ...}.

const CALC_OPS = [
  { value: "add", label: "add (sum)", arity: "n_ary" },
  { value: "subtract", label: "subtract", arity: "n_ary" },
  { value: "multiply", label: "multiply", arity: "n_ary" },
  { value: "divide", label: "divide", arity: "n_ary" },
  { value: "modulus", label: "modulus (remainder)", arity: "n_ary" },
  { value: "min", label: "minimum", arity: "n_ary" },
  { value: "max", label: "maximum", arity: "n_ary" },
  { value: "abs", label: "absolute value", arity: "unary" },
  { value: "round", label: "round", arity: "unary" },
  { value: "sqrt", label: "square root", arity: "unary" },
];
const ARITY = Object.fromEntries(CALC_OPS.map((o) => [o.value, o.arity]));
const OP_SYMBOL = { add: "+", subtract: "-", multiply: "*", divide: "/", modulus: "%" };

function newFieldOperand(fields) {
  return { kind: "field", field: fields[0]?.field || "" };
}
function newConstantOperand() {
  return { kind: "constant", value: 0 };
}
function newOperationOperand(fields) {
  return { kind: "operation", op: "add", operands: [newFieldOperand(fields), newFieldOperand(fields)] };
}

export function describeFormula(operand) {
  if (!operand) return "?";
  if (operand.kind === "field") return operand.field || "?";
  if (operand.kind === "constant") return String(operand.value ?? "?");
  if (operand.kind === "operation") {
    const parts = (operand.operands || []).map(describeFormula);
    if (OP_SYMBOL[operand.op]) return "(" + parts.join(` ${OP_SYMBOL[operand.op]} `) + ")";
    if (operand.op === "round") return `round(${parts[0] ?? "?"}, ${operand.precision ?? 2})`;
    return `${operand.op}(${parts.join(", ")})`;
  }
  return "?";
}

function OperandEditor({ operand, onChange, onRemove, fields, depth, canRemove }) {
  const kind = operand?.kind || "field";

  function setKind(nextKind) {
    if (nextKind === kind) return;
    if (nextKind === "field") onChange(newFieldOperand(fields));
    else if (nextKind === "constant") onChange(newConstantOperand());
    else onChange(newOperationOperand(fields));
  }

  return (
    <div className="cb-child">
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="cb-row" style={kind === "operation" ? { marginBottom: 6 } : undefined}>
          <select className="cb-value-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="field">Field</option>
            <option value="constant">Constant</option>
            <option value="operation">Formula</option>
          </select>
          {kind === "field" && (
            <select value={operand?.field || ""} onChange={(e) => onChange({ kind: "field", field: e.target.value })}>
              <option value="" disabled>field…</option>
              {fields.map((f) => <option key={f.field} value={f.field}>{f.field} ({f.type})</option>)}
            </select>
          )}
          {kind === "constant" && (
            <input type="number" value={operand?.value ?? 0}
                   onChange={(e) => onChange({ kind: "constant", value: Number(e.target.value) })} />
          )}
        </div>
        {kind === "operation" && (
          <CalcFormulaEditor formula={operand} onChange={onChange} fields={fields} depth={depth + 1} />
        )}
      </div>
      {canRemove && (
        <span className="cb-child-actions">
          <button className="icon-btn" title="Remove operand" onClick={onRemove}><Trash2 size={13} /></button>
        </span>
      )}
    </div>
  );
}

function CalcFormulaEditor({ formula, onChange, fields, depth = 0 }) {
  const op = formula?.op || "add";
  const arity = ARITY[op] || "n_ary";
  const operands = formula?.operands || [];

  function setOp(nextOp) {
    const nextArity = ARITY[nextOp];
    let nextOperands = operands;
    if (nextArity === "unary") {
      // Unary ops (abs/round/sqrt) always take exactly one operand —
      // switching from a multi-operand op keeps only the first so nothing
      // silently vanishes without the user noticing.
      nextOperands = operands.length ? [operands[0]] : [newFieldOperand(fields)];
    } else if (operands.length < 2) {
      const pad = Array.from({ length: 2 - operands.length }, () => newFieldOperand(fields));
      nextOperands = [...operands, ...pad];
    }
    const next = { kind: "operation", op: nextOp, operands: nextOperands };
    if (nextOp === "round") next.precision = formula?.precision ?? 2;
    onChange(next);
  }

  function updateOperand(i, nextOperand) {
    const o = [...operands]; o[i] = nextOperand; onChange({ ...formula, operands: o });
  }
  function removeOperand(i) {
    onChange({ ...formula, operands: operands.filter((_, idx) => idx !== i) });
  }
  function addOperand() {
    onChange({ ...formula, operands: [...operands, newFieldOperand(fields)] });
  }

  return (
    <div className={`cb-group depth-${depth % 4}`}>
      <div className="cb-group-head">
        <select className="cb-op" value={op} onChange={(e) => setOp(e.target.value)}>
          {CALC_OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {op === "round" && (
          <span className="cb-value">
            <span className="cb-and">decimals</span>
            <input type="number" min="0" style={{ width: 56 }} value={formula?.precision ?? 2}
                   onChange={(e) => onChange({ ...formula, precision: Number(e.target.value) })} />
          </span>
        )}
        {arity === "n_ary" && (
          <button className="btn btn--ghost btn--xs" onClick={addOperand}><Plus size={13} /> Operand</button>
        )}
      </div>
      <div className="cb-children">
        {operands.length === 0 && <div className="cb-empty">No operands yet — add one above.</div>}
        {operands.map((operand, i) => (
          <OperandEditor key={i} operand={operand} onChange={(next) => updateOperand(i, next)}
                          onRemove={() => removeOperand(i)} fields={fields} depth={depth}
                          canRemove={arity === "n_ary" && operands.length > 2} />
        ))}
      </div>
    </div>
  );
}

export default function CalcBuilder({ formula, onChange, fields }) {
  const root = formula && formula.kind === "operation" ? formula : newOperationOperand(fields);
  return (
    <>
      <CalcFormulaEditor formula={root} onChange={onChange} fields={fields} depth={0} />
      <p className="empty-hint" style={{ marginTop: 8 }}>= {describeFormula(root)}</p>
    </>
  );
}
