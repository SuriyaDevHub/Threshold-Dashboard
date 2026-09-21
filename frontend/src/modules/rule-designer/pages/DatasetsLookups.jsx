import { useCallback, useState } from "react";
import { Upload } from "lucide-react";
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

export default function DatasetsLookups() {
  const { actor, role } = useActor();
  const { loading, data, error, reload } = useAsync(
    useCallback(() => Promise.all([rd.datasets(), rd.referenceFiles()]).then(([d, r]) => ({ datasets: d.datasets, files: r.files })), []),
    [],
  );
  const [refName, setRefName] = useState("");
  const [refFile, setRefFile] = useState(null);
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
            <thead><tr><th>Name</th><th>Latest version</th><th>Records</th><th>Columns</th><th>Uploaded by</th></tr></thead>
            <tbody>
              {data.files.map((f) => {
                const v = f.versions[f.versions.length - 1];
                return (
                  <tr key={f.id}>
                    <td>{f.name} <span className="mono ds-id">{f.id}</span></td>
                    <td className="mono">v{v.version}</td>
                    <td className="mono">{v.records}</td>
                    <td className="mono">{v.columns.join(", ")}</td>
                    <td>{v.uploaded_by}</td>
                  </tr>
                );
              })}
              {data.files.length === 0 && <tr><td colSpan={5} className="empty-hint">No reference files yet.</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="controls controls--row">
          <label className="control" style={{ minWidth: 220 }}><span>Name</span>
            <input value={refName} onChange={(e) => setRefName(e.target.value)} placeholder="e.g. currency_reference" /></label>
          <label className="control"><span>CSV file</span>
            <input type="file" accept=".csv" onChange={(e) => setRefFile(e.target.files?.[0] || null)} /></label>
          <button className="btn" disabled={busy || !refFile || !refName.trim()} onClick={uploadReference}>
            <Upload size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />Upload version
          </button>
        </div>
        <p className="empty-hint">Uploading with the same name creates a new immutable version — published rules keep referencing the version they were tested against.</p>
      </Card>
    </>
  );
}
