import { useCallback, useEffect, useState } from "react";
import { Plus, Sparkles, MousePointer2 } from "lucide-react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";

function emptyWorkflow() {
  // Starts with zero nodes — a workflow needs no INPUT/Filter/Lookup/etc.
  // to be valid; a rule can be built from just a Condition + Outcome node
  // (or even a bare Condition, for a dry-run-only check). The palette
  // below lets the author add exactly the steps this rule needs.
  return { nodes: [], edges: [] };
}

export default function RuleList({ onOpenRule }) {
  const { actor, role, isAdmin } = useActor();
  const [productFilter, setProductFilter] = useState("");
  const { loading, data, error, reload } = useAsync(
    useCallback(() => rd.rules(productFilter || undefined), [productFilter]), [productFilter],
  );
  const products = useAsync(useCallback(() => rd.products(), []), []);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newProduct, setNewProduct] = useState("");
  const [nextRuleId, setNextRuleId] = useState("");
  const [mode, setMode] = useState("visual");
  const [createError, setCreateError] = useState(null);
  const [toggleError, setToggleError] = useState(null);

  async function toggleRuleEnabled(e, r) {
    e.stopPropagation(); // the row itself opens the rule — don't also navigate
    if (!isAdmin) return;
    setToggleError(null);
    try {
      await rd.setRuleEnabled(r.rule_id, actor, role, !r.enabled);
      await reload();
    } catch (err) {
      setToggleError(String(err.message || err));
    }
  }

  const productList = products.data?.products || [];
  useEffect(() => { if (productList.length && !newProduct) setNewProduct(productList[0].code); }, [productList, newProduct]);

  // The rule's identifier is never hand-typed — it's OAR-{PRODUCT}-NNN,
  // computed server-side from that product's APPROVED/PUBLISHED rule
  // count, so it stays in the same namespace as migrated {product}_validator.py
  // rules (e.g. OAR-PM-001). Re-fetched whenever the product changes.
  useEffect(() => {
    if (!newProduct) { setNextRuleId(""); return; }
    let cancelled = false;
    rd.nextRuleId(newProduct).then((r) => { if (!cancelled) setNextRuleId(r.rule_id); }).catch(() => {});
    return () => { cancelled = true; };
  }, [newProduct]);

  async function createRule() {
    if (!newName.trim() || !newProduct || !nextRuleId) return;
    setCreateError(null);
    const rule = {
      rule_id: nextRuleId, product: newProduct, name: newName.trim(), description: "", priority: 100,
      authoring_mode: mode, workflow: emptyWorkflow(), required_columns: [],
    };
    try {
      const created = await rd.createRule(actor, role, rule);
      setCreating(false); setNewName("");
      await reload();
      onOpenRule(created.rule_id);
    } catch (e) {
      setCreateError(String(e.message || e));
      // The computed id can go stale if another admin published a rule in
      // between — refresh it so the next attempt uses a fresh number.
      rd.nextRuleId(newProduct).then((r) => setNextRuleId(r.rule_id)).catch(() => {});
    }
  }

  if (loading) return <Loader label="Loading rules…" />;
  if (error) return <ErrorState error={error} />;
  const rules = data?.rules || [];

  return (
    <>
      <ModuleHeader
        title="Rules"
        description="Every rule belongs to a product, and compiles into the same canonical workflow model however it was authored."
        actions={isAdmin ? (
          <button className="btn" onClick={() => setCreating(true)}><Plus size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />New rule</button>
        ) : null}
      />

      <div className="controls controls--row" style={{ marginBottom: 14 }}>
        <label className="control"><span>Product</span>
          <select value={productFilter} onChange={(e) => setProductFilter(e.target.value)}>
            <option value="">all products</option>
            {productList.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
          </select>
        </label>
      </div>

      {toggleError && <div className="errorbox">{toggleError}</div>}

      {creating && (
        <Card title="New rule">
          {createError && <div className="errorbox">{createError}</div>}
          <div className="controls controls--row">
            <label className="control"><span>Product</span>
              <select value={newProduct} onChange={(e) => setNewProduct(e.target.value)}>
                {productList.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
              </select>
            </label>
            <label className="control"><span>Rule ID</span>
              <input className="mono" value={nextRuleId || "…"} disabled readOnly title="Auto-assigned from this product's approved rule count" />
            </label>
            <label className="control" style={{ minWidth: 280 }}>
              <span>Name</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. USD FX Deviation — High Risk" />
            </label>
            <div className="control">
              <span>Authoring mode</span>
              <div className="tabs">
                <button className={`tab ${mode === "visual" ? "active" : ""}`} onClick={() => setMode("visual")}>
                  <MousePointer2 size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Visual builder
                </button>
                <button className={`tab ${mode === "freetext" ? "active" : ""}`} onClick={() => setMode("freetext")}>
                  <Sparkles size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Free text / AI
                </button>
              </div>
            </div>
            <button className="btn" onClick={createRule} disabled={!newName.trim() || !newProduct || !nextRuleId}>Create</button>
            <button className="btn btn--ghost" onClick={() => setCreating(false)}>Cancel</button>
          </div>
          <p className="empty-hint">
            The rule ID is assigned automatically from the selected product — OAR-{"{PRODUCT}"}-NNN, counting only
            that product's approved/published rules. Either authoring mode produces the same rule — the AI Builder
            interprets your text into this same visual workflow, which you review and edit before anything runs.
          </p>
        </Card>
      )}

      <Card>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Rule</th><th>Product</th><th>Enabled</th><th>Reason code</th><th>What this rule does</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.rule_id} className="rd-clickable-row" onClick={() => onOpenRule(r.rule_id)}>
                  <td style={{ minWidth: 220, whiteSpace: "nowrap" }}>
                    <div>{r.name}</div>
                    <div className="mono ds-id">{r.rule_id}</div>
                    <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                      <span className={`rd-status rd-status--${r.status.toLowerCase()}`}>{r.status.replace(/_/g, " ")}</span>
                      <span className="mono" style={{ fontSize: 11, color: "var(--muted, #5b6775)" }}>priority {r.priority}</span>
                    </div>
                  </td>
                  <td className="mono">{r.product}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <label className="control" style={{ flexDirection: "row", alignItems: "center", gap: 6, minWidth: 0 }}>
                      <input type="checkbox" checked={r.enabled} disabled={!isAdmin} onChange={(e) => toggleRuleEnabled(e, r)} />
                    </label>
                  </td>
                  <td className="mono">
                    {r.reason_code ? r.reason_code : <span style={{ fontStyle: "italic", color: "var(--muted, #5b6775)" }}>(dynamic)</span>}
                  </td>
                  <td className="rd-summary-cell" title={r.summary}>{r.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rules.length === 0 && <p className="empty-hint" style={{ padding: 16 }}>No rules yet.</p>}
        </div>
      </Card>
    </>
  );
}
