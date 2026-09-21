import { useState } from "react";
import { ChevronDown, ChevronRight, Check, X, AlertTriangle } from "lucide-react";
import { Card, Stat } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";

// Parallel/shadow test: compares this rule's engine decision against a
// legacy validator's already-produced output (uploaded as CSV), joined by
// record id. No legacy code is imported or run here — its output is data,
// like any other upload. The category that matters most is "legacy only":
// a record the legacy validator alerted on that this rule set misses —
// the regression a migration must catch before cutover.

function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsText(file);
  });
}

function CATEGORY_LABEL(c) {
  return { agree_alert: "Agree — alert", agree_clear: "Agree — clear",
           new_only: "New rule only", legacy_only: "Legacy only (regression risk)" }[c] || c;
}

function StepIcon({ status }) {
  if (status === "ok") return <Check size={13} className="rd-step-ok" />;
  if (status === "skipped") return <X size={13} className="rd-step-skip" />;
  if (status === "lookup_miss") return <AlertTriangle size={13} className="rd-step-warn" />;
  return <X size={13} className="rd-step-err" />;
}

function MismatchRow({ m }) {
  const [open, setOpen] = useState(false);
  const isLegacyOnly = m.category === "legacy_only";
  return (
    <div className={`rd-record ${isLegacyOnly ? "" : ""}`} style={isLegacyOnly ? { borderColor: "var(--breach)" } : undefined}>
      <button className="rd-record-head" onClick={() => setOpen(!open)}
              style={isLegacyOnly ? { background: "var(--breach-bg)" } : { background: "var(--watch-bg)" }}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="mono">{String(m.record_id)}</span>
        <span className={`badge ${isLegacyOnly ? "badge--breach" : "badge--watch"}`}>{CATEGORY_LABEL(m.category)}</span>
        <span className="preview-note mono">legacy: {m.legacy_status} {m.legacy_reason_code ? `(${m.legacy_reason_code})` : ""}</span>
      </button>
      {open && (
        <div className="rd-record-trail">
          {m.trail.map((step, i) => (
            <div className="rd-trail-step" key={i}>
              <StepIcon status={step.status} />
              <span className="rd-trail-label">[{step.node_type}] {step.label}</span>
              {step.detail && <span className="rd-trail-detail mono">{step.detail}</span>}
            </div>
          ))}
          {Object.keys(m.new_outcome || {}).length > 0 && (
            <div className="rd-trail-outcome">
              <strong>New rule outcome:</strong>{" "}
              {Object.entries(m.new_outcome).map(([k, v]) => `${k} = ${v}`).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ShadowTestView({ ruleId, datasetId }) {
  const { actor, role } = useActor();
  const [file, setFile] = useState(null);
  const [recordIdField, setRecordIdField] = useState("trade_id");
  const [recordIdCol, setRecordIdCol] = useState("record_id");
  const [statusCol, setStatusCol] = useState("status");
  const [reasonCol, setReasonCol] = useState("reason_code");
  const [alertValues, setAlertValues] = useState("ALERTED, BREACH");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [filter, setFilter] = useState("legacy_only");

  async function run() {
    if (!file || !datasetId) return;
    setBusy(true); setError(null);
    try {
      const csvText = await readFileText(file);
      const res = await rd.shadowTest(ruleId, {
        actor, role, dataset_id: datasetId, record_id_field: recordIdField,
        legacy_csv_text: csvText,
        legacy_alert_values: alertValues.split(",").map((s) => s.trim()).filter(Boolean),
        record_id_col: recordIdCol, status_col: statusCol, reason_col: reasonCol || null,
      });
      setResult(res);
    } catch (e) {
      setError(String(e.message || e));
    }
    setBusy(false);
  }

  const mismatches = (result?.mismatches || []).filter((m) => filter === "all" || m.category === filter);

  return (
    <>
      <Card title="Shadow test vs. legacy validator">
        <p className="empty-hint" style={{ marginBottom: 12 }}>
          Upload what your existing <span className="mono">{"{product}_validator.py"}</span> decided for this
          dataset (a CSV with a record id and a status column) — nothing from that validator is imported or
          run here. This rule's engine runs the same dataset and the two are diffed record by record.
        </p>
        <div className="controls controls--row">
          <label className="control"><span>Legacy results CSV</span>
            <input type="file" accept=".csv" onChange={(e) => setFile(e.target.files?.[0] || null)} /></label>
          <label className="control"><span>Dataset record-id field</span>
            <input value={recordIdField} onChange={(e) => setRecordIdField(e.target.value)} /></label>
          <label className="control"><span>CSV record-id column</span>
            <input value={recordIdCol} onChange={(e) => setRecordIdCol(e.target.value)} /></label>
          <label className="control"><span>CSV status column</span>
            <input value={statusCol} onChange={(e) => setStatusCol(e.target.value)} /></label>
          <label className="control"><span>CSV reason-code column (optional)</span>
            <input value={reasonCol} onChange={(e) => setReasonCol(e.target.value)} /></label>
        </div>
        <div className="controls controls--row" style={{ marginTop: 10 }}>
          <label className="control" style={{ minWidth: 320 }}><span>Status values that mean "alert" for this legacy validator</span>
            <input value={alertValues} onChange={(e) => setAlertValues(e.target.value)} placeholder="ALERTED, BREACH" /></label>
          <button className="btn" disabled={busy || !file || !datasetId} onClick={run}>
            {busy ? "Running…" : "Run shadow test"}
          </button>
        </div>
        {!datasetId && <p className="empty-hint">Bind a dataset above first.</p>}
        {error && <div className="errorbox">{error}</div>}
      </Card>

      {result && (
        <>
          <div className="stat-grid">
            <Stat label="Compared" value={result.summary.total_compared} />
            <Stat label="Agreement rate" value={`${result.summary.agreement_rate_pct}%`} status="pass" />
            <Stat label="Agree — alert" value={result.summary.agree_alert} />
            <Stat label="Agree — clear" value={result.summary.agree_clear} />
          </div>
          <div className="stat-grid">
            <Stat label="New rule only" value={result.summary.new_only} status={result.summary.new_only ? "watch" : undefined} />
            <Stat label="Legacy only (regression risk)" value={result.summary.legacy_only} status={result.summary.legacy_only ? "breach" : undefined} />
            <Stat label="In dataset, no legacy result" value={result.summary.dataset_records_without_legacy_result} />
            <Stat label="In legacy file, not in dataset" value={result.summary.legacy_results_without_dataset_record} />
          </div>

          {result.summary.legacy_only > 0 && (
            <div className="errorbox">
              <strong>{result.summary.legacy_only} record(s) the legacy validator alerted on that this rule
              set does not.</strong> Review these before cutover — this is the category a migration must not
              silently drop.
            </div>
          )}

          <Card title={`Mismatches (${mismatches.length} shown)`}>
            <div className="tabs" style={{ marginBottom: 12 }}>
              {["legacy_only", "new_only", "all"].map((f) => (
                <button key={f} className={`tab ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
                  {f === "all" ? "all mismatches" : CATEGORY_LABEL(f)}
                </button>
              ))}
            </div>
            <div className="rd-record-list">
              {mismatches.map((m, i) => <MismatchRow m={m} key={i} />)}
              {mismatches.length === 0 && <p className="empty-hint">No records in this category.</p>}
            </div>
          </Card>
        </>
      )}
    </>
  );
}
