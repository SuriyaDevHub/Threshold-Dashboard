// Column-driven table. columns: [{ key, label, align, mono, render }]
export default function DataTable({ columns, rows, rowKey }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.align === "right" ? "ta-right" : ""}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey ? row[rowKey] : i}>
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={`${c.align === "right" ? "ta-right" : ""} ${
                    c.mono ? "mono" : ""
                  }`}
                >
                  {c.render ? c.render(row[c.key], row) : row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
