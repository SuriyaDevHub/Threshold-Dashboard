import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
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
