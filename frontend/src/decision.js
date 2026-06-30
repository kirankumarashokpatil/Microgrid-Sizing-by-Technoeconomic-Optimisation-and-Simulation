// Decision helpers — turn a sizing curve into an actual recommendation.
// A project manager doesn't want the whole curve; they want "how far is it worth
// pushing, and what does each extra point cost?". These compute that.
import { column } from "./api.js";

// Feasible (x, y) pairs from a curve, using the deliverable (must-buy) battery
// where available, else the LP bound. x is the target axis chosen by curveX.
export function curvePoints(table, xCol) {
  if (!table) return [];
  const yCol = table.columns.includes("Deliverable BESS Energy (MWh · Model R)")
    ? "Deliverable BESS Energy (MWh · Model R)" : "BESS Energy (MWh)";
  const x = column(table, xCol), y = column(table, yCol), feas = column(table, "Feasible");
  return x
    .map((xv, i) => ({ x: xv, y: y[i], ok: feas[i] !== false }))
    .filter((p) => p.ok && p.x != null && p.y != null && !isNaN(p.x) && !isNaN(p.y));
}

// Knee via the Kneedle chord-distance method: the elbow where a convex-increasing
// curve turns sharply upward. Returns the point + a "cost multiplier" = how much
// steeper the curve is just after the knee vs just before.
export function findKnee(points) {
  if (points.length < 3) return null;
  const x0 = points[0].x, x1 = points[points.length - 1].x;
  const y0 = points[0].y, y1 = points[points.length - 1].y;
  const dx = x1 - x0, dy = y1 - y0;
  if (dx === 0 || dy === 0) return null;
  let best = -Infinity, bi = 0;
  points.forEach((p, i) => {
    const xn = (p.x - x0) / dx;
    const yn = (p.y - y0) / dy;
    const d = xn - yn; // largest where the curve is still flat but x has advanced
    if (d > best) { best = d; bi = i; }
  });
  if (bi <= 0 || bi >= points.length - 1) return { ...points[bi], index: bi, multiplier: null };
  const slopeBefore = (points[bi].y - points[0].y) / (points[bi].x - points[0].x || 1);
  const slopeAfter = (points[points.length - 1].y - points[bi].y) / (points[points.length - 1].x - points[bi].x || 1);
  const multiplier = slopeBefore > 1e-6 ? slopeAfter / slopeBefore : null;
  return { ...points[bi], index: bi, multiplier };
}

// Decompose "how demand is served" across a sizing curve, using the operational
// grid import the engine reports per point. PV-direct is constant (PV serves load
// first regardless of battery); BESS→load is the rest of on-site supply. Lets the
// curve become the "grid import shrinks as SSR rises" comparison bar.
export function demandServedFromCurve(table) {
  if (!table || !table.columns.includes("Operational Grid Import (MWh)")) return null;
  const ssr = column(table, "Operational SSR (%)");
  const grid = column(table, "Operational Grid Import (MWh)");
  const feas = column(table, "Feasible");
  const tgt = column(table, "Target SSR (%)");
  const rows = grid.map((g, i) => ({ grid: g, ssr: ssr[i], target: tgt[i], ok: feas[i] !== false }))
    .filter((r) => r.ok && r.grid != null && r.ssr != null);
  if (rows.length < 2) return null;
  // demand from any point: grid = demand × (1 − SSR); pv-direct = demand − max(grid).
  const ref = rows.find((r) => r.ssr > 1) || rows[rows.length - 1];
  const demand = ref.grid / (1 - ref.ssr / 100);
  const pvDirect = demand - Math.max(...rows.map((r) => r.grid));
  return rows.map((r) => ({
    target: r.target, ssr: r.ssr, grid: r.grid,
    pv: pvDirect, bess: Math.max(0, demand - r.grid - pvDirect), demand,
  }));
}

// The achievable SSR band for this fixed PV: the floor (what PV reaches with NO
// battery) and the ceiling (the most SSR any battery can reach — SSR_max). A
// target only makes sense inside this band. Read from the curve's operational
// SSR: the flat low-target rows are the no-battery floor; the top feasible row
// is the ceiling.
export function ssrBand(table) {
  if (!table) return null;
  const tt = column(table, "Target Type")[0];
  if (tt !== "ssr") return null;
  const ssrCol = table.columns.includes("Operational SSR (%)") ? "Operational SSR (%)" : "Achieved SSR (%)";
  if (!table.columns.includes(ssrCol)) return null;
  const ssr = column(table, ssrCol);
  const bessE = column(table, "BESS Energy (MWh)");
  const feas = column(table, "Feasible");
  const vals = ssr.map((s, i) => ({ s, bess: bessE[i] || 0, ok: feas[i] !== false }))
    .filter((r) => r.ok && r.s != null);
  if (!vals.length) return null;
  // Floor = SSR where there is (essentially) no battery; ceiling = max reached.
  const noBess = vals.filter((r) => r.bess < 0.5);
  const floor = noBess.length ? Math.max(...noBess.map((r) => r.s)) : Math.min(...vals.map((r) => r.s));
  const ceiling = Math.max(...vals.map((r) => r.s));
  return { floor, ceiling };
}

// Marginal battery per unit of target, between consecutive feasible points.
export function marginalReturns(points) {
  const out = [];
  for (let i = 1; i < points.length; i++) {
    const dx = points[i].x - points[i - 1].x;
    if (dx <= 0) continue;
    out.push({ x: (points[i].x + points[i - 1].x) / 2, slope: (points[i].y - points[i - 1].y) / dx });
  }
  return out;
}
