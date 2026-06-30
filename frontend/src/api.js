// Thin client over the FastAPI backend. In dev, Vite proxies /api/* → :8000.
// This file is the React equivalent of the Streamlit api_get/api_run helpers —
// the only place that knows the backend exists.

const BASE = "/api";

async function handle(res) {
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = await res.text();
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export const getHealth = () => fetch(`${BASE}/health`).then(handle);
export const getCoverage = () => fetch(`${BASE}/coverage`).then(handle);
export const getScenarios = () => fetch(`${BASE}/scenarios`).then(handle);
export const getScenario = (id) => fetch(`${BASE}/scenarios/${id}`).then(handle);
export const getWizard = () => fetch(`${BASE}/wizard`).then(handle);

export const getProfileSummary = (profilePath) => {
  const q = profilePath ? `?profile_path=${encodeURIComponent(profilePath)}` : "";
  return fetch(`${BASE}/profile-summary${q}`).then(handle);
};

export const runScenario = (payload) =>
  fetch(`${BASE}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(handle);

export const uploadProfile = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return fetch(`${BASE}/upload-profile`, { method: "POST", body: fd }).then(handle);
};

// Turn the backend's {columns, rows} table into an array of row-objects, the
// shape React charts want.
export function recordsToObjects(rec) {
  if (!rec) return null;
  return rec.rows.map((row) =>
    Object.fromEntries(rec.columns.map((c, i) => [c, row[i]]))
  );
}

// Pull one column out of a {columns, rows} table as a flat array.
export function column(rec, name) {
  if (!rec) return [];
  const i = rec.columns.indexOf(name);
  if (i < 0) return [];
  return rec.rows.map((r) => r[i]);
}
