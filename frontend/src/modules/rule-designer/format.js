export function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export const STATUS_LABEL = {
  DRAFT: "Draft", VALIDATED: "Validated", DRY_RUN_COMPLETED: "Dry-run complete",
  PENDING_APPROVAL: "Pending approval", APPROVED: "Approved", PUBLISHED: "Published", REJECTED: "Rejected",
};
