import { useState, useCallback, useEffect } from "react";
import { Download, Trash2, RefreshCw } from "lucide-react";
import { api } from "../../api/client.js";
import { useAsync } from "../../lib/useAsync.js";
import { ModuleHeader, Card, Stat, Loader, ErrorState } from "../../components/ui.jsx";
import DataTable from "../../components/DataTable.jsx";
import MultiSelect from "../../components/MultiSelect.jsx";

export const meta = {
  id: "data-fetch",
  title: "Data Fetch",
  description: "Pull EPE / BRV S3 data into reusable datasets.",
  icon: "boxes",
  path: "/data-fetch",
  order: 1,
};

const PREVIEW_ROWS = 200;

// Columns come from the dataset itself — works for any real EPE / S3 schema.
function deriveColumns(rows) {
  if (!rows || rows.length === 0) return [];
  const seen = new Set();
  const cols = [];
  rows.slice(0, 50).forEach((r) =>
    Object.keys(r).forEach((k) => {
      if (k.startsWith("_") || seen.has(k)) return;
      seen.add(k);
      const sample = rows.find((x) => x[k] != null)?.[k];
      cols.push({
        key: k,
        label: k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
        mono: true,
        align: typeof sample === "number" ? "right" : undefined,
      });
    })
  );
  return cols;
}

