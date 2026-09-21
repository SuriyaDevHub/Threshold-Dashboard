import { useState, useCallback, useEffect } from "react";
import { Download } from "lucide-react";
import { api } from "../../api/client.js";
import { useAsync } from "../../lib/useAsync.js";
import { ModuleHeader, Card, Stat, Loader, ErrorState } from "../../components/ui.jsx";
import DataTable from "../../components/DataTable.jsx";
import DatasetSelector from "../../components/DatasetSelector.jsx";
import { fmtPct } from "../../lib/format.js";

export const meta = {
  id: "audit-sampling",
  title: "Audit Sampling",
  description: "Reproducible audit sample from a fetched EPE dataset.",
  icon: "list-checks",
  path: "/audit-sampling",
  order: 3,
};

export default function AuditSampling() {
  const eds = useAsync(useCallback(() => api.module("audit-sampling", "/exception-datasets"), []), []);
  const [datasetId, setDatasetId] = useState("");
  const [size, setSize] = useState(20);
  const [method, setMethod] = useState("stratified");
  const [seed, setSeed] = useState(42);

  const datasets = eds.data?.datasets || [];
  useEffect(() => { if (datasets.length && !datasetId) setDatasetId(datasets[0].id); }, [datasets, datasetId]);

  const load = useCallback(
    () => datasetId
      ? api.module("audit-sampling", "/sample", undefined,
          { method: "POST", body: JSON.stringify({ dataset_id: datasetId, size, method, seed }) })
      : Promise.resolve(null),
    [datasetId, size, method, seed]
  );
  const { loading, data, error, reload } = useAsync(load, [datasetId, size, method, seed]);
  const stats = data?.stats;

  const columns = [
    { key: "exception_id", label: "Exception", mono: true },
    { key: "trade_id", label: "Trade", mono: true },
    { key: "product_type", label: "Product", mono: true },
    { key: "legal_entity", label: "Entity", mono: true },
    { key: "source_system", label: "Source", mono: true },
    { key: "status", label: "Status" },
    { key: "ageing_days", label: "Age (d)", align: "right", mono: true },
  ];

  function exportCsv() {
    if (!data?.sample?.length) return;
    const keys = columns.map((c) => c.key);
    const csv = [keys.join(","), ...data.sample.map((r) => keys.map((k) => r[k]).join(","))].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = `audit_sample_${method}_seed${seed}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <ModuleHeader
        title="Audit Sampling"
        description="Runs on an EPE dataset pulled in Data Fetch. Same seed + dataset = same sample (the audit control)."
        actions={data ? <><button className="btn btn--ghost" onClick={exportCsv}><Download size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />Export CSV</button><button className="btn" onClick={reload}>Re-draw</button></> : null}
      />
      {eds.loading && <Loader label="Loading datasets…" />}
      {eds.error && <ErrorState error={eds.error} />}

      {eds.data && (
        <>
          <Card title="Sampling parameters">
            <div className="controls controls--row">
              <DatasetSelector label="Exception dataset (EPE)" datasets={datasets} value={datasetId} onChange={setDatasetId} emptyHint="Pull an EPE exception dataset in Data Fetch first." />
              <label className="control"><span>Sample size</span>
                <input type="number" min="1" max="500" value={size} onChange={(e) => setSize(Number(e.target.value))} /></label>
              <label className="control"><span>Method</span>
                <select value={method} onChange={(e) => setMethod(e.target.value)}>
                  <option value="stratified">Stratified (by product)</option>
                  <option value="random">Random</option>
                </select></label>
              <label className="control"><span>Seed</span>
                <input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></label>
            </div>
          </Card>

          {loading && <Loader />}
          {error && <ErrorState error={error} />}
          {data && (
            <>
              <div className="stat-grid">
                <Stat label="Population" value={stats.population_size} />
                <Stat label="Sample" value={stats.sample_size} />
                <Stat label="Coverage" value={fmtPct(stats.coverage_pct, 1)} />
                <Stat label="Strata covered" value={`${stats.strata_covered}/${stats.strata_total}`} />
              </div>
              <Card title={`Sample — ${method}, seed ${seed}`}>
                <DataTable columns={columns} rows={data.sample} rowKey="exception_id" />
              </Card>
            </>
          )}
        </>
      )}
    </>
  );
}
