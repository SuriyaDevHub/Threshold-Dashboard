import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Download, ImageDown } from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Stat, Loader, ErrorState } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { fmtDate } from "../format.js";

const COLUMNS = [
  { key: "name", label: "Rule" },
  { key: "productCode", label: "Product" },
  { key: "status", label: "Status" },
  { key: "hits", label: "Hits" },
  { key: "hit_pct", label: "Hit %" },
];

// Fixed categorical order (never cycled/reassigned by rank — a product
// keeps its color whether or not it's currently filtered in, and however
// many other series happen to be visible), validated for adjacent-pair
// colorblind safety up to 8 series (dataviz skill's reference palette,
// used unmodified — see references/palette.md).
const CATEGORICAL_PALETTE = [
  "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948",
];
const OTHER_COLOR = "#898781"; // muted ink — palette's own "axis/labels" role, for the fold-in bucket

function productColorMap(allProductCodes) {
  const sorted = [...allProductCodes].sort();
  const map = {};
  sorted.slice(0, CATEGORICAL_PALETTE.length).forEach((code, i) => { map[code] = CATEGORICAL_PALETTE[i]; });
  if (sorted.length > CATEGORICAL_PALETTE.length) map._other = sorted.slice(CATEGORICAL_PALETTE.length);
  return map;
}

// Every row's own STATUS (see rule_usage_service.compute_usage) collapses
// to one of three business-meaningful buckets: "alerted" (STATUS=ALERT —
// a real exception, nothing cleared it), "cleared_business" (STATUS=CLEAR
// with a RULE_ID — a business rule evaluated the record and its own logic
// cleared it) and "cleared_mkt" (STATUS=CLEAR with no RULE_ID — cleared by
// market-data validation, not any one rule). Optionally narrowed to a
// single product — this is what drives the trend chart when the Product
// filter below has a value selected.
function buildStatusChartData(dailyStatusCounts, productFilter) {
  const byDate = {};
  for (const { date, product, category, count } of dailyStatusCounts) {
    if (productFilter && product !== productFilter) continue;
    const row = byDate[date] || { date, alerted: 0, cleared_business: 0, cleared_mkt: 0 };
    row[category] = (row[category] || 0) + count;
    byDate[date] = row;
  }
  return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
}

// Per-product ALERT totals (across whatever date window is loaded) for the
// compact distribution legend shown beside the chart when it's showing the
// all-products aggregate rather than one selected product.
function buildProductAlertDistribution(dailyStatusCounts) {
  const totals = {};
  for (const { product, category, count } of dailyStatusCounts) {
    if (category !== "alerted") continue;
    totals[product] = (totals[product] || 0) + count;
  }
  return Object.entries(totals)
    .map(([product, count]) => ({ product, count }))
    .sort((a, b) => b.count - a.count);
}

// Sums one or more status categories, scoped to a single product when
// productFilter is set (reading product_status_totals — the same
// per-product breakdown rule_usage_service keeps for exactly this, rather
// than summing daily_status_counts, which silently excludes undated rows)
// or across every product when it isn't (status_totals).
function sumCategoryTotals(usageData, productFilter, categories) {
  if (!usageData?.available) return null;
  const totals = productFilter
    ? (usageData.product_status_totals?.[productFilter] || {})
    : (usageData.status_totals || {});
  return categories.reduce((s, c) => s + (totals[c] || 0), 0);
}

// A percent change computed off a tiny prior-period base is noise, not
// signal — 620 vs a prior period of 29 reads as "+2038%", which alarms
// without informing (one more/fewer row back then swings it by dozens of
// points). Below this floor, the absolute change and the prior number
// itself are shown instead of a percentage.
const MIN_PRIOR_FOR_PCT = 20;
function computeDelta(current, prior) {
  if (current == null || prior == null) return null;
  const diff = current - prior;
  if (prior < MIN_PRIOR_FOR_PCT) return { kind: "absolute", diff, prior };
  return { kind: "pct", pct: Math.round((diff / prior) * 1000) / 10 };
}
function deltaLabel(delta) {
  if (!delta) return null;
  if (delta.kind === "pct") return `${delta.pct > 0 ? "+" : ""}${delta.pct}% vs prior period`;
  const { diff, prior } = delta;
  return `${diff > 0 ? "+" : ""}${diff} vs prior period (only ${prior} then — too few for a %)`;
}

