import { useCallback } from "react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Stat, Loader, ErrorState } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { fmtDate } from "../format.js";

export default function Dashboard({ onOpenRule }) {
  const { loading, data, error } = useAsync(useCallback(() => rd.ruleUsage(), []), []);
  if (loading) return <Loader label="Loading rule usage…" />;
  if (error) return <ErrorState error={error} />;
  if (!data) return null;

  if (!data.available) {
    return (
      <Card title="Rule usage">
        <p className="empty-hint">
          The Global Live CSV isn't available yet, so rule-usage can't be computed.
        </p>
        <p className="mono" style={{ fontSize: 12 }}>
          Configured path: {data.csv_path || "(GLOBAL_LIVE_CSV_PATH is not set)"}
        </p>
        {data.error && <p className="empty-hint">{data.error}</p>}
      </Card>
    );
  }

  const unusedCount = data.products.reduce(
    (n, p) => n + p.rules.filter((r) => r.unused).length, 0,
  );

  return (
    <>
      <div className="stat-grid">
        <Stat label="Live rows" value={data.total_rows} sub={data.csv_path} />
        <Stat label="As of" value={fmtDate(data.as_of)} />
        <Stat label="Rules never hit" value={unusedCount} status={unusedCount ? "breach" : "pass"} />
      </div>

      {data.products.map((p) => (
        <Card key={p.code} title={`${p.name} (${p.code}) — ${p.total_live_rows} live row(s)`}>
          {!p.enabled && <p className="empty-hint">Product disabled — hit rates below reflect the live file regardless.</p>}
          {p.rules.length === 0 && <p className="empty-hint">No rules yet for this product.</p>}
          <div className="ds-list">
            {p.rules.map((r) => (
              <button key={r.rule_id} className="ds-row" style={{ width: "100%", textAlign: "left" }}
                      onClick={() => onOpenRule(r.rule_id)}>
                <span className="ds-label">{r.name} <span className="mono ds-id">{r.rule_id}</span></span>
                <span className={`rd-status rd-status--${r.status.toLowerCase()}`}>{r.status.replace(/_/g, " ")}</span>
                {!r.enabled && <span className="badge">disabled</span>}
                {r.unused && <span className="badge badge--breach">NOT SEEN</span>}
                <span className="ds-count mono">
                  {r.hits} hit(s){r.hit_pct !== null ? ` · ${r.hit_pct}%` : ""}
                </span>
              </button>
            ))}
          </div>
        </Card>
      ))}
    </>
  );
}
