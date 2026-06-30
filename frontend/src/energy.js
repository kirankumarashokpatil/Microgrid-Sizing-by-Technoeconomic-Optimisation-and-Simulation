// Exact annual energy decomposition from the dispatch flows — the same framing
// as the engine's energy-balance table: FOUR sources sum to demand. Nothing here
// is approximated; every number is a sum over the causal flows the engine
// returned, so the React view and the engine agree to the MWh.
import { column } from "./api.js";

export function computeEnergyBalance(flows, dt = 1) {
  if (!flows) return null;
  const sum = (c) => (flows.columns.includes(c)
    ? column(flows, c).reduce((a, v) => a + (v || 0), 0) * dt : 0);

  const demand = sum("load_mw");
  const bess = sum("bess_discharge_mw");      // battery → load
  const grid = sum("grid_import_mw");         // grid → load
  const unmet = sum("unmet_mw");              // shed
  // PV served directly to load is the residual of the balance — exactly how the
  // engine's table derives it (four sources must sum to demand).
  const pvDirect = Math.max(0, demand - bess - grid - unmet);

  const charge = sum("bess_charge_mw");       // PV → battery (BTM import-only)
  const pvAvail = sum("pv_avail_mw");
  const pvUsed = sum("pv_used_mw");           // = pvDirect + charge
  const curtailed = sum("curtailed_mw");
  const exported = sum("export_mw");

  const servedOnSite = pvDirect + bess;       // SSR numerator
  const ssr = demand > 0 ? (100 * servedOnSite) / demand : 0;
  const scr = pvAvail > 0 ? (100 * pvUsed) / pvAvail : 0;

  const peakGrid = flows.columns.includes("grid_import_mw")
    ? Math.max(...column(flows, "grid_import_mw")) : 0;

  return {
    demand, pvDirect, bess, grid, unmet, charge,
    pvAvail, pvUsed, curtailed, exported, servedOnSite, ssr, scr, peakGrid,
    // residual should be ~0 — the same integrity check the engine prints
    residual: demand - (pvDirect + bess + grid + unmet),
  };
}

export const fmtMWh = (v) =>
  (v ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 }) + " MWh";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Monthly decomposition of how demand was served — reveals seasonality (winter
// grid reliance vs summer self-sufficiency), the single most useful "when do I
// lean on the grid?" view. PV-direct is the residual, same as the annual table.
export function computeMonthly(flows, dt = 1) {
  if (!flows || !flows.columns.includes("timestamp")) return null;
  const ci = (n) => flows.columns.indexOf(n);
  const [iT, iL, iB, iG, iU] = ["timestamp", "load_mw", "bess_discharge_mw", "grid_import_mw", "unmet_mw"].map(ci);
  const M = Array.from({ length: 12 }, () => ({ pv: 0, bess: 0, grid: 0, load: 0 }));
  for (const row of flows.rows) {
    const m = new Date(row[iT]).getMonth();
    const load = row[iL] || 0, bess = row[iB] || 0, grid = row[iG] || 0, unmet = iU >= 0 ? row[iU] || 0 : 0;
    M[m].pv += Math.max(0, load - bess - grid - unmet) * dt;
    M[m].bess += bess * dt;
    M[m].grid += grid * dt;
    M[m].load += load * dt;
  }
  return M.map((m, i) => ({ month: MONTHS[i], ...m, ssr: m.load > 0 ? (100 * (m.pv + m.bess)) / m.load : 0 }));
}

// One representative day's hourly operation for a given month (defaults to the
// 15th, or the nearest available day) — used for the winter vs summer panels.
export function representativeDay(flows, month, dayOfMonth = 15) {
  if (!flows || !flows.columns.includes("timestamp")) return null;
  const ci = (n) => flows.columns.indexOf(n);
  const [iT, iL, iP, iB, iG, iS] =
    ["timestamp", "load_mw", "pv_avail_mw", "bess_discharge_mw", "grid_import_mw", "bess_soc_mwh"].map(ci);
  const inMonth = flows.rows.filter((r) => new Date(r[iT]).getMonth() === month);
  if (!inMonth.length) return null;
  let best = null, bestDiff = 1e9;
  for (const r of inMonth) {
    const diff = Math.abs(new Date(r[iT]).getDate() - dayOfMonth);
    if (diff < bestDiff) { bestDiff = diff; best = new Date(r[iT]).getDate(); }
  }
  const day = inMonth.filter((r) => new Date(r[iT]).getDate() === best)
    .sort((a, b) => new Date(a[iT]) - new Date(b[iT]));
  return {
    label: `${MONTHS[month]} ${best}`,
    hour: day.map((r) => new Date(r[iT]).getHours() + new Date(r[iT]).getMinutes() / 60),
    load: day.map((r) => r[iL] || 0),
    pv: day.map((r) => r[iP] || 0),
    bess: day.map((r) => r[iB] || 0),
    grid: day.map((r) => r[iG] || 0),
    soc: day.map((r) => (iS >= 0 ? r[iS] || 0 : 0)),
  };
}
