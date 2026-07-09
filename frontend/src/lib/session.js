// Session helpers — the "single source of truth" plumbing for the wizard.
//
// Two jobs:
//   1. sizingSignature(cfg, profile) — a stable string of ONLY the inputs that
//      change a sizing result. App stamps it onto a result when a run completes
//      and recomputes it each render; if they differ, the shown result is STALE
//      (its inputs moved on) and the UI must say so instead of pretending it's
//      current. Both App and Step4Sizing import this one function so they can
//      never disagree about what "stale" means.
//   2. load/saveSession — persist the design session to localStorage so a browser
//      refresh restores the project instead of nuking a minute of compute.

// The sizing-relevant slice of cfg + the dataset. Anything NOT here (project name,
// map viewport, …) can change without invalidating a result.
export function sizingSignature(cfg = {}, profile = null) {
  const t = cfg.topoSignals || null;
  const sig = {
    loads: (cfg.loads || []).map((l) => [l.load_type, +l.peak_mw || 0, +l.baseline_mw || 0]),
    parcels: cfg.parcels || null,      // solar/wind buildable area → nameplates
    tech: cfg.tech || null,            // which technologies are on
    topology: cfg.topology || null,
    // FlowDesigner-derived signals that override the radio defaults on /run.
    topo: t && {
      d: t.derived_topology, pv: t.pv_mw, wind: t.wind_mw,
      grid: t.grid_available, off: t.off_grid, sa: t.standalone,
      exp: t.export_limit_mw, peak: t.peak_load_mw,
    },
    obj: cfg.btmObjective ?? null,
    ssr: cfg.ssrTarget ?? null, gc: cfg.gcTarget ?? null,
    firm: cfg.firmnessTarget ?? null, curt: cfg.curtailmentTarget ?? null,
    dispatch: cfg.dispatch || null,    // merit order + grid-charging policy
    profile: profile?.profile_path || "bundled",
  };
  return JSON.stringify(sig);
}

const KEY = "dip.session.v2";

// Restore a persisted session, or null. Never throws (private-mode / corrupt JSON).
export function loadSession() {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// Persist a session slice. `flows` (a full 8760-row forward-eval) is deliberately
// NOT passed in by the caller — it's large and re-derivable by re-running — so the
// payload stays well under the localStorage quota.
export function saveSession(session) {
  try {
    localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* quota exceeded / disabled — persistence is best-effort, never fatal */
  }
}

export function clearSession() {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}
