import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft, Save, CheckCircle2, PlayCircle, Send, ThumbsUp, ThumbsDown, Rocket, Sparkles, Trash2,
} from "lucide-react";
import { Card, Loader, ErrorState, ModuleHeader } from "../../../components/ui.jsx";
import DatasetSelector from "../../../components/DatasetSelector.jsx";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";
import { fmtDate, STATUS_LABEL } from "../format.js";
import WorkflowCanvas from "../components/WorkflowCanvas.jsx";
import DryRunResults from "../components/DryRunResults.jsx";
import ImpactView from "../components/ImpactView.jsx";
import DiffView from "../components/DiffView.jsx";
import ShadowTestView from "../components/ShadowTestView.jsx";

const TABS = [
  { id: "workflow", label: "Workflow / Rule builder" },
  { id: "dryrun", label: "Dry run" },
  { id: "shadow", label: "Shadow test (vs legacy)" },
  { id: "impact", label: "Impact analysis" },
  { id: "review", label: "Review" },
  { id: "history", label: "History" },
];

export default function RuleWorkspace({ ruleId, onBack, onDeleted }) {
  const { actor, role } = useActor();
  const [rule, setRule] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [tab, setTab] = useState("workflow");
  const [datasets, setDatasets] = useState([]);
  const [schema, setSchema] = useState([]);
  const [datasetId, setDatasetId] = useState("");
  const [validation, setValidation] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [meta, setMeta] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [r, ds, m] = await Promise.all([rd.rule(ruleId), rd.datasets(), rd.meta()]);
      setRule(r); setDatasets(ds.datasets || []); setDatasetId(r.dataset_id || ds.datasets?.[0]?.id || "");
      setMeta(m);
      setDirty(false);
    } catch (e) { setError(e); } finally { setLoading(false); }
  }, [ruleId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (datasetId) rd.datasetSchema(datasetId).then((d) => setSchema(d.columns || [])).catch(() => setSchema([]));
  }, [datasetId]);

  function mutateWorkflow(next) {
    setRule((r) => ({ ...r, workflow: next }));
    setDirty(true);
  }

  async function save() {
    setBusy(true);
    try {
      const updated = await rd.updateRule(ruleId, actor, role, { ...rule, dataset_id: datasetId });
      setRule(updated); setDirty(false);
      setNotice({ kind: "pass", text: "Saved." });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  async function doValidate() {
    setBusy(true);
    try {
      if (dirty) await save();
      const v = await rd.validateRule(ruleId, actor);
      setValidation(v);
      setRule((r) => ({ ...r, status: v.status }));
      setNotice(v.ok ? { kind: "pass", text: "Validation passed." } : { kind: "breach", text: `${v.errors.length} error(s) found.` });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  async function transition(action) {
    setBusy(true);
    try {
      const body = { actor, role, comment: "" };
      const fn = { submit: rd.submit, approve: rd.approve, reject: rd.reject, publish: rd.publish }[action];
      const result = await fn(ruleId, body);
      setRule(result.rule || result);
      setNotice({ kind: "pass", text: `${action} ok.` });
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  async function remove() {
    if (!confirm(`Delete rule ${ruleId}? This cannot be undone.`)) return;
    await rd.deleteRule(ruleId, actor, role);
    onDeleted?.();
  }

  if (loading) return <Loader label="Loading rule…" />;
  if (error) return <ErrorState error={error} />;
  if (!rule) return null;

  const s = rule.status;
  const canSubmit = s === "DRY_RUN_COMPLETED" || s === "VALIDATED";
  const canApprove = s === "PENDING_APPROVAL";
  const canPublish = s === "APPROVED";

  return (
    <>
      <button className="btn btn--ghost" onClick={onBack} style={{ marginBottom: 14 }}>
        <ArrowLeft size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />Back to rules
      </button>

      <ModuleHeader
        title={rule.name}
        description={rule.description || rule.rule_id}
        actions={
          <div className="module-actions" style={{ flexWrap: "wrap" }}>
            <span className={`rd-status rd-status--${s.toLowerCase()}`}>{STATUS_LABEL[s] || s}</span>
            <span className="mono ds-count">v{rule.version}</span>
            {dirty && <button className="btn" disabled={busy} onClick={save}><Save size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Save</button>}
            <button className="btn btn--ghost" disabled={busy} onClick={doValidate}><CheckCircle2 size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Validate</button>
            {canSubmit && <button className="btn btn--ghost" disabled={busy} onClick={() => transition("submit")}><Send size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Submit for approval</button>}
            {canApprove && <button className="btn btn--ghost" disabled={busy} onClick={() => transition("approve")}><ThumbsUp size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Approve</button>}
            {canApprove && <button className="btn btn--ghost" disabled={busy} onClick={() => transition("reject")}><ThumbsDown size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Reject</button>}
            {canPublish && <button className="btn" disabled={busy} onClick={() => transition("publish")}><Rocket size={13} style={{ marginRight: 5, verticalAlign: "-2px" }} />Publish</button>}
            <button className="icon-btn" title="Delete rule" onClick={remove}><Trash2 size={16} /></button>
          </div>
        }
      />

      {notice && <div className={notice.kind === "breach" ? "errorbox" : "benefit-note"}>{notice.text}</div>}
      {validation && !validation.ok && (
        <div className="errorbox">
          <strong>Validation errors</strong>
          <ul>{validation.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          {validation.warnings.length > 0 && <><strong>Warnings</strong><ul>{validation.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></>}
        </div>
      )}

      <Card>
        <div className="controls controls--row">
          <DatasetSelector label="Bound dataset" datasets={datasets} value={datasetId}
                            onChange={(v) => { setDatasetId(v); setDirty(true); }} />
          <label className="control"><span>Priority</span>
            <input type="number" value={rule.priority} onChange={(e) => { setRule((r) => ({ ...r, priority: Number(e.target.value) })); setDirty(true); }} />
          </label>
        </div>
      </Card>

      <div className="tabs" style={{ marginBottom: 16 }}>
        {TABS.map((t) => (
          <button key={t.id} className={`tab ${tab === t.id ? "active" : ""}`} onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </div>

      {tab === "workflow" && (
        <WorkflowTab rule={rule} schema={schema} datasetId={datasetId} mutateWorkflow={mutateWorkflow} setNotice={setNotice} meta={meta} />
      )}
      {tab === "dryrun" && <DryRunTab ruleId={ruleId} datasetId={datasetId} schema={schema} onRan={(r) => setRule((prev) => ({ ...prev, status: prev.status === "VALIDATED" ? "DRY_RUN_COMPLETED" : prev.status }))} />}
      {tab === "shadow" && <ShadowTestView ruleId={ruleId} datasetId={datasetId} />}
      {tab === "impact" && <ImpactTab ruleId={ruleId} datasetId={datasetId} schema={schema} />}
      {tab === "review" && <ReviewTab ruleId={ruleId} />}
      {tab === "history" && <HistoryTab rule={rule} />}
    </>
  );
}

function WorkflowTab({ rule, schema, datasetId, mutateWorkflow, setNotice, meta }) {
  const [aiOpen, setAiOpen] = useState(false);
  const [text, setText] = useState("");
  const [interp, setInterp] = useState(null);
  const [busy, setBusy] = useState(false);

  async function interpret() {
    if (!datasetId) { setNotice({ kind: "breach", text: "Bind a dataset first." }); return; }
    setBusy(true);
    try {
      const result = await rd.interpretFreeText(text, datasetId);
      setInterp(result);
    } catch (e) { setNotice({ kind: "breach", text: String(e.message || e) }); }
    setBusy(false);
  }

  function apply() {
    mutateWorkflow(interp.workflow);
    setAiOpen(false); setInterp(null); setText("");
    setNotice({ kind: "pass", text: "Interpreted workflow applied — review the canvas and save." });
  }

  return (
    <>
      <Card>
        <button className="btn btn--ghost" onClick={() => setAiOpen(!aiOpen)}>
          <Sparkles size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />
          {aiOpen ? "Hide" : "Describe this rule in plain English"}
        </button>
        {aiOpen && (
          <div style={{ marginTop: 12 }}>
            <textarea rows={3} style={{ width: "100%", fontFamily: "inherit", padding: 10, borderRadius: 8, border: "1px solid var(--line)" }}
                      placeholder="e.g. For USD trades where the notional is above 1 million and the currency is USD, classify the trade as HIGH RISK."
                      value={text} onChange={(e) => setText(e.target.value)} />
            <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
              <button className="btn" disabled={busy || !text.trim()} onClick={interpret}>Interpret</button>
              {interp && <button className="btn btn--ghost" onClick={apply}>Apply to workflow (replaces canvas)</button>}
            </div>
            {interp && (
              <div style={{ marginTop: 10 }}>
                <div><strong>Interpreted rule:</strong> <span className="mono">{interp.interpreted_summary}</span></div>
                <div className="mono">confidence: {interp.confidence}</div>
                {interp.notes.length > 0 && (
                  <ul>{interp.notes.map((n, i) => <li key={i} className="preview-note">{n}</li>)}</ul>
                )}
                <p className="empty-hint">Nothing has executed yet — review, edit on the canvas below, then validate and dry-run.</p>
              </div>
            )}
          </div>
        )}
      </Card>
      <Card title="Workflow canvas">
        <WorkflowCanvas workflow={rule.workflow} onChange={mutateWorkflow} baseFields={schema} meta={meta} />
      </Card>
    </>
  );
}

function DryRunTab({ ruleId, datasetId, onRan }) {
  const [sampleMode, setSampleMode] = useState("full");
  const [recordIdField, setRecordIdField] = useState("trade_id");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const { actor, role } = useActor();

  async function run() {
    if (!datasetId) return;
    setBusy(true);
    try {
      const r = await rd.dryRun(ruleId, { actor, role, dataset_id: datasetId, sample_mode: sampleMode, record_id_field: recordIdField || null });
      setResult(r); onRan?.(r);
    } finally { setBusy(false); }
  }

  return (
    <>
      <Card title="Dry run configuration">
        <div className="controls controls--row">
          <label className="control"><span>Sample</span>
            <select value={sampleMode} onChange={(e) => setSampleMode(e.target.value)}>
              <option value="full">Entire dataset</option>
              <option value="sample_1000">Sample 1,000</option>
              <option value="sample_10000">Sample 10,000</option>
            </select>
          </label>
          <label className="control"><span>Record ID field</span>
            <input value={recordIdField} onChange={(e) => setRecordIdField(e.target.value)} placeholder="trade_id" />
          </label>
          <button className="btn" disabled={busy || !datasetId} onClick={run}>
            <PlayCircle size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />{busy ? "Running…" : "Run dry test"}
          </button>
        </div>
        {!datasetId && <p className="empty-hint">Select a dataset above first.</p>}
      </Card>
      <DryRunResults result={result} />
    </>
  );
}

function ImpactTab({ ruleId, datasetId }) {
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const { actor, role } = useActor();

  async function run() {
    if (!datasetId) return;
    setBusy(true);
    try {
      setResult(await rd.impactAnalysis(ruleId, { actor, role, dataset_id: datasetId, record_id_field: "trade_id" }));
    } finally { setBusy(false); }
  }

  return (
    <>
      <Card title="Compare current production rule vs. this proposed version">
        <button className="btn" disabled={busy || !datasetId} onClick={run}>{busy ? "Running…" : "Run impact analysis"}</button>
      </Card>
      <ImpactView result={result} />
    </>
  );
}

function ReviewTab({ ruleId }) {
  const [diff, setDiff] = useState(null);
  const [explanation, setExplanation] = useState("");
  useEffect(() => {
    rd.diff(ruleId).then(setDiff).catch(() => {});
    rd.explanation(ruleId).then((d) => setExplanation(d.explanation)).catch(() => {});
  }, [ruleId]);
  return (
    <>
      <Card title="Business-friendly explanation">
        <pre className="rd-explanation">{explanation}</pre>
      </Card>
      <DiffView diff={diff} />
    </>
  );
}

function HistoryTab({ rule }) {
  const [entries, setEntries] = useState([]);
  useEffect(() => { rd.audit({ rule_id: rule.rule_id }).then((d) => setEntries(d.entries || [])); }, [rule.rule_id]);
  return (
    <Card title="Approvals & audit trail for this rule">
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>When</th><th>Action</th><th>Actor</th><th>Role</th><th>Detail</th></tr></thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td className="mono">{fmtDate(e.timestamp)}</td>
                <td><span className="badge">{e.action}</span></td>
                <td>{e.actor}</td>
                <td className="mono">{e.role || "—"}</td>
                <td>{e.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
