import { useCallback, useState } from "react";
import { RefreshCw, Upload } from "lucide-react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";

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

export default function DatasetsLookups() {
  const { actor, role, isAdmin } = useActor();
  const { loading, data, error, reload } = useAsync(
    useCallback(() => Promise.all([rd.datasets(), rd.referenceFiles()]).then(([d, r]) => ({ datasets: d.datasets, files: r.files })), []),
    [],
  );
  const [refMode, setRefMode] = useState("upload"); // "upload" | "path"
  const [refName, setRefName] = useState("");
  const [refFile, setRefFile] = useState(null);
  const [refPath, setRefPath] = useState("");
  const [refAutoRefresh, setRefAutoRefresh] = useState("");
  const [dsLabel, setDsLabel] = useState("");
  const [dsFile, setDsFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);

  async function uploadReference() {
    if (!refFile || !refName.trim()) return;
    setBusy(true);
    try {
      const text = await readFileText(refFile);
      await rd.uploadReference(actor, role, refName.trim(), text);
      setRefName(""); setRefFile(null);
      setNotice({ kind: "pass", text: "Reference file uploaded." });
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  async function configureReferenceSource() {
    if (!refPath.trim() || !refName.trim()) return;
    setBusy(true);
    try {
      const minutes = refAutoRefresh.trim() ? Number(refAutoRefresh) : null;
      await rd.configureReferenceSource(actor, role, refName.trim(), refPath.trim(), null, minutes);
      setRefName(""); setRefPath(""); setRefAutoRefresh("");
      setNotice({ kind: "pass", text: "Reference file configured and synced." });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    reload(); // even on failure — the file is registered with last_sync_error set, worth seeing
    setBusy(false);
  }

  async function syncNow(fileId) {
    setBusy(true);
    try {
      await rd.syncReference(fileId, actor);
      setNotice({ kind: "pass", text: "Synced." });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    reload();
    setBusy(false);
  }

  async function uploadDataset() {
    if (!dsFile || !dsLabel.trim()) return;
    setBusy(true);
    try {
      const text = await readFileText(dsFile);
      await rd.uploadDataset(dsLabel.trim(), text);
      setDsLabel(""); setDsFile(null);
      setNotice({ kind: "pass", text: "Dataset uploaded." });
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  if (loading) return <Loader label="Loading datasets & reference files…" />;
  if (error) return <ErrorState error={error} />;

  return (
    <>
      <ModuleHeader title="Datasets & reference data" description="Input datasets and the lookup files enrichment steps join against." />
      {notice && <div className={notice.kind === "breach" ? "errorbox" : "benefit-note"}>{notice.text}</div>}

      <Card title="Datasets">
        <div className="ds-list" style={{ marginBottom: 14 }}>
          {data.datasets.map((d) => (
            <div className="ds-row" key={d.id} style={{ cursor: "default" }}>
              <span className="ds-label">{d.label}</span>
              <span className="ds-count mono">{d.row_count} rows</span>
              <span className="ds-id mono">{d.id}</span>
            </div>
          ))}
          {data.datasets.length === 0 && <p className="empty-hint">No datasets yet — pull one in Data Fetch, or upload a CSV below.</p>}
        </div>
        <div className="controls controls--row">
          <label className="control" style={{ minWidth: 220 }}><span>Label</span>
            <input value={dsLabel} onChange={(e) => setDsLabel(e.target.value)} placeholder="e.g. manual_trades_q3" /></label>
          <label className="control"><span>CSV file</span>
            <input type="file" accept=".csv" onChange={(e) => setDsFile(e.target.files?.[0] || null)} /></label>
          <button className="btn" disabled={busy || !dsFile || !dsLabel.trim()} onClick={uploadDataset}>
            <Upload size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />Upload dataset
          </button>
        </div>
      </Card>

      <Card title="Reference / lookup files">
        <div className="table-wrap" style={{ marginBottom: 14 }}>
          <table className="table">
            <thead>
              <tr>
                <th>Name</th><th>Source</th><th>Latest version</th><th>Records</th>
                <th>Columns</th><th>Updated by</th><th>Last synced</th><th />
              </tr>
            </thead>
            <tbody>
              {data.files.map((f) => {
                const v = f.versions[f.versions.length - 1];
                const isPath = f.source_mode === "path";
                return (
                  <tr key={f.id}>
                    <td>{f.name} <span className="mono ds-id">{f.id}</span></td>
                    <td>
                      {isPath ? <span className="mono" title={f.source_path}>path</span> : <span className="mono">upload</span>}
                      {isPath && f.auto_refresh_minutes ? <span className="empty-hint"> · every {f.auto_refresh_minutes}m</span> : null}
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
                      {isAdmin && isPath && (
                        <button className="btn btn--ghost btn--xs" disabled={busy} onClick={() => syncNow(f.id)} title={f.source_path}>
                          <RefreshCw size={12} /> Sync now
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
              {data.files.length === 0 && <tr><td colSpan={8} className="empty-hint">No reference files yet.</td></tr>}
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
          on "Sync now" or, if an auto-refresh interval is set, automatically in the background.
        </p>
      </Card>
    </>
  );
}
