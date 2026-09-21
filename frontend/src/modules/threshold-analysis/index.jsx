import { useState, useCallback, useEffect } from "react";
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, Cell, CartesianGrid,
} from "recharts";
import { api } from "../../api/client.js";
import { useAsync } from "../../lib/useAsync.js";
import { ModuleHeader, Card, Stat, Loader, ErrorState, StatusBadge } from "../../components/ui.jsx";
import DataTable from "../../components/DataTable.jsx";
import DatasetSelector from "../../components/DatasetSelector.jsx";

export const meta = {
  id: "threshold-analysis",
  title: "Threshold Analysis",
  description: "Per-product calibration & backtesting (dispatched by product type).",
  icon: "sliders",
  path: "/threshold-analysis",
  order: 2,
};

const STATUS_COLOR = { pass: "#1F8A4C", watch: "#C9821A", breach: "#C0392B" };
const alertStatus = (r) => (r >= 10 ? "breach" : r >= 4 ? "watch" : "pass");

function fmtUnit(v, unit) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (unit === "ccy") return Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (unit === "%") return `${Number(v).toFixed(3)}%`;
  if (unit) return `${Number(v).toFixed(2)} ${unit}`;
  return v;
}

// Build DataTable columns from an envelope's column specs.
function toColumns(specs) {
  return specs.map((c) => ({
    key: c.key, label: c.label, align: c.align, mono: c.mono,
    render: (val) => {
      if (c.status === "alert")
        return <span className="cell-status">{Number(val).toFixed(1)}% <StatusBadge status={alertStatus(val)} /></span>;
      const out = fmtUnit(val, c.unit);
      return c.strong ? <strong>{out}</strong> : out;
    },
  }));
}

