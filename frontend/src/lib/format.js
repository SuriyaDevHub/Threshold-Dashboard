export const fmtPct = (v, d = 2) =>
  v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}%`;

export const fmtNum = (v, d = 4) =>
  v === null || v === undefined ? "—" : Number(v).toFixed(d);

export const fmtMoney = (v) =>
  v === null || v === undefined
    ? "—"
    : new Intl.NumberFormat("en-US", { notation: "compact" }).format(v);

// Maps a breach rate to a control status used across modules.
export function breachStatus(ratePct) {
  if (ratePct >= 10) return "breach";
  if (ratePct >= 3) return "watch";
  return "pass";
}

export const STATUS_LABEL = {
  pass: "Pass",
  watch: "Watch",
  breach: "Breach",
};