function csvEscape(v) {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Download uses only the columns present in the dataset.
function downloadDataset(dataset, rows) {
  if (!rows || rows.length === 0) return;
  const keys = deriveColumns(rows).map((c) => c.key);
  const csv = [
    keys.join(","),
    ...rows.map((r) => keys.map((k) => csvEscape(r[k])).join(",")),
  ].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${dataset.source}_${dataset.product_type}_${dataset.id}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function DataFetch() {
  const cfg = useAsync(useCallback(() => api.module("data-fetch", "/filters"), []), []);
  const dsList = useAsync(useCallback(() => api.module("data-fetch", "/datasets"), []), []);

  const [tab, setTab] = useState("trades");
  const [productType, setProductType] = useState("");
  const [legalEntities, setLegalEntities] = useState([]);
  const [sourceSystems, setSourceSystems] = useState([]);
  const [startDate, setStartDate] = useState("2024-01-01");
  const [endDate, setEndDate] = useState(new Date().toISOString().slice(0, 10));

  useEffect(() => {
    if (cfg.data && !productType) {
      setProductType(cfg.data.product_types[0]);
      setLegalEntities([cfg.data.legal_entities[0]]);
      setSourceSystems([cfg.data.source_systems[0]]);
    }
  }, [cfg.data, productType]);

  const [fetching, setFetching] = useState(false);
  const [fetchErr, setFetchErr] = useState(null);

  // Selected dataset preview (full rows for download, sliced for display).
  const [selectedId, setSelectedId] = useState("");
  const [selected, setSelected] = useState(null);
  const [selLoading, setSelLoading] = useState(false);
  const [selErr, setSelErr] = useState(null);

  useEffect(() => {
    if (!selectedId) { setSelected(null); return; }
    let alive = true;
    setSelLoading(true); setSelErr(null);
    api.module("data-fetch", `/datasets/${selectedId}`, { limit: 50000 })
      .then((d) => alive && setSelected(d))
      .catch((e) => { if (alive) { setSelErr(e); setSelected(null); } })
      .finally(() => alive && setSelLoading(false));
    return () => { alive = false; };
  }, [selectedId]);

  async function runFetch() {
    setFetching(true); setFetchErr(null);
    try {
      const body = {
        product_type: productType, legal_entities: legalEntities,
        source_systems: sourceSystems, start_date: startDate, end_date: endDate,
      };
      const path = tab === "trades" ? "/trades" : "/exceptions";
      const data = await api.module("data-fetch", path, undefined, {
        method: "POST", body: JSON.stringify(body),
      });
      await dsList.reload();
      setSelectedId(data.dataset.id); // auto-select & preview the new dataset
    } catch (e) {
      setFetchErr(e);
    } finally {
      setFetching(false);
    }
  }

  async function removeDataset(id) {
    await api.module("data-fetch", `/datasets/${id}`, undefined, { method: "DELETE" });
    if (id === selectedId) setSelectedId("");
    dsList.reload();
  }

  const datasets = dsList.data?.datasets || [];
  const previewRows = selected?.rows || [];
  const cols = deriveColumns(previewRows);

  return (
    <>
      <ModuleHeader
        title="Data Fetch"
        description="Pull from BRV S3 (trades) or EPE (exceptions). Each pull is cached as a dataset the other modules can run on."
      />

      {cfg.loading && <Loader label="Loading filter options…" />}
      {cfg.error && <ErrorState error={cfg.error} />}

      {cfg.data && (
        <>
          <div className="tabs" style={{ marginBottom: 16 }}>
            <button className={`tab ${tab === "trades" ? "active" : ""}`} onClick={() => setTab("trades")}>BRV S3 — Trade Data</button>
            <button className={`tab ${tab === "exceptions" ? "active" : ""}`} onClick={() => setTab("exceptions")}>EPE — Exception Data</button>
          </div>

          <Card title="Filter options">
            <div className="filter-grid">
              <label className="control">
                <span>Product Type</span>
                <select value={productType} onChange={(e) => setProductType(e.target.value)}>
                  {cfg.data.product_types.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              </label>
              <MultiSelect label="Legal Entity(s)" options={cfg.data.legal_entities} selected={legalEntities} onChange={setLegalEntities} />
              <MultiSelect label="Source System(s)" options={cfg.data.source_systems} selected={sourceSystems} onChange={setSourceSystems} />
              <label className="control"><span>Start Date</span>
                <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label>
              <label className="control"><span>End Date</span>
                <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></label>
              <div className="control" style={{ justifyContent: "flex-end" }}>
                <button className="btn" onClick={runFetch} disabled={fetching}>
                  {fetching ? "Fetching…" : tab === "trades" ? "Fetch trades" : "Fetch exceptions"}
                </button>
              </div>
            </div>
          </Card>

          {fetching && <Loader label="Querying upstream…" />}
          {fetchErr && <ErrorState error={fetchErr} />}

          <Card title={
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              Loaded datasets
              <button className="icon-btn" onClick={dsList.reload} title="Refresh"><RefreshCw size={13} /></button>
            </span>
          }>
            {datasets.length === 0 ? (
              <div className="empty"><div className="empty-hint">Nothing cached yet. Fetch above to create a dataset.</div></div>
            ) : (
              <div className="ds-list">
                {datasets.map((d) => (
                  <div
                    key={d.id}
                    className={`ds-row ${d.id === selectedId ? "active" : ""}`}
                    onClick={() => setSelectedId(d.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && setSelectedId(d.id)}
                  >
                    <span className={`badge badge--${d.source === "EPE" ? "watch" : "pass"}`}>{d.source}</span>
                    <span className="ds-label">{d.label}</span>
                    <span className="mono ds-count">{d.row_count} rows</span>
                    <span className="mono ds-id">{d.id}</span>
                    <button
                      className="icon-btn"
                      onClick={(e) => { e.stopPropagation(); removeDataset(d.id); }}
                      title="Delete"
                    ><Trash2 size={14} /></button>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {selLoading && <Loader label="Loading preview…" />}
          {selErr && <ErrorState error={selErr} />}

          {selected && !selLoading && (
            <>
              <div className="stat-grid">
                <Stat label="Rows" value={selected.dataset.row_count} sub={selected.dataset.id} status="pass" />
                <Stat label="Source" value={selected.dataset.source} />
                <Stat label="Product" value={selected.dataset.product_type} />
                <Stat label="Range" value={`${selected.dataset.start_date} → ${selected.dataset.end_date}`} />
              </div>
              <Card title={`Preview — ${selected.dataset.label}`}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                  <span className="preview-note">
                    Showing {Math.min(previewRows.length, PREVIEW_ROWS)} of {selected.dataset.row_count} · {cols.length} columns
                  </span>
                  <button className="btn btn--ghost" onClick={() => downloadDataset(selected.dataset, previewRows)}>
                    <Download size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} /> Download CSV
                  </button>
                </div>
                {previewRows.length === 0
                  ? <div className="empty"><div className="empty-title">No rows in this dataset.</div></div>
                  : <DataTable columns={cols} rows={previewRows.slice(0, PREVIEW_ROWS)} />}
              </Card>
            </>
          )}
        </>
      )}
    </>
  );
}