// Generic param controls rendered from /config param specs.
function ParamControls({ specs, values, set }) {
  return (
    <div className="controls controls--row">
      {specs.map((p) =>
        p.type === "select" ? (
          <label className="control" key={p.key}><span>{p.label}</span>
            <select value={values[p.key] ?? p.default} onChange={(e) => set(p.key, e.target.value)}>
              {p.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </label>
        ) : (
          <label className="control" key={p.key}>
            <span>{p.label}: <strong className="mono">{values[p.key] ?? p.default}</strong></span>
            <input type="range" min={p.min} max={p.max} step={p.step}
              value={values[p.key] ?? p.default}
              onChange={(e) => set(p.key, Number(e.target.value))} />
          </label>
        )
      )}
    </div>
  );
}

function useParams(specs) {
  const [values, setValues] = useState({});
  useEffect(() => {
    if (specs) setValues(Object.fromEntries(specs.map((p) => [p.key, p.default])));
  }, [specs]);
  const set = (k, v) => setValues((s) => ({ ...s, [k]: v }));
  return [values, set];
}

export default function ThresholdAnalysis() {
  const tds = useAsync(useCallback(() => api.module("threshold-analysis", "/trade-datasets"), []), []);
  const eds = useAsync(useCallback(() => api.module("data-fetch", "/datasets", { source: "EPE" }), []), []);
  const [datasetId, setDatasetId] = useState("");
  const [tab, setTab] = useState("calibration");

  const datasets = tds.data?.datasets || [];
  const selected = datasets.find((d) => d.id === datasetId);
  useEffect(() => { if (datasets.length && !datasetId) setDatasetId(datasets[0].id); }, [datasets, datasetId]);

  return (
    <>
      <ModuleHeader title="Threshold Analysis"
        description="Calibration method is dispatched by product type — GFX Cash uses volatility banding, Cash Bonds uses per-check percentile/MAD. Backtesting scores against EPE confirmed off-market." />
      {tds.loading && <Loader label="Loading datasets…" />}
      {tds.error && <ErrorState error={tds.error} />}
      {tds.data && (
        <>
          <div className="toolbar">
            <DatasetSelector label="Trade dataset (BRV S3)" datasets={datasets} value={datasetId}
              onChange={setDatasetId} emptyHint="Pull a BRV S3 trade dataset in Data Fetch first." />
            {selected && (
              <div className="tabs">
                <button className={`tab ${tab === "calibration" ? "active" : ""}`} onClick={() => setTab("calibration")}>Calibration</button>
                <button className={`tab ${tab === "backtest" ? "active" : ""}`} onClick={() => setTab("backtest")}>Backtesting</button>
              </div>
            )}
          </div>
          {selected && <ProductPanel key={selected.id + tab} ds={selected} tab={tab} epeDatasets={eds.data?.datasets || []} />}
        </>
      )}
    </>
  );
}

function ProductPanel({ ds, tab, epeDatasets }) {
  const cfg = useAsync(useCallback(() => api.module("threshold-analysis", "/config", { product_type: ds.product_type }), [ds.product_type]), [ds.product_type]);
  const [params, set] = useParams(cfg.data?.params);
  const [epeId, setEpeId] = useState("");
  const [minutesL1, setMinutesL1] = useState(20);

  const run = useCallback(() => {
    if (!cfg.data) return Promise.resolve(null);
    const path = tab === "calibration" ? "/calibrate" : "/backtest";
    const body = tab === "calibration"
      ? { dataset_id: ds.id, params }
      : { dataset_id: ds.id, epe_dataset_id: epeId || null, params, minutes_per_l1: minutesL1 };
    return api.module("threshold-analysis", path, undefined, { method: "POST", body: JSON.stringify(body) });
  }, [cfg.data, tab, ds.id, params, epeId, minutesL1]);

  const { loading, data, error, reload } = useAsync(run, [cfg.data, tab, ds.id, JSON.stringify(params), epeId, minutesL1]);

  if (cfg.loading) return <Loader label="Loading method…" />;
  if (cfg.error) return <ErrorState error={cfg.error} />;

  return (
    <>
      <Card title={`Method — ${cfg.data.method_label}`}>
        <ParamControls specs={cfg.data.params} values={params} set={set} />
      </Card>
      {tab === "backtest" && (
        <Card title="Ground truth (EPE) & business benefit">
          <div className="controls controls--row">
            <label className="control" style={{ minWidth: 280 }}><span>EPE dataset (resolved exceptions)</span>
              <select value={epeId} onChange={(e) => setEpeId(e.target.value)}>
                <option value="">None (alert volume only)</option>
                {epeDatasets.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
              </select>
            </label>
            <label className="control"><span>Minutes per L1 review</span>
              <input type="number" min="1" max="120" value={minutesL1} onChange={(e) => setMinutesL1(Number(e.target.value))} />
            </label>
            <button className="btn" onClick={reload}>Re-run</button>
          </div>
        </Card>
      )}
      {loading && <Loader label="Running…" />}
      {error && <ErrorState error={error} />}
      {data && tab === "calibration" && <CalibrationView env={data} />}
      {data && tab === "backtest" && <BacktestView env={data} />}
    </>
  );
}

function SummaryCards({ summary }) {
  return (
    <div className="stat-grid">
      {summary.map((s, i) => <Stat key={i} label={s.label} value={s.value} status={s.status} />)}
    </div>
  );
}

function CalibrationView({ env }) {
  const chart = env.chart || {};
  const chartData = env.rows.map((r) => ({
    name: String(r[chart.x]), value: r[chart.y],
    status: r.alert_rate_pct != null ? alertStatus(r.alert_rate_pct) : "pass",
  }));
  return (
    <>
      <SummaryCards summary={env.summary} />
      <Card title={`${chart.y === "threshold" ? "Threshold" : "Alert rate"} by ${chart.x}`}>
        <div style={{ width: "100%", height: 240 }}>
          <ResponsiveContainer>
            <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
              <XAxis dataKey="name" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} unit={chart.unit === "%" ? "%" : ""} />
              <Tooltip formatter={(v) => [fmtUnit(v, chart.unit), chart.y]} contentStyle={{ fontSize: 12 }} />
              <Bar dataKey="value" radius={[3, 3, 0, 0]}>{chartData.map((d, i) => <Cell key={i} fill={STATUS_COLOR[d.status]} />)}</Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <Card title="Calibrated thresholds"><DataTable columns={toColumns(env.columns)} rows={env.rows} /></Card>
    </>
  );
}

function BenefitPanel({ b }) {
  const joinWarn = b.join_match_rate_pct < 60;
  return (
    <Card title="Business benefit — false-positive reduction vs current control">
      <div className="stat-grid">
        <Stat label="Current L1 false positives" value={b.current_false_positives}
          sub={`${b.current_fp_rate_pct}% of ${b.current_exceptions} alerts`} status="breach" />
        <Stat label="False positives eliminated" value={b.fp_eliminated}
          sub={`${b.fp_eliminated_pct}% of matched L1`} status="pass" />
        <Stat label="Genuine (L2) retained" value={`${b.tp_retained_pct}%`}
          sub={`${b.tp_retained} kept · ${b.tp_dropped} dropped`}
          status={b.tp_retained_pct >= 90 ? "pass" : b.tp_retained_pct >= 70 ? "watch" : "breach"} />
        <Stat label="Ops hours saved" value={b.hours_saved}
          sub={`@ ${b.minutes_per_l1} min / L1 review`} status="pass" />
      </div>
      <p className="benefit-note">
        Of {b.current_exceptions} exceptions raised by the current control, {b.current_false_positives} were
        closed at L1 (false positives). The candidate thresholds would remove {b.fp_eliminated} of these
        while keeping {b.tp_retained_pct}% of the genuine (L2-confirmed) off-market exceptions
        {b.tp_dropped > 0 ? ` (${b.tp_dropped} genuine dropped — lower the percentile to retain more)` : ""}.
      </p>
      {joinWarn && (
        <p className="benefit-warn">
          ⚠ Only {b.join_match_rate_pct}% of EPE exceptions ({b.join_matched}/{b.join_total}) matched a trade in
          this dataset by trade_id. Widen the trade dataset's date range or check the ID join — low match rate
          makes the numbers above understate the benefit.
        </p>
      )}
    </Card>
  );
}

function BacktestView({ env }) {
  const a = env.accuracy;
  return (
    <>
      {env.business_benefit && <BenefitPanel b={env.business_benefit} />}
      <SummaryCards summary={env.summary} />
      <Card title="Alert volume over time — candidate vs baseline">
        <div style={{ width: "100%", height: 250 }}>
          <ResponsiveContainer>
            <LineChart data={env.series} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#eef1f4" />
              <XAxis dataKey="date" tick={{ fontSize: 10 }} /><YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip contentStyle={{ fontSize: 12 }} /><Legend wrapperStyle={{ fontSize: 12 }} />
              <Line type="monotone" dataKey="candidate_flags" name="Candidate" stroke="#0E7C7B" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="baseline_flags" name="Baseline" stroke="#C9821A" strokeWidth={2} strokeDasharray="5 4" dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Card>
      {env.has_ground_truth ? (
        <>
          <div className="stat-grid">
            <Stat label="Precision" value={a.precision.toFixed(2)} status={a.precision >= 0.8 ? "pass" : a.precision >= 0.5 ? "watch" : "breach"} />
            <Stat label="Recall (vs confirmed)" value={a.recall.toFixed(2)} status={a.recall >= 0.8 ? "pass" : a.recall >= 0.5 ? "watch" : "breach"} />
            <Stat label="F1" value={a.f1.toFixed(2)} />
            <Stat label="Missed (FN)" value={a.fn} status={a.fn === 0 ? "pass" : "breach"} />
          </div>
          <Card title="Confusion — candidate vs confirmed off-market">
            <div className="confusion">
              <div className="cm-cell cm-tp"><span className="mono">{a.tp}</span><small>True positive</small></div>
              <div className="cm-cell cm-fp"><span className="mono">{a.fp}</span><small>False positive</small></div>
              <div className="cm-cell cm-fn"><span className="mono">{a.fn}</span><small>False negative</small></div>
              <div className="cm-cell cm-tn"><span className="mono">{a.tn}</span><small>True negative</small></div>
            </div>
          </Card>
        </>
      ) : (
        <Card><div className="empty"><div className="empty-title">Select an EPE dataset to score precision / recall.</div>
          <div className="empty-hint">Recall is vs confirmed off-market only — historic passes aren't labelled.</div></div></Card>
      )}
      <Card title="Per-group accuracy"><DataTable columns={toColumns(env.columns)} rows={env.rows} /></Card>
    </>
  );
}
