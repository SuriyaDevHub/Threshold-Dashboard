import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client.js";
import { useAsync } from "../../lib/useAsync.js";
import { ModuleHeader, Card, Stat, Loader, ErrorState } from "../../components/ui.jsx";
import { registry } from "../../registry.js";
import Icon from "../../components/Icon.jsx";

export const meta = {
  id: "home",
  title: "Overview",
  description: "Pull data once, run every module on it.",
  icon: "dashboard",
  path: "/",
  order: 0,
};

export default function Home() {
  const navigate = useNavigate();
  const { loading, data, error } = useAsync(
    useCallback(() => api.module("data-fetch", "/datasets"), []),
    []
  );
  const datasets = data?.datasets || [];
  const trades = datasets.filter((d) => d.source === "BRV_S3");
  const exceptions = datasets.filter((d) => d.source === "EPE");

  return (
    <>
      <ModuleHeader
        title="Off-Market Rate Control"
        description="Data Fetch is the source of truth: pull from EPE / BRV S3 once, then Threshold Analysis and Audit Sampling run on the cached dataset."
      />

      {loading && <Loader />}
      {error && <ErrorState error={error} />}

      {data && (
        <>
          <div className="stat-grid">
            <Stat label="Datasets loaded" value={datasets.length} />
            <Stat label="BRV S3 (trades)" value={trades.length} />
            <Stat label="EPE (exceptions)" value={exceptions.length} />
            <Stat label="Total rows cached" value={datasets.reduce((s, d) => s + d.row_count, 0)} />
          </div>

          <Card title="Modules">
            <div className="module-grid">
              {registry.filter((m) => m.path !== "/").map((m) => (
                <button key={m.id} className="module-card" onClick={() => navigate(m.path)}>
                  <div className="module-card-icon"><Icon name={m.icon} size={20} /></div>
                  <div className="module-card-title">{m.title}</div>
                  <div className="module-card-desc">{m.description}</div>
                </button>
              ))}
            </div>
          </Card>

          {datasets.length === 0 && (
            <Card>
              <div className="empty">
                <div className="empty-title">No datasets yet.</div>
                <div className="empty-hint">Head to Data Fetch and pull a trade or exception dataset to begin.</div>
              </div>
            </Card>
          )}
        </>
      )}
    </>
  );
}
