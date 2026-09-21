import { useState } from "react";
import { ChevronDown, ChevronRight, Check, X, AlertTriangle } from "lucide-react";
import { Stat, Card } from "../../../components/ui.jsx";

function StepIcon({ status }) {
  if (status === "ok") return <Check size={13} className="rd-step-ok" />;
  if (status === "skipped") return <X size={13} className="rd-step-skip" />;
  if (status === "lookup_miss") return <AlertTriangle size={13} className="rd-step-warn" />;
  return <X size={13} className="rd-step-err" />;
}

function RecordRow({ record }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`rd-record ${record.matched ? "rd-record--matched" : ""}`}>
      <button className="rd-record-head" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span className="mono">{String(record.record_id)}</span>
        <span className={`badge ${record.matched ? "badge--pass" : ""}`}>
          {record.matched ? "MATCHED" : "no match"}
        </span>
        {record.error && <span className="badge badge--breach">{record.error}</span>}
      </button>
      {open && (
        <div className="rd-record-trail">
          {record.trail.map((step, i) => (
            <div className="rd-trail-step" key={i}>
              <StepIcon status={step.status} />
              <span className="rd-trail-label">[{step.node_type}] {step.label}</span>
              {step.detail && <span className="rd-trail-detail mono">{step.detail}</span>}
            </div>
          ))}
          {record.matched && Object.keys(record.outcome || {}).length > 0 && (
            <div className="rd-trail-outcome">
              <strong>Outcome:</strong>{" "}
              {Object.entries(record.outcome).map(([k, v]) => `${k} = ${v}`).join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function DryRunResults({ result }) {
  const [filter, setFilter] = useState("all"); // all | matched | not_matched
  if (!result) return null;
  const s = result.summary;
  const records = (result.records || []).filter((r) =>
    filter === "all" ? true : filter === "matched" ? r.matched : !r.matched);

  return (
    <>
      <div className="stat-grid">
        <Stat label="Total records" value={s.total_records} />
        <Stat label="Matched" value={s.matched} status="pass" sub={`${s.match_rate_pct}%`} />
        <Stat label="Not matched" value={s.not_matched} />
        <Stat label="Lookup failures" value={s.lookup_failures} status={s.lookup_failures ? "watch" : undefined} />
      </div>
      <div className="stat-grid">
        <Stat label="Errors" value={s.errors} status={s.errors ? "breach" : undefined} />
        <Stat label="Execution time" value={`${s.execution_time_s}s`} />
        <Stat label="Match rate" value={`${s.match_rate_pct}%`} />
        <Stat label="Sample mode" value={result.sample_mode} />
      </div>

      {result.enrichment?.length > 0 && (
        <Card title="Enrichment diagnostics">
          {result.enrichment.map((e) => (
            <div key={e.node_id} className="rd-enrich-row">
              <div className="rd-enrich-label">{e.label}</div>
              <div className="rd-enrich-stats mono">
                {e.input_records} in · {e.successful_lookups} matched · {e.lookup_failures} failed ·{" "}
                {e.match_rate_pct}% match rate
              </div>
              {Object.keys(e.duplicate_keys || {}).length > 0 && (
                <div className="benefit-warn">
                  Duplicate lookup keys: {Object.entries(e.duplicate_keys).map(([k, c]) => `${k} (${c})`).join(", ")}
                </div>
              )}
              {e.unmatched_keys?.length > 0 && (
                <div className="preview-note">
                  Unmatched: {e.unmatched_keys.slice(0, 15).map((k) => JSON.stringify(k)).join(", ")}
                  {e.unmatched_keys.length > 15 ? "…" : ""}
                </div>
              )}
            </div>
          ))}
        </Card>
      )}

      <Card title={`Record-level explainability (${records.length} shown)`}>
        <div className="tabs" style={{ marginBottom: 12 }}>
          {["all", "matched", "not_matched"].map((f) => (
            <button key={f} className={`tab ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
              {f.replace("_", " ")}
            </button>
          ))}
        </div>
        <div className="rd-record-list">
          {records.slice(0, 200).map((r, i) => <RecordRow record={r} key={i} />)}
        </div>
      </Card>
    </>
  );
}
