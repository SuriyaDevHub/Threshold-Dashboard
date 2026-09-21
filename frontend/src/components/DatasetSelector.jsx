// Dropdown of stored datasets (from Data Fetch). Used by analysis modules.
export default function DatasetSelector({ label, datasets, value, onChange, emptyHint }) {
  if (!datasets || datasets.length === 0) {
    return (
      <div className="ds-empty">
        <span className="ds-empty-title">No datasets loaded.</span>
        <span className="ds-empty-hint">{emptyHint || "Pull one in Data Fetch first."}</span>
      </div>
    );
  }
  return (
    <label className="control" style={{ minWidth: 280 }}>
      <span>{label}</span>
      <select value={value || ""} onChange={(e) => onChange(e.target.value)}>
        <option value="" disabled>Select a dataset…</option>
        {datasets.map((d) => (
          <option key={d.id} value={d.id}>
            {d.label || `${d.product_type} · ${d.row_count} rows`}
          </option>
        ))}
      </select>
    </label>
  );
}
