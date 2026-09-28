import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Download, ImageDown } from "lucide-react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid,
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
// colorblind safety on stacked bars up to 8 series (dataviz skill's
// reference palette, used unmodified — see references/palette.md).
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

function buildDailyChartData(dailyProductCounts, colorMap) {
  const otherSet = new Set(colorMap._other || []);
  const byDate = {};
  for (const { date, product, count } of dailyProductCounts) {
    const series = otherSet.has(product) ? "Other" : product;
    byDate[date] = byDate[date] || { date };
    byDate[date][series] = (byDate[date][series] || 0) + count;
  }
  return Object.values(byDate).sort((a, b) => a.date.localeCompare(b.date));
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

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }

  const colorMap = useMemo(
    () => productColorMap((data?.available ? data.products : []).map((p) => p.code)),
    [data],
  );
  const chartData = useMemo(
    () => (data?.available ? buildDailyChartData(data.daily_product_counts, colorMap) : []),
    [data, colorMap],
  );
  const chartSeries = useMemo(() => {
    const codes = (data?.available ? data.products : []).map((p) => p.code);
    const known = codes.filter((c) => colorMap[c]);
    return colorMap._other ? [...known, "Other"] : known;
  }, [data, colorMap]);
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

  const unusedCount = rows.filter((r) => r.unused).length;

  return (
    <>
      <div className="stat-grid">
        <Stat label="Live rows" value={data.total_rows} sub={data.csv_path} />
        <Stat label="As of" value={fmtDate(data.as_of)} />
        <Stat label="Rules never hit" value={unusedCount} status={unusedCount ? "breach" : "pass"} />
      </div>

      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
          <div ref={chartWrapRef} style={{ flex: 1, minWidth: 0, background: "#ffffff" }}>
            <h2 className="card-title" style={{ margin: "0 0 14px" }}>Daily volume by product</h2>
            {chartData.length === 0 ? (
              <p className="empty-hint">No dated rows in the current window to chart.</p>
            ) : (
              <div style={{ width: "100%", height: 260 }}>
                <ResponsiveContainer>
                  <BarChart data={chartData} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef1f4" />
                    <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <Tooltip contentStyle={{ fontSize: 12 }} />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    {chartSeries.map((code) => (
                      <Bar key={code} dataKey={code} name={code} stackId="vol" radius={[2, 2, 2, 2]}
                           fill={code === "Other" ? OTHER_COLOR : colorMap[code]} />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
          <button className="btn btn--ghost" onClick={exportChartPng} disabled={chartData.length === 0} style={{ flexShrink: 0 }}>
            <ImageDown size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Export chart (PNG)
          </button>
        </div>
      </Card>

      <Card>
        <div className="controls controls--row" style={{ marginBottom: 14 }}>
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
