import { useCallback, useEffect, useState } from "react";
import { RotateCcw } from "lucide-react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";
import { fmtDate } from "../format.js";

export default function Versions() {
  const { actor, role, isAdmin } = useActor();
  const [productFilter, setProductFilter] = useState("");
  const { loading, data, error, reload } = useAsync(
    useCallback(() => rd.versions(productFilter || undefined), [productFilter]), [productFilter],
  );
  const products = useAsync(useCallback(() => rd.products(), []), []);
  const [openYaml, setOpenYaml] = useState(null);
  const [yamlText, setYamlText] = useState("");
  const [notice, setNotice] = useState(null);

  async function viewYaml(v) {
    const key = `${v.product}:${v.version}`;
    if (openYaml === key) { setOpenYaml(null); return; }
    const d = await rd.versionYaml(v.product, v.version);
    setYamlText(d.yaml); setOpenYaml(key);
  }

  async function rollback(v) {
    if (!confirm(`Roll back ${v.product} to v${v.version}? This publishes a new version restoring that snapshot's rules.`)) return;
    try {
      await rd.rollback({ actor, role, product: v.product, version: v.version });
      setNotice({ kind: "pass", text: `${v.product} rolled back to v${v.version} (published as a new version).` });
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
  }

  if (loading) return <Loader label="Loading versions…" />;
  if (error) return <ErrorState error={error} />;
  const versions = [...(data.versions || [])].reverse();

  return (
    <>
      <ModuleHeader title="Version history" description="Every publish snapshots the whole rules YAML for one product — nothing is ever deleted; rollback creates a new version. Version numbers are independent per product." />
      {notice && <div className={notice.kind === "breach" ? "errorbox" : "benefit-note"}>{notice.text}</div>}
      <Card>
        <div className="controls controls--row">
          <label className="control"><span>Product</span>
            <select value={productFilter} onChange={(e) => setProductFilter(e.target.value)}>
              <option value="">all products</option>
              {(products.data?.products || []).map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
            </select>
          </label>
        </div>
      </Card>
      <Card>
        <div className="ds-list">
          {versions.map((v) => {
            const key = `${v.product}:${v.version}`;
            return (
              <div className="card" key={key} style={{ marginBottom: 10 }}>
                <div className="lk-row" style={{ justifyContent: "space-between" }}>
                  <div>
                    <span className="badge">{v.product}</span> <strong className="mono">v{v.version}</strong> — {v.description}
                    <div className="ds-count mono">{v.created_by} · {fmtDate(v.created_at)} · rules: {v.rule_ids_changed.join(", ")}</div>
                  </div>
                  <div className="lk-row">
                    <button className="btn btn--ghost btn--xs" onClick={() => viewYaml(v)}>{openYaml === key ? "Hide YAML" : "View YAML"}</button>
                    {isAdmin && (
                      <button className="btn btn--ghost btn--xs" onClick={() => rollback(v)}>
                        <RotateCcw size={12} style={{ marginRight: 4, verticalAlign: "-2px" }} />Rollback to this
                      </button>
                    )}
                  </div>
                </div>
                {openYaml === key && <pre className="rd-yaml-diff mono">{yamlText}</pre>}
              </div>
            );
          })}
          {versions.length === 0 && <p className="empty-hint">No published versions yet — publish a rule to create v1.</p>}
        </div>
      </Card>
    </>
  );
}