// The [dateFrom, dateTo] window's own length, immediately preceding it —
// e.g. window 09-20..09-24 (5 days) compares against 09-15..09-19.
function priorPeriod(dateFrom, dateTo) {
  if (!dateFrom || !dateTo) return null;
  const from = new Date(`${dateFrom}T00:00:00Z`);
  const to = new Date(`${dateTo}T00:00:00Z`);
  const spanDays = Math.round((to - from) / 86400000) + 1;
  const priorTo = new Date(from);
  priorTo.setUTCDate(priorTo.getUTCDate() - 1);
  const priorFrom = new Date(priorTo);
  priorFrom.setUTCDate(priorFrom.getUTCDate() - (spanDays - 1));
  const iso = (d) => d.toISOString().slice(0, 10);
  return { from: iso(priorFrom), to: iso(priorTo) };
}

function csvEscape(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// The default recharts tooltip lists each line's value with no total, so
// a viewer has to add Alerted + both Cleared lines themselves to see the
// day's full exception count. This adds that sum as a fourth, visually
// distinct row.
function ChartTooltip({ active, payload, label }) {
  if (!active || !payload || !payload.length) return null;
  const total = payload.reduce((s, p) => s + (p.value || 0), 0);
  return (
    <div style={{
      background: "#ffffff", border: "1px solid var(--line, #e4e8ee)", borderRadius: 8,
      padding: "8px 12px", fontSize: 12, boxShadow: "var(--shadow, 0 1px 3px rgba(16,24,40,0.08))",
    }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} style={{ color: p.color }}>{p.name} : {p.value}</div>
      ))}
      <div style={{ marginTop: 4, paddingTop: 4, borderTop: "1px solid var(--line, #e4e8ee)", fontWeight: 600 }}>
        Total : {total}
      </div>
    </div>
  );
}

function SortIcon({ active, dir }) {
  if (!active) return <ArrowUpDown size={12} style={{ opacity: 0.35, marginLeft: 4, verticalAlign: "-2px" }} />;
  const Icon = dir === "asc" ? ArrowUp : ArrowDown;
  return <Icon size={12} style={{ marginLeft: 4, verticalAlign: "-2px" }} />;
}

