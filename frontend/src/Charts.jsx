// Analyst-grade result charts. Each component is defensive: if the data it needs
// isn't present, it renders nothing, so the same <Results> can drive a point
// solve, a curve, a surface, or a year of flows.
import Plot from "./Plot.jsx";
import { column, recordsToObjects } from "./api.js";
import { computeEnergyBalance, computeMonthly, representativeDay, fmtMWh } from "./energy.js";
import { curvePoints, findKnee, marginalReturns, demandServedFromCurve, ssrBand } from "./decision.js";
import { PLOT_THEME, PLOT_CONFIG } from "./fields.js";

const layout = (extra = {}) => ({ ...PLOT_THEME, ...extra });
const C = { pv: "#f5a623", bess: "#3fb950", grid: "#f85149", unmet: "#8b98a5", curtail: "#6e7681" };

// ── KPI cards ──────────────────────────────────────────────────────────────
function unitFor(key) {
  if (key.includes("%")) return "%";
  if (key.includes("MWh")) return "MWh";
  if (key.includes("MW")) return "MW";
  return "";
}
export function KpiCards({ kpis }) {
  const entries = Object.entries(kpis || {}).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return null;
  return (
    <div className="kpi-grid">
      {entries.map(([k, v]) => {
        const unit = unitFor(k);
        const label = k.replace(/\s*\(.*?\)\s*/g, "").trim();
        return (
          <div className="kpi" key={k}>
            <div className="label">{label}</div>
            <div className="value">
              {typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : v}
              {unit && <span className="unit"> {unit}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Decision scorecard — the handful of numbers a PM reports upward ──────────
// Each tile pairs the figure with a plain-language verdict, so the page answers
// "what do I buy, what grid do I contract, and is anything wrong?" at a glance.
export function Scorecard({ result, dt }) {
  const d = result.design || {};
  const k = result.kpis || {};
  const bal = result.flows ? computeEnergyBalance(result.flows, dt) : null;
  const tiles = [];

  if (d.bess_mwh != null) {
    const eol = d.bess_mwh_eol ? ` · install ${d.bess_mwh_eol.toFixed(0)} for EoL` : "";
    tiles.push({ title: "Battery to buy", value: `${(d.bess_mw || 0).toFixed(0)} MW / ${(d.bess_mwh || 0).toFixed(0)} MWh`,
      note: `${(d.duration_h || 0).toFixed(1)} h duration${eol}`, tone: "neutral" });
  }
  const peak = k["GCmin Peak (MW)"] ?? d.gc_mw;
  if (peak != null) {
    tiles.push({ title: "Grid connection to contract", value: `${peak.toFixed(1)} MW`,
      note: "the firm import to secure (grid = output)", tone: "accent" });
  }
  if (k["SSR (%)"] != null) {
    tiles.push({ title: "Self-sufficiency (SSR)", value: `${k["SSR (%)"].toFixed(1)}%`,
      note: k["SCR (%)"] != null ? `self-consumption ${k["SCR (%)"].toFixed(0)}%` : "", tone: "neutral" });
  }
  if (bal) {
    const cpct = bal.pvAvail > 0 ? (100 * bal.curtailed) / bal.pvAvail : 0;
    tiles.push({ title: "PV curtailment", value: `${cpct.toFixed(0)}%`,
      note: cpct > 25 ? "PV likely oversized for this load" : cpct > 10 ? "some PV spilled" : "PV well utilised",
      tone: cpct > 25 ? "bad" : cpct > 10 ? "warn" : "good" });
  }
  const unmet = k["Total Unmet Load (MWh)"];
  if (unmet != null) {
    tiles.push({ title: "Reliability", value: unmet <= 0.5 ? "Firm" : `${unmet.toFixed(0)} MWh shed`,
      note: unmet <= 0.5 ? "no load shed all year" : "supply gap — size up", tone: unmet <= 0.5 ? "good" : "bad" });
  }
  if (!tiles.length) return null;

  const colors = { good: "#3fb950", warn: "#f5a623", bad: "#f85149", accent: "#3fb6ff", neutral: "#8b98a5" };
  return (
    <div className="kpi-grid">
      {tiles.map((t) => (
        <div className="kpi" key={t.title} style={{ borderLeft: `3px solid ${colors[t.tone]}` }}>
          <div className="label">{t.title}</div>
          <div className="value" style={{ fontSize: 20 }}>{t.value}</div>
          {t.note && <div className="subtle" style={{ marginTop: 4 }}>{t.note}</div>}
        </div>
      ))}
    </div>
  );
}

// ── Design summary table ─────────────────────────────────────────────────────
export function DesignTable({ design }) {
  const entries = Object.entries(design || {}).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return null;
  const pretty = {
    pv_mw: "PV nameplate (MW)", bess_mw: "BESS power (MW)", bess_mwh: "BESS energy (MWh)",
    bess_mwh_eol: "BESS energy, EoL-sized (MWh)", gc_mw: "Grid connection (MW)", duration_h: "Duration (h)",
  };
  return (
    <table className="simple">
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td>{pretty[k] || k}</td>
            <td>{typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// What varies along a curve depends on the scenario, and the engine overloads
// the "Target SSR (%)" column (firmness sweeps PV; curtailment reuses that
// column). So we pick the X axis from the Target Type, not a fixed name.
function curveX(table) {
  const tt = column(table, "Target Type")[0];
  const cols = table.columns;
  const pick = (col, label, title) =>
    cols.includes(col) ? { col, label, title } : null;
  if (tt === "peak_shaving")
    return pick("Target Grid Connection (MW)", "Grid connection target (MW)", "Battery size vs grid connection");
  if (tt === "firmness")
    return pick("PV Nameplate (MW)", "PV nameplate (MW)", "Battery size vs PV (firmness target)");
  if (tt === "curtailment")
    return pick("Target SSR (%)", "Max curtailment (%)", "Battery size vs allowed curtailment");
  return pick("Target SSR (%)", "Self-sufficiency target (%)", "Battery size vs self-sufficiency (the knee)");
}

// ── Knee curve (any sizing curve) ────────────────────────────────────────────
// Y = BESS energy. For BTM curves three lines tell the honest story: LP
// lower-bound vs deliverable (Model R) vs EoL-sized.
export function KneeCurve({ table }) {
  if (!table) return null;
  const cols = table.columns;
  const meta = curveX(table);
  if (!meta || !cols.includes("BESS Energy (MWh)")) return null;

  const x = column(table, meta.col);
  const feasible = column(table, "Feasible");
  const keep = (arr) => arr.map((v, i) => (feasible[i] === false ? null : v));

  const ht = (name) => `${name}: %{y:,.0f} MWh<extra>${meta.label.replace(/\s*\(.*?\)/, "")} %{x}</extra>`;
  const traces = [{
    x, y: keep(column(table, "BESS Energy (MWh)")),
    name: "LP lower bound", mode: "lines+markers", type: "scatter",
    line: { color: "#3fb6ff", dash: "dot" }, hovertemplate: ht("LP bound"),
  }];
  if (cols.includes("Deliverable BESS Energy (MWh · Model R)")) {
    traces.push({
      x, y: keep(column(table, "Deliverable BESS Energy (MWh · Model R)")),
      name: "Deliverable (must buy)", mode: "lines+markers", type: "scatter",
      line: { color: "#f5a623", width: 3 }, hovertemplate: ht("Deliverable"),
    });
  }
  if (cols.includes("EoL-Sized BESS Energy (MWh)")) {
    traces.push({
      x, y: keep(column(table, "EoL-Sized BESS Energy (MWh)")),
      name: "EoL-sized (install)", mode: "lines+markers", type: "scatter",
      line: { color: "#3fb950" }, hovertemplate: ht("EoL-sized"),
    });
  }
  // Mark the sweet spot so the curve becomes a recommendation, not just data.
  const knee = findKnee(curvePoints(table, meta.col));
  const annotations = [];
  const shapes = [];
  if (knee) {
    traces.push({
      x: [knee.x], y: [knee.y], mode: "markers", type: "scatter", name: "sweet spot",
      marker: { color: "#fff", size: 13, symbol: "star", line: { color: "#f5a623", width: 2 } },
    });
    annotations.push({
      x: knee.x, y: knee.y, ax: 0, ay: -42, text: "sweet spot",
      font: { color: "#fff", size: 11 }, arrowcolor: "#f5a623",
    });
  }
  // Floor (no battery) and ceiling (SSR_max) bound the feasible target band.
  const band = ssrBand(table);
  if (band) {
    for (const [val, label] of [[band.floor, "PV-only floor"], [band.ceiling, "ceiling (SSR_max)"]]) {
      shapes.push({ type: "line", x0: val, x1: val, yref: "paper", y0: 0, y1: 1,
        line: { color: "#8b98a5", dash: "dash", width: 1 } });
      annotations.push({ x: val, yref: "paper", y: 1, yanchor: "bottom", text: `${val.toFixed(0)}% ${label}`,
        showarrow: false, font: { size: 10, color: "#8b98a5" } });
    }
  }
  return (
    <Plot
      data={traces}
      layout={layout({
        title: meta.title,
        xaxis: { ...PLOT_THEME.xaxis, title: meta.label },
        yaxis: { ...PLOT_THEME.yaxis, title: "BESS energy (MWh)" },
        height: 420, annotations, shapes,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Marginal returns: battery needed per extra unit of target ────────────────
// Makes "diminishing returns" literal — the bars climb where each extra point of
// SSR (or MW of grid relief) starts demanding disproportionately more battery.
export function MarginalReturns({ table }) {
  if (!table) return null;
  const meta = curveX(table);
  if (!meta) return null;
  const marg = marginalReturns(curvePoints(table, meta.col));
  if (marg.length < 2) return null;
  const unit = meta.label.includes("%") ? "per +1%" : "per unit";
  return (
    <Plot
      data={[{
        x: marg.map((m) => m.x), y: marg.map((m) => m.slope), type: "bar",
        marker: { color: marg.map((m) => m.slope), colorscale: "YlOrRd" },
      }]}
      layout={layout({
        title: `Extra battery needed ${unit} — where it gets expensive`,
        xaxis: { ...PLOT_THEME.xaxis, title: meta.label },
        yaxis: { ...PLOT_THEME.yaxis, title: `MWh ${unit}` },
        height: 300,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// One-line recommendation derived from the knee, for a banner above the curve.
export function kneeRecommendation(table) {
  const meta = curveX(table);
  if (!meta) return null;
  const knee = findKnee(curvePoints(table, meta.col));
  if (!knee) return null;
  const axis = meta.label.replace(/\s*\(.*?\)/, "");
  const at = meta.label.includes("%") ? `${knee.x.toFixed(0)}%` : `${knee.x.toFixed(0)} MW`;
  const mult = knee.multiplier && knee.multiplier > 1.3
    ? ` Beyond it, each step costs ~${knee.multiplier.toFixed(1)}× more battery.` : "";
  return `Recommended sweet spot: ${axis} ≈ ${at}, needing ~${fmtMWh(knee.y)} of battery.${mult}`;
}

// ── What you actually GET across the curve (operational outcomes) ────────────
// Plots the achieved SSR / SCR / peak grid along the sweep, so the curve shows
// not just "how much battery" but "what it buys you". Only renders when the
// operational overlay columns are present (BTM curves).
export function OperationalOverlay({ table }) {
  if (!table) return null;
  const meta = curveX(table);
  if (!meta) return null;
  const cols = table.columns;
  const x = column(table, meta.col);
  const has = (c) => cols.includes(c);
  const traces = [];
  if (has("Operational SSR (%)"))
    traces.push({ x, y: column(table, "Operational SSR (%)"), name: "Operational SSR (%)",
      mode: "lines+markers", type: "scatter", line: { color: "#f5a623" } });
  if (has("Operational SCR (%)"))
    traces.push({ x, y: column(table, "Operational SCR (%)"), name: "Operational SCR (%)",
      mode: "lines+markers", type: "scatter", line: { color: "#3fb950" } });
  if (has("Operational Peak Grid (MW)"))
    traces.push({ x, y: column(table, "Operational Peak Grid (MW)"), name: "Peak grid (MW)",
      mode: "lines+markers", type: "scatter", yaxis: "y2", line: { color: "#f85149" } });
  if (!traces.length) return null;
  return (
    <Plot
      data={traces}
      layout={layout({
        title: "What each design actually delivers",
        xaxis: { ...PLOT_THEME.xaxis, title: meta.label },
        yaxis: { ...PLOT_THEME.yaxis, title: "%" },
        yaxis2: { title: "Peak grid (MW)", overlaying: "y", side: "right", gridcolor: "transparent" },
        height: 360,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Gauges for headline percentage KPIs (glanceable for point solves) ────────
export function Gauges({ kpis }) {
  const specs = [
    { key: "SSR (%)", label: "Self-sufficiency", color: "#f5a623" },
    { key: "SCR (%)", label: "Self-consumption", color: "#3fb950" },
  ].filter((s) => typeof kpis?.[s.key] === "number");
  if (!specs.length) return null;
  return (
    <Plot
      data={specs.map((s, i) => ({
        type: "indicator", mode: "gauge+number", value: kpis[s.key],
        title: { text: s.label, font: { size: 13 } },
        number: { suffix: "%" },
        domain: { row: 0, column: i },
        gauge: { axis: { range: [0, 100] }, bar: { color: s.color },
          bgcolor: "#222b34", bordercolor: "#2d3742" },
      }))}
      layout={layout({
        grid: { rows: 1, columns: specs.length, pattern: "independent" },
        height: 200, margin: { l: 20, r: 20, t: 30, b: 10 },
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Surface heatmap (PV × target → BESS) ─────────────────────────────────────
export function SurfaceHeatmap({ table }) {
  if (!table) return null;
  const cols = table.columns;
  if (!cols.includes("PV Nameplate (MW)") || !cols.includes("BESS Energy (MWh)")) return null;
  const tCol = cols.includes("Target SSR (%)") ? "Target SSR (%)"
    : cols.includes("Target Grid Connection (MW)") ? "Target Grid Connection (MW)" : null;
  if (!tCol) return null;

  const rows = recordsToObjects(table);
  const pvs = [...new Set(rows.map((r) => r["PV Nameplate (MW)"]))].sort((a, b) => a - b);
  const tgts = [...new Set(rows.map((r) => r[tCol]))].sort((a, b) => a - b);
  const z = pvs.map((pv) =>
    tgts.map((t) => {
      const m = rows.find((r) => r["PV Nameplate (MW)"] === pv && r[tCol] === t);
      return m && m["Feasible"] !== false ? m["BESS Energy (MWh)"] : null;
    })
  );
  return (
    <Plot
      data={[{ z, x: tgts, y: pvs, type: "heatmap", colorscale: "Viridis", colorbar: { title: "BESS MWh" } }]}
      layout={layout({
        title: "BESS energy across the PV × target design space",
        xaxis: { ...PLOT_THEME.xaxis, title: tCol },
        yaxis: { ...PLOT_THEME.yaxis, title: "PV nameplate (MW)" },
        height: 420,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Flow helpers ─────────────────────────────────────────────────────────────
function flowRows(flows) {
  const rows = recordsToObjects(flows);
  return rows.map((r) => {
    const d = new Date(r.timestamp);
    return { ...r, _hour: d.getHours(), _date: d.toDateString(), _ts: d.getTime() };
  });
}

// ── The energy balance: FOUR sources sum to demand ───────────────────────────
// One stacked bar = total demand, split into where it was met from. The grid
// slice is the "output" the CEO brief says to minimise; unmet must be zero.
export function EnergyBalanceBar({ flows, dt }) {
  const b = computeEnergyBalance(flows, dt);
  if (!b) return null;
  const seg = (name, val, color) => ({
    x: [val], y: ["Demand met by"], name, orientation: "h", type: "bar",
    marker: { color }, text: [`${name}: ${fmtMWh(val)}`], textposition: "inside", insidetextanchor: "middle",
  });
  return (
    <Plot
      data={[
        seg("PV direct", b.pvDirect, C.pv),
        seg("Battery", b.bess, C.bess),
        seg("Grid import", b.grid, C.grid),
        ...(b.unmet > 0.5 ? [seg("Unmet", b.unmet, C.unmet)] : []),
      ]}
      layout={layout({
        title: `Annual energy balance — demand ${fmtMWh(b.demand)} (residual ${b.residual.toFixed(0)})`,
        barmode: "stack", height: 200, showlegend: true,
        xaxis: { ...PLOT_THEME.xaxis, title: "MWh / yr" },
        yaxis: { ...PLOT_THEME.yaxis, automargin: true },
        margin: { l: 110, r: 24, t: 40, b: 44 },
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── SSR & SCR shown as the ratios they ARE (numerator within denominator) ────
export function SsrScrBars({ flows, dt }) {
  const b = computeEnergyBalance(flows, dt);
  if (!b) return null;
  return (
    <Plot
      data={[
        { y: ["SSR", "SCR"], x: [b.servedOnSite, b.pvUsed], name: "achieved",
          orientation: "h", type: "bar", marker: { color: [C.bess, C.pv] },
          text: [`served on-site ${fmtMWh(b.servedOnSite)}`, `PV used ${fmtMWh(b.pvUsed)}`], textposition: "inside" },
        { y: ["SSR", "SCR"], x: [Math.max(0, b.demand - b.servedOnSite), Math.max(0, b.pvAvail - b.pvUsed)],
          name: "shortfall", orientation: "h", type: "bar", marker: { color: "rgba(140,152,165,0.3)" },
          text: [`from grid ${fmtMWh(b.demand - b.servedOnSite)}`, `curtailed ${fmtMWh(b.pvAvail - b.pvUsed)}`], textposition: "inside" },
      ]}
      layout={layout({
        title: `SSR ${b.ssr.toFixed(1)}%  ·  SCR ${b.scr.toFixed(1)}%`,
        barmode: "stack", height: 220,
        xaxis: { ...PLOT_THEME.xaxis, title: "MWh / yr" },
        yaxis: { ...PLOT_THEME.yaxis, automargin: true },
        margin: { l: 60, r: 24, t: 40, b: 44 },
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── PV utilisation: available split into used-on-site vs curtailed ───────────
export function PvUtilisation({ flows, dt }) {
  const b = computeEnergyBalance(flows, dt);
  if (!b || b.pvAvail < 0.5) return null;
  const pct = (v) => ((100 * v) / b.pvAvail).toFixed(0) + "%";
  return (
    <Plot
      data={[{
        labels: ["PV → load (direct)", "PV → battery", "PV curtailed"],
        values: [b.pvDirect, b.charge, b.curtailed],
        type: "pie", hole: 0.55,
        marker: { colors: [C.pv, C.bess, C.curtail] },
        textinfo: "label+percent", sort: false,
        hovertemplate: "%{label}: %{value:,.0f} MWh (%{percent})<extra></extra>",
      }]}
      layout={layout({
        title: `PV available ${fmtMWh(b.pvAvail)} — ${pct(b.curtailed)} curtailed`,
        height: 320, margin: { l: 10, r: 10, t: 40, b: 10 }, showlegend: false,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Monthly seasonality: how demand is served, month by month + SSR line ─────
// The "when do I lean on the grid?" chart. Winter months show a grid slice;
// summer is near-fully self-sufficient — the seasonality that drives sizing.
export function MonthlySeasonality({ flows, dt, onMonthClick, selectedMonth }) {
  const M = computeMonthly(flows, dt);
  if (!M) return null;
  const x = M.map((m) => m.month);
  const ht = (name) => `${name}: %{y:,.0f} MWh<extra>%{x}</extra>`;
  // Highlight the drilled-into month so the link to the day chart is obvious.
  const lineCol = (key) => M.map((_, i) => (i === selectedMonth ? "#fff" : "rgba(0,0,0,0)"));
  const bar = (key, name, color) => ({
    x, y: M.map((m) => m[key]), name, type: "bar", marker: { color, line: { color: lineCol(key), width: 1.5 } },
    hovertemplate: ht(name),
  });
  return (
    <Plot
      data={[
        bar("pv", "PV → load", C.pv),
        bar("bess", "BESS → load", C.bess),
        bar("grid", "Grid import", C.grid),
        { x, y: M.map((m) => m.ssr), name: "SSR (%)", type: "scatter", mode: "lines+markers",
          yaxis: "y2", line: { color: "#a371f7", width: 2 },
          hovertemplate: "SSR: %{y:.0f}%<extra>%{x}</extra>" },
      ]}
      layout={layout({
        title: "Monthly seasonality — winter grid reliance" + (onMonthClick ? "  (click a month →)" : ""),
        barmode: "stack", height: 380, hovermode: "x unified",
        xaxis: { ...PLOT_THEME.xaxis, fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "MWh / month", fixedrange: true },
        yaxis2: { title: "SSR (%)", overlaying: "y", side: "right", range: [0, 100], gridcolor: "transparent", fixedrange: true },
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
      onClick={onMonthClick ? (e) => { const p = e.points?.[0]; if (p) onMonthClick(p.pointIndex); } : undefined}
    />
  );
}

// ── Winter vs summer representative day — dispatch + SOC, side by side ────────
// One day's hourly operation. Reused for the fixed winter/summer panels AND the
// dynamic "click a month" drill-down, so both look and scale the same.
export function DayPanel({ day, title, maxMW, maxSOC }) {
  if (!day) return null;
  const mMW = maxMW || Math.max(...[...day.load, ...day.pv, ...day.bess, ...day.grid], 1);
  const mSOC = maxSOC || Math.max(...day.soc, 1);
  const ht = (n) => `${n}: %{y:.1f} MW<extra></extra>`;
  return (
    <Plot
      data={[
        { x: day.hour, y: day.load, name: "Load", mode: "lines", type: "scatter", line: { color: "#3fb6ff", width: 2.5 }, hovertemplate: ht("Load") },
        { x: day.hour, y: day.pv, name: "PV avail", mode: "lines", type: "scatter", line: { color: C.pv }, hovertemplate: ht("PV avail") },
        { x: day.hour, y: day.bess, name: "BESS→load", mode: "lines", type: "scatter", line: { color: C.bess }, hovertemplate: ht("BESS→load") },
        { x: day.hour, y: day.grid, name: "Grid→load", mode: "lines", type: "scatter", line: { color: C.grid }, hovertemplate: ht("Grid→load") },
        { x: day.hour, y: day.soc, name: "SOC", mode: "lines", type: "scatter", yaxis: "y2",
          line: { color: "#8b98a5", dash: "dot" }, hovertemplate: "SOC: %{y:,.0f} MWh<extra></extra>" },
      ]}
      layout={layout({
        title, height: 320, hovermode: "x unified",
        xaxis: { ...PLOT_THEME.xaxis, title: "Hour of day", dtick: 3, range: [0, 24], fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "MW", range: [0, mMW * 1.05], fixedrange: true },
        yaxis2: { title: "SOC (MWh)", overlaying: "y", side: "right", gridcolor: "transparent",
          range: [0, mSOC * 1.05], fixedrange: true },
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

export function SeasonalDays({ flows }) {
  const winter = representativeDay(flows, 0);  // January
  const summer = representativeDay(flows, 6);  // July
  if (!winter && !summer) return null;
  // Shared scales so the two days are honestly comparable.
  const maxMW = Math.max(...[winter, summer].filter(Boolean)
    .flatMap((d) => [...d.load, ...d.pv, ...d.bess, ...d.grid]), 1);
  const maxSOC = Math.max(...[winter, summer].filter(Boolean).flatMap((d) => d.soc), 1);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
      <DayPanel day={winter} maxMW={maxMW} maxSOC={maxSOC}
        title={`Winter day (${winter?.label}) — battery empties, grid fills the gap`} />
      <DayPanel day={summer} maxMW={maxMW} maxSOC={maxSOC}
        title={`Summer day (${summer?.label}) — PV covers load, battery cycles`} />
    </div>
  );
}

// ── Demand served, compared across the whole SSR curve ───────────────────────
// Reads the operational grid import per target: as you push SSR up, the grid
// slice (the "output") shrinks. This is the cross-scenario comparison view.
export function DemandServedByScenario({ table }) {
  const rows = demandServedFromCurve(table);
  if (!rows) return null;
  const x = rows.map((r) => `${r.target}%`);
  const bar = (key, name, color) => ({ x, y: rows.map((r) => r[key]), name, type: "bar", marker: { color },
    hovertemplate: `${name}: %{y:,.0f} MWh<extra>SSR target %{x}</extra>` });
  return (
    <Plot
      data={[bar("pv", "PV → load", C.pv), bar("bess", "BESS → load", C.bess), bar("grid", "Grid import", C.grid)]}
      layout={layout({
        title: "How annual demand is served, by SSR target",
        barmode: "stack", height: 360, hovermode: "x unified",
        xaxis: { ...PLOT_THEME.xaxis, title: "SSR target", fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "MWh / yr", fixedrange: true },
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Annual energy Sankey (exact provenance, no approximation) ─────────────────
export function EnergySankey({ flows, dt }) {
  const b = computeEnergyBalance(flows, dt);
  if (!b) return null;
  const labels = ["PV", "Grid", "Battery", "Load", "Curtailed", "Export"];
  const [PV, GRID, BATT, LOAD, CURT, EXP] = [0, 1, 2, 3, 4, 5];
  const links = [
    { s: PV, t: LOAD, v: b.pvDirect },
    { s: PV, t: BATT, v: b.charge },
    { s: PV, t: CURT, v: b.curtailed },
    { s: PV, t: EXP, v: b.exported },
    { s: GRID, t: LOAD, v: b.grid },
    { s: BATT, t: LOAD, v: b.bess },
  ].filter((l) => l.v > 0.01);
  return (
    <Plot
      data={[{
        type: "sankey",
        node: { label: labels, pad: 18, thickness: 16,
          color: [C.pv, C.grid, C.bess, "#3fb6ff", C.curtail, "#a371f7"] },
        link: { source: links.map((l) => l.s), target: links.map((l) => l.t),
          value: links.map((l) => Math.round(l.v)), color: "rgba(140,152,165,0.25)" },
      }]}
      layout={layout({ title: "Annual energy flows (MWh)", height: 360 })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Carpet heatmap: hour-of-day × day-of-year, coloured by grid import ────────
export function CarpetHeatmap({ flows, valueCol = "grid_import_mw", title }) {
  if (!flows || !flows.columns.includes(valueCol)) return null;
  const rows = flowRows(flows);
  // Bucket into [hour][dayIndex] averaging anything sub-hourly.
  const days = [...new Set(rows.map((r) => r._date))];
  const dayIdx = Object.fromEntries(days.map((d, i) => [d, i]));
  const z = Array.from({ length: 24 }, () => Array(days.length).fill(null));
  const cnt = Array.from({ length: 24 }, () => Array(days.length).fill(0));
  rows.forEach((r) => {
    const di = dayIdx[r._date];
    z[r._hour][di] = (z[r._hour][di] || 0) + (r[valueCol] || 0);
    cnt[r._hour][di] += 1;
  });
  for (let h = 0; h < 24; h++)
    for (let d = 0; d < days.length; d++)
      if (cnt[h][d]) z[h][d] = z[h][d] / cnt[h][d];

  return (
    <Plot
      data={[{
        z, type: "heatmap", colorscale: "Inferno",
        x: days.map((_, i) => i), y: Array.from({ length: 24 }, (_, h) => h),
        colorbar: { title: "MW" },
        hovertemplate: "Day %{x}, hour %{y}: %{z:.1f} MW<extra></extra>",
      }]}
      layout={layout({
        title: title || "Grid import — hour of day × day of year",
        xaxis: { ...PLOT_THEME.xaxis, title: "Day of year", range: [0, days.length], fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "Hour of day", dtick: 6, range: [0, 23], fixedrange: true },
        height: 380,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Representative day dispatch (stacked supply vs load line) ─────────────────
export function DispatchDay({ flows }) {
  if (!flows || !flows.columns.includes("load_mw")) return null;
  const rows = flowRows(flows);
  // Peak day = the calendar day with the highest single load.
  const byDay = {};
  rows.forEach((r) => {
    if (!byDay[r._date] || r.load_mw > byDay[r._date].peak) byDay[r._date] = { peak: r.load_mw };
  });
  const peakDate = Object.entries(byDay).sort((a, b) => b[1].peak - a[1].peak)[0]?.[0];
  const day = rows.filter((r) => r._date === peakDate).sort((a, b) => a._ts - b._ts);
  if (!day.length) return null;
  const x = day.map((r) => r._hour + (new Date(r.timestamp).getMinutes() / 60));

  const area = (key, name, color) => ({
    x, y: day.map((r) => r[key] || 0), name, stackgroup: "supply",
    mode: "none", type: "scatter", fillcolor: color, line: { width: 0 },
    hovertemplate: `${name}: %{y:.1f} MW<extra></extra>`,
  });
  return (
    <Plot
      data={[
        area("pv_used_mw", "PV → load", "rgba(245,166,35,0.7)"),
        area("bess_discharge_mw", "Battery → load", "rgba(63,185,80,0.7)"),
        area("grid_import_mw", "Grid → load", "rgba(248,81,73,0.6)"),
        { x, y: day.map((r) => r.load_mw), name: "Load", mode: "lines",
          type: "scatter", line: { color: "#e6edf3", width: 2.5 },
          hovertemplate: "Load: %{y:.1f} MW<extra></extra>" },
      ]}
      layout={layout({
        title: `Dispatch on the peak-load day (${peakDate})`,
        hovermode: "x unified",
        xaxis: { ...PLOT_THEME.xaxis, title: "Hour of day", dtick: 3, range: [0, 24], fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "MW" },
        height: 360,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Battery state of charge over a representative fortnight ───────────────────
// Shows the daily cycling against the installed energy (the SOC band the design
// table reports). A flat line would mean the battery isn't working.
export function SocBand({ flows, designMwh }) {
  if (!flows || !flows.columns.includes("bess_soc_mwh")) return null;
  const soc = column(flows, "bess_soc_mwh");
  if (!soc.length || Math.max(...soc) <= 0) return null;
  const win = soc.slice(0, Math.min(soc.length, 336)); // first 14 days (hourly)
  const x = win.map((_, i) => i);
  const traces = [{
    x, y: win, mode: "lines", type: "scatter", name: "SOC (MWh)",
    fill: "tozeroy", line: { color: "#3fb6ff" }, fillcolor: "rgba(63,182,255,0.15)",
    hovertemplate: "Hour %{x}: %{y:,.0f} MWh<extra></extra>",
  }];
  const top = designMwh || Math.max(...soc);
  return (
    <Plot
      data={traces}
      layout={layout({
        title: "Battery state of charge — first two weeks",
        xaxis: { ...PLOT_THEME.xaxis, title: "Hour", range: [0, win.length], fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "SOC (MWh)", range: [0, top * 1.05] },
        height: 300,
        shapes: top ? [{ type: "line", x0: 0, x1: win.length, y0: top, y1: top,
          line: { color: "#8b98a5", dash: "dot", width: 1 } }] : [],
        annotations: top ? [{ x: 0, y: top, xanchor: "left", yanchor: "bottom",
          text: `installed ${top.toFixed(0)} MWh`, showarrow: false, font: { size: 11, color: "#8b98a5" } }] : [],
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}

// ── Load-duration curve for grid import ──────────────────────────────────────
export function GridDurationCurve({ flows, dt }) {
  if (!flows || !flows.columns.includes("grid_import_mw")) return null;
  const g = column(flows, "grid_import_mw").slice().sort((a, b) => b - a);
  const x = g.map((_, i) => (100 * i) / g.length);
  return (
    <Plot
      data={[{ x, y: g, mode: "lines", type: "scatter", fill: "tozeroy",
        line: { color: "#3fb6ff" }, name: "Grid import",
        hovertemplate: "Top %{x:.0f}% of hours exceed %{y:.1f} MW<extra></extra>" }]}
      layout={layout({
        title: "Grid import duration curve",
        xaxis: { ...PLOT_THEME.xaxis, title: "% of hours grid import is exceeded", range: [0, 100], fixedrange: true },
        yaxis: { ...PLOT_THEME.yaxis, title: "Grid import (MW)" },
        height: 320,
      })}
      config={PLOT_CONFIG}
      style={{ width: "100%" }}
    />
  );
}
