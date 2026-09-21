import { useCallback, useEffect, useState } from "react";
import { Plus, Sparkles, MousePointer2 } from "lucide-react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";
import { fmtDate } from "../format.js";

function emptyWorkflow() {
  return { nodes: [{ id: "n_input", type: "input", label: "Input dataset", position: { x: 0, y: 0 } }], edges: [] };
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
  const [mode, setMode] = useState("visual");
  const [createError, setCreateError] = useState(null);

  const productList = products.data?.products || [];
  useEffect(() => { if (productList.length && !newProduct) setNewProduct(productList[0].code); }, [productList, newProduct]);

  async function createRule() {
    if (!newName.trim() || !newProduct) return;
    setCreateError(null);
    const rule_id = newName.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_|_$/g, "") || `RULE_${Date.now()}`;
    const rule = {
      rule_id, product: newProduct, name: newName.trim(), description: "", priority: 100,
      authoring_mode: mode, workflow: emptyWorkflow(), required_columns: [],
    };
    try {
      const created = await rd.createRule(actor, role, rule);
      setCreating(false); setNewName("");
      await reload();
      onOpenRule(created.rule_id);
    } catch (e) {
      setCreateError(String(e.message || e));
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

      {creating && (
        <Card title="New rule">
          {createError && <div className="errorbox">{createError}</div>}
          <div className="controls controls--row">
            <label className="control" style={{ minWidth: 280 }}>
              <span>Name</span>
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. USD FX Deviation — High Risk" />
            </label>
            <label className="control"><span>Product</span>
              <select value={newProduct} onChange={(e) => setNewProduct(e.target.value)}>
                {productList.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
              </select>
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
            <button className="btn" onClick={createRule} disabled={!newName.trim() || !newProduct}>Create</button>
            <button className="btn btn--ghost" onClick={() => setCreating(false)}>Cancel</button>
          </div>
          <p className="empty-hint">
            Either mode produces the same rule — the AI Builder interprets your text into this same visual workflow,
            which you review and edit before anything runs.
          </p>
        </Card>
      )}

      <Card>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Rule</th><th>Product</th><th>Status</th><th>Priority</th><th>Version</th><th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.rule_id} className="rd-clickable-row" onClick={() => onOpenRule(r.rule_id)}>
                  <td><div>{r.name}</div><div className="mono ds-id">{r.rule_id}</div></td>
                  <td className="mono">{r.product}{!r.enabled && <span className="badge badge--breach" style={{ marginLeft: 6 }}>disabled</span>}</td>
                  <td><span className={`rd-status rd-status--${r.status.toLowerCase()}`}>{r.status.replace(/_/g, " ")}</span></td>
                  <td className="mono">{r.priority}</td>
                  <td className="mono">v{r.version}</td>
                  <td className="mono">{fmtDate(r.updated_at)}</td>
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