export default function Dashboard({ onOpenRule }) {
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [rangeInitialized, setRangeInitialized] = useState(false);
  const [productFilter, setProductFilter] = useState("");
  const [search, setSearch] = useState("");
  const [unusedOnly, setUnusedOnly] = useState(false);
  const [sort, setSort] = useState({ key: "hits", dir: "asc" });

  const { loading, data, error } = useAsync(
    useCallback(() => rd.ruleUsage(dateFrom || undefined, dateTo || undefined), [dateFrom, dateTo]),
    [dateFrom, dateTo],
  );

  // A second, silent fetch for the window immediately preceding the
  // selected one, purely to give the "exceptions caught" KPI a trend
  // direction — same data shape, just a different date range.
  const priorRange = useMemo(() => priorPeriod(dateFrom, dateTo), [dateFrom, dateTo]);
  const { data: priorData } = useAsync(
    useCallback(
      () => (priorRange ? rd.ruleUsage(priorRange.from, priorRange.to) : Promise.resolve(null)),
      [priorRange],
    ),
    [priorRange],
  );

  // Default the picker to the data's own span on first successful load,
  // rather than leaving it blank — the user narrows from "everything" down,
  // same as the reference dashboard's date picker starting populated.
  useEffect(() => {
    if (data?.available && !rangeInitialized && data.earliest_date && data.latest_date) {
      setRangeInitialized(true);
      setDateFrom(data.earliest_date);
      setDateTo(data.latest_date);
    }
  }, [data, rangeInitialized]);

  const rows = useMemo(() => {
    if (!data?.available) return [];
    return data.products.flatMap((p) =>
      p.rules.map((r) => ({ ...r, productCode: p.code, productName: p.name })),
    );
  }, [data]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (productFilter && r.productCode !== productFilter) return false;
      if (unusedOnly && !r.unused) return false;
      if (q && !r.name.toLowerCase().includes(q) && !r.rule_id.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [rows, productFilter, search, unusedOnly]);

  const sorted = useMemo(() => {
    const { key, dir } = sort;
    const mul = dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "string") return av.localeCompare(bv) * mul;
      return (av - bv) * mul;
    });
  }, [filtered, sort]);

  // Every rule row narrowed to the selected product only — this is the
  // scope the whole KPI band (exceptions caught/cleared, coverage, rules
  // needing attention) uses, independent of the table's own search/
  // not-seen-only filters below (those stay table-specific).
  const productScoped = useMemo(
    () => (productFilter ? rows.filter((r) => r.productCode === productFilter) : rows),
    [rows, productFilter],
  );

  // "Exceptions caught"/"Exceptions cleared" — ALERT vs. CLEAR rows for
  // the current product scope — plus each one's direction vs. the
  // immediately preceding, equal-length window.
  const totalAlerts = useMemo(() => sumCategoryTotals(data, productFilter, ["alerted"]), [data, productFilter]);
  const priorTotalAlerts = useMemo(
    () => sumCategoryTotals(priorData, productFilter, ["alerted"]), [priorData, productFilter],
  );
  const alertsDelta = useMemo(() => computeDelta(totalAlerts, priorTotalAlerts), [totalAlerts, priorTotalAlerts]);

  const totalCleared = useMemo(
    () => sumCategoryTotals(data, productFilter, ["cleared_business", "cleared_mkt"]), [data, productFilter],
  );
  const priorTotalCleared = useMemo(
    () => sumCategoryTotals(priorData, productFilter, ["cleared_business", "cleared_mkt"]), [priorData, productFilter],
  );
  const clearedDelta = useMemo(() => computeDelta(totalCleared, priorTotalCleared), [totalCleared, priorTotalCleared]);

  // Coverage — of every published, enabled (i.e. actually live) rule in
  // scope, what share caught at least one exception in this window. A
  // published rule with zero hits is either quiet (nothing wrong) or
  // broken (silently not firing) — coverage can't tell those apart, but
  // it flags how many rules need a human to make that call (see
  // deadRules below).
  const liveRules = useMemo(
    () => productScoped.filter((r) => r.status === "PUBLISHED" && r.enabled), [productScoped],
  );
  const coveragePct = useMemo(() => {
    if (!liveRules.length) return null;
    return Math.round((liveRules.filter((r) => r.hits > 0).length / liveRules.length) * 100);
  }, [liveRules]);

  const deadRules = useMemo(() => productScoped.filter((r) => r.unused), [productScoped]);

  // Top rules by impact — respects the same product/search/unused filters
  // as the table below, always ranked by hits regardless of the table's
  // own sort column.
  const leaderboardRules = useMemo(
    () => [...filtered].filter((r) => r.hits > 0).sort((a, b) => b.hits - a.hits).slice(0, 8),
    [filtered],
  );
  const maxLeaderboardHits = leaderboardRules[0]?.hits || 1;

  // Driven by the Product filter below: a product selected narrows the
  // trend to that product's own three lines; "all products" (the default)
  // aggregates every product into the same three lines, with a compact
  // per-product distribution legend alongside for the alert share.
  const statusChartData = useMemo(
    () => (data?.available ? buildStatusChartData(data.daily_status_counts, productFilter) : []),
    [data, productFilter],
  );
  const productAlertDistribution = useMemo(
    () => (data?.available ? buildProductAlertDistribution(data.daily_status_counts) : []),
    [data],
  );

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }

  const colorMap = useMemo(
    () => productColorMap((data?.available ? data.products : []).map((p) => p.code)),
    [data],
  );
  const chartWrapRef = useRef(null);

  function exportTableCsv() {
    const headers = ["Rule", "Rule ID", "Product", "Status", "Enabled", "Hits", "Hit %", "Not seen"];
    const lines = [headers.join(",")];
    for (const r of sorted) {
      lines.push([
        r.name, r.rule_id, r.productCode, r.status, r.enabled ? "yes" : "no",
        r.hits, r.hit_pct !== null ? r.hit_pct : "", r.unused ? "yes" : "no",
      ].map(csvEscape).join(","));
    }
    downloadBlob(new Blob([lines.join("\n")], { type: "text/csv" }), "rule_usage.csv");
  }

  function exportChartPng() {
    // The wrapped node holds the title *and* the chart — recharts renders
    // its <Legend> as an HTML <ul> sibling of the <svg> (not inside it), so
    // grabbing just the inner <svg> (the old approach) silently drops the
    // legend and the title lives outside the chart wrapper entirely.
    // Serializing the whole node inside an <svg><foreignObject> is the
    // standard way to rasterize mixed HTML+SVG DOM without a new dependency.
    const node = chartWrapRef.current;
    if (!node) return;
    const width = node.getBoundingClientRect().width;

    const clone = node.cloneNode(true);
    clone.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
    clone.style.width = `${width}px`;
    clone.style.height = "auto";
    clone.style.background = "#ffffff";
    // The isolated, data-URI'd SVG has no access to the page's stylesheet,
    // so text would otherwise fall back to the browser's serif default —
    // set the font explicitly (inherited by every descendant) and let
    // color/other properties fall back to their (black-ish) initial values.
    // Changing the font can itself reflow text (e.g. wrap the legend onto
    // an extra line), so this must happen *before* the clone's height is
    // measured below — measuring the unstyled live node first and reusing
    // that number here previously clipped exactly that extra line.
    clone.style.fontFamily = "system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif";
    clone.style.color = "#1a2027";

    // Lay the clone out for real (off-screen) so its height reflects the
    // fonts/styles it will actually render with, including any content —
    // like recharts' absolutely-positioned <Legend> — that overflows the
    // wrapper's own box without enlarging it.
    clone.style.position = "fixed";
    clone.style.top = "-10000px";
    clone.style.left = "-10000px";
    clone.style.visibility = "hidden";
    document.body.appendChild(clone);
    const cloneRect = clone.getBoundingClientRect();
    let maxBottom = cloneRect.bottom;
    for (const el of clone.querySelectorAll("*")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      if (r.bottom > maxBottom) maxBottom = r.bottom;
    }
    // Chromium's offscreen rasterization of a foreignObject (the path
    // canvas.drawImage takes) measures text a few pixels taller than the
    // same markup's on-screen paint — a bottom row that fits exactly on
    // screen can still get clipped in the exported bitmap. A fixed safety
    // margin absorbs that discrepancy; it's blank canvas, so it costs
    // nothing but a sliver of white space below the legend.
    const SAFETY_MARGIN = 32;
    const height = (maxBottom - cloneRect.top) + SAFETY_MARGIN;
    document.body.removeChild(clone);
    clone.style.position = "";
    clone.style.top = "";
    clone.style.left = "";
    clone.style.visibility = "";
    clone.style.height = `${maxBottom - cloneRect.top}px`;

    const svgNS = "http://www.w3.org/2000/svg";
    const outer = document.createElementNS(svgNS, "svg");
    outer.setAttribute("xmlns", svgNS);
    outer.setAttribute("width", width);
    outer.setAttribute("height", height);
    outer.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const bg = document.createElementNS(svgNS, "rect");
    bg.setAttribute("width", "100%");
    bg.setAttribute("height", "100%");
    bg.setAttribute("fill", "#ffffff");
    outer.appendChild(bg);
    const foreignObject = document.createElementNS(svgNS, "foreignObject");
    foreignObject.setAttribute("width", "100%");
    foreignObject.setAttribute("height", `${height}`);
    foreignObject.appendChild(clone);
    outer.appendChild(foreignObject);
    const svgText = new XMLSerializer().serializeToString(outer);

    const img = new Image();
    img.onload = () => {
      const scale = 2; // export at 2x for a crisper paste into a slide
      const canvas = document.createElement("canvas");
      canvas.width = width * scale; canvas.height = height * scale;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0, width, height);
      canvas.toBlob((blob) => blob && downloadBlob(blob, "rule_usage_trend.png"));
    };
    // A blob: object URL here taints the canvas ("Tainted canvases may not
    // be exported") for this specific foreignObject-SVG-to-canvas path in
    // Chromium — a data: URI does not.
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgText);
  }

  if (loading && !data) return <Loader label="Loading rule usage…" />;
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

  const alertsDeltaLabel = deltaLabel(alertsDelta);
  const clearedDeltaLabel = deltaLabel(clearedDelta);
  const selectedProductName = productFilter
    ? (data.products.find((p) => p.code === productFilter)?.name || productFilter)
    : null;
  const scopeLabel = selectedProductName || "all products";

  return (
    <>
      <p className="preview-note" style={{ margin: "0 0 12px" }}>
        Data as of {fmtDate(data.as_of)} · <span className="mono">{data.csv_path}</span> · {data.total_rows} live rows in the full feed
      </p>

      <Card>
        <div className="controls controls--row">
          <label className="control"><span>From</span>
            <input type="date" value={dateFrom} min={data.earliest_date || undefined} max={dateTo || data.latest_date || undefined}
                   onChange={(e) => setDateFrom(e.target.value)} />
          </label>
          <label className="control"><span>To</span>
            <input type="date" value={dateTo} min={dateFrom || data.earliest_date || undefined} max={data.latest_date || undefined}
                   onChange={(e) => setDateTo(e.target.value)} />
          </label>
          <label className="control"><span>Product</span>
            <select value={productFilter} onChange={(e) => setProductFilter(e.target.value)}>
              <option value="">all products</option>
              {data.products.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
            </select>
          </label>
          <label className="control" style={{ minWidth: 220 }}><span>Search</span>
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="rule name or id…" />
          </label>
          <label className="control" style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={unusedOnly} onChange={(e) => setUnusedOnly(e.target.checked)} />
            <span>Not seen only</span>
          </label>
          <div className="control" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn--ghost" onClick={exportTableCsv} disabled={sorted.length === 0}>
              <Download size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Export table (CSV)
            </button>
          </div>
        </div>
        <p className="preview-note" style={{ margin: "10px 0 0" }}>
          Product, Search and "Not seen only" filter the whole page below — the KPI band, leaderboard, trend
          chart and rules table all scope to {scopeLabel}
          {(search.trim() || unusedOnly) && ", further narrowed by search/not-seen-only where noted"}.
        </p>
      </Card>

      <div className="stat-grid">
        <Stat label="Exceptions caught" value={totalAlerts ?? "—"} sub={alertsDeltaLabel || `ALERTed rows — ${scopeLabel}`} />
        <Stat label="Exceptions cleared" value={totalCleared ?? "—"} sub={clearedDeltaLabel || `CLEARed rows — ${scopeLabel}`} />
        <Stat label="Rule coverage" value={coveragePct == null ? "—" : `${coveragePct}%`}
              sub={`${liveRules.filter((r) => r.hits > 0).length} of ${liveRules.length} live rules — ${scopeLabel}`}
              status={coveragePct == null ? undefined : coveragePct >= 80 ? "pass" : coveragePct >= 50 ? "watch" : "breach"} />
        <Stat label="Rules needing attention" value={deadRules.length}
              sub={`published, enabled, zero hits — ${scopeLabel}`} status={deadRules.length ? "breach" : "pass"} />
      </div>

      <Card title="Top rules by impact">
        {leaderboardRules.length === 0 ? (
          <p className="empty-hint">No rules with hits in the current window/filters.</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {leaderboardRules.map((r, i) => {
              const color = colorMap[r.productCode] || OTHER_COLOR;
              return (
                <div key={`${r.productCode}:${r.rule_id}`}
                     className="rd-clickable-row"
                     onClick={() => onOpenRule(r.rule_id)}
                     style={{ display: "flex", alignItems: "center", gap: 12, padding: "6px 8px", borderRadius: 8 }}>
                  <span className="mono" style={{ width: 18, color: "var(--muted)", fontSize: 12, flexShrink: 0 }}>{i + 1}</span>
                  <div style={{ width: 220, flexShrink: 0, minWidth: 0 }} title={r.name}>
                    <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</div>
                    <div className="mono ds-id">{r.rule_id} · {r.productCode}</div>
                  </div>
                  <div style={{ flex: 1, minWidth: 0, background: "#eef1f4", borderRadius: 4, height: 10 }}>
                    <div style={{
                      width: `${Math.max(2, (r.hits / maxLeaderboardHits) * 100)}%`, height: "100%",
                      background: color, borderRadius: 4,
                    }} />
                  </div>
                  <span className="mono" style={{ width: 56, textAlign: "right", flexShrink: 0 }}>{r.hits}</span>
                  <span className="mono" style={{ width: 56, textAlign: "right", flexShrink: 0, color: "var(--muted)" }}>
                    {r.hit_pct !== null ? `${r.hit_pct}%` : "—"}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
          <div ref={chartWrapRef} style={{ flex: 1, minWidth: 0, background: "#ffffff" }}>
            <h2 className="card-title" style={{ margin: "0 0 4px" }}>
              Exceptions caught over time — {selectedProductName || "all products"}
            </h2>
            <p className="preview-note" style={{ margin: "0 0 14px" }}>
              <strong style={{ color: "var(--breach, #c0392b)" }}>Alerted</strong>: a real exception, nothing
              cleared it. <strong style={{ color: "var(--pass, #1f8a4c)" }}>Cleared (business rule)</strong>: a
              rule evaluated the record and its own logic cleared it. <strong style={{ color: "var(--muted, #5b6775)" }}>
              Cleared (market data)</strong>: cleared by market-data validation, not any one rule.
            </p>
            {statusChartData.length === 0 ? (
              <p className="empty-hint">No dated rows in the current window to chart.</p>
            ) : (
              <div style={{ width: "100%", height: 260 }}>
                <ResponsiveContainer>
                  <LineChart data={statusChartData} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef1f4" />
                    <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <Tooltip content={<ChartTooltip />} />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Line type="monotone" dataKey="alerted" name="Alerted" dot={false}
                          strokeWidth={2} stroke="var(--breach, #c0392b)" />
                    <Line type="monotone" dataKey="cleared_business" name="Cleared (business rule)" dot={false}
                          strokeWidth={2} stroke="var(--pass, #1f8a4c)" />
                    <Line type="monotone" dataKey="cleared_mkt" name="Cleared (market data)" dot={false}
                          strokeWidth={2} stroke="var(--muted, #5b6775)" />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "stretch", gap: 12, flexShrink: 0, width: 190 }}>
            {!productFilter && productAlertDistribution.length > 0 && (() => {
              const distributionTotal = productAlertDistribution.reduce((s, d) => s + d.count, 0);
              return (
                <div title="Share of this window's Alerted rows by product — click a product in the filter below to see just its trend.">
                  <div style={{
                    fontSize: 11, fontWeight: 600, color: "var(--muted)",
                    textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 2,
                  }}>
                    Alerts by product
                  </div>
                  <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 8 }}>
                    share of {distributionTotal} alerted rows
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {productAlertDistribution.map(({ product, count }) => {
                      const pct = distributionTotal ? Math.round((count / distributionTotal) * 100) : 0;
                      return (
                        <div key={product}
                             title={`${product} — ${count} alerted row${count === 1 ? "" : "s"} (${pct}% of ${distributionTotal} in this window)`}
                             style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                          <span style={{
                            width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                            background: colorMap[product] || OTHER_COLOR,
                          }} />
                          <span className="mono" style={{
                            flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                          }}>{product}</span>
                          <span className="mono">{count}</span>
                          <span className="mono" style={{ width: 34, textAlign: "right", color: "var(--muted)" }}>{pct}%</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })()}
            <button className="btn btn--ghost" onClick={exportChartPng} disabled={statusChartData.length === 0}
                    style={{ alignSelf: "flex-end" }}>
              <ImageDown size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Export chart (PNG)
            </button>
          </div>
        </div>
      </Card>

      <Card title="Rules needing attention">
        {deadRules.length === 0 ? (
          <p className="empty-hint">
            No governance gaps — every published, enabled rule caught at least one exception in this window.
          </p>
        ) : (
          <>
            <p className="preview-note" style={{ marginBottom: 10 }}>
              Published and enabled, but zero hits in the selected window — either genuinely quiet, or silently
              not firing. Worth a human look either way.
            </p>
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Rule</th><th>Product</th><th>Status</th></tr></thead>
                <tbody>
                  {deadRules.map((r) => (
                    <tr key={`${r.productCode}:${r.rule_id}`} className="rd-clickable-row" onClick={() => onOpenRule(r.rule_id)}>
                      <td style={{ minWidth: 220 }}>
                        <div>{r.name}</div>
                        <div className="mono ds-id">{r.rule_id}</div>
                      </td>
                      <td className="mono">{r.productCode}</td>
                      <td><span className="badge badge--breach">NOT SEEN</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Card>

      <Card title="Rules matching the current filters">
        {loading && <p className="empty-hint">Refreshing…</p>}

        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c.key} style={{ cursor: "pointer", userSelect: "none" }} onClick={() => toggleSort(c.key)}>
                    {c.label}
                    <SortIcon active={sort.key === c.key} dir={sort.dir} />
                  </th>
                ))}
                <th />
              </tr>
            </thead>
            <tbody>
              {sorted.map((r) => (
                <tr key={`${r.productCode}:${r.rule_id}`} className="rd-clickable-row" onClick={() => onOpenRule(r.rule_id)}>
                  <td style={{ minWidth: 220 }}>
                    <div>{r.name}</div>
                    <div className="mono ds-id">{r.rule_id}</div>
                  </td>
                  <td className="mono">{r.productCode}</td>
                  <td>
                    <span className={`rd-status rd-status--${r.status.toLowerCase()}`}>{r.status.replace(/_/g, " ")}</span>
                    {!r.enabled && <span className="badge" style={{ marginLeft: 6 }}>disabled</span>}
                  </td>
                  <td className="mono">{r.hits}</td>
                  <td className="mono">{r.hit_pct !== null ? `${r.hit_pct}%` : "—"}</td>
                  <td>{r.unused && <span className="badge badge--breach">NOT SEEN</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {sorted.length === 0 && <p className="empty-hint" style={{ padding: 16 }}>No rules match the current filters.</p>}
        </div>
      </Card>
    </>
  );
}
