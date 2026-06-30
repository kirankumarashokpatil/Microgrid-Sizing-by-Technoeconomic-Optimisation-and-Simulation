// Dynamic one-line answers — every chart should say what it's FOR and what it
// found. These read the same flows/curve the chart draws, so the sentence and
// the picture can never disagree.
import { column } from "./api.js";
import { computeEnergyBalance, computeMonthly, fmtMWh } from "./energy.js";
import { demandServedFromCurve } from "./decision.js";

export function balanceInsight(flows, dt) {
  const b = computeEnergyBalance(flows, dt);
  if (!b) return null;
  const gpct = b.demand > 0 ? (100 * b.grid) / b.demand : 0;
  return `${b.ssr.toFixed(0)}% of demand is met on-site; the grid still supplies ${fmtMWh(b.grid)} (${gpct.toFixed(0)}%).`;
}

export function pvInsight(flows, dt) {
  const b = computeEnergyBalance(flows, dt);
  if (!b || b.pvAvail < 1) return null;
  const cpct = (100 * b.curtailed) / b.pvAvail;
  const verdict = cpct > 25 ? "PV looks oversized for this load" : cpct > 10 ? "some PV is spilled" : "PV is well used";
  return `${cpct.toFixed(0)}% of available PV is curtailed — ${verdict}.`;
}

export function monthlyInsight(flows, dt) {
  const M = computeMonthly(flows, dt);
  if (!M) return null;
  const sorted = [...M].sort((a, b) => a.ssr - b.ssr);
  const worst = sorted[0], best = sorted[sorted.length - 1];
  const lean = M.filter((m) => m.ssr < 60).map((m) => m.month);
  const tail = lean.length ? ` Grid is leaned on mainly in ${lean.join(", ")}.` : "";
  return `Self-sufficiency swings from ${worst.ssr.toFixed(0)}% (${worst.month}) to ${best.ssr.toFixed(0)}% (${best.month}).${tail}`;
}

export function socInsight(flows) {
  if (!flows || !flows.columns.includes("bess_soc_mwh")) return null;
  const soc = column(flows, "bess_soc_mwh");
  if (!soc.length) return null;
  const lo = Math.min(...soc), hi = Math.max(...soc);
  if (hi <= 0) return "No battery in this design — nothing to cycle.";
  return `The battery swings between ${fmtMWh(lo)} and ${fmtMWh(hi)} — ${hi - lo < hi * 0.1 ? "barely cycling (over-sized?)" : "actively cycling"}.`;
}

export function durationInsight(flows) {
  if (!flows || !flows.columns.includes("grid_import_mw")) return null;
  const g = column(flows, "grid_import_mw").slice().sort((a, b) => b - a);
  if (!g.length) return null;
  const p5 = g[Math.floor(g.length * 0.05)];
  const zero = g.filter((v) => v <= 0.01).length;
  return `The top 5% of hours need ≥ ${p5.toFixed(0)} MW of grid; ${(100 * zero / g.length).toFixed(0)}% of hours need none.`;
}

export function demandServedInsight(table) {
  const rows = demandServedFromCurve(table);
  if (!rows || rows.length < 2) return null;
  const a = rows[0], z = rows[rows.length - 1];
  return `Raising the target from ${a.ssr.toFixed(0)}% to ${z.ssr.toFixed(0)}% cuts grid import from ${fmtMWh(a.grid)} to ${fmtMWh(z.grid)}.`;
}
