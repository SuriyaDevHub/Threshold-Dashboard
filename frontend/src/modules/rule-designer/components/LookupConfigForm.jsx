import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { rd } from "../api.js";
import ConditionBuilder, { newGroup } from "./ConditionBuilder.jsx";
import TransformOpFields, { transformHint } from "./TransformOpFields.jsx";

// Visual lookup/enrichment designer (spec §11-15, §29): exact / composite /
// range / date lookups against an uploaded reference file, plus self_group
// (spec extension) which groups the INPUT dataset by a field and broadcasts
// one representative row's values to every row sharing that group key —
// no reference file involved. Join type, missing-match handling and
// duplicate-key resolution strategy round out every mode.

export default function LookupConfigForm({ lookup, onChange, sourceFields, meta }) {
  const [refFiles, setRefFiles] = useState([]);
  const [refDetail, setRefDetail] = useState(null);
  const isSelfGroup = lookup.lookup_type === "self_group";

  useEffect(() => { rd.referenceFiles().then((d) => setRefFiles(d.files || [])); }, []);
  useEffect(() => {
    if (lookup.reference_file_id) {
      rd.referenceFile(lookup.reference_file_id).then(setRefDetail).catch(() => setRefDetail(null));
    } else {
      setRefDetail(null);
    }
  }, [lookup.reference_file_id]);

  const refColumns = refDetail?.versions?.[refDetail.versions.length - 1]?.columns || [];
  // self_group has no reference file — a representative row shares the
  // source dataset's own schema, so "fields to enrich with" and "priority
  // field" pick from sourceFields instead of a reference file's columns.
  const enrichColumns = isSelfGroup ? sourceFields.map((f) => f.field) : refColumns;

  function set(patch) { onChange({ ...lookup, ...patch }); }

  function addJoinKey() {
    set({ join_keys: [...(lookup.join_keys || []), { source: sourceFields[0]?.field || "", reference: refColumns[0] || "" }] });
  }
  function updateJoinKey(i, patch) {
    const jk = [...(lookup.join_keys || [])]; jk[i] = { ...jk[i], ...patch }; set({ join_keys: jk });
  }
  function removeJoinKey(i) {
    set({ join_keys: (lookup.join_keys || []).filter((_, idx) => idx !== i) });
  }

  function addFieldMap() {
    set({ fields: [...(lookup.fields || []), { source_column: enrichColumns[0] || "", output_field: "" }] });
  }
  function updateFieldMap(i, patch) {
    const fm = [...(lookup.fields || [])]; fm[i] = { ...fm[i], ...patch }; set({ fields: fm });
  }
  function removeFieldMap(i) {
    set({ fields: (lookup.fields || []).filter((_, idx) => idx !== i) });
  }

  return (
    <div className="lk-form">
      <div className="lk-row">
        <label className="control"><span>Lookup type</span>
          <select value={lookup.lookup_type} onChange={(e) => set({ lookup_type: e.target.value })}>
            <option value="exact">Exact match</option>
            <option value="composite">Composite key</option>
            <option value="range">Range</option>
            <option value="date">Date-based</option>
            <option value="self_group">Self-group (within dataset)</option>
          </select>
        </label>
        {!isSelfGroup && (
          <label className="control"><span>Reference file</span>
            <select value={lookup.reference_file_id || ""} onChange={(e) => set({ reference_file_id: e.target.value })}>
              <option value="" disabled>select…</option>
              {refFiles.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
          </label>
        )}
        <label className="control"><span>Join type</span>
          <select value={lookup.join_type} onChange={(e) => set({ join_type: e.target.value })}>
            <option value="left">LEFT (default)</option>
            <option value="inner">INNER</option>
            <option value="right">RIGHT</option>
            <option value="full">FULL</option>
          </select>
        </label>
      </div>

      {isSelfGroup && (
        <div className="lk-block">
          <div className="lk-block-title">Group by</div>
          <div className="lk-row">
            <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>group rows where</span>
            <select value={lookup.group_by_field || ""} onChange={(e) => set({ group_by_field: e.target.value })}>
              <option value="" disabled>field…</option>
              {sourceFields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
            </select>
            <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>is the same</span>
          </div>
          <div className="lk-block-title" style={{ marginTop: 10 }}>Representative row (optional)</div>
          <ConditionBuilder group={lookup.selector || newGroup()} onChange={(g) => set({ selector: g })}
                            fields={sourceFields} meta={meta} />
          <p className="empty-hint" style={{ marginTop: 6 }}>
            Within each group, the first row matching this condition is the one whose fields get broadcast to every
            row sharing the same {lookup.group_by_field || "group"} value — e.g. the row flagged as the parent leg.
            Leave empty to just use each group's first row.
          </p>
        </div>
      )}

      {(lookup.lookup_type === "exact" || lookup.lookup_type === "composite") && (
        <div className="lk-block">
          <div className="lk-block-title">Join keys</div>
          {(lookup.join_keys || []).map((jk, i) => (
            <div key={i} style={{ border: "1px solid var(--line)", borderRadius: 7, padding: 8, marginBottom: 8 }}>
              <div className="lk-row" style={{ marginBottom: 0 }}>
                <span className="mono">source</span>
                <select value={jk.source} onChange={(e) => updateJoinKey(i, { source: e.target.value })}>
                  {sourceFields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
                </select>
                <span className="mono">=</span>
                <select value={jk.reference} onChange={(e) => updateJoinKey(i, { reference: e.target.value })}>
                  {refColumns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <button className="icon-btn" onClick={() => removeJoinKey(i)}><Trash2 size={13} /></button>
              </div>
              <div className="lk-row" style={{ marginTop: 6, marginBottom: 0 }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>transform</span>
                <TransformOpFields transform={jk.transform} onChange={(t) => updateJoinKey(i, { transform: t })} allowNone />
              </div>
              {jk.transform?.op && (
                <p className="empty-hint" style={{ marginTop: 4 }}>
                  Applied to both the source and reference values before matching — e.g. "upper" lets a
                  lowercase source value match an uppercase reference key.
                  {transformHint(jk.transform.op) ? ` ${transformHint(jk.transform.op)}` : ""}
                </p>
              )}
            </div>
          ))}
          <button className="btn btn--ghost btn--xs" onClick={addJoinKey}><Plus size={13} /> Add join key</button>
        </div>
      )}

      {lookup.lookup_type === "range" && (
        <div className="lk-block">
          <div className="lk-block-title">Range match</div>
          <div className="lk-row">
            <label className="control"><span>Source field</span>
              <select value={lookup.range_field || ""} onChange={(e) => set({ range_field: e.target.value })}>
                {sourceFields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
              </select>
            </label>
            <label className="control"><span>Reference lower-bound column</span>
              <select value={lookup.range_low_column || ""} onChange={(e) => set({ range_low_column: e.target.value })}>
                {refColumns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <label className="control"><span>Reference upper-bound column</span>
              <select value={lookup.range_high_column || ""} onChange={(e) => set({ range_high_column: e.target.value })}>
                {refColumns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
          </div>
        </div>
      )}

      {lookup.lookup_type === "date" && (
        <div className="lk-block">
          <div className="lk-block-title">Date-effective match</div>
          <div className="lk-row">
            <label className="control"><span>Source date field</span>
              <select value={lookup.date_field || ""} onChange={(e) => set({ date_field: e.target.value })}>
                {sourceFields.map((f) => <option key={f.field} value={f.field}>{f.field}</option>)}
              </select>
            </label>
            <label className="control"><span>Effective-from column</span>
              <select value={lookup.date_from_column || ""} onChange={(e) => set({ date_from_column: e.target.value })}>
                {refColumns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <label className="control"><span>Effective-to column</span>
              <select value={lookup.date_to_column || ""} onChange={(e) => set({ date_to_column: e.target.value })}>
                {refColumns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
          </div>
        </div>
      )}

      <div className="lk-block">
        <div className="lk-block-title">Fields to enrich with</div>
        {(lookup.fields || []).map((fm, i) => (
          <div key={i} style={{ border: "1px solid var(--line)", borderRadius: 7, padding: 8, marginBottom: 8 }}>
            <div className="lk-row" style={{ marginBottom: 0 }}>
              <select value={fm.source_column} onChange={(e) => updateFieldMap(i, { source_column: e.target.value })}>
                {enrichColumns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <span className="mono">→</span>
              <input placeholder="output field name" value={fm.output_field}
                     onChange={(e) => updateFieldMap(i, { output_field: e.target.value })} />
              <button className="icon-btn" onClick={() => removeFieldMap(i)}><Trash2 size={13} /></button>
            </div>
            {isSelfGroup && (
              <div className="lk-row" style={{ marginTop: 6, marginBottom: 0 }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>across the whole group</span>
                <select value={fm.aggregate || ""} onChange={(e) => updateFieldMap(i, { aggregate: e.target.value || null })}>
                  <option value="">just this row (default)</option>
                  <option value="sum">sum</option>
                  <option value="count">count</option>
                  <option value="min">min</option>
                  <option value="max">max</option>
                </select>
              </div>
            )}
            <div className="lk-row" style={{ marginTop: 6, marginBottom: 0 }}>
              <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>transform</span>
              <TransformOpFields transform={fm.transform} onChange={(t) => updateFieldMap(i, { transform: t })} allowNone />
            </div>
            {transformHint(fm.transform?.op) && <p className="empty-hint" style={{ marginTop: 4 }}>{transformHint(fm.transform?.op)}</p>}
            {isSelfGroup && fm.aggregate && (
              <p className="empty-hint" style={{ marginTop: 4 }}>
                Every row sharing the same {lookup.group_by_field || "group"} value gets the {fm.aggregate} of{" "}
                {fm.source_column || "this field"} across the whole group, not just the representative row's own value.
              </p>
            )}
          </div>
        ))}
        <button className="btn btn--ghost btn--xs" onClick={addFieldMap}><Plus size={13} /> Add field</button>
      </div>

      <div className="lk-block">
        <div className="lk-block-title">If no match</div>
        <div className="lk-row">
          <select value={lookup.missing_strategy} onChange={(e) => set({ missing_strategy: e.target.value })}>
            <option value="reject">Reject record</option>
            <option value="continue_null">Continue with null</option>
            <option value="default">Use default value</option>
            <option value="flag">Flag record</option>
            <option value="fallback">Use fallback lookup</option>
          </select>
          {lookup.missing_strategy === "flag" && (
            <input placeholder="flag field name" value={lookup.flag_field || ""}
                   onChange={(e) => set({ flag_field: e.target.value })} />
          )}
          {lookup.missing_strategy === "fallback" && (
            <select value={lookup.fallback_reference_file_id || ""} onChange={(e) => set({ fallback_reference_file_id: e.target.value })}>
              <option value="">fallback file…</option>
              {refFiles.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
          )}
        </div>
        {lookup.missing_strategy === "default" && (lookup.fields || []).length > 0 && (
          <div className="lk-row">
            {(lookup.fields || []).map((fm) => (
              <label className="control" key={fm.output_field}><span>default {fm.output_field}</span>
                <input value={lookup.default_values?.[fm.output_field] ?? ""}
                       onChange={(e) => set({ default_values: { ...(lookup.default_values || {}), [fm.output_field]: e.target.value } })} />
              </label>
            ))}
          </div>
        )}
      </div>

      {!isSelfGroup && (
      <div className="lk-block">
        <div className="lk-block-title">If the reference key has duplicates</div>
        <div className="lk-row">
          <select value={lookup.priority_strategy} onChange={(e) => set({ priority_strategy: e.target.value })}>
            <option value="first_match">First match</option>
            <option value="highest_priority">Highest priority</option>
            <option value="latest_effective_date">Latest effective date</option>
            <option value="lowest_threshold">Lowest threshold</option>
            <option value="highest_threshold">Highest threshold</option>
          </select>
          {lookup.priority_strategy !== "first_match" && (
            <select value={lookup.priority_field || ""} onChange={(e) => set({ priority_field: e.target.value })}>
              <option value="">priority field…</option>
              {enrichColumns.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          )}
        </div>
      </div>
      )}
    </div>
  );
}
