import { useCallback, useState } from "react";
import { useAsync } from "../../../lib/useAsync.js";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import { rd } from "../api.js";
import { fmtDate } from "../format.js";

export default function Audit() {
  const [ruleId, setRuleId] = useState("");
  const [action, setAction] = useState("");
  const { loading, data, error } = useAsync(
    useCallback(() => rd.audit({ rule_id: ruleId || undefined, action: action || undefined }), [ruleId, action]),
    [ruleId, action],
  );

  return (
    <>
      <ModuleHeader title="Audit trail" description="Every mutation — create, edit, dry-run, submit, approve, reject, publish, rollback — logged append-only." />
      <Card>
        <div className="controls controls--row">
          <label className="control"><span>Filter by rule id</span>
            <input value={ruleId} onChange={(e) => setRuleId(e.target.value)} placeholder="e.g. FX_DEVIATION_HIGH_RISK" /></label>
          <label className="control"><span>Filter by action</span>
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="">all</option>
              {["CREATE", "EDIT", "DELETE", "DRY_RUN", "SUBMIT", "APPROVE", "REJECT", "PUBLISH", "ROLLBACK"].map((a) => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
          </label>
        </div>
      </Card>
      {loading && <Loader />}
      {error && <ErrorState error={error} />}
      {data && (
        <Card>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>When</th><th>Action</th><th>Rule</th><th>Actor</th><th>Role</th><th>Detail</th></tr></thead>
              <tbody>
                {data.entries.map((e) => (
                  <tr key={e.id}>
                    <td className="mono">{fmtDate(e.timestamp)}</td>
                    <td><span className="badge">{e.action}</span></td>
                    <td className="mono">{e.rule_id || "—"}</td>
                    <td>{e.actor}</td>
                    <td className="mono">{e.role || "—"}</td>
                    <td>{e.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.entries.length === 0 && <p className="empty-hint" style={{ padding: 16 }}>No matching audit entries.</p>}
          </div>
        </Card>
      )}
    </>
  );
}
