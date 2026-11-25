import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Plus, Trash2 } from "lucide-react";
import { rd } from "../api.js";
import { useActor } from "../RoleContext.jsx";
import ConditionBuilder, { newGroup, withCurrent } from "./ConditionBuilder.jsx";
import ReferenceFileManager from "./ReferenceFileManager.jsx";
import TransformOpFields, { transformHint } from "./TransformOpFields.jsx";

// Visual lookup/enrichment designer (spec §11-15, §29): exact / composite /
// range / date lookups against an uploaded reference file, plus self_group
// (spec extension) which groups the INPUT dataset by a field and broadcasts
// one representative row's values to every row sharing that group key —
// no reference file involved. Join type, missing-match handling and
// duplicate-key resolution strategy round out every mode.

export default function LookupConfigForm({ lookup, onChange, sourceFields, meta }) {
  const { actor, role, isAdmin } = useActor();
  const [refFiles, setRefFiles] = useState([]);
  const [refDetail, setRefDetail] = useState(null);
  const [showRefManager, setShowRefManager] = useState(false);
  const isSelfGroup = lookup.lookup_type === "self_group";
  const groupByLabel = [lookup.group_by_field, ...(lookup.group_by_fields || [])].filter(Boolean).join(" + ") || "group";
  // Day-partitioned: the reference rows come from a file resolved per
  // record from its own COB date (e.g. the legacy GfxValidator's
  // RiverIndex — one "All trades.csv" per day), not an admin-uploaded/
  // path-configured reference file — see day_partitioned_source.py.
  const canBeDayPartitioned = lookup.lookup_type === "exact" || lookup.lookup_type === "composite"
    || lookup.lookup_type === "interpolated";
  const isDayPartitioned = canBeDayPartitioned && lookup.reference_source === "day_partitioned";
  // The reference-file registry still matters for a day-partitioned
  // lookup whose missing_strategy is "fallback" — the fallback file is
  // always a registry file, independent of how the primary source works.
  const needsRefRegistry = !isSelfGroup && (!isDayPartitioned || lookup.missing_strategy === "fallback");
  const isXlsxDayTemplate = /\.(xlsx|xlsm|xlsb)$/i.test((lookup.day_partition_path_template || "").trim());

  // Day-partitioned has no admin-known file to introspect ahead of time —
  // its columns are only knowable by actually reading some day's resolved
  // file, which is what "Load columns" below does on demand (never
  // automatically: the path template alone doesn't resolve to a file
  // without a sample day, and we don't want to hit the filesystem on
  // every keystroke). Until loaded (or if it fails — e.g. no file for
  // that sample day yet), the pickers fall back to free text.
  const [daySampleDate, setDaySampleDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [dayColumns, setDayColumns] = useState([]);
  const [dayColumnsError, setDayColumnsError] = useState(null);
  const [loadingDayColumns, setLoadingDayColumns] = useState(false);

  function reloadRefFiles() { return rd.referenceFiles().then((d) => setRefFiles(d.files || [])); }
  useEffect(() => { reloadRefFiles(); }, []);
  useEffect(() => {
    if (lookup.reference_file_id && !isDayPartitioned) {
      rd.referenceFile(lookup.reference_file_id).then(setRefDetail).catch(() => setRefDetail(null));
    } else {
      setRefDetail(null);
    }
  }, [lookup.reference_file_id, isDayPartitioned]);
  // A changed path template invalidates whatever columns were loaded for
  // the previous one — stale column names would otherwise silently keep
  // populating the pickers for a file that's no longer what's configured.
  useEffect(() => { setDayColumns([]); setDayColumnsError(null); }, [lookup.day_partition_path_template]);

  async function loadDayColumns() {
    if (!lookup.day_partition_path_template) return;
    setLoadingDayColumns(true);
    try {
      const d = await rd.dayPartitionedColumns(lookup.day_partition_path_template, daySampleDate, {
        sourceFormat: lookup.day_partition_source_format,
        sheetName: lookup.day_partition_sheet_name,
        columnRange: lookup.day_partition_column_range,
        passwordEnvVar: lookup.day_partition_password_env_var,
        password: lookup.day_partition_password,
      });
      setDayColumns(d.columns || []);
      setDayColumnsError(d.error || null);
    } catch (e) {
      setDayColumns([]);
      setDayColumnsError(e?.message || String(e));
    } finally {
      setLoadingDayColumns(false);
    }
  }

  const refColumns = isDayPartitioned ? [] : (refDetail?.versions?.[refDetail.versions.length - 1]?.columns || []);
  // Day-partitioned's "reference columns" come from the on-demand Load
  // columns preview instead of an admin-known file — empty until loaded
  // (or if the preview failed), in which case every picker below falls
  // back to free text exactly as before.
  const dayOrRefColumns = isDayPartitioned ? dayColumns : refColumns;
  // self_group has no reference file — a representative row shares the
  // source dataset's own schema, so "fields to enrich with" and "priority
  // field" pick from sourceFields instead of a reference file's columns.
  const enrichColumns = isSelfGroup ? sourceFields.map((f) => f.field) : dayOrRefColumns;

  function set(patch) { onChange({ ...lookup, ...patch }); }

  function addJoinKey() {
    set({ join_keys: [...(lookup.join_keys || []), { source: sourceFields[0]?.field || "", reference: dayOrRefColumns[0] || "" }] });
  }
  function updateJoinKey(i, patch) {
    const jk = [...(lookup.join_keys || [])]; jk[i] = { ...jk[i], ...patch }; set({ join_keys: jk });
  }
  function removeJoinKey(i) {
    set({ join_keys: (lookup.join_keys || []).filter((_, idx) => idx !== i) });
  }

  // A join key's reference side is normally a single column name, but can
  // be a list of column names to concatenate (no separator) — a reference
  // file that splits what the dataset treats as one logical field across
  // two (or more) of its own columns. These helpers keep that collapsed
  // back down to a plain string whenever there's only one column, so an
  // ordinary (non-composite) join key's saved shape never changes.
  function refValueAsArray(jk) {
    return Array.isArray(jk.reference) ? jk.reference : [jk.reference ?? ""];
  }
  function updateJoinKeyReferenceColumn(i, ri, value) {
    const arr = [...refValueAsArray((lookup.join_keys || [])[i])];
    arr[ri] = value;
    updateJoinKey(i, { reference: arr.length === 1 ? arr[0] : arr });
  }
  function addJoinKeyReferenceColumn(i) {
    const arr = [...refValueAsArray((lookup.join_keys || [])[i]), dayOrRefColumns[0] || ""];
    updateJoinKey(i, { reference: arr });
  }
  function removeJoinKeyReferenceColumn(i, ri) {
    const arr = refValueAsArray((lookup.join_keys || [])[i]).filter((_, idx) => idx !== ri);
    updateJoinKey(i, { reference: arr.length === 1 ? arr[0] : arr });
  }

  // Symmetric with the reference-side helpers above — a join key's SOURCE
  // side can likewise be a list of dataset fields to concatenate (no
  // separator), as the alternative to pre-building the combined value with
  // a "concatenate fields" TRANSFORM node. This is what makes a per-column
  // fallback (below) actually express "default just this one component,
  // keep the others' real per-record values" — a single fixed fallback
  // string for an already-merged key could never do that.
  function srcValueAsArray(jk) {
    return Array.isArray(jk.source) ? jk.source : [jk.source ?? ""];
  }
  function updateJoinKeySourceColumn(i, si, value) {
    const arr = [...srcValueAsArray((lookup.join_keys || [])[i])];
    arr[si] = value;
    updateJoinKey(i, { source: arr.length === 1 ? arr[0] : arr });
  }
  function addJoinKeySourceColumn(i) {
    const arr = [...srcValueAsArray((lookup.join_keys || [])[i]), sourceFields[0]?.field || ""];
    updateJoinKey(i, { source: arr });
  }
  function removeJoinKeySourceColumn(i, si) {
    const jk = (lookup.join_keys || [])[i];
    const arr = srcValueAsArray(jk).filter((_, idx) => idx !== si);
    const defaults = defaultsAsArray(jk).filter((_, idx) => idx !== si);
    updateJoinKey(i, {
      source: arr.length === 1 ? arr[0] : arr,
      default_on_miss: arr.length === 1 ? (defaults[0] || undefined) : defaults,
    });
  }

  // A join key's default_on_miss mirrors its source shape: a single
  // fallback value for a plain string source, or a PARALLEL array (one
  // slot per source column, blank = no fallback for that column) for a
  // composite source — see lookup_engine._apply_source_defaults().
  function defaultsAsArray(jk) {
    const src = srcValueAsArray(jk);
    const d = Array.isArray(jk.default_on_miss) ? jk.default_on_miss
      : (src.length === 1 ? [jk.default_on_miss ?? ""] : []);
    return src.map((_, i) => d[i] ?? "");
  }
  function updateJoinKeyDefault(i, si, value) {
    const jk = (lookup.join_keys || [])[i];
    const src = srcValueAsArray(jk);
    const defaults = defaultsAsArray(jk);
    defaults[si] = value;
    updateJoinKey(i, { default_on_miss: src.length === 1 ? (defaults[0] || undefined) : defaults });
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
            <option value="interpolated">Curve interpolation</option>
            <option value="self_group">Self-group (within dataset)</option>
          </select>
        </label>
        {canBeDayPartitioned && (
          <label className="control"><span>Reference source</span>
            <select value={lookup.reference_source || "file"} onChange={(e) => set({ reference_source: e.target.value })}>
              <option value="file">Uploaded/path reference file</option>
              <option value="day_partitioned">Day-partitioned file (per record's COB date)</option>
            </select>
          </label>
        )}
        {!isSelfGroup && !isDayPartitioned && (
          <label className="control"><span>Reference file</span>
            <select value={lookup.reference_file_id || ""} onChange={(e) => set({ reference_file_id: e.target.value })}>
              <option value="" disabled>select…</option>
              {refFiles.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
          </label>
        )}
        {needsRefRegistry && (
          <button type="button" className="btn btn--ghost btn--xs" style={{ alignSelf: "flex-end" }}
                  onClick={() => setShowRefManager((v) => !v)}>
            {showRefManager ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Reference files
          </button>
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

      {needsRefRegistry && showRefManager && (
        <div className="lk-block">
          <div className="lk-block-title">Reference files</div>
          <ReferenceFileManager actor={actor} role={role} isAdmin={isAdmin} onChanged={reloadRefFiles} />
        </div>
      )}

      {isSelfGroup && (
        <div className="lk-block">
          <div className="lk-block-title">Group by</div>
          <div className="lk-row">
            <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>group rows where</span>
            <select value={lookup.group_by_field || ""} onChange={(e) => set({ group_by_field: e.target.value })}>
              <option value="" disabled>field…</option>
              {withCurrent(sourceFields, lookup.group_by_field).map((f) => (
                <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
              ))}
            </select>
            <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>is the same</span>
          </div>
          {(lookup.group_by_fields || []).map((f, i) => (
            <div className="lk-row" key={i} style={{ marginTop: 4 }}>
              <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>and</span>
              <select value={f} onChange={(e) => {
                const next = [...(lookup.group_by_fields || [])]; next[i] = e.target.value; set({ group_by_fields: next });
              }}>
                <option value="" disabled>field…</option>
                {withCurrent(sourceFields, f).map((sf) => (
                  <option key={sf.field} value={sf.field}>{sf.field}{sf.unavailable ? " (unavailable)" : ""}</option>
                ))}
              </select>
              <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>is also the same</span>
              <button className="icon-btn" onClick={() => {
                set({ group_by_fields: (lookup.group_by_fields || []).filter((_, idx) => idx !== i) });
              }}><Trash2 size={13} /></button>
            </div>
          ))}
          <button className="btn btn--ghost btn--xs" style={{ marginTop: 6 }}
                  onClick={() => set({ group_by_fields: [...(lookup.group_by_fields || []), sourceFields[0]?.field || ""] })}>
            <Plus size={13} /> Add another group-by field
          </button>
          <p className="empty-hint" style={{ marginTop: 4 }}>
            Rows only land in the same group when every field above matches — e.g. CCY *and* Tenor together, so two
            trades that merely share a currency but have different tenors are never treated as a pair.
          </p>
          <div className="lk-block-title" style={{ marginTop: 10 }}>Representative row (optional)</div>
          <ConditionBuilder group={lookup.selector || newGroup()} onChange={(g) => set({ selector: g })}
                            fields={sourceFields} meta={meta} />
          <p className="empty-hint" style={{ marginTop: 6 }}>
            Within each group, the first row matching this condition is the one whose fields get broadcast to every
            row sharing the same {groupByLabel} value — e.g. the row flagged as the parent leg.
            Leave empty to just use each group's first row.
          </p>
        </div>
      )}

      {isDayPartitioned && (
        <div className="lk-block">
          <div className="lk-block-title">Day-partitioned source</div>
          <div className="lk-row">
            <label className="control" style={{ minWidth: 320 }}><span>Path template</span>
              <input value={lookup.day_partition_path_template || ""}
                     onChange={(e) => set({ day_partition_path_template: e.target.value })}
                     placeholder="/mnt/river/%Y/%m/%d/All trades.csv" /></label>
            <label className="control"><span>Day field</span>
              <select value={lookup.day_partition_field || ""} onChange={(e) => set({ day_partition_field: e.target.value })}>
                <option value="" disabled>field…</option>
                {withCurrent(sourceFields, lookup.day_partition_field).map((f) => (
                  <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
                ))}
              </select>
            </label>
          </div>
          {isXlsxDayTemplate && (
            <div className="lk-row" style={{ marginTop: 6 }}>
              <label className="control" style={{ minWidth: 180 }}><span>Sheet name</span>
                <input value={lookup.day_partition_sheet_name || ""}
                       onChange={(e) => set({ day_partition_sheet_name: e.target.value })}
                       placeholder="e.g. TP Market Data" /></label>
              <label className="control" style={{ minWidth: 120 }}><span>Column range</span>
                <input value={lookup.day_partition_column_range || ""}
                       onChange={(e) => set({ day_partition_column_range: e.target.value })}
                       placeholder="e.g. A:I" /></label>
              <label className="control" style={{ minWidth: 260 }}><span>Password env var (optional)</span>
                <input value={lookup.day_partition_password_env_var || ""}
                       onChange={(e) => set({ day_partition_password_env_var: e.target.value })}
                       placeholder="e.g. TP_MARKET_DATA_XLSX_PASSWORD"
                       disabled={!!(lookup.day_partition_password || "").trim()} /></label>
              <label className="control" style={{ minWidth: 180 }}><span>— or type a password</span>
                <input type="password" value={lookup.day_partition_password || ""}
                       onChange={(e) => set({ day_partition_password: e.target.value })}
                       placeholder={lookup.day_partition_encrypted_password ? "unchanged (stored encrypted)" : "stored encrypted"}
                       disabled={!!(lookup.day_partition_password_env_var || "").trim()} /></label>
            </div>
          )}
          <p className="empty-hint" style={{ marginTop: 6 }}>
            For each record, the Day field's value (e.g. OMRCTRADECLOSEOFBUSINESSDATE) is parsed as a date and
            the path template is rendered as a strftime pattern against it — standard codes anywhere in the
            path, including the filename: %Y (4-digit year), %y (2-digit year), %m (01-12), %b/%B (Sep/September),
            %d (01-31). E.g. "/mnt/river/%Y/%B/trades_%Y%m%d.csv" → "/mnt/river/2026/September/trades_20260924.csv".
            So each record is looked up against its own day's file, however that file's path is actually laid out.
            A path ending in .xlsx/.xlsm/.xlsb is read as Excel — password-protected is fine (name a server env var, or
            type a password directly in; it's encrypted before it's saved, never kept as plain text), and if one
            sheet holds two separate tables side by side, a second LOOKUP node with the same path/sheet and the
            other table's column range (e.g. "A:I" vs. "K:U") reads it as an independent source.
          </p>
          <div className="lk-row" style={{ marginTop: 10 }}>
            <label className="control"><span>Sample day (to read column names from)</span>
              <input type="date" value={daySampleDate} onChange={(e) => setDaySampleDate(e.target.value)} />
            </label>
            <button className="btn btn--ghost btn--xs" disabled={!lookup.day_partition_path_template || loadingDayColumns}
                    onClick={loadDayColumns}>
              {loadingDayColumns ? "Loading…" : "Load columns"}
            </button>
            {dayColumns.length > 0 && <span className="empty-hint">{dayColumns.length} column(s) loaded</span>}
          </div>
          {dayColumnsError && (
            <p className="empty-hint" style={{ color: "var(--breach, #c0392b)" }}>
              Couldn't read columns for that sample day: {dayColumnsError}. You can still type the column name
              directly below — it just won't be checked against the real file until this resolves.
            </p>
          )}
        </div>
      )}

      {(lookup.lookup_type === "exact" || lookup.lookup_type === "composite" || lookup.lookup_type === "interpolated") && (
        <div className="lk-block">
          <div className="lk-block-title">{lookup.lookup_type === "interpolated" ? "Curve key" : "Join keys"}</div>
          {(lookup.join_keys || []).map((jk, i) => (
            <div key={i} style={{ border: "1px solid var(--line)", borderRadius: 7, padding: 8, marginBottom: 8 }}>
              <div className="lk-row" style={{ marginBottom: 0 }}>
                <span className="mono">source</span>
                {srcValueAsArray(jk).map((srcField, si) => (
                  <span key={si} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    {si > 0 && <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>+</span>}
                    <select value={srcField} onChange={(e) => updateJoinKeySourceColumn(i, si, e.target.value)}>
                      {withCurrent(sourceFields, srcField).map((f) => (
                        <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
                      ))}
                    </select>
                    {srcValueAsArray(jk).length > 1 && (
                      <button className="icon-btn" onClick={() => removeJoinKeySourceColumn(i, si)}>
                        <Trash2 size={11} />
                      </button>
                    )}
                  </span>
                ))}
                <button className="btn btn--ghost btn--xs" onClick={() => addJoinKeySourceColumn(i)}>
                  + combine column
                </button>
                <span className="mono">=</span>
                {refValueAsArray(jk).map((refCol, ri) => (
                  <span key={ri} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    {ri > 0 && <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>+</span>}
                    {isDayPartitioned && dayColumns.length === 0 ? (
                      <input value={refCol || ""} placeholder="e.g. UTI" style={{ width: 110 }}
                             onChange={(e) => updateJoinKeyReferenceColumn(i, ri, e.target.value)} />
                    ) : (
                      <select value={refCol} onChange={(e) => updateJoinKeyReferenceColumn(i, ri, e.target.value)}>
                        {dayOrRefColumns.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    )}
                    {refValueAsArray(jk).length > 1 && (
                      <button className="icon-btn" onClick={() => removeJoinKeyReferenceColumn(i, ri)}>
                        <Trash2 size={11} />
                      </button>
                    )}
                  </span>
                ))}
                <button className="btn btn--ghost btn--xs" onClick={() => addJoinKeyReferenceColumn(i)}>
                  + combine column
                </button>
                <button className="icon-btn" onClick={() => removeJoinKey(i)}><Trash2 size={13} /></button>
              </div>
              {srcValueAsArray(jk).length > 1 && (
                <p className="empty-hint" style={{ marginTop: 4 }}>
                  Built from these dataset fields concatenated together, plain (no separator) —
                  e.g. {srcValueAsArray(jk).join(" + ")} — for when the dataset has no single field already
                  holding the combined value (the alternative to a "concatenate fields" TRANSFORM node upstream).
                </p>
              )}
              {refValueAsArray(jk).length > 1 && (
                <p className="empty-hint" style={{ marginTop: 4 }}>
                  Matches against these reference columns' values concatenated together, plain (no separator) —
                  e.g. {refValueAsArray(jk).join(" + ")} — for a reference file that splits one logical key part
                  across more than one of its own columns.
                </p>
              )}
              <div className="lk-row" style={{ marginTop: 6, marginBottom: 0 }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--muted)" }}>fallback if no match</span>
                {srcValueAsArray(jk).map((srcField, si) => (
                  <label key={si} className="control" style={{ minWidth: 110 }}>
                    <span>{srcField || "…"}</span>
                    <input value={defaultsAsArray(jk)[si] ?? ""} placeholder="none"
                           onChange={(e) => updateJoinKeyDefault(i, si, e.target.value)} />
                  </label>
                ))}
              </div>
              {defaultsAsArray(jk).some((d) => d) && (
                <p className="empty-hint" style={{ marginTop: 4 }}>
                  The record's own value is always tried first. Only if NOTHING matches does this rule retry once
                  with every filled-in fallback above substituted together (fields left blank keep the record's
                  own real value) — e.g. a site with no curve of its own falls back to a "DEFAULT" curve while
                  currency/tenor still use the record's real values. A match found this way is flagged in the
                  dry-run trail as using a default, not shown as an ordinary match.
                </p>
              )}
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
                {withCurrent(sourceFields, lookup.range_field).map((f) => (
                  <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
                ))}
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
                {withCurrent(sourceFields, lookup.date_field).map((f) => (
                  <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
                ))}
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

      {lookup.lookup_type === "interpolated" && (
        <div className="lk-block">
          <div className="lk-block-title">Tenor-ladder interpolation</div>
          <div className="lk-row">
            <label className="control"><span>Source tenor field (day count)</span>
              <select value={lookup.interp_source_field || ""} onChange={(e) => set({ interp_source_field: e.target.value })}>
                <option value="" disabled>field…</option>
                {withCurrent(sourceFields, lookup.interp_source_field).map((f) => (
                  <option key={f.field} value={f.field}>{f.field}{f.unavailable ? " (unavailable)" : ""}</option>
                ))}
              </select>
            </label>
            <label className="control"><span>Reference node column (day count)</span>
              {isDayPartitioned && dayColumns.length === 0 ? (
                <input value={lookup.interp_node_field || ""} placeholder="e.g. Days to Maturity"
                       onChange={(e) => set({ interp_node_field: e.target.value })} />
              ) : (
                <select value={lookup.interp_node_field || ""} onChange={(e) => set({ interp_node_field: e.target.value })}>
                  <option value="" disabled>column…</option>
                  {dayOrRefColumns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
            </label>
            <label className="control"><span>Outside the curve's range</span>
              <select value={lookup.interp_out_of_range || "clamp"} onChange={(e) => set({ interp_out_of_range: e.target.value })}>
                <option value="clamp">Clamp to nearest edge node</option>
                <option value="reject">Treat as no match</option>
              </select>
            </label>
          </div>
          <p className="empty-hint" style={{ marginTop: 6 }}>
            Within the curve matched by the curve key above, the record's tenor (as a day count) is bracketed
            between the nearest lower/equal and next-higher node by the reference node column, and each field
            below is linearly interpolated between those two nodes' values. An exact node hit needs no
            interpolation; outside the curve's own range, behavior follows the setting above.
          </p>
        </div>
      )}

      <div className="lk-block">
        <div className="lk-block-title">Fields to enrich with</div>
        {(lookup.fields || []).map((fm, i) => (
          <div key={i} style={{ border: "1px solid var(--line)", borderRadius: 7, padding: 8, marginBottom: 8 }}>
            <div className="lk-row" style={{ marginBottom: 0 }}>
              {isDayPartitioned && dayColumns.length === 0 ? (
                <input value={fm.source_column || ""} placeholder="e.g. Notional"
                       onChange={(e) => updateFieldMap(i, { source_column: e.target.value })} />
              ) : (
                <select value={fm.source_column} onChange={(e) => updateFieldMap(i, { source_column: e.target.value })}>
                  {enrichColumns.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
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
                  <option value="first">first (non-null)</option>
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
                Every row sharing the same {groupByLabel} value gets the {fm.aggregate} of{" "}
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
            isDayPartitioned && dayColumns.length === 0 ? (
              <input value={lookup.priority_field || ""} placeholder="priority field…"
                     onChange={(e) => set({ priority_field: e.target.value })} />
            ) : (
              <select value={lookup.priority_field || ""} onChange={(e) => set({ priority_field: e.target.value })}>
                <option value="">priority field…</option>
                {enrichColumns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            )
          )}
        </div>
      </div>
      )}
    </div>
  );
}
