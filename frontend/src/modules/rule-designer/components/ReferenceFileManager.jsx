import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Trash2, Upload } from "lucide-react";
import { rd } from "../api.js";

// The reference/lookup-file registry's admin controls (upload a CSV,
// configure a path — including a password-protected .xlsx with a
// specific sheet/column-range — sync, delete with an in-use warning),
// lifted out of the old standalone "Datasets & Lookups" page so a LOOKUP
// node can manage its own reference file without leaving the node editor.
// Keeps reference_store.py's version history/audit trail exactly as it
// was — this only moves where the same controls are rendered.

function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsText(file);
  });
}

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

export default function ReferenceFileManager({ actor, role, isAdmin, onChanged }) {
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refMode, setRefMode] = useState("upload"); // "upload" | "path"
  const [refName, setRefName] = useState("");
  const [refFile, setRefFile] = useState(null);
  const [refPath, setRefPath] = useState("");
  const [refAutoRefresh, setRefAutoRefresh] = useState("");
  const [refSheetName, setRefSheetName] = useState("");
  const [refColumnRange, setRefColumnRange] = useState("");
  const [refPasswordEnvVar, setRefPasswordEnvVar] = useState("");
  const [refPassword, setRefPassword] = useState("");
  const isXlsxPath = /\.(xlsx|xlsm|xlsb)$/i.test(refPath.trim());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  const reload = useCallback(() => {
    setLoading(true);
    return rd.referenceFiles().then((d) => setFiles(d.files || [])).finally(() => setLoading(false));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  function afterChange() {
    reload();
    onChanged?.();
  }

  async function uploadReference() {
    if (!refFile || !refName.trim()) return;
    setBusy(true);
    try {
      const text = await readFileText(refFile);
      await rd.uploadReference(actor, role, refName.trim(), text);
      setRefName(""); setRefFile(null);
      setNotice({ kind: "pass", text: "Reference file uploaded." });
      afterChange();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  async function configureReferenceSource() {
    if (!refPath.trim() || !refName.trim()) return;
    if (refPasswordEnvVar.trim() && refPassword.trim()) {
      setNotice({ kind: "breach", text: "Pick one: a password env var, or a typed password — not both." });
      return;
    }
    setBusy(true);
    try {
      const minutes = refAutoRefresh.trim() ? Number(refAutoRefresh) : null;
      await rd.configureReferenceSource(actor, role, refName.trim(), refPath.trim(), null, minutes, {
        sheetName: refSheetName.trim(),
        columnRange: refColumnRange.trim(),
        passwordEnvVar: refPasswordEnvVar.trim(),
        password: refPassword,
      });
      setRefName(""); setRefPath(""); setRefAutoRefresh("");
      setRefSheetName(""); setRefColumnRange(""); setRefPasswordEnvVar(""); setRefPassword("");
      setNotice({ kind: "pass", text: "Reference file configured and synced." });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    afterChange(); // even on failure — the file is registered with last_sync_error set, worth seeing
    setBusy(false);
  }

  async function deleteReference(fileId, name) {
    setBusy(true);
    try {
      const usage = await rd.referenceFileUsage(fileId);
      const rules = usage.rules || [];
      const warning = rules.length
        ? `Used by ${rules.length} rule(s): ${rules.map((r) => `${r.product}/${r.rule_id}`).join(", ")}. Delete anyway?`
        : `Delete reference file "${name}"? This removes every version and can't be undone.`;
      if (!window.confirm(warning)) { setBusy(false); return; }
      await rd.deleteReference(fileId, actor);
      setNotice({ kind: "pass", text: "Reference file deleted." });
      afterChange();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  async function syncNow(fileId) {
    setBusy(true);
    try {
      await rd.syncReference(fileId, actor);
      setNotice({ kind: "pass", text: "Synced." });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    afterChange();
    setBusy(false);
  }

  return (
    <div className="ref-file-manager">
      {notice && <div className={notice.kind === "breach" ? "errorbox" : "benefit-note"}>{notice.text}</div>}
      <div className="table-wrap" style={{ marginBottom: 14 }}>
        <table className="table">
          <thead>
            <tr>
              <th>Name</th><th>Source</th><th>Latest version</th><th>Records</th>
              <th>Columns</th><th>Updated by</th><th>Last synced</th><th />
            </tr>
          </thead>
          <tbody>
            {files.map((f) => {
              const v = f.versions[f.versions.length - 1];
              const isPath = f.source_mode === "path";
              return (
                <tr key={f.id}>
                  <td>{f.name} <span className="mono ds-id">{f.id}</span></td>
                  <td>
                    {isPath ? <span className="mono" title={f.source_path}>path</span> : <span className="mono">upload</span>}
                    {isPath && f.auto_refresh_minutes ? <span className="empty-hint"> · every {f.auto_refresh_minutes}m</span> : null}
                    {isPath && f.password_configured ? <span className="empty-hint" title="Password configured"> · 🔒</span> : null}
                  </td>
                  <td className="mono">{v ? `v${v.version}` : "—"}</td>
                  <td className="mono">{v ? v.records : "—"}</td>
                  <td className="mono">{v ? v.columns.join(", ") : "—"}</td>
                  <td>{v ? v.uploaded_by : "—"}</td>
                  <td>
                    {isPath ? (
                      <span style={f.last_sync_error ? { color: "var(--breach)" } : undefined} title={f.last_sync_error || ""}>
                        {f.last_sync_error ? "sync failed" : fmtTime(f.last_synced_at)}
                      </span>
                    ) : "—"}
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 6 }}>
                      {isAdmin && isPath && (
                        <button className="btn btn--ghost btn--xs" disabled={busy} onClick={() => syncNow(f.id)} title={f.source_path}>
                          <RefreshCw size={12} /> Sync now
                        </button>
                      )}
                      {isAdmin && (
                        <button className="btn btn--ghost btn--xs" disabled={busy} onClick={() => deleteReference(f.id, f.name)}
                                title="Delete this reference file">
                          <Trash2 size={12} />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {!loading && files.length === 0 && <tr><td colSpan={8} className="empty-hint">No reference files yet.</td></tr>}
          </tbody>
        </table>
      </div>
      {isAdmin ? (
        <>
          <div className="controls controls--row" style={{ marginBottom: 10, alignItems: "center" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
              <input type="radio" checked={refMode === "upload"} onChange={() => setRefMode("upload")} /> Upload a file
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
              <input type="radio" checked={refMode === "path"} onChange={() => setRefMode("path")} /> Configure a path
            </label>
          </div>
          {refMode === "upload" ? (
            <div className="controls controls--row">
              <label className="control" style={{ minWidth: 220 }}><span>Name</span>
                <input value={refName} onChange={(e) => setRefName(e.target.value)} placeholder="e.g. currency_reference" /></label>
              <label className="control"><span>CSV file</span>
                <input type="file" accept=".csv" onChange={(e) => setRefFile(e.target.files?.[0] || null)} /></label>
              <button className="btn" disabled={busy || !refFile || !refName.trim()} onClick={uploadReference}>
                <Upload size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />Upload version
              </button>
            </div>
          ) : (
            <div className="controls controls--row">
              <label className="control" style={{ minWidth: 220 }}><span>Name</span>
                <input value={refName} onChange={(e) => setRefName(e.target.value)} placeholder="e.g. currency_reference" /></label>
              <label className="control" style={{ minWidth: 280 }}><span>File path</span>
                <input value={refPath} onChange={(e) => setRefPath(e.target.value)}
                  placeholder="e.g. /mnt/shared/reference/currency.csv" /></label>
              <label className="control" style={{ minWidth: 160 }}><span>Auto-refresh (minutes, optional)</span>
                <input type="number" min="0" value={refAutoRefresh} onChange={(e) => setRefAutoRefresh(e.target.value)}
                  placeholder="e.g. 60" /></label>
              {isXlsxPath && (
                <>
                  <label className="control" style={{ minWidth: 180 }}><span>Sheet name</span>
                    <input value={refSheetName} onChange={(e) => setRefSheetName(e.target.value)}
                      placeholder="e.g. TP Market Data" /></label>
                  <label className="control" style={{ minWidth: 120 }}><span>Column range</span>
                    <input value={refColumnRange} onChange={(e) => setRefColumnRange(e.target.value)}
                      placeholder="e.g. A:I" /></label>
                  <label className="control" style={{ minWidth: 260 }}><span>Password env var (optional)</span>
                    <input value={refPasswordEnvVar} onChange={(e) => setRefPasswordEnvVar(e.target.value)}
                      placeholder="e.g. TP_MARKET_DATA_XLSX_PASSWORD" disabled={!!refPassword.trim()} /></label>
                  <label className="control" style={{ minWidth: 180 }}><span>— or type a password</span>
                    <input type="password" value={refPassword} onChange={(e) => setRefPassword(e.target.value)}
                      placeholder="stored encrypted" disabled={!!refPasswordEnvVar.trim()} /></label>
                </>
              )}
              <button className="btn" disabled={busy || !refPath.trim() || !refName.trim()} onClick={configureReferenceSource}>
                <RefreshCw size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />Save & sync now
              </button>
            </div>
          )}
        </>
      ) : (
        <p className="preview-note">Managing reference files is Admin-only.</p>
      )}
      <p className="empty-hint">
        Uploading (or syncing) with the same name creates a new immutable version — published rules keep
        referencing the version they were tested against. A path-configured file is re-read from that path
        on "Sync now" or, if an auto-refresh interval is set, automatically in the background. A path ending
        in .xlsx/.xlsm/.xlsb is read as Excel — password-protected is fine: either name a server-side environment
        variable holding the password, or type one directly in (it's encrypted before it's stored, never kept
        as plain text). If one sheet holds two separate tables side by side, configure a path twice with the
        same path/sheet and a different column range (e.g. "A:I" and "K:U") and name each one — they become
        two independent reference files. Deleting a reference file removes every version and can't be undone —
        you'll be warned first if any rule still looks it up.
      </p>
    </div>
  );
}
