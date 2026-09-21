import { useCallback } from "react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Stat, Loader, ErrorState } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { fmtDate } from "../format.js";

export default function Dashboard({ onOpenRule }) {
  const { loading, data, error } = useAsync(useCallback(() => rd.dashboard(), []), []);
  if (loading) return <Loader label="Loading dashboard…" />;
  if (error) return <ErrorState error={error} />;
  if (!data) return null;

  return (
    <>
      <div className="stat-grid">
        <Stat label="Total rules" value={data.total_rules} />
        <Stat label="Published" value={data.published} status="pass" />
        <Stat label="Draft" value={data.draft} />
        <Stat label="Pending approval" value={data.pending_approval} status={data.pending_approval ? "watch" : undefined} />
      </div>
      <div className="stat-grid">
        <Stat label="Reference files" value={data.reference_files} />
        <Stat label="Versions published" value={data.versions_published} />
      </div>

      <Card title="Products">
        <div className="ds-list">
          {data.products.map((p) => (
            <div key={p.code} className="ds-row" style={{ cursor: "default" }}>
              <span className="ds-label">{p.name} <span className="mono ds-id">{p.code}</span></span>
              {!p.enabled && <span className="badge badge--breach">disabled</span>}
              <span className="ds-count mono">{p.rule_count} rule(s)</span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Recently updated rules">
        {data.recent_rules.length === 0 && <p className="empty-hint">No rules yet — create one in the Rules tab.</p>}
        <div className="ds-list">
          {data.recent_rules.map((r) => (
            <button key={r.rule_id} className="ds-row" style={{ width: "100%", textAlign: "left" }} onClick={() => onOpenRule(r.rule_id)}>
              <span className="ds-label">{r.name} <span className="mono ds-id">{r.rule_id}</span></span>
              <span className="mono ds-id">{r.product}</span>
              <span className={`rd-status rd-status--${r.status.toLowerCase()}`}>{r.status.replace(/_/g, " ")}</span>
              <span className="ds-count mono">{fmtDate(r.updated_at)} · {r.updated_by}</span>
            </button>
          ))}
        </div>
      </Card>

      <Card title="Recent activity">
        <div className="ds-list">
          {data.recent_activity.map((e) => (
            <div key={e.id} className="ds-row" style={{ cursor: "default" }}>
              <span className="badge">{e.action}</span>
              <span className="ds-label">{e.rule_id || e.detail || "—"} {e.detail && e.rule_id ? `— ${e.detail}` : ""}</span>
              <span className="ds-count mono">{e.actor} · {fmtDate(e.timestamp)}</span>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}
