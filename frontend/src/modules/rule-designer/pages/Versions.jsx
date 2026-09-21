import { useCallback, useState } from "react";
import { RotateCcw } from "lucide-react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";
import { fmtDate } from "../format.js";

export default function Versions() {
  const { actor, role } = useActor();
  const { loading, data, error, reload } = useAsync(useCallback(() => rd.versions(), []), []);
  const [openYaml, setOpenYaml] = useState(null);
  const [yamlText, setYamlText] = useState("");
  const [notice, setNotice] = useState(null);

  async function viewYaml(v) {
    if (openYaml === v) { setOpenYaml(null); return; }
    const d = await rd.versionYaml(v);
    setYamlText(d.yaml); setOpenYaml(v);
  }

  async function rollback(v) {
    if (!confirm(`Roll back to v${v}? This publishes a new version restoring that snapshot's rules.`)) return;
    try {
      await rd.rollback({ actor, role, version: v });
      setNotice({ kind: "pass", text: `Rolled back to v${v} (published as a new version).` });
      reload();
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
  }

  if (loading) return <Loader label="Loading versions…" />;
  if (error) return <ErrorState error={error} />;
  const versions = [...(data.versions || [])].reverse();

  return (
    <>
      <ModuleHeader title="Version history" description="Every publish snapshots the whole rules YAML — nothing is ever deleted; rollback creates a new version." />
      {notice && <div className={notice.kind === "breach" ? "errorbox" : "benefit-note"}>{notice.text}</div>}
      <Card>
        <div className="ds-list">
          {versions.map((v) => (
            <div className="card" key={v.version} style={{ marginBottom: 10 }}>
              <div className="lk-row" style={{ justifyContent: "space-between" }}>
                <div>
                  <strong className="mono">v{v.version}</strong> — {v.description}
                  <div className="ds-count mono">{v.created_by} · {fmtDate(v.created_at)} · rules: {v.rule_ids_changed.join(", ")}</div>
                </div>
                <div className="lk-row">
                  <button className="btn btn--ghost btn--xs" onClick={() => viewYaml(v.version)}>{openYaml === v.version ? "Hide YAML" : "View YAML"}</button>
                  {role === "ADMIN" || role === "APPROVER" ? (
                    <button className="btn btn--ghost btn--xs" onClick={() => rollback(v.version)}>
                      <RotateCcw size={12} style={{ marginRight: 4, verticalAlign: "-2px" }} />Rollback to this
                    </button>
                  ) : null}
                </div>
              </div>
              {openYaml === v.version && <pre className="rd-yaml-diff mono">{yamlText}</pre>}
            </div>
          ))}
          {versions.length === 0 && <p className="empty-hint">No published versions yet — publish a rule to create v1.</p>}
        </div>
      </Card>
    </>
  );
}
