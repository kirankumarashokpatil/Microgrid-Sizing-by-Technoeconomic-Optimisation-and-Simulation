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

// Detect the scenario from the supplied inputs (replaces the question wizard).
export const resolveScenario = (inputs) =>
  fetch(`${BASE}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(inputs),
  }).then(handle);

// The feasible SSR band [min, max] for these inputs (fast, before picking a target).
export const ssrRange = (inputs) =>
  fetch(`${BASE}/ssr-range`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(inputs),
  }).then(handle);

// Phase 2 — re-cost an already-sized curve with new CAPEX assumptions (no LP re-solve).
export const overlayEconomics = (inputs) =>
  fetch(`${BASE}/economics`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(inputs),
  }).then(handle);

export const getProfileSummary = (profilePath) => {
  const q = profilePath ? `?profile_path=${encodeURIComponent(profilePath)}` : "";
  return fetch(`${BASE}/profile-summary${q}`).then(handle);
};

// Real load / solar / wind shapes from the dataset (downsampled) for charting.
export const getProfileSeries = (profilePath, points = 336) => {
  const q = new URLSearchParams({ points });
  if (profilePath) q.set("profile_path", profilePath);
  return fetch(`${BASE}/profile-series?${q}`).then(handle);
};

// Available generation a parcel can host — computed by the backend (core/site.py).
export const parcelCapacity = (inputs) =>
  fetch(`${BASE}/parcel-capacity`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(inputs),
  }).then(handle);

// The reciprocal: a target generation (MW) → the land (ha) it needs + yields.
export const capacityFromMw = (inputs) =>
  fetch(`${BASE}/capacity-from-mw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(inputs),
  }).then(handle);

export const runScenario = (payload) =>
  fetch(`${BASE}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(handle);

// Land-first evaluation core (Phase 1): one land/demand input → a scenario
// comparison { inputs, scenarios: [grid-only, generation no-BESS, generation+BESS] }.
export const runDesign = (payload) =>
  fetch(`${BASE}/design`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(handle);

// Split optimiser (Phase 3, sweep): the tool decides the cost-optimal solar/BESS
// split within the available land → { recommended, frontier, pv_ceiling_mw }.
export const runOptimiseSplit = (payload) =>
  fetch(`${BASE}/optimise-split`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(handle);

// Download an .xlsx workbook of the current result (built server-side, no re-run).
export async function exportXlsx(payload) {
  const res = await fetch(`${BASE}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Export failed (HTTP ${res.status})`);
  const blob = await res.blob();
  const dispo = res.headers.get("Content-Disposition") || "";
  const m = dispo.match(/filename="([^"]+)"/);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = m ? m[1] : "DIP_result.xlsx";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export const uploadProfile = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return fetch(`${BASE}/upload-profile`, { method: "POST", body: fd }).then(handle);
};

// Parse an uploaded land-boundary export (GeoJSON / KMZ / KML) into parcel
// rings — {rings: [{latlngs, area_ha, centroid, name}]} — computed backend-side.
export const parseBoundary = (file) => {
  const fd = new FormData();
  fd.append("file", file);
  return fetch(`${BASE}/parse-boundary`, { method: "POST", body: fd }).then(handle);
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
