// Thin wrapper over the shared api client, scoped to the rule-designer module.
import { api } from "../../api/client.js";

const MOD = "rule-designer";
const get = (path, params) => api.module(MOD, path, params);
const post = (path, body) => api.module(MOD, path, undefined, { method: "POST", body: JSON.stringify(body) });
const put = (path, body) => api.module(MOD, path, undefined, { method: "PUT", body: JSON.stringify(body) });
const del = (path, params) => api.module(MOD, path, params, { method: "DELETE" });

export const rd = {
  meta: () => get("/meta"),
  dashboard: () => get("/dashboard"),

  datasets: () => get("/datasets"),
  datasetSchema: (id) => get(`/datasets/${id}/schema`),
  datasetPreview: (id, limit = 50) => get(`/datasets/${id}/preview`, { limit }),
  uploadDataset: (label, csv_text) => post("/datasets/upload", { label, csv_text }),

  referenceFiles: () => get("/reference-files"),
  referenceFile: (id) => get(`/reference-files/${id}`),
  referenceRows: (id, version, limit = 50) => get(`/reference-files/${id}/rows`, { version, limit }),
  uploadReference: (actor, role, name, csv_text) => post("/reference-files/upload", { actor, role, name, csv_text }),
  deleteReference: (id, actor) => del(`/reference-files/${id}`, { actor }),

  rules: () => get("/rules"),
  rule: (id) => get(`/rules/${id}`),
  createRule: (actor, role, rule) => post("/rules", { actor, role, rule }),
  updateRule: (id, actor, role, rule) => put(`/rules/${id}`, { actor, role, rule }),
  deleteRule: (id, actor, role) => del(`/rules/${id}`, { actor, role }),
  validateRule: (id, actor) => post(`/rules/${id}/validate?actor=${encodeURIComponent(actor)}`, {}),

  dryRun: (id, body) => post(`/rules/${id}/dry-run`, body),
  getDryRun: (id) => get(`/dry-runs/${id}`),
  listDryRuns: (ruleId) => get(`/rules/${ruleId}/dry-runs`),

  impactAnalysis: (id, body) => post(`/rules/${id}/impact-analysis`, body),
  explanation: (id) => get(`/rules/${id}/explanation`),
  diff: (id, against_version) => get(`/rules/${id}/diff`, { against_version }),

  interpretFreeText: (text, dataset_id) => post("/free-text/interpret", { text, dataset_id }),

  submit: (id, body) => post(`/rules/${id}/submit`, body),
  approve: (id, body) => post(`/rules/${id}/approve`, body),
  reject: (id, body) => post(`/rules/${id}/reject`, body),
  publish: (id, body) => post(`/rules/${id}/publish`, body),
  rollback: (body) => post("/rollback", body),

  versions: () => get("/versions"),
  version: (v) => get(`/versions/${v}`),
  versionYaml: (v) => get(`/versions/${v}/yaml`),

  yamlInspect: () => get("/yaml/inspect"),
  yamlCurrent: () => get("/yaml/current"),

  audit: (params) => get("/audit", params),
};

export function newId(prefix) {
  return `${prefix}_${Math.random().toString(16).slice(2, 10)}`;
}
