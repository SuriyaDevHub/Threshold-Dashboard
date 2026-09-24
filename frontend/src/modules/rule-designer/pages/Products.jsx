import { useCallback, useState } from "react";
import { Plus, ShieldAlert, Pencil, AlertTriangle } from "lucide-react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";

const MIGRATION_LABEL = { not_migrated: "Not migrated", in_progress: "In progress", migrated: "Migrated" };
const MIGRATION_CLASS = { not_migrated: "", in_progress: "watch", migrated: "pass" };

export default function Products() {
  const { actor, role, isAdmin } = useActor();
  const { loading, data, error, reload } = useAsync(useCallback(() => rd.products(), []), []);
  const consistency = useAsync(useCallback(() => (isAdmin ? rd.productsConsistencyCheck() : Promise.resolve(null)), [isAdmin]), [isAdmin]);
  const [creating, setCreating] = useState(false);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [notice, setNotice] = useState(null);
  const [renamingCode, setRenamingCode] = useState(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameInFlight, setRenameInFlight] = useState(false);

  async function toggle(p) {
    if (!isAdmin) return;
    if (p.enabled && !confirm(
      `Disable ${p.name}? Every record for this product will fail-safe to ALERT until it's re-enabled — this is the same behavior as a product with zero active rules.`
    )) return;
    try {
      await rd.setProductEnabled(actor, role, p.code, !p.enabled);
      setNotice({ kind: "pass", text: `${p.name} ${!p.enabled ? "enabled" : "disabled"}.` });
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
  }

  async function setMigration(p, status) {
    if (!isAdmin) return;
    try {
      await rd.setProductMigrationStatus(actor, role, p.code, status);
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
  }

  async function createProduct() {
    try {
      await rd.createProduct(actor, role, code.trim().toUpperCase(), name.trim(), description.trim());
      setCreating(false); setCode(""); setName(""); setDescription("");
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
  }

  function startRename(p) {
    setRenamingCode(p.code);
    setRenameValue(p.code);
  }

  async function submitRename(p) {
    if (renameInFlight) return; // guard against a double-click firing two renames
    const newCode = renameValue.trim().toUpperCase();
    if (!newCode || newCode === p.code) { setRenamingCode(null); return; }
    if (!confirm(
      `Rename ${p.code} to ${newCode}? This moves every rule (and, where unambiguous, version history) from `
      + `${p.code} to ${newCode} — if ${newCode} is already a registered product, ${p.code}'s rules are merged `
      + `into it instead. This is how a product code that doesn't match what your validator integration actually `
      + `calls it gets fixed. It can't be undone automatically.`
    )) return;
    setRenameInFlight(true);
    try {
      const result = await rd.renameProduct(actor, role, p.code, newCode);
      setRenamingCode(null);
      setNotice({
        kind: "pass",
        text: `${p.code} renamed to ${result.product.code}${result.merged ? " (merged into the existing product)" : ""} — ${result.rules_moved} rule(s) moved.`,
      });
      reload();
      consistency.reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    finally { setRenameInFlight(false); }
  }

  if (loading) return <Loader label="Loading products…" />;
  if (error) return <ErrorState error={error} />;

  return (
    <>
      <ModuleHeader
        title="Products"
        description="Every rule belongs to a product. Disabling a product is the admin kill switch — its rule engine stops evaluating and every record fail-safes to ALERT, per the non-negotiable never-silently-pass default."
        actions={isAdmin ? (
          <button className="btn" onClick={() => setCreating(!creating)}>
            <Plus size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />New product
          </button>
        ) : null}
      />
      {notice && <div className={notice.kind === "breach" ? "errorbox" : "benefit-note"}>{notice.text}</div>}
      {!isAdmin && (
        <div className="preview-note" style={{ marginBottom: 14 }}>
          Viewing as User — enable/disable and migration status are Admin-only.
        </div>
      )}

      {creating && (
        <Card title="New product">
          <div className="controls controls--row">
            <label className="control"><span>Code</span>
              <input className="mono" value={code} onChange={(e) => setCode(e.target.value)} placeholder="e.g. REPO" /></label>
            <label className="control" style={{ minWidth: 220 }}><span>Name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Repo" /></label>
            <label className="control" style={{ minWidth: 260 }}><span>Description</span>
              <input value={description} onChange={(e) => setDescription(e.target.value)} /></label>
            <button className="btn" disabled={!code.trim() || !name.trim()} onClick={createProduct}>Create</button>
          </div>
        </Card>
      )}

      {isAdmin && consistency.data && (
        consistency.data.orphaned_with_rules.length > 0 || consistency.data.empty_scaffold_dirs.length > 0
      ) && (
        <Card title="Consistency check">
          {consistency.data.orphaned_with_rules.length > 0 && (
            <div style={{ marginBottom: consistency.data.empty_scaffold_dirs.length ? 14 : 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
                <AlertTriangle size={14} style={{ color: "var(--breach, #c0392b)" }} />
                <strong>Rule directories with no registered product</strong>
              </div>
              <p className="empty-hint" style={{ marginTop: 0, marginBottom: 10 }}>
                Rules exist on disk under these codes, but GenericValidator(product=…) won't find them under a code
                that's not registered here. Rename each one to whatever your validator integration actually calls it.
              </p>
              {consistency.data.orphaned_with_rules.map((o) => (
                <div key={o.code} className="lk-row" style={{ marginBottom: 6 }}>
                  <span className="mono ds-id">{o.code}</span>
                  <span className="ds-count">{o.rule_count} rule(s)</span>
                  {renamingCode === o.code ? (
                    <>
                      <input
                        className="mono" style={{ width: 160 }} autoFocus disabled={renameInFlight}
                        value={renameValue} onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") submitRename({ code: o.code }); if (e.key === "Escape" && !renameInFlight) setRenamingCode(null); }}
                      />
                      <button className="btn btn--ghost" disabled={renameInFlight} onClick={() => submitRename({ code: o.code })}>
                        {renameInFlight ? "Saving…" : "Save"}
                      </button>
                      <button className="btn btn--ghost" disabled={renameInFlight} onClick={() => setRenamingCode(null)}>Cancel</button>
                    </>
                  ) : (
                    <button className="btn btn--ghost" onClick={() => startRename({ code: o.code })}>Rename to fix</button>
                  )}
                </div>
              ))}
            </div>
          )}
          {consistency.data.empty_scaffold_dirs.length > 0 && (
            <p className="empty-hint" style={{ marginBottom: 0 }}>
              Empty, unregistered rule directories with no rules in them (harmless — created the first time
              something touched that code, nothing to move): {consistency.data.empty_scaffold_dirs.join(", ")}.
            </p>
          )}
        </Card>
      )}

      <div className="ds-list">
        {data.products.map((p) => (
          <Card key={p.code}>
            <div className="lk-row" style={{ justifyContent: "space-between" }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <strong>{p.name}</strong>
                  {renamingCode === p.code ? (
                    <>
                      <input
                        className="mono" style={{ width: 160 }} autoFocus disabled={renameInFlight}
                        value={renameValue} onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") submitRename(p); if (e.key === "Escape" && !renameInFlight) setRenamingCode(null); }}
                      />
                      <button className="btn btn--ghost" disabled={renameInFlight} onClick={() => submitRename(p)}>
                        {renameInFlight ? "Saving…" : "Save"}
                      </button>
                      <button className="btn btn--ghost" disabled={renameInFlight} onClick={() => setRenamingCode(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <span className="mono ds-id">{p.code}</span>
                      {isAdmin && (
                        <button
                          className="btn btn--ghost" title="Rename this product's code"
                          style={{ padding: "2px 6px" }} onClick={() => startRename(p)}
                        >
                          <Pencil size={12} />
                        </button>
                      )}
                    </>
                  )}
                  {!p.enabled && (
                    <span className="badge badge--breach">
                      <ShieldAlert size={11} style={{ marginRight: 3, verticalAlign: "-2px" }} />disabled
                    </span>
                  )}
                </div>
                <div className="ds-count">{p.description}</div>
              </div>
              <div className="lk-row">
                <span className={`badge ${MIGRATION_CLASS[p.migration_status] ? `badge--${MIGRATION_CLASS[p.migration_status]}` : ""}`}>
                  {MIGRATION_LABEL[p.migration_status]}
                </span>
                {isAdmin && (
                  <select value={p.migration_status} onChange={(e) => setMigration(p, e.target.value)}>
                    <option value="not_migrated">Not migrated</option>
                    <option value="in_progress">In progress</option>
                    <option value="migrated">Migrated</option>
                  </select>
                )}
                <label className="control" style={{ flexDirection: "row", alignItems: "center", gap: 6, minWidth: 0 }}>
                  <input type="checkbox" checked={p.enabled} disabled={!isAdmin} onChange={() => toggle(p)} />
                  <span>{p.enabled ? "Enabled" : "Disabled"}</span>
                </label>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </>
  );
}
