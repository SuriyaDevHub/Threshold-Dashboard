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
              {"Alert" in record.outcome && (
                <span className={`badge ${record.outcome.Alert ? "badge--breach" : "badge--pass"}`}>
                  {record.outcome.Alert ? "ALERT" : "CLEAR"}
                </span>
              )}
              {record.outcome.Reason && <span className="mono" style={{ marginLeft: 8 }}>{record.outcome.Reason}</span>}
              {record.outcome.Commentary && <div className="rd-trail-detail">{record.outcome.Commentary}</div>}
              {Object.entries(record.outcome).filter(([k]) => !["Alert", "Reason", "Commentary"].includes(k)).length > 0 && (
                <div className="rd-trail-detail mono">
                  {Object.entries(record.outcome)
                    .filter(([k]) => !["Alert", "Reason", "Commentary"].includes(k))
                    .map(([k, v]) => `${k} = ${v}`).join(", ")}
                </div>
              )}
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
  const RENDER_CAP = 200;
  const displayedRecords = records.slice(0, RENDER_CAP);

  // `records` only ever carries a capped trace per bucket (matched_shown /
  // not_matched_shown out of the true matched / not_matched totals) — never
  // claim it's the complete set when the cap was hit, so a sparse match
  // scattered past the cap doesn't silently read as "nothing matched". The
  // list is then rendered through its own RENDER_CAP on top of that, so
  // "shown" always reflects what's actually on screen, whichever cap bit.
  const totalForFilter = filter === "matched" ? s.matched : filter === "not_matched" ? s.not_matched : s.matched + s.not_matched;
  const isTruncated = displayedRecords.length < totalForFilter;

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

      <Card title={`Record-level explainability (${isTruncated ? `${displayedRecords.length} of ${totalForFilter}` : `${displayedRecords.length}`} shown)`}>
        <div className="tabs" style={{ marginBottom: 12 }}>
          {["all", "matched", "not_matched"].map((f) => (
            <button key={f} className={`tab ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
              {f.replace("_", " ")}
            </button>
          ))}
        </div>
        {isTruncated && (
          <div className="preview-note" style={{ marginBottom: 8 }}>
            Only the first {displayedRecords.length.toLocaleString()} of {totalForFilter.toLocaleString()} {filter === "all" ? "records" : filter.replace("_", " ")} get a per-record trace kept for a dry run this size — the totals above still cover every record. Narrow the sample (e.g. Specific IDs) to inspect ones outside this list.
          </div>
        )}
        <div className="rd-record-list">
          {displayedRecords.map((r, i) => <RecordRow record={r} key={i} />)}
        </div>
      </Card>
    </>
  );
}
