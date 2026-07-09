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

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Build a display-ready, LEGIBLE dispatch window for the chosen period, with
// quantitative series (MW) including curtailment + unmet:
//   period "year"  → daily means (≈365 pts) so the day/night picket-fence smooths
//                    into readable bands; x-ticks at month starts.
//   period "summer"|"winter" → one representative hourly week (168 pts) so the
//                    actual charge/discharge shape is visible; x-ticks per day.
// Returns { n, load, direct, bess, grid, curt, unmet, ticks:[{pos,label}], unit,
//           labelAt(i), yMax }.
export function dispatchWindow(flows, period = "year") {
  const ts = column(flows, "timestamp");
  const load = column(flows, "load_mw").map(Number);
  const grid = column(flows, "grid_import_mw").map(Number);
  const disc = column(flows, "bess_discharge_mw").map(Number);
  const curt = column(flows, "curtailed_mw").map(Number);
  const unmet = column(flows, "unmet_mw").map(Number);
  const n = load.length;
  if (!n) return { n: 0 };
  const direct = load.map((l, i) => Math.max(0, l - (grid[i] || 0) - (disc[i] || 0) - (unmet[i] || 0)));
  const dates = ts.map((t) => new Date(t));
  const finish = (o) => {
    o.yMax = Math.max(1, ...o.load.map((l, i) => l + (o.curt[i] || 0))) * 1.06;
    return o;
  };

  if (period === "year") {
    const L = [], D = [], B = [], G = [], C = [], U = [], ticks = []; const dayKeys = [];
    let key = null, acc = null, cnt = 0, lastMonth = -1;
    const flush = () => { if (acc && cnt) { L.push(acc.l / cnt); D.push(acc.d / cnt); B.push(acc.b / cnt); G.push(acc.g / cnt); C.push(acc.c / cnt); U.push(acc.u / cnt); } };
    for (let i = 0; i < n; i++) {
      const k = String(ts[i]).slice(0, 10);
      if (k !== key) {
        flush(); key = k; acc = { l: 0, d: 0, b: 0, g: 0, c: 0, u: 0 }; cnt = 0;
        const mo = dates[i].getMonth();
        if (mo !== lastMonth) { ticks.push({ pos: L.length, label: MONTHS[mo] }); lastMonth = mo; }
        dayKeys.push(k);
      }
      acc.l += load[i]; acc.d += direct[i]; acc.b += disc[i]; acc.g += grid[i]; acc.c += curt[i]; acc.u += unmet[i] || 0; cnt++;
    }
    flush();
    return finish({ n: L.length, load: L, direct: D, bess: B, grid: G, curt: C, unmet: U, ticks,
      unit: "daily-average MW", labelAt: (i) => dayKeys[i] || "" });
  }

  // Representative hourly week (steps-per-day inferred so 15-min data works too).
  const spd = Math.max(1, Math.round(n / 365));
  const want = period === "summer" ? [6, 8] : [0, 8];   // [month, day-of-month] to start near
  let s = dates.findIndex((d) => d.getMonth() === want[0] && d.getDate() >= want[1]);
  if (s < 0) s = Math.floor(n / 2);
  const len = Math.min(spd * 7, n - s);
  const sl = (a) => a.slice(s, s + len);
  const ticks = [];
  for (let i = 0; i < len; i += spd) ticks.push({ pos: i, label: `${dates[s + i].getDate()}/${dates[s + i].getMonth() + 1}` });
  return finish({ n: len, load: sl(load), direct: sl(direct), bess: sl(disc), grid: sl(grid),
    curt: sl(curt), unmet: sl(unmet), ticks, unit: "MW (hourly)",
    labelAt: (i) => String(ts[s + i]).slice(5, 16).replace("T", " ") });
}
