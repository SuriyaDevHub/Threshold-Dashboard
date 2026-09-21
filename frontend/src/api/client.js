// Tiny fetch wrapper. All module data goes through here.
const BASE = "/api";

async function request(path, { params, ...opts } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    });
  }
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${text ? ` — ${text}` : ""}`);
  }
  return res.json();
}

export const api = {
  get: (path, params) => request(path, { params }),
  // Convenience for module endpoints. Pass opts for POST: api.module(id, path, undefined, {method:'POST', body})
  module: (id, path, params, opts) => request(`/modules/${id}${path}`, { params, ...opts }),
  listModules: () => request("/modules"),
  health: () => request("/health"),
};
