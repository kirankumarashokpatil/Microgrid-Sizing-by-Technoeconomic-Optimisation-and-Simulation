// Small reusable SVG chart builders (dependency-free) for the results views.
// All take plain arrays/objects and return React-ready <svg> children via the
// components below. Colours match the NatPower palette.

import { column } from "./api.js";
import { downsample } from "./svg.js";

export const PALETTE = {
  pv: "#e0922f", wind: "#1f8a8a", bess: "#2f8f5b", grid: "#c2603a",
  unmet: "#9aa7ad", curtail: "#7a6f9b", load: "#3f7cac", soc: "#2f8f5b",
};

// Merit-order split of the real flows → annual MWh totals (dt in hours).
export function energyTotals(flows, dt = 1) {
  const load = column(flows, "load_mw").map(Number);
  const pvav = column(flows, "pv_avail_mw").map(Number);
  const grid = column(flows, "grid_import_mw").map(Number);
  const disc = column(flows, "bess_discharge_mw").map(Number);
  const chg  = column(flows, "bess_charge_mw").map(Number);
  const unmet = column(flows, "unmet_mw").map(Number);
  const n = load.length;
  let dLoad = 0, dDirect = 0, dBess = 0, dGrid = 0, dUnmet = 0, dPvAv = 0, dPvUsed = 0, dCurt = 0;
  for (let i = 0; i < n; i++) {
    const l = load[i] || 0, g = grid[i] || 0, d = disc[i] || 0;
    const direct = Math.max(0, l - g - d);
    const pv = pvav[i] || 0, pvToLoad = Math.min(pv, direct), pvToBess = Math.min(Math.max(0, pv - pvToLoad), chg[i] || 0);
    dLoad += l; dDirect += direct; dBess += d; dGrid += g; dUnmet += unmet[i] || 0;
    dPvAv += pv; dPvUsed += pvToLoad + pvToBess; dCurt += Math.max(0, pv - pvToLoad - pvToBess);
  }
  const s = (v) => Math.round(v * dt);
  return {
    demand: { total: s(dLoad), direct: s(dDirect), bess: s(dBess), grid: s(dGrid), unmet: s(dUnmet) },
    gen: { avail: s(dPvAv), used: s(dPvUsed), curtail: s(dCurt) },
  };
}

// Monthly stacked dispatch (direct / bess / grid) from the flows.
export function monthlyDispatch(flows, dt = 1) {
  const ts = column(flows, "timestamp");
  const load = column(flows, "load_mw").map(Number);
  const grid = column(flows, "grid_import_mw").map(Number);
  const disc = column(flows, "bess_discharge_mw").map(Number);
  const M = Array.from({ length: 12 }, () => ({ direct: 0, bess: 0, grid: 0 }));
  for (let i = 0; i < ts.length; i++) {
    const m = new Date(ts[i]).getMonth();
    if (isNaN(m)) continue;
    const g = grid[i] || 0, d = disc[i] || 0;
    M[m].direct += Math.max(0, (load[i] || 0) - g - d) * dt;
    M[m].bess += d * dt; M[m].grid += g * dt;
  }
  return M.map((x) => ({ direct: Math.round(x.direct), bess: Math.round(x.bess), grid: Math.round(x.grid) }));
}

export function socSeries(flows) {
  return downsample(column(flows, "bess_soc_mwh").map(Number), 300);
}
