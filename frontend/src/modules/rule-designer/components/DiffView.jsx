import { Card } from "../../../components/ui.jsx";

function YamlDiffLines({ lines }) {
  if (!lines?.length) return <p className="empty-hint">No prior published version to diff against.</p>;
  return (
    <pre className="rd-yaml-diff mono">
      {lines.map((l, i) => {
        const cls = l.startsWith("+") ? "rd-diff-add" : l.startsWith("-") ? "rd-diff-del" : l.startsWith("@@") ? "rd-diff-hunk" : "";
        return <div key={i} className={cls}>{l || " "}</div>;
      })}
    </pre>
  );
}

export default function DiffView({ diff }) {
  if (!diff) return null;
  return (
    <>
      <Card title="Business logic — before vs. after">
        <div className="rd-diff-cols">
          <pre className="rd-diff-col mono">{diff.business_logic.before}</pre>
          <pre className="rd-diff-col mono">{diff.business_logic.after}</pre>
        </div>
      </Card>
      <Card title="Enrichment diff">
        <div className="lk-row">
          <span><strong>Added:</strong> {diff.enrichment.added.join(", ") || "—"}</span>
          <span><strong>Removed:</strong> {diff.enrichment.removed.join(", ") || "—"}</span>
          <span><strong>Unchanged:</strong> {diff.enrichment.unchanged.join(", ") || "—"}</span>
        </div>
      </Card>
      <Card title="YAML diff (developer-friendly)">
        <YamlDiffLines lines={diff.yaml_diff} />
      </Card>
    </>
  );
}
