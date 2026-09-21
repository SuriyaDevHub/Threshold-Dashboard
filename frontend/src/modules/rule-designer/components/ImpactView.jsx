import { Stat, Card } from "../../../components/ui.jsx";

export default function ImpactView({ result }) {
  if (!result) return null;
  return (
    <>
      <div className="stat-grid">
        <Stat label="Current alerts" value={result.current_matches} sub={result.current_version ? `v${result.current_version}` : "not yet published"} />
        <Stat label="Proposed alerts" value={result.proposed_matches} sub={`v${result.proposed_version}`} />
        <Stat label="New" value={result.new_matches} status={result.new_matches ? "watch" : undefined} />
        <Stat label="Removed" value={result.removed_matches} status={result.removed_matches ? "breach" : undefined} />
      </div>
      <div className="stat-grid">
        <Stat label="Unchanged" value={result.unchanged_matches} status="pass" />
        <Stat label="Outcome changes" value={result.outcome_changes?.length || 0} />
        <Stat label="Lookup changes" value={result.lookup_changes?.length || 0} />
      </div>
      {result.outcome_changes?.length > 0 && (
        <Card title="Outcome changes (same match, different result)">
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Record</th><th>Before</th><th>After</th></tr></thead>
              <tbody>
                {result.outcome_changes.slice(0, 50).map((c, i) => (
                  <tr key={i}>
                    <td className="mono">{String(c.record_id)}</td>
                    <td className="mono">{JSON.stringify(c.before)}</td>
                    <td className="mono">{JSON.stringify(c.after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      <Card title="New matches (sample)">
        <div className="preview-note mono">{(result.new_match_ids || []).slice(0, 40).join(", ") || "(none)"}</div>
      </Card>
      <Card title="Removed matches (sample)">
        <div className="preview-note mono">{(result.removed_match_ids || []).slice(0, 40).join(", ") || "(none)"}</div>
      </Card>
    </>
  );
}
